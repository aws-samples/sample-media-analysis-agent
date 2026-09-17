"""
Agent tools — AWS media analysis and S3 operations exposed as Strands tools.

Each @tool function is a discrete capability the agent can invoke.
Detection results are cached in-memory so the agent can search/filter
without re-running expensive Rekognition jobs.
"""

import contextvars
import os
import tempfile
import time
import uuid
from typing import Optional

import boto3
from strands import tool

import config

# ── T-02 mitigation: per-session detection cache ──────────────────────────
#
# Streamlit runs each browser session's script in a separate thread inside
# ONE Python process. Module globals are therefore shared across every
# concurrent user. The previous design stored per-session data in the module
# global `_detection_cache` and "isolated" it by rebinding the module
# attribute from chat_app.py on each run — which cannot isolate anything,
# because the rebind is visible to every other thread immediately.
#
# The fix uses two mechanisms with distinct jobs:
#
#   ContextVar          — PROPAGATION. Carries the current session's cache
#                         into any thread that inherits the context. Verified
#                         to survive the strands agent invocation chain
#                         (strands._async.run_async uses copy_context() +
#                         executor.submit(context.run, ...), and
#                         asyncio.to_thread copies context per CPython
#                         stdlib), so @tool functions see the correct cache.
#
#   st.session_state    — PERSISTENCE. Survives Streamlit reruns. The
#                         ContextVar points at the same dict object that
#                         lives in session_state.
#
# Raw threading.Thread does NOT inherit context (empirically verified:
# ContextVar.get() raises LookupError in a fresh thread). Background threads
# must therefore receive the cache explicitly and call set_session_cache()
# at the top of each thread target — see background_processor.py.
#
# See docs/threat-model.md T-02.

_session_cache_var: contextvars.ContextVar = contextvars.ContextVar(
    "detection_cache"
)

# Single-tenant fallback for non-Streamlit callers (agent.py running under
# Bedrock AgentCore, CLI use, unit tests). Each of those runs one tenant per
# process, so a module-level dict is safe there. It is NEVER reached from the
# Streamlit path, because the ContextVar is set on every script run.
_detection_cache: dict = {}


def set_session_cache(cache: dict):
    """Bind `cache` as the current context's detection cache.

    Returns the ContextVar token so callers can reset() it if they need to
    restore the previous value (used by background thread targets).

    Must be called on EVERY Streamlit script run, not once: Streamlit may
    reuse threads, and a reused thread retains the previous occupant's
    ContextVar value unless it is re-set (empirically verified).
    """
    return _session_cache_var.set(cache)


def reset_session_cache(token) -> None:
    """Restore the ContextVar to its prior value using a set() token."""
    _session_cache_var.reset(token)


def _has_streamlit_context() -> bool:
    """True only when called from inside a real Streamlit script run.

    Necessary because st.session_state does NOT raise when accessed outside a
    ScriptRunContext — it logs a warning and returns a throwaway proxy. A
    try/except around st.session_state therefore silently succeeds and hands
    back a dict whose writes go nowhere. Checking for the ScriptRunContext
    directly is the only reliable discriminator.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except ImportError:
        try:
            # Streamlit moved this symbol between versions.
            from streamlit.runtime.scriptrunner_utils.script_run_context import (
                get_script_run_ctx,
            )
        except ImportError:
            return False
    try:
        return get_script_run_ctx() is not None
    except Exception:
        return False


def get_detection_cache() -> dict:
    """Return the detection cache for the current session.

    Resolution order:
      1. ContextVar — set by chat_app.py per script run, and by background
         thread targets. Checked first so worker threads never touch
         st.session_state (unavailable outside a ScriptRunContext).
      2. st.session_state — the persistence layer for the Streamlit path.
         Only consulted when a real ScriptRunContext exists.
      3. Module-level dict — single-tenant fallback (agent.py under Bedrock
         AgentCore, CLI use, unit tests).
    """
    try:
        return _session_cache_var.get()
    except LookupError:
        pass

    if _has_streamlit_context():
        try:
            import streamlit as st
            if "_detection_cache" not in st.session_state:
                st.session_state["_detection_cache"] = {}
            return st.session_state["_detection_cache"]
        except Exception:
            pass

    return _detection_cache


# ── S3 path ownership validation (T-10) ───────────────────────────────────
#
# Tools that accept an S3 location as an argument are trusting whatever the
# model passed them. Two distinct failure modes follow from that, and both
# were observed on the first live deployment:
#
#   1. Authorization. A crafted request — or a plainly-worded one, since
#      "clean up s3 keys alice/videos/x.mp4" contains no attack pattern —
#      could reach another user's prefix. The task role can read and delete
#      across the whole bucket, so nothing downstream stops it.
#   2. Correctness. The model does not reliably know the bucket name and
#      will invent one. A real run produced
#      s3://bedrock-data-auto-us-east-1-<account>/videos/<original filename>,
#      a bucket that does not exist, and the resulting generic
#      ValidationException sent troubleshooting toward IAM for several
#      minutes.
#
# Validating at the tool boundary fixes both: out-of-prefix access is
# refused regardless of how the instruction arrived, and a fabricated
# bucket fails immediately with a message that names the real problem.
#
# See docs/threat-model.md T-10.

class S3AccessDenied(Exception):
    """An S3 location outside the caller's own prefix was requested."""


def _ownership_hint() -> str:
    """Append the session's real S3 URI to a refusal, so the agent can retry.

    Without this the model receives a bare rejection and tends to guess again.
    Naming the correct object makes the refusal self-correcting.
    """
    key = get_detection_cache().get("last_s3_key")
    if key:
        return f" The object uploaded in this session is s3://{config.S3_BUCKET}/{key} — use that."
    return " No object has been uploaded in this session yet."


