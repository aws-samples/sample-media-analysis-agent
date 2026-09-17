"""
Bedrock Data Automation (BDA) tools for video and audio analysis.

BDA provides multimodal content processing — transcription, video summaries,
chapter detection, and scene analysis in a single API call. This is an
alternative to the Rekognition + Transcribe pipeline for supported use cases.

Advantages over separate Rekognition + Transcribe:
- Single API call for video summary + transcript + chapter detection
- Speaker identification from visual cues (name cards, slides)
- Scene-level summaries with timestamps
- Supports Chinese, Japanese, Korean, and 7 other languages

Limitations:
- Fewer languages than Transcribe (10 vs 100+)
- Requires a BDA project or profile ARN
- Output goes to S3 (not returned inline)
"""

import json
import logging
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import boto3
from strands import tool

import config
from tools import (
    get_detection_cache,
    _get_session,
    read_document,
    resolve_owned_s3_key,
    owned_s3_uri,
    _owner_prefix,
    S3AccessDenied,
)

# ─────────────────────────────────────────────────────────────────────────
# Cache-aware short-circuit helpers
# ─────────────────────────────────────────────────────────────────────────

def _check_cached_analysis(s3_uri: str, analysis_type: str = "video") -> Optional[str]:
    """Check if analysis already exists in cache for the given S3 URI.

    Returns a marker string telling the agent to ask the user how to proceed,
    or None if no cache hit (caller should run the full analysis).
    """
    last_s3_key = get_detection_cache().get("last_s3_key", "")
    last_video_path = get_detection_cache().get("last_video_path", "")

    s3_key_part = s3_uri.split("/", 3)[-1] if "/" in s3_uri else s3_uri
    file_match = (
        (last_s3_key and (last_s3_key in s3_uri or last_s3_key == s3_key_part))
        or (last_video_path and last_video_path in s3_uri)
    )

    has_video_summary = bool(get_detection_cache().get("bda_video_summary"))
    has_transcript = bool(get_detection_cache().get("transcript_text"))

    if not file_match:
        return None
    if not (has_video_summary or has_transcript):
        return None

    cached_summary = get_detection_cache().get("bda_video_summary", "")
    transcript_preview = get_detection_cache().get("transcript_text", "")[:500]

    return (
        "**[CACHED_ANALYSIS_AVAILABLE]**\n\n"
        f"This {analysis_type} was already analyzed in this session. "
        "Before running a fresh (and costly) re-analysis, ask the user to choose one of three options:\n\n"
        "1. **Reuse the previous analysis** — show the cached findings (recommended if the file is unchanged)\n"
        "2. **Re-analyze fresh** — run the full pipeline again (use this only if the user explicitly confirms)\n"
        "3. **Ask a specific question** — answer from the cached findings without re-running anything\n\n"
        "**Cached summary preview:**\n"
        f"{cached_summary[:400] if cached_summary else '(no video summary cached)'}\n\n"
        "**Cached transcript preview:**\n"
        f"{transcript_preview if transcript_preview else '(no transcript cached)'}\n\n"
        "Do NOT call analyze_video_with_bda or any analysis tool again until the user makes an explicit choice."
    )


def _check_cached_transcript() -> Optional[str]:
    """Check if a transcript is already in the cache. Returns a marker or None."""
    transcript = get_detection_cache().get("transcript_text", "")
    if not transcript:
        return None

    source = get_detection_cache().get("transcript_source", "previous analysis")
    language = get_detection_cache().get("transcript_language", "auto-detected")
    word_count = len(transcript.split())

    return (
        "**[CACHED_TRANSCRIPT_AVAILABLE]**\n\n"
        f"A transcript is already available from {source} ({language}, ~{word_count} words). "
        "Before regenerating, ask the user to choose:\n\n"
        "1. **Use the cached transcript** — instant, no extra cost (recommended)\n"
        "2. **Regenerate the transcript** — re-run the transcription tool (slow, costs API calls)\n"
        "3. **Show / search / summarize the existing transcript**\n\n"
        f"**Transcript preview:** {transcript[:300]}...\n\n"
        "Do NOT call transcribe_audio or analyze_audio_with_bda again until the user picks option 2 explicitly."
    )

logger = logging.getLogger(__name__)

# BDA configuration
BDA_OUTPUT_PREFIX = "bda-output"


def _get_bda_runtime_client():
    """Get the BDA runtime client."""
    session = _get_session()
    return session.client("bedrock-data-automation-runtime", region_name=config.AWS_REGION)


def _get_bda_client():
    """Get the BDA management client."""
    session = _get_session()
    return session.client("bedrock-data-automation", region_name=config.AWS_REGION)


def _get_default_profile_arn():
    """Get the default BDA profile ARN for the current account and region.
    
    Pattern: arn:aws:bedrock:{region}:{account_id}:data-automation-profile/us.data-automation-v1
    """
    session = _get_session()
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    return f"arn:aws:bedrock:{config.AWS_REGION}:{account_id}:data-automation-profile/us.data-automation-v1"


