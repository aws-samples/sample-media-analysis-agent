"""
Background processing for video uploads.

Starts S3 upload, Rekognition label detection, and Transcribe jobs
in background threads as soon as a video is uploaded. Results are
stored in the shared detection cache so the agent can use them
immediately when the user asks a question.
"""

import os
import threading
import time
import uuid

import boto3

import config
from tools import (
    get_detection_cache,
    set_session_cache,
    reset_session_cache,
    _format_timestamp,
)

# ── T-02: per-session background job state ────────────────────────────────
#
# This state used to live in a module-global `_bg_state` dict, which is
# shared across every Streamlit session thread in the process. Two users
# uploading concurrently clobbered each other's progress, and the *_error
# strings can carry S3 keys, filenames, and AWS error text referencing the
# other user's resources — the same cross-session disclosure as the
# detection cache itself (found during the T-02 review; see threat model).
#
# Status now lives under the "_bg_state" key of the per-session detection
# cache, which is resolved through the ContextVar. The lock is retained
# because the three background threads of a SINGLE session still mutate
# their own session's status concurrently.
_bg_lock = threading.Lock()

_BG_STATE_DEFAULT = {
    "upload_status": "idle",       # idle, running, done, error
    "labels_status": "idle",
    "transcribe_status": "idle",
    "upload_error": "",
    "labels_error": "",
    "transcribe_error": "",
}


def _bg_state_for_session() -> dict:
    """Return (creating if needed) this session's background-state dict."""
    cache = get_detection_cache()
    if "_bg_state" not in cache:
        cache["_bg_state"] = dict(_BG_STATE_DEFAULT)
    return cache["_bg_state"]


def get_status() -> dict:
    """Get the calling session's background processing status (thread-safe)."""
    with _bg_lock:
        return dict(_bg_state_for_session())


def _set_status(key: str, value: str, error: str = ""):
    with _bg_lock:
        state = _bg_state_for_session()
        state[key] = value
        if error:
            state[key.replace("_status", "_error")] = error


def _get_session():
    if config.AWS_PROFILE:
        return boto3.Session(
            profile_name=config.AWS_PROFILE,
            region_name=config.AWS_REGION,
        )
    return boto3.Session(region_name=config.AWS_REGION)


def _upload_to_s3(local_path: str, user_prefix: str = None) -> str:
    """Upload video to S3 and return the S3 key.
    
    user_prefix should be passed from the caller (which has Streamlit context).
    If not provided, falls back to config.get_user_prefix() which may not work
    correctly in background threads.
    """
    _set_status("upload_status", "running")
    try:
        session = _get_session()
        s3 = session.client("s3")
        ext = os.path.splitext(local_path)[1]
        # Use passed-in user_prefix if available, otherwise fall back
        prefix = user_prefix if user_prefix else config.get_user_prefix()
        s3_key = f"{prefix}/videos/{uuid.uuid4().hex}{ext}"
        s3.upload_file(
            local_path,
            config.S3_BUCKET,
            s3_key,
            ExtraArgs={"ExpectedBucketOwner": config.get_account_id()},
        )

        get_detection_cache()["last_s3_key"] = s3_key
        get_detection_cache()["last_video_path"] = local_path
        _set_status("upload_status", "done")
        return s3_key
    except Exception as e:
        _set_status("upload_status", "error", str(e))
        return ""


def _run_label_detection(s3_key: str):
    """Run Rekognition label detection and cache results."""
    _set_status("labels_status", "running")
    try:
        session = _get_session()
        rek = session.client("rekognition")

        response = rek.start_label_detection(
            Video={"S3Object": {"Bucket": config.S3_BUCKET, "Name": s3_key}},
            NotificationChannel={
                "SNSTopicArn": config.SNS_TOPIC_ARN,
                "RoleArn": config.REKOGNITION_ROLE_ARN,
            },
            MinConfidence=config.MIN_CONFIDENCE,
        )
        job_id = response["JobId"]

        for _ in range(config.MAX_POLL_ATTEMPTS):
            resp = rek.get_label_detection(JobId=job_id, SortBy="TIMESTAMP")
            if resp["JobStatus"] == "SUCCEEDED":
                break
            elif resp["JobStatus"] == "FAILED":
                _set_status("labels_status", "error", resp.get("StatusMessage", "Unknown"))
                return
            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            _set_status("labels_status", "error", "Timed out")
            return

        all_labels = list(resp.get("Labels", []))
        next_token = resp.get("NextToken")
        while next_token:
            resp = rek.get_label_detection(JobId=job_id, SortBy="TIMESTAMP", NextToken=next_token)
            all_labels.extend(resp.get("Labels", []))
            next_token = resp.get("NextToken")

        get_detection_cache()["labels"] = all_labels
        get_detection_cache()["job_id"] = job_id
        _set_status("labels_status", "done")

    except Exception as e:
        _set_status("labels_status", "error", str(e))