def _owner_prefix() -> str:
    """Return the calling user's S3 prefix, usable from worker threads.

    The session cache is authoritative and is checked first, because it
    propagates into worker threads through the ContextVar. chat_app.py seeds
    it on every script run, while a ScriptRunContext still exists.

    Why the cache has to come first: config.get_user_prefix() locates the
    Cognito identity via st.session_state and, when it cannot, falls back to a
    name derived from the IAM caller. Agent tools run in an asyncio worker
    thread with no ScriptRunContext, so calling it there returns the task
    role's session name — a value that matches none of the user's objects.

    An earlier version of this function cached whatever it resolved, including
    that IAM fallback. When the first call happened to land in a worker thread,
    the wrong prefix was written into the session cache and every subsequent
    ownership check in that session refused the user's own files. The direct
    resolution below is therefore only cached when a Streamlit context is
    present, so a worker thread can never poison the session.
    """
    cache = get_detection_cache()
    cached = cache.get("user_prefix")
    if cached:
        return cached

    # No cached value. Either this is not a Streamlit session at all (agent.py
    # under Bedrock AgentCore, CLI use, tests), where the IAM-derived prefix is
    # the correct answer because uploads use it too — or it is a worker thread
    # that ran before seeding, where the IAM-derived prefix will simply fail
    # the ownership check. Failing closed is the acceptable outcome there.
    try:
        prefix = config.get_user_prefix()
    except Exception:
        return ""

    if not prefix:
        return ""

    if _has_streamlit_context():
        cache["user_prefix"] = prefix

    return prefix


def resolve_owned_s3_key(value: str) -> str:
    """Normalise an S3 argument to a bare key and verify the caller owns it.

    Accepts either a full ``s3://<bucket>/<key>`` URI or a bare key, so callers
    are not required to know which form a tool wants.

    Raises:
        S3AccessDenied: the bucket is not the configured bucket, or the key
            does not sit under the calling user's prefix.
    """
    if not value or not str(value).strip():
        raise S3AccessDenied("No S3 location supplied.")

    raw = str(value).strip()
    key = raw

    if raw.startswith("s3://"):
        remainder = raw[len("s3://"):]
        bucket, _, key = remainder.partition("/")
        if bucket != config.S3_BUCKET:
            raise S3AccessDenied(
                f"Refusing to access bucket '{bucket}': this deployment only uses "
                f"'{config.S3_BUCKET}'." + _ownership_hint()
            )

    key = key.lstrip("/")

    # Reject traversal before prefix matching, so '<prefix>/../other/x' cannot
    # satisfy the startswith test below.
    if ".." in key.split("/"):
        raise S3AccessDenied(f"Refusing S3 key containing a parent-directory segment: '{key}'")

    if not key:
        raise S3AccessDenied("S3 location resolved to an empty key.")

    prefix = _owner_prefix()
    if not prefix:
        raise S3AccessDenied(
            "Could not determine the calling user's S3 prefix, so ownership of "
            f"'{key}' cannot be verified. Refusing rather than guessing."
        )

    if key != prefix and not key.startswith(f"{prefix}/"):
        raise S3AccessDenied(
            f"Refusing to access '{key}': outside this user's prefix '{prefix}/'."
            + _ownership_hint()
        )

    return key


def owned_s3_uri(key: str) -> str:
    """Build a full s3:// URI for a key, after verifying ownership."""
    return f"s3://{config.S3_BUCKET}/{resolve_owned_s3_key(key)}"


def _get_session():
    if config.AWS_PROFILE:
        return boto3.Session(
            profile_name=config.AWS_PROFILE,
            region_name=config.AWS_REGION,
        )
    return boto3.Session(region_name=config.AWS_REGION)


def _format_timestamp(ms: int) -> str:
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


@tool
def upload_video_to_s3(local_path: str) -> str:
    """Upload a local video file to S3 for Rekognition processing.

    Use this for a video file provided by the user. Returns the S3 key needed
    for detection tools.

    Args:
        local_path: Path to the local video file.

    Returns:
        S3 key of the uploaded video.
    """
    if not os.path.exists(local_path):
        return f"ERROR: File not found: {local_path}"

    # Check if this video is already uploaded (avoid double-upload from background processing)
    existing_key = get_detection_cache().get("last_s3_key")
    existing_path = get_detection_cache().get("last_video_path")
    if existing_key and existing_path == local_path:
        size_mb = os.path.getsize(local_path) / (1024 * 1024)
        return f"Already uploaded to s3://{config.S3_BUCKET}/{existing_key} ({size_mb:.1f} MB). Use s3_key='{existing_key}' for detection."

    session = _get_session()
    s3 = session.client("s3")
    ext = os.path.splitext(local_path)[1]
    s3_key = f"{config.get_user_prefix()}/videos/{uuid.uuid4().hex}{ext}"
    s3.upload_file(
        local_path,
        config.S3_BUCKET,
        s3_key,
        ExtraArgs={"ExpectedBucketOwner": config.get_account_id()},
    )
    size_mb = os.path.getsize(local_path) / (1024 * 1024)
    get_detection_cache()["last_s3_key"] = s3_key
    get_detection_cache()["last_video_path"] = local_path
    return f"Uploaded to s3://{config.S3_BUCKET}/{s3_key} ({size_mb:.1f} MB). Use s3_key='{s3_key}' for detection."