def _get_project_arn():
    """Get the first available BDA project ARN, or None if no projects exist."""
    try:
        client = _get_bda_client()
        response = client.list_data_automation_projects()
        projects = response.get("projects", [])
        if projects:
            # Use the first LIVE project
            for proj in projects:
                if proj.get("projectStage") == "LIVE":
                    return proj["projectArn"]
            # Fallback to first project regardless of stage
            return projects[0]["projectArn"]
    except Exception as e:
        logger.warning("Could not list BDA projects: %s", e)
    return None


_PROJECT_MISSING_MARKER = (
    "**[BDA_PROJECT_MISSING]**\n\n"
    "No Bedrock Data Automation (BDA) project was found in this account. "
    "BDA requires a project to run. Ask the user to choose how to proceed:\n\n"
    "1. **Create a default BDA project** — call `create_bda_project` with no "
    "arguments. Takes ~30 seconds. After creation, retry the analysis.\n"
    "2. **Switch to Traditional (Rekognition + Transcribe) mode** — the user "
    "can change this in the sidebar under **⚙️ Analysis Mode**, then re-upload "
    "the video. In BDA (mandatory) mode, `detect_labels`, `detect_faces`, and "
    "`transcribe_audio` are intentionally disabled, so switching modes is the "
    "correct path to use those tools.\n"
    "3. **Cancel** — skip the analysis entirely.\n\n"
    "Do not call `analyze_video_with_bda` or `analyze_audio_with_bda` again "
    "until the user picks option 1 and the project is created, or until they "
    "explicitly retry."
)


@tool
def create_bda_project(project_name: str = "video-analytic-agent-default", description: str = "") -> str:
    """Create a Bedrock Data Automation (BDA) project with sensible defaults.

    Creates a project that supports both video and audio analysis, including
    transcript, summary, chapter detection, and content moderation.

    Call this tool only when:
    - A BDA tool returned [BDA_PROJECT_MISSING] and the user chose to create a project, OR
    - The user has explicitly asked to create a BDA project.

    Args:
        project_name: Name for the new project. Defaults to "video-analytic-agent-default".
        description: Optional human-readable description.

    Returns:
        Success message with the new project ARN, or a clear error if creation failed.
    """
    standard_output = {
        "video": {
            "extraction": {
                "category": {
                    "state": "ENABLED",
                    "types": ["TRANSCRIPT", "TEXT_DETECTION", "LOGOS"],
                },
                "boundingBox": {"state": "ENABLED"},
            },
            "generativeField": {
                "state": "ENABLED",
                "types": ["VIDEO_SUMMARY", "CHAPTER_SUMMARY", "IAB"],
            },
        },
        "audio": {
            "extraction": {
                "category": {
                    "state": "ENABLED",
                    "types": ["TRANSCRIPT", "AUDIO_CONTENT_MODERATION"],
                },
            },
            "generativeField": {
                "state": "ENABLED",
                "types": ["AUDIO_SUMMARY", "TOPIC_SUMMARY", "IAB"],
            },
        },
    }
    try:
        client = _get_bda_client()
        kwargs = {
            "projectName": project_name,
            "standardOutputConfiguration": standard_output,
        }
        if description:
            kwargs["projectDescription"] = description
        else:
            kwargs["projectDescription"] = "Default project for video-analytic-agent"

        response = client.create_data_automation_project(**kwargs)
        project_arn = response.get("projectArn", "")
        if not project_arn:
            return (
                f"ERROR: BDA project creation returned no ARN. "
                f"Response: {json.dumps(response, default=str)}"
            )
        logger.info("BDA: Created project '%s' (%s)", project_name, project_arn)
        return (
            f"✅ Created BDA project '{project_name}'.\n\n"
            f"ARN: `{project_arn}`\n\n"
            "The project is now LIVE and you can run BDA analysis on the uploaded file. "
            "Retry the analysis to use the new project."
        )
    except Exception as e:
        return (
            f"ERROR: Could not create BDA project — {e}\n\n"
            "Alternatives:\n"
            f"1. Create the project manually in the Bedrock console: "
            f"https://{config.AWS_REGION}.console.aws.amazon.com/bedrock/home"
            f"?region={config.AWS_REGION}#/data-automation/projects\n"
            "2. Switch to Traditional (Rekognition + Transcribe) mode in the "
            "sidebar under **⚙️ Analysis Mode**, then re-upload the video. "
            "Traditional tools are disabled in BDA mandatory mode; switching "
            "modes enables `detect_labels` and `transcribe_audio`."
        )


