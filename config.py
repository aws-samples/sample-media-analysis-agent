"""Configuration for ProServe Discovery Agent.

Reads from environment variables or an optional local .env file.
ECS deployment injects required values through CloudFormation.
"""

import os
import sys
import tempfile

# Platform-portable temp directory (respects $TMPDIR, falls back to /tmp on
# Linux). Referenced by other modules to avoid hardcoded "/tmp" paths.
TMP_DIR = tempfile.gettempdir()

# ── Load .env file if it exists ───────────────────────────────────────────

_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Only set if not already in environment (env vars take precedence)
                if key and key not in os.environ:
                    os.environ[key] = value

# ── AWS Authentication ────────────────────────────────────────────────────
# Option 1: SSO Profile
# Option 2: Static credentials (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + optional AWS_SESSION_TOKEN)

AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")

# ── Infrastructure (from CloudFormation stack outputs) ────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
REKOGNITION_ROLE_ARN = os.environ.get("REKOGNITION_ROLE_ARN", "")

# ── Per-user upload staging quota (T-07) ──────────────────────────────────
#
# The container's ephemeral disk is shared by every Streamlit session running
# in the task, and each session's state lives in that task's memory. Without a
# per-user ceiling, one user filling the disk crashes the task and destroys
# every other user's in-flight analysis — transcripts, detection caches and
# conversation history alike.
#
# A quota converts that shared-fate failure into a self-limiting one: a user
# can exhaust their own allowance and nobody else's. That is the security
# property; total disk sizing is a separate capacity decision (see
# EphemeralStorage in deploy/ecs-fargate-stack.yaml).
#
# Keyed per USER, not per session, so opening several browser tabs shares one
# allowance instead of multiplying it.
#
# 15 GiB accommodates two of the largest supported videos plus documents,
# and permits 13 concurrent users on a 200 GiB task
# (the Fargate maximum) after reserving room for the image.
UPLOAD_QUOTA_GB = float(os.environ.get("UPLOAD_QUOTA_GB", "15"))
UPLOAD_QUOTA_BYTES = int(UPLOAD_QUOTA_GB * 1024 * 1024 * 1024)

# Validate required infrastructure config
_missing = []
if not S3_BUCKET:
    _missing.append("S3_BUCKET")
if not SNS_TOPIC_ARN:
    _missing.append("SNS_TOPIC_ARN")
if not REKOGNITION_ROLE_ARN:
    _missing.append("REKOGNITION_ROLE_ARN")

if _missing:
    print(
        f"ERROR: Missing required configuration: {', '.join(_missing)}\n"
        f"Set these environment variables before starting the agent.",
        file=sys.stderr,
    )

# ── Rekognition settings ─────────────────────────────────────────────────

MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "50.0"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
MAX_POLL_ATTEMPTS = int(os.environ.get("MAX_POLL_ATTEMPTS", "360"))

# ── Agent settings ────────────────────────────────────────────────────────

# Cross-region inference profile (the "us." prefix). Bedrock routes the
# request to one of several US regions, which has IAM consequences — see
# the TaskRole bedrock:InvokeModel statement in
# deploy/ecs-fargate-stack.yaml.
#
# Previously us.anthropic.claude-sonnet-4-20250514-v1:0. Anthropic marked
# that version LEGACY, and Bedrock rejects legacy models for accounts that
# have not used them within 30 days. The failure appears at first agent
# invocation, not at deploy time. Check modelLifecycle.status via
# `aws bedrock list-foundation-models --by-provider anthropic` when this
# breaks again, because eventually it will.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# ── Account ID resolution (lazy) ─────────────────────────────────────────

_resolved_account_id: str = ""


def get_account_id() -> str:
    """Return the AWS account ID, resolving via STS if not set in env."""
    global _resolved_account_id, AWS_ACCOUNT_ID
    if AWS_ACCOUNT_ID:
        return AWS_ACCOUNT_ID
    if _resolved_account_id:
        return _resolved_account_id
    try:
        import boto3

        session = (
            boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
            if AWS_PROFILE
            else boto3.Session(region_name=AWS_REGION)
        )
        sts = session.client("sts")
        _resolved_account_id = sts.get_caller_identity()["Account"]
        return _resolved_account_id
    except Exception:
        return ""


# ── User prefix for S3 multi-tenant isolation ─────────────────────────────


def get_user_prefix() -> str:
    """Return a user-specific S3 prefix derived from the authenticated user.

    If Cognito auth is active, uses the authenticated user's email prefix.
    Otherwise falls back to IAM identity resolution.

    NOTE: This must NOT be cached globally — each concurrent user has a
    different identity. Uses Streamlit session state for per-user caching.

    Examples:
      - Cognito: 'firstname.lastname' (from first.last@email.com)
      - SSO: 'first.last' (from first.last@company.com)
      - IAM role: 'AWSAdmin/first.last@company.com' → 'first.last'
      - IAM user: 'fl' → 'fl'
      - Fallback: 'default'
    """
    # Try per-session cache first (Streamlit context)
    try:
        import streamlit as st
        if "user_prefix" in st.session_state and st.session_state["user_prefix"]:
            return st.session_state["user_prefix"]
    except Exception:
        pass

    prefix = ""

    # Try Cognito auth first (if available)
    try:
        from auth import get_user_id as auth_get_user_id, is_authenticated
        if is_authenticated():
            prefix = auth_get_user_id()
    except (ImportError, RuntimeError):
        pass

    # Direct session state fallback (in case auth module import fails in tool context)
    if not prefix or prefix == "default":
        try:
            import streamlit as st
            user = st.session_state.get("user")
            if user and user.get("email") and user["email"] != "unknown":
                import re as re_mod
                email = user["email"]
                prefix = email.replace("@", "_at_")
                prefix = re_mod.sub(r"[^a-zA-Z0-9._-]", "_", prefix)
        except Exception:
            pass

    # Fall back to IAM identity if no Cognito user
    if not prefix or prefix == "default":
        try:
            import boto3, re

            session = (
                boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
                if AWS_PROFILE
                else boto3.Session(region_name=AWS_REGION)
            )
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            arn = identity.get("Arn", "")

            if ":assumed-role/" in arn:
                session_name = arn.split("/")[-1]
            elif ":user/" in arn:
                session_name = arn.split("/")[-1]
            else:
                session_name = identity.get("UserId", "default")

            if "@" in session_name:
                session_name = session_name.split("@")[0]

            import re as re_mod
            session_name = re_mod.sub(r"[^a-zA-Z0-9._-]", "_", session_name)
            prefix = session_name or "default"

        except Exception:
            prefix = "default"

    # Cache in session state for this user's session
    try:
        import streamlit as st
        st.session_state["user_prefix"] = prefix
    except Exception:
        pass

    return prefix