@tool
def detect_labels(s3_key: str, min_confidence: float = 50.0) -> str:
    """Run Amazon Rekognition label detection on a video in S3.

    Detects objects, scenes, and activities. This is async — starts the job,
    polls until complete, and caches all results for subsequent searches.

    Args:
        s3_key: S3 key of the video (from upload_video_to_s3). May also be a
            full s3:// URI. Must be within the calling user's own prefix.
        min_confidence: Minimum confidence threshold (0-100). Default 50.

    Returns:
        Summary of detected labels with counts.
    """
    # T-10: refuse before doing anything, including serving cached results.
    try:
        s3_key = resolve_owned_s3_key(s3_key)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    # Check if labels are already cached (avoid double-processing from background)
    if "labels" in get_detection_cache():
        all_labels = get_detection_cache()["labels"]
        label_counts = {}
        for item in all_labels:
            name = item.get("Label", {}).get("Name", "")
            if name:
                label_counts[name] = label_counts.get(name, 0) + 1
        top = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        lines = [f"[CACHED] Detected {len(all_labels)} label instances across {len(label_counts)} unique labels."]
        lines.append(f"Top labels: {', '.join(f'{n} ({c}x)' for n, c in top)}")
        lines.append("Use search_detections to find specific objects, or list_all_labels for full inventory.")
        return "\n".join(lines)

    session = _get_session()
    rek = session.client("rekognition")

    response = rek.start_label_detection(
        Video={"S3Object": {"Bucket": config.S3_BUCKET, "Name": s3_key}},
        NotificationChannel={
            "SNSTopicArn": config.SNS_TOPIC_ARN,
            "RoleArn": config.REKOGNITION_ROLE_ARN,
        },
        MinConfidence=min_confidence,
    )
    job_id = response["JobId"]

    for _ in range(config.MAX_POLL_ATTEMPTS):
        resp = rek.get_label_detection(JobId=job_id, SortBy="TIMESTAMP")
        if resp["JobStatus"] == "SUCCEEDED":
            break
        elif resp["JobStatus"] == "FAILED":
            return f"ERROR: Label detection failed: {resp.get('StatusMessage', 'Unknown')}"
        time.sleep(config.POLL_INTERVAL_SECONDS)
    else:
        return "ERROR: Label detection timed out after 30 minutes"

    all_labels = list(resp.get("Labels", []))
    next_token = resp.get("NextToken")
    while next_token:
        resp = rek.get_label_detection(JobId=job_id, SortBy="TIMESTAMP", NextToken=next_token)
        all_labels.extend(resp.get("Labels", []))
        next_token = resp.get("NextToken")

    label_counts = {}
    for item in all_labels:
        name = item.get("Label", {}).get("Name", "")
        if name:
            label_counts[name] = label_counts.get(name, 0) + 1

    top = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    get_detection_cache()["labels"] = all_labels
    get_detection_cache()["job_id"] = job_id

    lines = [f"Detected {len(all_labels)} label instances across {len(label_counts)} unique labels."]
    lines.append(f"Top labels: {', '.join(f'{n} ({c}x)' for n, c in top)}")
    lines.append("Use search_detections to find specific objects, or list_all_labels for full inventory.")
    return "\n".join(lines)


@tool
def detect_faces(s3_key: str) -> str:
    """Run Amazon Rekognition face detection on a video in S3.

    Use when the user asks about faces, expressions, age, gender, or emotions.

    Args:
        s3_key: S3 key of the video (from upload_video_to_s3). May also be a
            full s3:// URI. Must be within the calling user's own prefix.

    Returns:
        Summary of detected faces.
    """
    # T-10: refuse before doing anything, including serving cached results.
    try:
        s3_key = resolve_owned_s3_key(s3_key)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    # Check if faces are already cached (avoid double-processing)
    if "faces" in get_detection_cache():
        all_faces = get_detection_cache()["faces"]
        if not all_faces:
            return "[CACHED] No faces detected in the video."
        return f"[CACHED] Detected {len(all_faces)} face appearances. Use search_detections with target='face' for timestamps and attributes."

    session = _get_session()
    rek = session.client("rekognition")

    response = rek.start_face_detection(
        Video={"S3Object": {"Bucket": config.S3_BUCKET, "Name": s3_key}},
        NotificationChannel={
            "SNSTopicArn": config.SNS_TOPIC_ARN,
            "RoleArn": config.REKOGNITION_ROLE_ARN,
        },
        FaceAttributes="ALL",
    )
    job_id = response["JobId"]

    for _ in range(config.MAX_POLL_ATTEMPTS):
        resp = rek.get_face_detection(JobId=job_id)
        if resp["JobStatus"] == "SUCCEEDED":
            break
        elif resp["JobStatus"] == "FAILED":
            return f"ERROR: Face detection failed: {resp.get('StatusMessage', 'Unknown')}"
        time.sleep(config.POLL_INTERVAL_SECONDS)
    else:
        return "ERROR: Face detection timed out after 30 minutes"

    all_faces = list(resp.get("Faces", []))
    next_token = resp.get("NextToken")
    while next_token:
        resp = rek.get_face_detection(JobId=job_id, NextToken=next_token)
        all_faces.extend(resp.get("Faces", []))
        next_token = resp.get("NextToken")

    get_detection_cache()["faces"] = all_faces
    get_detection_cache()["job_id"] = job_id

    if not all_faces:
        return "No faces detected in the video."
    return f"Detected {len(all_faces)} face appearances. Use search_detections with target='face' for timestamps and attributes."