@tool
def analyze_video_with_bda(s3_uri: str, output_s3_uri: str = "", force: bool = False) -> str:
    """Analyze a video using Bedrock Data Automation (BDA).

    BDA provides video summary, chapter summaries, full audio transcript,
    and scene detection in a single API call. Use this as an alternative
    to running detect_labels + transcribe_audio separately.

    Supports: English, Chinese, Japanese, Korean, Portuguese, French,
    Italian, Spanish, German, Cantonese, Taiwanese.

    Args:
        s3_uri: S3 URI of the video file
            (e.g., s3://amzn-s3-demo-videos/videos/file.mp4).
            A bare key is also accepted, and is preferred — you do not need to
            know the bucket name. Must be within the calling user's prefix.
        output_s3_uri: S3 URI for output (optional, defaults to a location under
            the calling user's own prefix)
        force: If True, skip the cache check and always re-run analysis.
            Set this only when the user has explicitly asked to re-analyze.

    Returns:
        Video summary, chapter summaries, and transcript status.
    """
    # T-10 / correctness. This tool used to require an s3:// URI and then take
    # the bucket from it (bucket = parts[0]) for the OUTPUT location too. Since
    # nothing ever told the model the bucket name, it invented one — a real run
    # passed s3://bedrock-data-auto-us-east-1-<account>/..., a nonexistent
    # bucket, and BDA answered with a generic "Unable to read file from given S3
    # location. Check bucket name, key, region and read permissions." That sent
    # debugging toward IAM when the actual fault was a fabricated argument.
    #
    # Now: a bare key is accepted, ownership is verified, and the bucket comes
    # from configuration rather than from the model.
    try:
        s3_uri = owned_s3_uri(s3_uri)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    # Cache short-circuit — only re-run if force=True or no prior analysis exists
    if not force:
        cached = _check_cached_analysis(s3_uri, analysis_type="video")
        if cached:
            return cached

    bucket = config.S3_BUCKET

    if not output_s3_uri:
        # Output goes under the caller's own prefix rather than a shared
        # bda-output/ space, so one user's analysis artefacts are not colocated
        # with another's.
        output_s3_uri = (
            f"s3://{bucket}/{_owner_prefix()}/{BDA_OUTPUT_PREFIX}/{uuid.uuid4().hex}/"
        )
    else:
        try:
            output_s3_uri = f"s3://{bucket}/{resolve_owned_s3_key(output_s3_uri)}"
        except S3AccessDenied as e:
            return f"ERROR: output location rejected. {e}"

    try:
        client = _get_bda_runtime_client()

        # Invoke BDA async processing
        logger.info("BDA: Invoking async processing for %s", s3_uri)
        logger.info("BDA: Output location: %s", output_s3_uri)

        # Get project ARN (required for BDA invocation)
        project_arn = _get_project_arn()
        logger.info("BDA: Using project ARN: %s", project_arn)

        if not project_arn:
            return _PROJECT_MISSING_MARKER

        profile_arn = _get_default_profile_arn()
        logger.info("BDA: profile=%s, project=%s", profile_arn, project_arn)

        invoke_params = {
            "inputConfiguration": {
                "s3Uri": s3_uri,
            },
            "outputConfiguration": {
                "s3Uri": output_s3_uri,
            },
            "dataAutomationConfiguration": {
                "dataAutomationProjectArn": project_arn,
                "stage": "LIVE",
            },
            "dataAutomationProfileArn": profile_arn,
        }

        # DIAG: emit full invoke params via print so they're visible in
        # CloudWatch regardless of Python logger level. Remove once BDA
        # is verified working end-to-end after the security hardening
        # deployment.
        print(f"[BDA-DIAG] video invoke_params={json.dumps(invoke_params, default=str)}", flush=True)

        try:
            response = client.invoke_data_automation_async(**invoke_params)
        except Exception as _e:
            print(f"[BDA-DIAG] video invoke FAILED type={type(_e).__name__} msg={str(_e)[:400]}", flush=True)
            raise

        invocation_arn = response.get("invocationArn", "")
        logger.info("BDA: Invocation ARN: %s", invocation_arn)
        print(f"[BDA-DIAG] video invocation_arn={invocation_arn}", flush=True)
        if not invocation_arn:
            return "ERROR: BDA invocation failed — no invocation ARN returned. Full response: " + json.dumps(response, default=str)

        # Poll for completion
        max_attempts = config.MAX_POLL_ATTEMPTS
        poll_interval = config.POLL_INTERVAL_SECONDS

        for attempt in range(max_attempts):
            status_resp = client.get_data_automation_status(
                invocationArn=invocation_arn
            )
            status = status_resp.get("status", "")
            logger.info("BDA: Poll attempt %d — status: %s", attempt + 1, status)

            if status == "Success":
                break
            elif status in ("FAILED", "STOPPED", "ServiceError", "ClientError"):
                error = status_resp.get("error", {})
                error_msg = error.get("message", json.dumps(status_resp, default=str))
                logger.error("BDA: Processing %s — error: %s", status, error_msg)
                return f"ERROR: BDA processing {status}: {error_msg}"

            time.sleep(poll_interval)
        else:
            return f"ERROR: BDA processing timed out after {max_attempts} attempts."

        # Get output location
        output_config = status_resp.get("outputConfiguration", {})
        result_s3_uri = output_config.get("s3Uri", output_s3_uri)
        logger.info("BDA: Processing complete. Output at: %s", result_s3_uri)

        # Read results from S3
        return _read_bda_results(result_s3_uri, bucket)

    except Exception as e:
        error_msg = str(e)
        logger.error("BDA: Exception during video analysis: %s", error_msg, exc_info=True)
        if "ValidationException" in error_msg:
            return f"ERROR: BDA validation failed — {error_msg}. Ensure BDA is enabled in your account and region ({config.AWS_REGION})."
        if "AccessDeniedException" in error_msg:
            return f"ERROR: Access denied to BDA. Check IAM permissions for bedrock:InvokeDataAutomationAsync. Details: {error_msg}"
        if "ResourceNotFoundException" in error_msg:
            return f"ERROR: BDA resource not found. You may need to create a BDA project first in the Bedrock console. Details: {error_msg}"
        return f"ERROR: BDA invocation failed: {error_msg}"