def _run_transcription(s3_key: str):
    """Run Transcribe job and cache results."""
    _set_status("transcribe_status", "running")
    try:
        session = _get_session()
        transcribe = session.client("transcribe")

        job_name = f"video-agent-bg-{uuid.uuid4().hex[:12]}"
        s3_uri = f"s3://{config.S3_BUCKET}/{s3_key}"

        ext = s3_key.rsplit(".", 1)[-1].lower()
        format_map = {
            "mp3": "mp3", "mp4": "mp4", "m4a": "mp4",
            "wav": "wav", "flac": "flac", "ogg": "ogg", "webm": "webm",
        }
        media_format = format_map.get(ext, "mp4")

        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": s3_uri},
            MediaFormat=media_format,
            IdentifyLanguage=True,
            LanguageOptions=[
                "en-US", "zh-CN", "zh-TW", "ja-JP", "ko-KR",
                "es-US", "fr-FR", "de-DE", "pt-BR", "it-IT",
            ],
            Settings={
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": 10,
            },
        )

        for _ in range(config.MAX_POLL_ATTEMPTS):
            resp = transcribe.get_transcription_job(TranscriptionJobName=job_name)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            elif status == "FAILED":
                reason = resp["TranscriptionJob"].get("FailureReason", "Unknown")
                _set_status("transcribe_status", "error", reason)
                return
            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            _set_status("transcribe_status", "error", "Timed out")
            return

        # Get transcript
        transcript_uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        detected_lang = resp["TranscriptionJob"].get("LanguageCode", "unknown")

        # AWS Transcribe returns an https:// presigned URL; enforce that
        # explicitly to defend against a compromised/mis-configured response
        # returning file:// or a custom scheme (bandit B310).
        import json
        import urllib.request
        from urllib.parse import urlparse
        _scheme = urlparse(transcript_uri).scheme
        if _scheme != "https":
            _set_status(
                "transcribe_status",
                "error",
                f"Refusing to fetch transcript from non-HTTPS URI (scheme={_scheme!r})",
            )
            return
        # Scheme is validated to be 'https' immediately above; URL is a
        # presigned AWS Transcribe response URI.
        with urllib.request.urlopen(transcript_uri) as response:  # nosec B310
            transcript_data = json.loads(response.read().decode("utf-8"))

        results = transcript_data.get("results", {})
        transcripts = results.get("transcripts", [])
        full_text = transcripts[0]["transcript"] if transcripts else ""

        # Build timestamped segments
        items = results.get("items", [])
        segments = []
        current_line = []
        current_start = None

        for item in items:
            if item["type"] == "pronunciation":
                start_time = float(item.get("start_time", 0))
                word = item["alternatives"][0]["content"]
                if current_start is None:
                    current_start = start_time
                current_line.append(word)
                if len(current_line) >= 10:
                    ts = _format_timestamp(int(current_start * 1000))
                    segments.append(f"[{ts}] {' '.join(current_line)}")
                    current_line = []
                    current_start = None
            elif item["type"] == "punctuation":
                if current_line:
                    current_line[-1] += item["alternatives"][0]["content"]

        if current_line and current_start is not None:
            ts = _format_timestamp(int(current_start * 1000))
            segments.append(f"[{ts}] {' '.join(current_line)}")

        get_detection_cache()["transcript_text"] = full_text
        get_detection_cache()["transcript_language"] = detected_lang
        get_detection_cache()["transcript_segments"] = segments

        # Clean up job
        try:
            transcribe.delete_transcription_job(TranscriptionJobName=job_name)
        except Exception:
            pass

        _set_status("transcribe_status", "done")

    except Exception as e:
        _set_status("transcribe_status", "error", str(e))


def _in_session_context(cache: dict, fn, *args, **kwargs):
    """Run `fn` in a thread with `cache` bound as the session cache.

    T-02: raw threading.Thread does NOT inherit contextvars — a fresh thread
    starts with an empty context and ContextVar.get() raises LookupError
    (empirically verified). Every thread target must therefore bind the cache
    explicitly.

    Deliberately does NOT use contextvars.copy_context() + ctx.run(): a single
    Context object cannot be entered from two threads concurrently
    ("RuntimeError: cannot enter context: <Context> is already entered"), and
    the label/transcribe threads below run in parallel. Binding from an
    explicitly-passed dict avoids that class of bug entirely.
    """
    token = set_session_cache(cache)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_session_cache(token)


def start_background_processing(video_path: str, user_prefix: str = None):
    """Start background upload + analysis for a video file.

    Launches three threads:
    1. Upload to S3
    2. Rekognition label detection (after upload completes)
    3. Transcribe audio (after upload completes)

    All results are stored in the calling session's detection cache.

    Must be called from the Streamlit script thread: the session cache and
    user_prefix are captured here and passed explicitly into the worker
    threads, which cannot reach st.session_state.
    """
    # T-02: capture the caller's session cache while we still have Streamlit
    # context. Every worker thread binds this same dict.
    session_cache = get_detection_cache()

    # Reset this session's status (not the process-wide status).
    with _bg_lock:
        state = _bg_state_for_session()
        state.update(_BG_STATE_DEFAULT)

    def _pipeline():
        # Step 1: Upload (pass user_prefix since we're in a background thread)
        s3_key = _upload_to_s3(video_path, user_prefix=user_prefix)
        if not s3_key:
            return

        # Step 2: Run label detection and transcription in parallel.
        # Each nested thread re-binds the session cache — context does not
        # propagate across a threading.Thread boundary, not even from a
        # thread that already has it bound.
        label_thread = threading.Thread(
            target=_in_session_context,
            args=(session_cache, _run_label_detection, s3_key),
            daemon=True,
        )
        transcribe_thread = threading.Thread(
            target=_in_session_context,
            args=(session_cache, _run_transcription, s3_key),
            daemon=True,
        )
        label_thread.start()
        transcribe_thread.start()
        label_thread.join()
        transcribe_thread.join()

    pipeline_thread = threading.Thread(
        target=_in_session_context,
        args=(session_cache, _pipeline),
        daemon=True,
    )
    pipeline_thread.start()