@tool
def search_detections(target: str, min_confidence: float = 50.0, max_results: int = 50) -> str:
    """Search cached detection results for a specific object or face.

    Call AFTER detect_labels or detect_faces. Searches cached results
    without re-running Rekognition. Can be called multiple times with
    different targets.

    Args:
        target: Object to search for (e.g., 'car', 'dog', 'face').
        min_confidence: Minimum confidence threshold (0-100). Default 50.
        max_results: Maximum results to return. Default 50.

    Returns:
        Matching timestamps with confidence scores.
    """
    target_lower = target.lower()
    is_face = target_lower in ("face", "faces", "person face")

    if is_face and "faces" in get_detection_cache():
        all_faces = get_detection_cache()["faces"]
        matches = []
        for item in all_faces:
            face = item.get("Face", {})
            conf = face.get("Confidence", 0)
            ts_ms = item.get("Timestamp", 0)
            if conf >= min_confidence:
                age = face.get("AgeRange", {})
                gender = face.get("Gender", {})
                emotions = face.get("Emotions", [])
                top_emotion = max(emotions, key=lambda e: e.get("Confidence", 0)) if emotions else {}
                matches.append(
                    f"{_format_timestamp(ts_ms)} | Face | {conf:.1f}% | "
                    f"{gender.get('Value', '?')}, age {age.get('Low', '?')}-{age.get('High', '?')}, "
                    f"{top_emotion.get('Type', '?')}"
                )
        if not matches:
            return f"No faces found with confidence >= {min_confidence}%."
        total = len(matches)
        matches = matches[:max_results]
        return f"Found {total} face detections (showing {len(matches)}):\nTimestamp | Label | Confidence | Attributes\n" + "\n".join(matches)

    elif "labels" in get_detection_cache():
        all_labels = get_detection_cache()["labels"]
        matches = []
        seen_ts = set()
        for item in all_labels:
            label = item.get("Label", {})
            name = label.get("Name", "").lower()
            conf = label.get("Confidence", 0)
            ts_ms = item.get("Timestamp", 0)

            name_match = target_lower in name or name in target_lower
            parent_match = any(
                target_lower in p.get("Name", "").lower()
                for p in label.get("Parents", [])
            )

            if (name_match or parent_match) and conf >= min_confidence and ts_ms not in seen_ts:
                seen_ts.add(ts_ms)
                matches.append(
                    f"{_format_timestamp(ts_ms)} | {label.get('Name', '')} | {conf:.1f}%"
                )

        matches.sort()
        if not matches:
            return f"No '{target}' found with confidence >= {min_confidence}%. Try lowering the confidence threshold."
        total = len(matches)
        matches = matches[:max_results]
        return f"Found {total} timestamps with '{target}' (showing {len(matches)}):\nTimestamp | Label | Confidence\n" + "\n".join(matches)

    else:
        return "No detection results cached. Run detect_labels or detect_faces first."


@tool
def list_all_labels(min_count: int = 3) -> str:
    """List all unique labels detected in the video, sorted by frequency.

    Call AFTER detect_labels. Shows everything Rekognition found.

    Args:
        min_count: Only show labels appearing at least this many times. Default 3.

    Returns:
        All detected labels with occurrence counts.
    """
    if "labels" not in get_detection_cache():
        return "No label detection results cached. Run detect_labels first."

    label_counts = {}
    for item in get_detection_cache()["labels"]:
        name = item.get("Label", {}).get("Name", "")
        if name:
            label_counts[name] = label_counts.get(name, 0) + 1

    filtered = [(n, c) for n, c in label_counts.items() if c >= min_count]
    filtered.sort(key=lambda x: x[1], reverse=True)

    if not filtered:
        return f"No labels found with {min_count}+ occurrences."

    lines = [f"All detected labels ({len(filtered)} with {min_count}+ occurrences):"]
    for name, count in filtered:
        lines.append(f"  {name}: {count}x")
    return "\n".join(lines)


@tool
def get_video_file_info(file_path: str) -> str:
    """Check if a local video file exists and get its size.

    Args:
        file_path: Path to the local video file.

    Returns:
        File info or error if not found.
    """
    if not os.path.exists(file_path):
        return f"ERROR: File not found: {file_path}"

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    ext = os.path.splitext(file_path)[1]
    return f"File: {file_path}\nSize: {size_mb:.1f} MB\nFormat: {ext}\nReady for upload."


# ── Reference Image Analysis & Face Search Tools ──────────────────────────

FACE_COLLECTION_ID = "video-analytic-agent-faces"


@tool
def analyze_reference_image(image_path: str) -> str:
    """Analyze a reference image to identify what objects and faces it contains.

    Use this when the user uploads a photo and wants to find those objects/people
    in a video. Returns all detected labels and face details from the image.

    Args:
        image_path: Local path to the reference image (jpg, png).

    Returns:
        All detected labels and faces in the image.
    """
    if not os.path.exists(image_path):
        return f"ERROR: Image not found: {image_path}"

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    session = _get_session()
    rek = session.client("rekognition")

    # Detect labels (objects, scenes)
    label_resp = rek.detect_labels(
        Image={"Bytes": image_bytes},
        MinConfidence=50.0,
        MaxLabels=30,
    )
    labels = label_resp.get("Labels", [])

    # Detect faces
    face_resp = rek.detect_faces(
        Image={"Bytes": image_bytes},
        Attributes=["ALL"],
    )
    faces = face_resp.get("FaceDetails", [])

    # Cache for cross-referencing
    get_detection_cache()["ref_image_labels"] = labels
    get_detection_cache()["ref_image_faces"] = faces
    get_detection_cache()["ref_image_path"] = image_path

    # Build summary
    lines = [f"Reference image: {image_path}"]
    lines.append(f"\nDetected {len(labels)} labels:")
    for lbl in sorted(labels, key=lambda x: x["Confidence"], reverse=True):
        parents = ", ".join(p["Name"] for p in lbl.get("Parents", []))
        parent_str = f" (category: {parents})" if parents else ""
        lines.append(f"  {lbl['Name']}: {lbl['Confidence']:.1f}%{parent_str}")

    if faces:
        lines.append(f"\nDetected {len(faces)} face(s):")
        for i, face in enumerate(faces, 1):
            age = face.get("AgeRange", {})
            gender = face.get("Gender", {})
            emotions = face.get("Emotions", [])
            top_emotion = max(emotions, key=lambda e: e.get("Confidence", 0)) if emotions else {}
            lines.append(
                f"  Face {i}: {gender.get('Value', '?')}, "
                f"age {age.get('Low', '?')}-{age.get('High', '?')}, "
                f"emotion: {top_emotion.get('Type', '?')} ({top_emotion.get('Confidence', 0):.0f}%)"
            )
    else:
        lines.append("\nNo faces detected in the image.")

    return "\n".join(lines)