@tool
def analyze_audio_with_bda(s3_uri: str, output_s3_uri: str = "", force: bool = False) -> str:
    """Analyze an audio file using Bedrock Data Automation (BDA).

    Provides full audio transcript with speaker diarization and summary.
    Supports: English, Chinese, Japanese, Korean, Portuguese, French,
    Italian, Spanish, German, Cantonese, Taiwanese.

    Args:
        s3_uri: S3 URI of the audio file
            (e.g., s3://amzn-s3-demo-audio/audio/file.mp3).
            A bare key is also accepted and is preferred. Must be within the
            calling user's prefix.
        output_s3_uri: S3 URI for output (optional, defaults to a location under
            the calling user's own prefix)
        force: If True, skip the cache check and always re-run analysis.
            Set this only when the user has explicitly asked to re-analyze.

    Returns:
        Full transcript and audio summary.
    """
    # T-10: same treatment as analyze_video_with_bda — see the note there.
    try:
        s3_uri = owned_s3_uri(s3_uri)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    # Cache short-circuit
    if not force:
        cached = _check_cached_transcript()
        if cached:
            return cached

    bucket = config.S3_BUCKET

    if not output_s3_uri:
        output_s3_uri = (
            f"s3://{bucket}/{_owner_prefix()}/{BDA_OUTPUT_PREFIX}/{uuid.uuid4().hex}/"
        )
    else:
        try:
            output_s3_uri = f"s3://{bucket}/{resolve_owned_s3_key(output_s3_uri)}"
        except S3AccessDenied as e:
            return f"ERROR: output location rejected. {e}"

    try:
        client = _get_bda_runtime_client()

        logger.info("BDA Audio: Invoking async processing for %s", s3_uri)

        project_arn = _get_project_arn()
        if not project_arn:
            return _PROJECT_MISSING_MARKER

        profile_arn = _get_default_profile_arn()

        invoke_params = {
            "inputConfiguration": {
                "s3Uri": s3_uri,
            },
            "outputConfiguration": {
                "s3Uri": output_s3_uri,
            },
            "dataAutomationConfiguration": {
                "dataAutomationProjectArn": project_arn,
                "stage": "LIVE",
            },
            "dataAutomationProfileArn": profile_arn,
        }

        response = client.invoke_data_automation_async(**invoke_params)

        invocation_arn = response.get("invocationArn", "")
        logger.info("BDA Audio: Invocation ARN: %s", invocation_arn)
        if not invocation_arn:
            return "ERROR: BDA invocation failed — no invocation ARN returned. Full response: " + json.dumps(response, default=str)

        # Poll for completion
        for attempt in range(config.MAX_POLL_ATTEMPTS):
            status_resp = client.get_data_automation_status(
                invocationArn=invocation_arn
            )
            status = status_resp.get("status", "")
            logger.info("BDA Audio: Poll attempt %d — status: %s", attempt + 1, status)

            if status == "Success":
                break
            elif status in ("FAILED", "STOPPED", "ServiceError", "ClientError"):
                error = status_resp.get("error", {})
                error_msg = error.get("message", json.dumps(status_resp, default=str))
                logger.error("BDA Audio: Processing %s — error: %s", status, error_msg)
                return f"ERROR: BDA processing {status}: {error_msg}"

            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            return "ERROR: BDA processing timed out."

        output_config = status_resp.get("outputConfiguration", {})
        result_s3_uri = output_config.get("s3Uri", output_s3_uri)
        logger.info("BDA Audio: Processing complete. Output at: %s", result_s3_uri)

        return _read_bda_results(result_s3_uri, bucket)

    except Exception as e:
        logger.error("BDA Audio: Exception: %s", str(e), exc_info=True)
        return f"ERROR: BDA audio analysis failed: {e}"