@tool
def index_face_for_search(image_path: str) -> str:
    """Index faces from a reference image into a Rekognition Collection for video face search.

    Call this AFTER analyze_reference_image if faces were detected and the user
    wants to find that person in a video. Creates a face collection and indexes
    the face(s) from the image.

    Args:
        image_path: Local path to the reference image containing the face(s).

    Returns:
        Number of faces indexed and their IDs.
    """
    if not os.path.exists(image_path):
        return f"ERROR: Image not found: {image_path}"

    session = _get_session()
    rek = session.client("rekognition")

    # Create collection (ignore if exists)
    try:
        rek.create_collection(CollectionId=FACE_COLLECTION_ID)
    except rek.exceptions.ResourceAlreadyExistsException:
        # Delete and recreate to ensure clean state for new search
        rek.delete_collection(CollectionId=FACE_COLLECTION_ID)
        rek.create_collection(CollectionId=FACE_COLLECTION_ID)

    # Index faces from the image
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    response = rek.index_faces(
        CollectionId=FACE_COLLECTION_ID,
        Image={"Bytes": image_bytes},
        DetectionAttributes=["ALL"],
        MaxFaces=10,
        QualityFilter="AUTO",
    )

    indexed = response.get("FaceRecords", [])
    if not indexed:
        return "No faces could be indexed from this image. The image may not contain clear enough faces for indexing."

    get_detection_cache()["indexed_face_ids"] = [r["Face"]["FaceId"] for r in indexed]

    lines = [f"Indexed {len(indexed)} face(s) into search collection:"]
    for i, record in enumerate(indexed, 1):
        face = record["Face"]
        detail = record.get("FaceDetail", {})
        age = detail.get("AgeRange", {})
        gender = detail.get("Gender", {})
        lines.append(
            f"  Face {i}: ID={face['FaceId'][:12]}... "
            f"{gender.get('Value', '?')}, age {age.get('Low', '?')}-{age.get('High', '?')}, "
            f"confidence {face.get('Confidence', 0):.1f}%"
        )
    lines.append("\nReady for face search in video. Use search_faces_in_video next.")
    return "\n".join(lines)


@tool
def search_faces_in_video(s3_key: str, min_confidence: float = 80.0) -> str:
    """Search a video for faces that match the indexed reference face(s).

    Call this AFTER index_face_for_search and upload_video_to_s3.
    Uses Rekognition StartFaceSearch to find timestamps where the
    reference person appears in the video.

    Args:
        s3_key: S3 key of the video (from upload_video_to_s3).
        min_confidence: Minimum match confidence (0-100). Default 80.

    Returns:
        Timestamps where the reference face appears with match confidence.
    """
    # T-10: verify the caller owns this key before starting a Rekognition job.
    try:
        s3_key = resolve_owned_s3_key(s3_key)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    session = _get_session()
    rek = session.client("rekognition")

    # Start face search job
    response = rek.start_face_search(
        Video={"S3Object": {"Bucket": config.S3_BUCKET, "Name": s3_key}},
        CollectionId=FACE_COLLECTION_ID,
        NotificationChannel={
            "SNSTopicArn": config.SNS_TOPIC_ARN,
            "RoleArn": config.REKOGNITION_ROLE_ARN,
        },
        FaceMatchThreshold=min_confidence,
    )
    job_id = response["JobId"]

    # Poll for completion
    for _ in range(config.MAX_POLL_ATTEMPTS):
        resp = rek.get_face_search(JobId=job_id, SortBy="TIMESTAMP")
        if resp["JobStatus"] == "SUCCEEDED":
            break
        elif resp["JobStatus"] == "FAILED":
            return f"ERROR: Face search failed: {resp.get('StatusMessage', 'Unknown')}"
        time.sleep(config.POLL_INTERVAL_SECONDS)
    else:
        return "ERROR: Face search timed out after 30 minutes"

    # Collect all pages
    all_persons = list(resp.get("Persons", []))
    next_token = resp.get("NextToken")
    while next_token:
        resp = rek.get_face_search(JobId=job_id, SortBy="TIMESTAMP", NextToken=next_token)
        all_persons.extend(resp.get("Persons", []))
        next_token = resp.get("NextToken")

    # Filter for actual matches
    matches = []
    for person in all_persons:
        face_matches = person.get("FaceMatches", [])
        if face_matches:
            ts_ms = person.get("Timestamp", 0)
            best_match = max(face_matches, key=lambda m: m.get("Similarity", 0))
            similarity = best_match.get("Similarity", 0)
            face_id = best_match.get("Face", {}).get("FaceId", "?")
            matches.append({
                "timestamp_ms": ts_ms,
                "timestamp": _format_timestamp(ts_ms),
                "similarity": round(similarity, 1),
                "face_id": face_id[:12],
            })

    if not matches:
        return f"The reference person was NOT found in the video (threshold: {min_confidence}%). Try lowering min_confidence."

    # Deduplicate close timestamps (within 500ms)
    deduped = [matches[0]]
    for m in matches[1:]:
        if m["timestamp_ms"] - deduped[-1]["timestamp_ms"] > 500:
            deduped.append(m)

    lines = [f"Found the reference person at {len(deduped)} timestamps in the video:"]
    lines.append("Timestamp | Similarity")
    for m in deduped[:100]:
        lines.append(f"  {m['timestamp']} | {m['similarity']}%")

    if len(deduped) > 100:
        lines.append(f"  ... and {len(deduped) - 100} more")

    # Time range summary
    first = deduped[0]["timestamp"]
    last = deduped[-1]["timestamp"]
    span_s = (deduped[-1]["timestamp_ms"] - deduped[0]["timestamp_ms"]) / 1000
    lines.append(f"\nAppears from {first} to {last} (span: {span_s:.1f}s)")

    return "\n".join(lines)