def _read_bda_results(output_s3_uri: str, bucket: str) -> str:
    """Read and parse BDA output from S3.

    BDA writes a job_metadata.json that points to the actual result files.
    The result.json contains video summary, transcript, and chapter summaries.
    """
    session = _get_session()
    s3 = session.client("s3")

    # The output_s3_uri points to the job_metadata.json
    # Parse it to find the actual result path
    metadata_key = output_s3_uri.replace(f"s3://{bucket}/", "")

    try:
        # Read job_metadata.json
        resp = s3.get_object(
            Bucket=bucket, Key=metadata_key, ExpectedBucketOwner=config.get_account_id()
        )
        metadata = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:
        # Try listing objects in the output prefix to find job_metadata.json
        prefix = metadata_key.rsplit("/", 1)[0] if "/" in metadata_key else metadata_key
        try:
            list_resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
            objects = list_resp.get("Contents", [])
            # Find job_metadata.json
            for obj in objects:
                if obj["Key"].endswith("job_metadata.json"):
                    resp = s3.get_object(
                        Bucket=bucket,
                        Key=obj["Key"],
                        ExpectedBucketOwner=config.get_account_id(),
                    )
                    metadata = json.loads(resp["Body"].read().decode("utf-8"))
                    break
            else:
                return f"ERROR: Could not find job_metadata.json in BDA output. Prefix: {prefix}, Objects found: {len(objects)}"
        except Exception as e2:
            return f"ERROR: Could not read BDA output: {e2}"

    # Extract the result.json path from metadata
    output_metadata = metadata.get("output_metadata", [])
    if not output_metadata:
        return "ERROR: BDA job_metadata.json has no output_metadata."

    result_path = None
    for asset in output_metadata:
        segments = asset.get("segment_metadata", [])
        for seg in segments:
            path = seg.get("standard_output_path", "")
            if path:
                result_path = path
                break
        if result_path:
            break

    if not result_path:
        return "ERROR: No standard_output_path found in BDA metadata."

    # Read the result.json
    result_key = result_path.replace(f"s3://{bucket}/", "")
    try:
        resp = s3.get_object(
            Bucket=bucket, Key=result_key, ExpectedBucketOwner=config.get_account_id()
        )
        result = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:
        return f"ERROR: Could not read BDA result file at {result_path}: {e}"

    # Parse the BDA result structure
    video_summary = ""
    transcript_text = ""
    chapter_summaries = []
    detected_language = "auto-detected"
    transcript_segments = []

    # Video summary
    video_data = result.get("video", {})
    if isinstance(video_data, dict):
        video_summary = video_data.get("summary", "")

        # Transcript — can be string or dict with representation.text
        transcript = video_data.get("transcript", "")
        if isinstance(transcript, dict):
            rep = transcript.get("representation", {})
            if isinstance(rep, dict):
                transcript_text = rep.get("text", "")
            elif isinstance(rep, str):
                transcript_text = rep
        elif isinstance(transcript, str):
            transcript_text = transcript

    # Chapter summaries with timestamps
    chapters = result.get("chapters", [])
    for ch in chapters:
        ts = ch.get("start_timecode_smpte", "")
        summary = ch.get("summary", "")
        if summary:
            chapter_summaries.append(f"[{ts}] {summary}")

        # Build transcript segments from chapter transcripts
        ch_transcript = ch.get("transcript", "")
        if isinstance(ch_transcript, dict):
            rep = ch_transcript.get("representation", {})
            ch_text = rep.get("text", "") if isinstance(rep, dict) else ""
        elif isinstance(ch_transcript, str):
            ch_text = ch_transcript
        else:
            ch_text = ""
        if ch_text and ts:
            transcript_segments.append(f"[{ts}] {ch_text[:200]}")

    # If no chapter-level segments, build from full transcript
    if not transcript_segments and transcript_text:
        # Split by speaker markers [spk_X]:
        speaker_parts = re.split(r'(\[spk_\d+\]:)', transcript_text)
        current_speaker = ""
        for part in speaker_parts:
            if re.match(r'\[spk_\d+\]:', part):
                current_speaker = part
            elif part.strip():
                transcript_segments.append(f"{current_speaker} {part.strip()[:150]}")

    # Statistics
    stats = result.get("statistics", {})
    speaker_count = stats.get("speaker_count", 0)
    chapter_count = stats.get("chapter_count", 0)

    # Cache results (compatible with get_transcript_text)
    if transcript_text:
        get_detection_cache()["transcript_text"] = transcript_text
        get_detection_cache()["transcript_language"] = detected_language
        get_detection_cache()["transcript_segments"] = transcript_segments
        get_detection_cache()["transcript_source"] = "bedrock_data_automation"

    if video_summary:
        get_detection_cache()["bda_video_summary"] = video_summary

    if chapter_summaries:
        get_detection_cache()["bda_chapters"] = chapter_summaries

    # Build output
    lines = []

    if video_summary:
        lines.append("## Video Summary (BDA)")
        lines.append(video_summary)
        lines.append("")

    lines.append(f"**Statistics:** {chapter_count} chapters, {speaker_count} speakers detected")
    lines.append("")

    if chapter_summaries:
        lines.append("## Chapter Summaries")
        for ch in chapter_summaries:
            lines.append(f"- {ch}")
        lines.append("")

    if transcript_text:
        lines.append(f"## Transcript ({len(transcript_text)} characters)")
        # Show first 50 segments
        lines.extend(transcript_segments[:50])
        if len(transcript_segments) > 50:
            lines.append(f"... and {len(transcript_segments) - 50} more segments")
        lines.append("")
        lines.append("Use get_transcript_text for the full raw transcript.")

    if not lines:
        lines.append("BDA processing completed but no content was extracted.")
        lines.append(f"Result keys: {list(result.keys())}")

    return "\n".join(lines)