@tool
def match_image_objects_in_video(min_confidence: float = 50.0) -> str:
    """Cross-reference objects from the reference image with video detection results.

    Call this AFTER both analyze_reference_image and detect_labels have been run.
    Compares labels found in the reference image against labels detected in the
    video, and returns timestamps where matching objects appear.

    This works for non-face objects (cars, dogs, buildings, instruments, etc.).

    Args:
        min_confidence: Minimum confidence for matches. Default 50.

    Returns:
        For each matching object: timestamps where it appears in the video.
    """
    if "ref_image_labels" not in get_detection_cache():
        return "No reference image analyzed. Run analyze_reference_image first."
    if "labels" not in get_detection_cache():
        return "No video labels detected. Run detect_labels first."

    ref_labels = get_detection_cache()["ref_image_labels"]
    video_labels = get_detection_cache()["labels"]

    # Get reference label names (above threshold)
    ref_names = set()
    for lbl in ref_labels:
        if lbl["Confidence"] >= min_confidence:
            ref_names.add(lbl["Name"].lower())

    # Search video labels for matches
    results = {}
    for item in video_labels:
        label = item.get("Label", {})
        name = label.get("Name", "")
        conf = label.get("Confidence", 0)
        ts_ms = item.get("Timestamp", 0)

        if name.lower() in ref_names and conf >= min_confidence:
            if name not in results:
                results[name] = []
            results[name].append({
                "timestamp": _format_timestamp(ts_ms),
                "confidence": round(conf, 1),
            })

    if not results:
        ref_list = ", ".join(sorted(ref_names))
        return f"None of the reference image objects ({ref_list}) were found in the video above {min_confidence}% confidence."

    lines = [f"Found {len(results)} matching object(s) from the reference image in the video:\n"]
    for name, timestamps in sorted(results.items()):
        lines.append(f"  {name}: {len(timestamps)} appearances")
        # Show first 10 timestamps
        for t in timestamps[:10]:
            lines.append(f"    {t['timestamp']} ({t['confidence']}%)")
        if len(timestamps) > 10:
            lines.append(f"    ... and {len(timestamps) - 10} more")
        lines.append("")

    return "\n".join(lines)


@tool
def cleanup_face_collection() -> str:
    """Delete the face search collection to clean up resources.

    Call this when done with face search to avoid leaving indexed faces
    in the Rekognition collection.

    Returns:
        Confirmation of deletion.
    """
    session = _get_session()
    rek = session.client("rekognition")
    try:
        rek.delete_collection(CollectionId=FACE_COLLECTION_ID)
        get_detection_cache().pop("indexed_face_ids", None)
        return f"Face collection '{FACE_COLLECTION_ID}' deleted."
    except Exception as e:
        return f"Could not delete collection: {e}"


# ── Audio Transcription & Translation Tools ───────────────────────────────

import json as _json


@tool
def upload_audio_to_s3(audio_path: str) -> str:
    """Upload an audio file to S3 for Transcribe processing.

    Args:
        audio_path: Path to the local audio file (mp3, m4a, wav, etc.).

    Returns:
        S3 URI for the uploaded audio.
    """
    if not os.path.exists(audio_path):
        return f"ERROR: Audio file not found: {audio_path}"

    session = _get_session()
    s3 = session.client("s3")
    ext = os.path.splitext(audio_path)[1]
    s3_key = f"{config.get_user_prefix()}/audio/{uuid.uuid4().hex}{ext}"
    s3.upload_file(
        audio_path,
        config.S3_BUCKET,
        s3_key,
        ExtraArgs={"ExpectedBucketOwner": config.get_account_id()},
    )
    s3_uri = f"s3://{config.S3_BUCKET}/{s3_key}"
    size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    return f"Uploaded to {s3_uri} ({size_mb:.1f} MB)"


@tool
def transcribe_audio(s3_uri: str, force: bool = False) -> str:
    """Transcribe audio or video using Amazon Transcribe with automatic language detection.

    Starts an async transcription job, polls until complete, and returns
    the full transcript with detected language. Supports 100+ languages
    including English, Chinese, Japanese, Korean, Spanish, etc.

    Amazon Transcribe accepts both audio files (mp3, wav, m4a) and video
    files (mp4) directly — no need to extract audio first for mp4 files.

    Args:
        s3_uri: S3 URI of the audio or video file
            (e.g., s3://amzn-s3-demo-videos/videos/file.mp4).
            A bare key is also accepted. Must be within the calling user's prefix.
        force: If True, skip cache and always re-run. Set this only when the user
            has explicitly asked to regenerate.

    Returns:
        Full transcript text with detected language and timestamps,
        OR a cache marker prompting the user for re-analysis options.
    """
    # T-10: validate and canonicalise before anything else. Accepting a bare key
    # here means the agent no longer has to know the bucket name to call this.
    try:
        s3_uri = owned_s3_uri(s3_uri)
    except S3AccessDenied as e:
        return f"ERROR: {e}"

    # Cache short-circuit — surface options to the user instead of silent reuse
    if not force and "transcript_text" in get_detection_cache() and get_detection_cache()["transcript_text"]:
        transcript = get_detection_cache()["transcript_text"]
        source = get_detection_cache().get("transcript_source", "previous analysis")
        language = get_detection_cache().get("transcript_language", "auto-detected")
        word_count = len(transcript.split())

        return (
            "**[CACHED_TRANSCRIPT_AVAILABLE]**\n\n"
            f"A transcript is already available from {source} ({language}, ~{word_count} words). "
            "Before regenerating, ask the user to choose:\n\n"
            "1. **Use the cached transcript** — instant, no extra cost (recommended)\n"
            "2. **Regenerate the transcript** — re-run Amazon Transcribe (slow, costs API calls)\n"
            "3. **Show / search / summarize the existing transcript**\n\n"
            f"**Transcript preview:** {transcript[:300]}...\n\n"
            "Do NOT call transcribe_audio again until the user picks option 2 explicitly."
        )

    session = _get_session()
    transcribe = session.client("transcribe")

    job_name = f"video-agent-{uuid.uuid4().hex[:12]}"

    # Determine media format from URI
    ext = s3_uri.rsplit(".", 1)[-1].lower()
    format_map = {"mp3": "mp3", "mp4": "mp4", "m4a": "mp4", "wav": "wav", "flac": "flac", "ogg": "ogg", "webm": "webm"}
    media_format = format_map.get(ext, "mp3")

    transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        Media={"MediaFileUri": s3_uri},
        MediaFormat=media_format,
        IdentifyLanguage=True,
        LanguageOptions=["en-US", "zh-CN", "zh-TW", "ja-JP", "ko-KR", "es-US", "fr-FR", "de-DE", "pt-BR", "it-IT"],
        Settings={
            "ShowSpeakerLabels": True,
            "MaxSpeakerLabels": 10,
        },
    )

    # Poll for completion
    for _ in range(config.MAX_POLL_ATTEMPTS):
        resp = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
        if status == "COMPLETED":
            break
        elif status == "FAILED":
            reason = resp["TranscriptionJob"].get("FailureReason", "Unknown")
            return f"ERROR: Transcription failed: {reason}"
        time.sleep(config.POLL_INTERVAL_SECONDS)
    else:
        return "ERROR: Transcription timed out."

    # Get the transcript from the output URI
    transcript_uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
    detected_lang = resp["TranscriptionJob"].get("LanguageCode", "unknown")

    # Download transcript JSON from the URI. AWS Transcribe always returns an
    # https:// presigned URL, but validate the scheme explicitly to defend
    # against a compromised/mis-configured response returning file:// or a
    # custom scheme (bandit B310).
    import urllib.request
    from urllib.parse import urlparse
    _scheme = urlparse(transcript_uri).scheme
    if _scheme != "https":
        return f"ERROR: Refusing to fetch transcript from non-HTTPS URI (scheme={_scheme!r})"
    # Scheme is validated to be 'https' immediately above; URL is a presigned
    # AWS Transcribe response URI.
    with urllib.request.urlopen(transcript_uri) as response:  # nosec B310
        transcript_data = _json.loads(response.read().decode("utf-8"))

    # Extract full transcript text
    results = transcript_data.get("results", {})
    transcripts = results.get("transcripts", [])
    full_text = transcripts[0]["transcript"] if transcripts else ""

    # Extract timestamped segments
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

            # Break into lines roughly every 10 words
            if len(current_line) >= 10:
                ts = _format_timestamp(int(current_start * 1000))
                segments.append(f"[{ts}] {' '.join(current_line)}")
                current_line = []
                current_start = None
        elif item["type"] == "punctuation":
            if current_line:
                current_line[-1] += item["alternatives"][0]["content"]

    # Flush remaining
    if current_line and current_start is not None:
        ts = _format_timestamp(int(current_start * 1000))
        segments.append(f"[{ts}] {' '.join(current_line)}")

    # Cache transcript for summarization
    get_detection_cache()["transcript_text"] = full_text
    get_detection_cache()["transcript_language"] = detected_lang
    get_detection_cache()["transcript_segments"] = segments

    # Build output
    lines = [f"Language detected: {detected_lang}"]
    lines.append(f"Transcript length: {len(full_text)} characters")
    lines.append(f"\n--- Timestamped Transcript ---\n")
    lines.extend(segments)

    # Clean up the job
    try:
        transcribe.delete_transcription_job(TranscriptionJobName=job_name)
    except Exception:
        pass

    return "\n".join(lines)