@tool
def list_bda_projects() -> str:
    """List available Bedrock Data Automation projects in the account.

    Use this to find the project ARN if you want to use a custom BDA project
    with specific output configurations.

    Returns:
        List of BDA projects with their ARNs and status.
    """
    try:
        client = _get_bda_client()
        response = client.list_data_automation_projects()
        projects = response.get("projects", [])

        if not projects:
            return (
                "No BDA projects found in this account. BDA requires a project to run analysis. "
                "To proceed, ask the user whether to:\n"
                "1. Create a default project — call `create_bda_project` with no arguments\n"
                "2. Switch to Traditional (Rekognition + Transcribe) mode in the "
                "sidebar under **⚙️ Analysis Mode**, then re-upload the video. "
                "Traditional tools are disabled in BDA mandatory mode; switching "
                "modes enables `detect_labels` and `transcribe_audio`."
            )

        lines = [f"Found {len(projects)} BDA project(s):"]
        for proj in projects:
            arn = proj.get("projectArn", "")
            name = proj.get("projectName", "")
            status = proj.get("projectStatus", "")
            lines.append(f"  - {name} ({status}): {arn}")

        return "\n".join(lines)

    except Exception as e:
        if "AccessDeniedException" in str(e):
            return "Access denied to list BDA projects. Check IAM permissions."
        return f"ERROR: {e}"


# ── File Classification ───────────────────────────────────────────────────

# Extensions supported by BDA (video, audio, documents)
BDA_VIDEO_AUDIO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".wav"}
BDA_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".tiff", ".tif", ".jpeg", ".jpg", ".png"}

# Extensions that must be handled locally via read_document
LOCAL_ONLY_EXTENSIONS = {".doc", ".svg", ".csv", ".xlsx", ".xls", ".txt", ".md"}