@tool
def get_transcript_text() -> str:
    """Get the full transcript text from the last transcription.

    Call AFTER transcribe_audio. Returns the raw transcript text
    for summarization or translation by the agent.

    Returns:
        Full transcript text and detected language.
    """
    if "transcript_text" not in get_detection_cache():
        return "No transcript available. Run transcribe_audio first."

    text = get_detection_cache()["transcript_text"]
    lang = get_detection_cache()["transcript_language"]

    return f"Language: {lang}\n\nTranscript:\n{text}"


@tool
def get_cached_analysis() -> str:
    """Retrieve all cached analysis results for the most recently analyzed file.

    Call this when the user chooses to REUSE a previously cached analysis (i.e.,
    after they answered "yes, use the cached result" to a [CACHED_*_AVAILABLE] prompt).
    Returns the full cached findings: video summary, transcript, chapters, labels.

    Returns:
        All cached analysis results, formatted for direct presentation to the user.
    """
    parts = []

    # BDA video summary
    summary = get_detection_cache().get("bda_video_summary", "")
    if summary:
        parts.append(f"## Video Summary\n\n{summary}")

    # BDA chapters
    chapters = get_detection_cache().get("bda_chapters", [])
    if chapters:
        parts.append("## Chapters\n\n" + "\n".join(f"- {ch}" for ch in chapters))

    # Transcript
    transcript = get_detection_cache().get("transcript_text", "")
    if transcript:
        lang = get_detection_cache().get("transcript_language", "auto-detected")
        source = get_detection_cache().get("transcript_source", "previous analysis")
        parts.append(f"## Transcript ({lang}, source: {source})\n\n{transcript}")

    # Rekognition labels
    labels = get_detection_cache().get("labels", [])
    if labels:
        parts.append(f"## Detected Objects/Scenes ({len(labels)} unique labels)\n\n"
                     + ", ".join(sorted(set(l.get("Name", "") for l in labels[:50] if isinstance(l, dict)))))

    if not parts:
        return "No cached analysis available. Run an analysis tool first."

    return "\n\n".join(parts)


# ── Data Cleanup Tools ────────────────────────────────────────────────────

@tool
def cleanup_local_files(file_paths: str) -> str:
    """Delete local temporary files (downloaded videos, extracted audio).

    Use this when the user confirms they want to clean up local files
    after analysis is complete. Accepts comma-separated paths.

    Args:
        file_paths: Comma-separated list of local file paths to delete.

    Returns:
        Summary of deleted files.
    """
    paths = [p.strip() for p in file_paths.split(",") if p.strip()]
    results = []
    for path in paths:
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            os.remove(path)
            results.append(f"Deleted: {path} ({size_mb:.1f} MB)")
        else:
            results.append(f"Not found (already removed): {path}")
    return "\n".join(results) if results else "No files to delete."