def _classify_file(file_path: str) -> str:
    """Classify a file into its processing method.

    Returns one of: 'bda_media', 'bda_document', 'local', 'doc_convert'
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext in BDA_VIDEO_AUDIO_EXTENSIONS:
        return "bda_media"
    elif ext in BDA_DOCUMENT_EXTENSIONS:
        return "bda_document"
    elif ext == ".doc":
        return "doc_convert"
    else:
        return "local"


def _convert_doc_to_docx(doc_path: str) -> str:
    """Convert a .doc file to .docx using LibreOffice or textutil (macOS).

    Returns the path to the converted .docx file, or raises an exception.
    """
    output_dir = os.path.dirname(doc_path) or config.TMP_DIR
    base_name = os.path.splitext(os.path.basename(doc_path))[0]
    docx_path = os.path.join(output_dir, f"{base_name}.docx")

    # Try textutil on macOS first (no extra install needed)
    try:
        result = subprocess.run(
            ["textutil", "-convert", "docx", "-output", docx_path, doc_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(docx_path):
            return docx_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try LibreOffice as fallback
    try:
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "docx", "--outdir", output_dir, doc_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and os.path.exists(docx_path):
            return docx_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    raise RuntimeError(
        f"Could not convert {doc_path} to .docx. "
        "Install LibreOffice or use macOS textutil."
    )


def _verify_s3_object_exists(bucket: str, key: str, max_retries: int = 3) -> bool:
    """Verify a file exists in S3 using head_object with retries.

    Handles eventual consistency by retrying with backoff.
    """
    session = _get_session()
    s3 = session.client("s3")
    for attempt in range(max_retries):
        try:
            s3.head_object(
                Bucket=bucket,
                Key=key,
                ExpectedBucketOwner=config.get_account_id(),
            )
            return True
        except s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))  # backoff: 1s, 2s, 3s
                    continue
                return False
            raise
    return False


def _upload_file_to_s3(file_path: str) -> tuple[str, str, str]:
    """Upload a local file to S3 and verify it exists.

    Returns: (s3_uri, bucket, s3_key)
    Raises RuntimeError if upload fails verification.
    """
    session = _get_session()
    s3 = session.client("s3")
    ext = os.path.splitext(file_path)[1].lower()
    s3_key = f"{config.get_user_prefix()}/bda-input/{uuid.uuid4().hex}{ext}"
    bucket = config.S3_BUCKET

    s3.upload_file(
        file_path,
        bucket,
        s3_key,
        ExtraArgs={"ExpectedBucketOwner": config.get_account_id()},
    )

    # Verify the file exists in S3 before proceeding
    if not _verify_s3_object_exists(bucket, s3_key):
        raise RuntimeError(
            f"S3 upload verification failed: {file_path} → s3://{bucket}/{s3_key}. "
            "File not found after upload."
        )

    return f"s3://{bucket}/{s3_key}", bucket, s3_key


def _process_file_with_bda(file_path: str, s3_uri: str, bucket: str) -> str:
    """Run BDA invoke_data_automation_async on a single file and poll to completion.

    Returns the extracted content or an error message (never falls back silently).
    """
    output_s3_uri = f"s3://{bucket}/{BDA_OUTPUT_PREFIX}/{uuid.uuid4().hex}/"

    try:
        client = _get_bda_runtime_client()
        project_arn = _get_project_arn()
        if not project_arn:
            return f"BDA ERROR [{file_path}]: {_PROJECT_MISSING_MARKER}"

        profile_arn = _get_default_profile_arn()

        invoke_params = {
            "inputConfiguration": {
                "s3Uri": s3_uri,
            },
            "outputConfiguration": {
                "s3Uri": output_s3_uri,
            },
            "dataAutomationConfiguration": {
                "dataAutomationProjectArn": project_arn,
                "stage": "LIVE",
            },
            "dataAutomationProfileArn": profile_arn,
        }

        response = client.invoke_data_automation_async(**invoke_params)
        invocation_arn = response.get("invocationArn", "")
        if not invocation_arn:
            return f"BDA ERROR [{file_path}]: No invocation ARN returned. Response: {json.dumps(response, default=str)}"

        # Poll for completion — match on "Success" status
        for attempt in range(config.MAX_POLL_ATTEMPTS):
            status_resp = client.get_data_automation_status(invocationArn=invocation_arn)
            status = status_resp.get("status", "")

            if status == "Success":
                break
            elif status in ("FAILED", "STOPPED", "ServiceError", "ClientError"):
                error = status_resp.get("error", {})
                error_msg = error.get("message", json.dumps(status_resp, default=str))
                return f"BDA ERROR [{file_path}]: Processing {status} — {error_msg}"

            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            return f"BDA ERROR [{file_path}]: Processing timed out after {config.MAX_POLL_ATTEMPTS * config.POLL_INTERVAL_SECONDS}s"

        # Read results from S3
        output_config = status_resp.get("outputConfiguration", {})
        result_s3_uri = output_config.get("s3Uri", output_s3_uri)
        return _read_bda_results(result_s3_uri, bucket)

    except Exception as e:
        return f"BDA ERROR [{file_path}]: {e}"


def _process_local_file(file_path: str) -> str:
    """Process a file locally using read_document.

    Handles SVG specially: extracts text if available, otherwise notes it's visual-only.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".svg":
        return _process_svg(file_path)

    # Use the existing read_document tool function directly
    return read_document(file_path=file_path)


def _process_svg(file_path: str) -> str:
    """Extract text from SVG if available, otherwise note it's a visual diagram."""
    if not os.path.exists(file_path):
        return f"ERROR: File not found: {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            svg_content = f.read()

        # Extract text elements from SVG
        texts = re.findall(r'<text[^>]*>(.*?)</text>', svg_content, re.DOTALL)
        # Also check for tspan elements
        tspans = re.findall(r'<tspan[^>]*>(.*?)</tspan>', svg_content, re.DOTALL)
        all_text = texts + tspans
        text_content = "\n".join(t.strip() for t in all_text if t.strip())

        if text_content:
            return (
                f"SVG file: {file_path}\n\n"
                f"Extracted text content:\n{text_content}"
            )
        return (
            f"SVG file: {file_path}\n\n"
            "Visual diagram — no text content extractable. "
            "This file contains vector graphics without embedded text elements."
        )
    except Exception as e:
        return f"ERROR reading SVG {file_path}: {e}"