@tool
def cleanup_s3_objects(s3_keys: str) -> str:
    """Delete objects from S3 (uploaded videos, audio files).

    Use this when the user confirms they want to remove uploaded files
    from S3 after analysis is complete. Accepts comma-separated S3 keys.

    Args:
        s3_keys: Comma-separated list of S3 keys to delete. Every key must be
            within the calling user's own prefix; others are refused.

    Returns:
        Summary of deleted S3 objects.
    """
    raw_keys = [k.strip() for k in s3_keys.split(",") if k.strip()]

    # T-10: this tool DELETES, so validate every key up front and abort the
    # whole call if any one of them is out of bounds. Partial deletion driven by
    # a partly-valid argument list would be worse than refusing outright.
    #
    # Note this needs no prompt injection to matter: "clean up s3 keys
    # alice/videos/x.mp4" is a well-formed request containing no attack pattern,
    # and before this check the task role would have carried it out.
    #
    # Validation runs before the client is constructed so a refused request
    # performs no AWS setup at all.
    keys = []
    for raw in raw_keys:
        try:
            keys.append(resolve_owned_s3_key(raw))
        except S3AccessDenied as e:
            return f"ERROR: refusing the entire cleanup request. {e}"

    if not keys:
        return "No S3 objects to delete."

    session = _get_session()
    s3 = session.client("s3")

    results = []
    for key in keys:
        try:
            s3.delete_object(
                Bucket=config.S3_BUCKET,
                Key=key,
                ExpectedBucketOwner=config.get_account_id(),
            )
            results.append(f"Deleted: s3://{config.S3_BUCKET}/{key}")
        except Exception as e:
            results.append(f"Failed to delete {key}: {e}")
    return "\n".join(results) if results else "No S3 objects to delete."


# ── Document Reading Tools ─────────────────────────────────────────────────

@tool
def read_document(file_path: str) -> str:
    """Read and extract text content from a document file.

    Supports PDF, Word (docx), Excel (xlsx/csv), and plain text files.
    Use this when the user uploads a document and asks about its contents.

    Args:
        file_path: Path to the document file. Must be inside the upload
            staging area; paths elsewhere on the filesystem are refused.

    Returns:
        Extracted text content from the document.
    """
    # T-10: this tool previously read any path the model supplied, which made
    # the container filesystem readable through the agent — /proc/self/environ,
    # /app source, and so on. Uploads are all staged under the system temp
    # directory (chat_app.py uses tempfile.mkdtemp() per upload batch), so
    # confining reads to that tree costs no functionality.
    #
    # realpath on both sides resolves symlinks before comparison, which also
    # closes traversal attempts like /tmp/../etc/passwd and handles platforms
    # where the temp dir is itself a symlink (macOS /var -> /private/var).
    try:
        resolved = os.path.realpath(file_path)
        staging_root = os.path.realpath(tempfile.gettempdir())
        if not (resolved == staging_root or resolved.startswith(staging_root + os.sep)):
            return (
                f"ERROR: refusing to read '{file_path}': outside the upload staging "
                f"area. Only files uploaded in this session can be read."
            )
    except Exception as e:
        return f"ERROR: could not validate path '{file_path}': {e}"

    if not os.path.exists(resolved):
        return f"ERROR: File not found: {file_path}"

    file_path = resolved
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext == ".pdf":
            try:
                import pymupdf
                doc = pymupdf.open(file_path)
                pages = []
                for i, page in enumerate(doc):
                    text = page.get_text()
                    if text.strip():
                        pages.append(f"--- Page {i+1} ---\n{text}")
                doc.close()
                if pages:
                    return f"PDF: {file_path} ({len(pages)} pages)\n\n" + "\n\n".join(pages)
                return "PDF file contains no extractable text (may be scanned/image-based)."
            except ImportError:
                # pymupdf is the primary and only PDF backend. PyPDF2 was
                # previously a fallback here but is deprecated upstream
                # (last release 3.0.1, March 2023) with unpatched CVEs
                # (GHSA-4vvm-4w3v-6mr8) and no future fixes planned. If
                # pymupdf ever becomes unavailable, this returns a
                # clear install hint rather than silently degrading.
                return "ERROR: No PDF library installed. Run: pip install pymupdf"

        elif ext == ".svg":
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                svg_content = f.read()
            # Extract text elements from SVG
            import re
            texts = re.findall(r'<text[^>]*>(.*?)</text>', svg_content, re.DOTALL)
            text_content = "\n".join(t.strip() for t in texts if t.strip())
            if text_content:
                return f"SVG file: {file_path}\n\nExtracted text elements:\n{text_content}\n\n(SVG is a vector image — text elements extracted. For visual analysis, convert to PNG first.)"
            return f"SVG file: {file_path} ({len(svg_content)} chars)\n\nNo text elements found. This is a vector graphic — for visual analysis, convert to PNG first."

        elif ext in (".docx", ".doc"):
            from docx import Document as DocxDoc
            doc = DocxDoc(file_path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            tables_text = []
            for table in doc.tables:
                rows = []
                for row in table.rows:
                    rows.append(" | ".join(cell.text.strip() for cell in row.cells))
                tables_text.append("\n".join(rows))
            content = "\n".join(paragraphs)
            if tables_text:
                content += "\n\n--- Tables ---\n" + "\n\n".join(tables_text)
            return f"Word document: {file_path}\n\n{content}"

        elif ext in (".xlsx", ".xls"):
            import pandas as pd
            xls = pd.ExcelFile(file_path)
            sheets = []
            for sheet_name in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet_name)
                sheets.append(f"--- Sheet: {sheet_name} ({len(df)} rows) ---\n{df.to_string(max_rows=50)}")
            return f"Excel file: {file_path}\n\n" + "\n\n".join(sheets)

        elif ext == ".csv":
            import pandas as pd
            df = pd.read_csv(file_path, on_bad_lines="skip")
            return f"CSV file: {file_path} ({len(df)} rows)\n\n{df.to_string(max_rows=50)}"

        elif ext in (".txt", ".md", ".log", ".json"):
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return f"Text file: {file_path} ({len(content)} chars)\n\n{content[:10000]}"

        else:
            return f"Unsupported file type: {ext}. Supported: pdf, docx, xlsx, csv, txt, md, json"

    except Exception as e:
        return f"ERROR reading {file_path}: {e}"