@tool
def analyze_all_files_with_bda(file_paths: list[str]) -> str:
    """Analyze multiple files using the best method for each file type.

    Routes each file to the appropriate analysis method:
    - Video/audio (mp4, mov, avi, mkv, webm, mp3, m4a, wav) → BDA
    - BDA-supported documents (pdf, docx, tiff, jpeg, png) → BDA
    - Unsupported formats (csv, xlsx, txt, md, svg) → local read_document
    - .doc files → auto-converted to .docx, then processed via BDA

    All BDA invocations run in parallel. Local file reads also run in parallel.
    Results are aggregated into a unified response with a Sources section.

    Args:
        file_paths: List of file paths (local paths or s3:// URIs).

    Returns:
        Combined analysis results from all files with a Sources section
        showing which method was used for each file.
    """
    if not file_paths:
        return "ERROR: No file paths provided."

    # Classify files into processing groups
    bda_files: list[tuple[str, str]] = []  # (original_path, path_to_process)
    local_files: list[tuple[str, str]] = []  # (original_path, path_to_process)
    sources: dict[str, str] = {}  # file → method used
    errors: list[str] = []

    for fp in file_paths:
        fp = fp.strip()
        if not fp:
            continue

        # Handle S3 URIs directly — assume they're BDA-compatible
        if fp.startswith("s3://"):
            bda_files.append((fp, fp))
            continue

        if not os.path.exists(fp):
            errors.append(f"File not found: {fp}")
            sources[fp] = "SKIPPED (not found)"
            continue

        classification = _classify_file(fp)

        if classification == "doc_convert":
            # Convert .doc → .docx first
            try:
                docx_path = _convert_doc_to_docx(fp)
                bda_files.append((fp, docx_path))
            except RuntimeError as e:
                errors.append(f"{fp}: {e}")
                sources[fp] = "FAILED (.doc conversion)"
        elif classification in ("bda_media", "bda_document"):
            bda_files.append((fp, fp))
        else:
            local_files.append((fp, fp))

    # ── Process BDA files in parallel ─────────────────────────────────────
    bda_results: dict[str, str] = {}

    def _bda_worker(original_path: str, process_path: str) -> tuple[str, str]:
        """Upload to S3 (if local), verify, then invoke BDA."""
        try:
            if process_path.startswith("s3://"):
                # Already in S3 — verify it exists
                parts = process_path.replace("s3://", "").split("/", 1)
                bucket = parts[0]
                key = parts[1] if len(parts) > 1 else ""
                if not _verify_s3_object_exists(bucket, key):
                    return original_path, f"BDA ERROR [{original_path}]: File not found in S3: {process_path}"
                s3_uri = process_path
            else:
                # Upload local file to S3
                s3_uri, bucket, _ = _upload_file_to_s3(process_path)

            result = _process_file_with_bda(original_path, s3_uri, bucket)
            return original_path, result
        except Exception as e:
            return original_path, f"BDA ERROR [{original_path}]: {e}"

    # ── Process local files in parallel ───────────────────────────────────
    local_results: dict[str, str] = {}

    def _local_worker(original_path: str, process_path: str) -> tuple[str, str]:
        """Read file locally."""
        result = _process_local_file(process_path)
        return original_path, result

    # Run both groups concurrently
    max_workers = min(10, len(bda_files) + len(local_files))
    if max_workers == 0:
        # Only errors, no processable files
        if errors:
            return "## Errors\n\n" + "\n".join(f"- {e}" for e in errors)
        return "ERROR: No valid files to process."

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}

        # Submit BDA jobs
        for original_path, process_path in bda_files:
            future = executor.submit(_bda_worker, original_path, process_path)
            futures[future] = ("bda", original_path)

        # Submit local jobs
        for original_path, process_path in local_files:
            future = executor.submit(_local_worker, original_path, process_path)
            futures[future] = ("local", original_path)

        # Collect results
        for future in as_completed(futures):
            method, original_path = futures[future]
            try:
                path, result = future.result()
                if method == "bda":
                    bda_results[path] = result
                    ext = os.path.splitext(path)[1].lower()
                    if path.startswith("s3://"):
                        sources[path] = "BDA (already in S3)"
                    elif ext in BDA_VIDEO_AUDIO_EXTENSIONS:
                        sources[path] = "BDA (video/audio)"
                    elif ext == ".doc":
                        sources[path] = "BDA (converted .doc → .docx)"
                    else:
                        sources[path] = "BDA (document)"
                else:
                    local_results[path] = result
                    ext = os.path.splitext(path)[1].lower()
                    if ext == ".svg":
                        sources[path] = "Local (SVG text extraction)"
                    else:
                        sources[path] = "Local (read_document)"
            except Exception as e:
                errors.append(f"{original_path}: Unexpected error — {e}")
                sources[original_path] = "FAILED (exception)"

    # ── Aggregate results ─────────────────────────────────────────────────
    output_lines = []
    output_lines.append("# File Analysis Results")
    output_lines.append("")

    # Show results in order of input
    file_order = [fp.strip() for fp in file_paths if fp.strip()]
    for fp in file_order:
        if fp in bda_results:
            output_lines.append(f"## {os.path.basename(fp)}")
            output_lines.append(f"*Source: {fp}*")
            output_lines.append("")
            output_lines.append(bda_results[fp])
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")
        elif fp in local_results:
            output_lines.append(f"## {os.path.basename(fp)}")
            output_lines.append(f"*Source: {fp}*")
            output_lines.append("")
            output_lines.append(local_results[fp])
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

    # Show errors
    if errors:
        output_lines.append("## Errors")
        output_lines.append("")
        for err in errors:
            output_lines.append(f"- {err}")
        output_lines.append("")

    # Sources section — labeled clearly so the agent always includes it
    output_lines.append("## Sources — Analysis Method Per File")
    output_lines.append("")
    output_lines.append("| File | Method |")
    output_lines.append("|------|--------|")
    for fp in file_order:
        method = sources.get(fp, "Unknown")
        output_lines.append(f"| {os.path.basename(fp)} | {method} |")
    output_lines.append("")

    return "\n".join(output_lines)
