"""Cognito authentication for Streamlit.

Provides login/logout functionality using Amazon Cognito User Pool.
Supports two modes:
  1. Direct API auth (USER_PASSWORD_AUTH) — works over HTTP, no Hosted UI redirect needed
  2. Hosted UI (OAuth code flow) — requires HTTPS callback URL

Mode is auto-selected: if APP_URL is HTTPS, uses Hosted UI. Otherwise, uses direct API auth.

Environment variables required (production / Cognito auth):
  COGNITO_USER_POOL_ID - Cognito User Pool ID
  COGNITO_CLIENT_ID - Cognito App Client ID
  COGNITO_DOMAIN - Cognito Hosted UI domain (optional for direct auth mode)
  APP_URL - The application URL (e.g., http://video-analytic-alb-xxx.us-east-1.elb.amazonaws.com)

Environment variables required (local development WITHOUT auth):
  ALLOW_INSECURE_LOCAL=true - Explicit opt-in to run without authentication.
  STREAMLIT_SERVER_ADDRESS=127.0.0.1 - Bind Streamlit to loopback only.

Any other combination (missing Cognito config without opt-in, partial Cognito config,
or ALLOW_INSECURE_LOCAL=true with a non-loopback bind) raises InsecureConfigurationError
at module import time and refuses to start. See T-01 in docs/threat-model.md.
"""

import os
import json
import time
import logging
import urllib.parse
from typing import Optional, Dict

import boto3
import streamlit as st

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "").strip()
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "").strip()
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "").strip()
APP_URL = os.environ.get("APP_URL", "http://localhost:8501")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ── T-01 mitigation: fail-closed authentication configuration ───────────────
#
# Previous behavior derived AUTH_ENABLED implicitly from `bool(pool_id AND
# client_id)`. If Cognito env vars were missing (e.g., empty CloudFormation
# parameter defaults), the app would silently start as fully-open. That is a
# fail-open design that propagates to every reader of this reference
# architecture. This block replaces it with a fail-closed one: authentication
# is on unless the operator explicitly opts out AND is binding to a loopback
# address. Anything else raises at import time.
#
# See docs/threat-model.md § T-01 for the full rationale.


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when the auth configuration is unsafe.

    Refuses to start rather than serving traffic under an ambiguous or
    obviously-insecure configuration.
    """


_ALLOW_INSECURE_LOCAL = (
    os.environ.get("ALLOW_INSECURE_LOCAL", "").strip().lower() == "true"
)

# T-06: hide the Sign Up tab in the UI when the Cognito user pool is
# admin-invite only (AllowAdminCreateUserOnly=true). Value MUST agree with
# the CFN AllowSelfSignup parameter on cognito-stack; any value other than
# "true" hides Sign Up and shows admin-invite guidance. Defaults to hidden.
_ALLOW_SELF_SIGNUP = (
    os.environ.get("ALLOW_SELF_SIGNUP", "").strip().lower() == "true"
)

_STREAMLIT_BIND = os.environ.get("STREAMLIT_SERVER_ADDRESS", "").strip().lower()
_LOOPBACK_ADDRESSES = {"127.0.0.1", "localhost", "::1"}
_BIND_IS_LOOPBACK = _STREAMLIT_BIND in _LOOPBACK_ADDRESSES
_COGNITO_CONFIGURED = bool(COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID)
_COGNITO_PARTIAL = (
    bool(COGNITO_USER_POOL_ID or COGNITO_CLIENT_ID) and not _COGNITO_CONFIGURED
)


def _assert_auth_configuration_valid() -> bool:
    """Decide whether authentication is enforced.

    Returns:
        True  — Cognito is fully configured; auth is enforced.
        False — Operator explicitly opted out AND bind is loopback; auth is
                disabled for local development only.

    Raises:
        InsecureConfigurationError — any unsafe or ambiguous configuration.
    """
    # Partial Cognito config is always a hard failure: the operator clearly
    # intended to enable auth, and letting the request through with a
    # half-wired pool would be worse than telling them explicitly to fix it.
    if _COGNITO_PARTIAL:
        raise InsecureConfigurationError(
            "Partial Cognito configuration: both COGNITO_USER_POOL_ID and "
            "COGNITO_CLIENT_ID must be set together (production/staging), or "
            "both left empty AND ALLOW_INSECURE_LOCAL=true "
            "AND STREAMLIT_SERVER_ADDRESS=127.0.0.1 (local development only)."
        )

    if _COGNITO_CONFIGURED:
        return True

    # No Cognito config. Auth can only be disabled with explicit opt-in.
    if not _ALLOW_INSECURE_LOCAL:
        raise InsecureConfigurationError(
            "Authentication is not configured and no explicit local-development "
            "opt-in was provided. Either set COGNITO_USER_POOL_ID and "
            "COGNITO_CLIENT_ID to enable Cognito auth, or set "
            "ALLOW_INSECURE_LOCAL=true AND STREAMLIT_SERVER_ADDRESS=127.0.0.1 "
            "to run without authentication for local development only."
        )

    # Opt-in was given. Verify the bind is loopback; refuse to run an
    # unauthenticated Streamlit reachable on 0.0.0.0 or any real interface.
    if not _BIND_IS_LOOPBACK:
        raise InsecureConfigurationError(
            "ALLOW_INSECURE_LOCAL=true requires STREAMLIT_SERVER_ADDRESS to be "
            f"a loopback address (127.0.0.1, localhost, or ::1). Current value: "
            f"{_STREAMLIT_BIND!r}. Refusing to run without authentication on a "
            "non-loopback bind."
        )

    logger.warning(
        "AUTH DISABLED — running in insecure local mode "
        "(ALLOW_INSECURE_LOCAL=true, STREAMLIT_SERVER_ADDRESS=%s). "
        "DO NOT USE IN PRODUCTION OR ON ANY SHARED NETWORK.",
        _STREAMLIT_BIND,
    )
    return False


AUTH_ENABLED = _assert_auth_configuration_valid()

# T-03: refresh access/ID tokens this many seconds before they expire.
# 120s gives a safety window for clock skew and mid-request expiration
# without triggering a refresh so early that we burn Cognito API calls.
# The refresh happens silently in is_authenticated(); users notice nothing
# unless the refresh itself fails (revoked user or 30d idle refresh token).
REFRESH_SAFETY_WINDOW = 120

# Use Hosted UI only if APP_URL is HTTPS (Cognito requirement)
USE_HOSTED_UI = AUTH_ENABLED and COGNITO_DOMAIN and APP_URL.startswith("https://")


def is_authenticated() -> bool:
    """Check if the current session has a valid authenticated user.

    T-03: enforces token expiry by silently refreshing access/ID tokens
    when they're within REFRESH_SAFETY_WINDOW of expiring. If refresh
    fails (revoked user, expired refresh token, network error), the
    session is cleared and False is returned. Caller then routes to
    show_login_page(), which surfaces the auth_error message set here.
    """
    if not AUTH_ENABLED:
        # Auth is disabled only after _assert_auth_configuration_valid()
        # confirmed ALLOW_INSECURE_LOCAL=true and STREAMLIT_SERVER_ADDRESS is
        # a loopback address (see T-01 mitigation above). Fabricate the
        # local-dev user so the rest of the app has a stable identity.
        if "user" not in st.session_state:
            st.session_state["user"] = {
                "email": "local-dev",
                "sub": "local-dev",
                "username": "local-dev",
            }
        return True

    user = st.session_state.get("user")
    if not user:
        return False

    # T-03: refresh if the ID token is close to (or past) expiration.
    # token_expiry is a Unix timestamp from the ID token's `exp` claim
    # (captured by _extract_user_from_auth_result at login time).
    token_expiry = user.get("token_expiry", 0)
    if token_expiry and time.time() >= (token_expiry - REFRESH_SAFETY_WINDOW):
        if not _refresh_tokens():
            # Refresh failed: user was probably revoked in Cognito, or
            # their refresh token expired (30d idle default). Clear the
            # session and surface a clean message on the login page.
            logger.info(
                "Session invalidated for %s (token refresh failed)",
                user.get("email", "<unknown>"),
            )
            st.session_state["user"] = None
            st.session_state["auth_error"] = (
                "Your session has expired. Please sign in again."
            )
            return False

    return True


def get_current_user() -> Optional[Dict]:
    """Return the current authenticated user info, or None."""
    return st.session_state.get("user")


def get_user_email() -> str:
    """Return the current user's email for S3 prefix and logging."""
    user = get_current_user()
    if user:
        return user.get("email", "unknown")
    return "unknown"


def get_user_id() -> str:
    """Return a sanitized user ID suitable for S3 prefixes and temp dirs.
    
    Uses the full email (with @ replaced by _at_) for easy trackability.
    Example: xyz@company.com → xyz_at_company.com
    """
    import re
    email = get_user_email()
    if email and email != "unknown":
        # Replace @ with _at_ for readability, keep the domain for uniqueness
        user_id = email.replace("@", "_at_")
        user_id = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id)
        return user_id
    return "default"


def show_login_page():
    """Display the login page — uses direct API auth or Hosted UI based on config."""
    if USE_HOSTED_UI:
        _show_hosted_ui_login()
    else:
        _show_direct_login()


def _show_direct_login():
    """Display a Streamlit login form that authenticates directly via Cognito API."""
    st.markdown("## 🔐 ProServe Discovery Service")
    st.markdown("Please sign in to continue.")
    st.markdown("")

    # Show error from previous attempt
    if "auth_error" in st.session_state and st.session_state["auth_error"]:
        st.error(st.session_state["auth_error"])
        st.session_state["auth_error"] = None

    # Show success message
    if "auth_success" in st.session_state and st.session_state["auth_success"]:
        st.success(st.session_state["auth_success"])
        st.session_state["auth_success"] = None

    # ── Set-permanent-password flow (T-06) ───────────────────────────
    # Admin-invited users hit NEW_PASSWORD_REQUIRED on first sign-in.
    # Route the UI to a set-password form; on success, continue into
    # whichever challenge comes next (typically MFA_SETUP).
    if st.session_state.get("new_password_pending"):
        _show_new_password_form()
        st.stop()

    # ── MFA flows ────────────────────────────────────────────────────
    # If a prior _authenticate_user() call returned an MFA challenge,
    # route the UI to the appropriate step instead of showing the
    # normal sign-in tabs.
    if st.session_state.get("mfa_challenge_pending"):
        _show_mfa_challenge_form()
        st.stop()

    if st.session_state.get("mfa_setup_pending"):
        _show_mfa_setup_form()
        st.stop()

    # Tabs: Sign In / (Sign Up when self-signup enabled) / Forgot Password.
    # T-06: hide Sign Up when the pool is admin-invite only, and add a
    # short caption explaining how to get an account instead.
    if _ALLOW_SELF_SIGNUP:
        tab_signin, tab_signup, tab_forgot = st.tabs(
            ["Sign In", "Sign Up", "Forgot Password"]
        )
    else:
        st.caption(
            "Self-signup is disabled for this deployment. Contact your "
            "administrator to request an account."
        )
        st.markdown("")
        tab_signin, tab_forgot = st.tabs(["Sign In", "Forgot Password"])
        tab_signup = None

    with tab_signin:
        with st.form("login_form"):
            email = st.text_input("Email", placeholder="user@example.com", key="signin_email")
            password = st.text_input("Password", type="password", key="signin_password")
            submitted = st.form_submit_button("Sign In", use_container_width=True)

        if submitted:
            if not email or not password:
                st.error("Please enter both email and password.")
                st.stop()

            user = _authenticate_user(email, password)
            if user:
                st.session_state["user"] = user
                _log_action("login", f"User logged in: {user['email']}")
                st.rerun()
            else:
                st.stop()

    # T-06: only render the Sign Up tab body when self-signup is enabled.
    if tab_signup is not None:
        with tab_signup:
            # Check if we're in the verification step
            if st.session_state.get("signup_pending_verification"):
                st.info(f"A verification code was sent to **{st.session_state.get('signup_email', '')}**. Enter it below.")
                with st.form("verify_form"):
                    code = st.text_input("Verification Code", placeholder="123456", key="verify_code")
                    verify_submitted = st.form_submit_button("Verify & Complete Sign Up", use_container_width=True)

                if verify_submitted:
                    if not code:
                        st.error("Please enter the verification code.")
                        st.stop()
                    _confirm_signup(st.session_state["signup_email"], code)
                    st.stop()

                if st.button("← Back to Sign Up"):
                    st.session_state["signup_pending_verification"] = False
                    st.rerun()
            else:
                with st.form("signup_form"):
                    signup_email = st.text_input("Email", placeholder="user@example.com", key="signup_email_input")
                    signup_password = st.text_input("Password", type="password", key="signup_password",
                                                   help="Min 8 chars, uppercase, lowercase, number")
                    signup_password_confirm = st.text_input("Confirm Password", type="password", key="signup_password_confirm")
                    signup_submitted = st.form_submit_button("Sign Up", use_container_width=True)

                if signup_submitted:
                    if not signup_email or not signup_password:
                        st.error("Please fill in all fields.")
                        st.stop()
                    if signup_password != signup_password_confirm:
                        st.error("Passwords do not match.")
                        st.stop()
                    _signup_user(signup_email, signup_password)
                    st.stop()

    with tab_forgot:
        # Check if we're in the reset step
        if st.session_state.get("reset_pending_code"):
            st.info(f"A reset code was sent to **{st.session_state.get('reset_email', '')}**. Enter it below with your new password.")
            with st.form("reset_form"):
                reset_code = st.text_input("Reset Code", placeholder="123456", key="reset_code")
                new_password = st.text_input("New Password", type="password", key="new_password")
                new_password_confirm = st.text_input("Confirm New Password", type="password", key="new_password_confirm")
                reset_submitted = st.form_submit_button("Reset Password", use_container_width=True)

            if reset_submitted:
                if not reset_code or not new_password:
                    st.error("Please fill in all fields.")
                    st.stop()
                if new_password != new_password_confirm:
                    st.error("Passwords do not match.")
                    st.stop()
                _confirm_forgot_password(st.session_state["reset_email"], reset_code, new_password)
                st.stop()

            if st.button("← Back"):
                st.session_state["reset_pending_code"] = False
                st.rerun()
        else:
            with st.form("forgot_form"):
                forgot_email = st.text_input("Email", placeholder="user@example.com", key="forgot_email")
                forgot_submitted = st.form_submit_button("Send Reset Code", use_container_width=True)

            if forgot_submitted:
                if not forgot_email:
                    st.error("Please enter your email.")
                    st.stop()
                _initiate_forgot_password(forgot_email)
                st.stop()

    st.stop()


def _signup_user(email: str, password: str):
    """Register a new user via Cognito SignUp API."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
            ],
        )
        # Store email for verification step
        st.session_state["signup_email"] = email
        st.session_state["signup_pending_verification"] = True
        st.rerun()

    except client.exceptions.UsernameExistsException:
        st.error("An account with this email already exists. Please sign in.")
    except client.exceptions.InvalidPasswordException as e:
        st.error(f"Password does not meet requirements: {str(e)}")
    except Exception as e:
        st.error(f"Sign up failed: {str(e)}")


def _confirm_signup(email: str, code: str):
    """Confirm sign-up with the verification code."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.confirm_sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
        )
        st.session_state["signup_pending_verification"] = False
        st.session_state["auth_success"] = "Account verified! You can now sign in."
        st.rerun()

    except client.exceptions.CodeMismatchException:
        st.error("Invalid verification code. Please try again.")
    except client.exceptions.ExpiredCodeException:
        st.error("Verification code expired. Please sign up again.")
    except Exception as e:
        st.error(f"Verification failed: {str(e)}")


def _initiate_forgot_password(email: str):
    """Send a password reset code to the user's email."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
        )
        st.session_state["reset_email"] = email
        st.session_state["reset_pending_code"] = True
        st.rerun()

    except client.exceptions.UserNotFoundException:
        st.error("No account found with this email.")
    except client.exceptions.LimitExceededException:
        st.error("Too many attempts. Please try again later.")
    except Exception as e:
        st.error(f"Failed to send reset code: {str(e)}")


def _confirm_forgot_password(email: str, code: str, new_password: str):
    """Confirm password reset with code and new password."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )
        st.session_state["reset_pending_code"] = False
        st.session_state["auth_success"] = "Password reset successful! You can now sign in."
        st.rerun()

    except client.exceptions.CodeMismatchException:
        st.error("Invalid reset code. Please try again.")
    except client.exceptions.ExpiredCodeException:
        st.error("Reset code expired. Please request a new one.")
    except client.exceptions.InvalidPasswordException as e:
        st.error(f"Password does not meet requirements: {str(e)}")
    except Exception as e:
        st.error(f"Password reset failed: {str(e)}")


def _authenticate_user(email: str, password: str) -> Optional[Dict]:
    """Authenticate user directly via Cognito InitiateAuth API."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)

        # Use USER_PASSWORD_AUTH flow (requires enabling in the user pool client)
        response = client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": email,
                "PASSWORD": password,
            },
        )

        # Handle challenges: NEW_PASSWORD_REQUIRED, SOFTWARE_TOKEN_MFA,
        # MFA_SETUP. The MFA flows use Streamlit session state to
        # persist the Cognito Session token across reruns and route
        # the UI to the correct step (see _show_direct_login).
        if "ChallengeName" in response:
            challenge = response["ChallengeName"]
            cognito_session = response.get("Session")

            if challenge == "NEW_PASSWORD_REQUIRED":
                # T-06: admin-invited user must set a permanent password on
                # first sign-in. Route the UI to _show_new_password_form()
                # via session state (see the routing block at the top of
                # _show_direct_login). After the password is set, Cognito
                # will typically return MFA_SETUP; that follow-up challenge
                # is handled in _respond_to_new_password_challenge.
                st.session_state["new_password_pending"] = True
                st.session_state["new_password_session"] = cognito_session
                st.session_state["new_password_email"] = email
                st.rerun()

            elif challenge == "SOFTWARE_TOKEN_MFA":
                # User already has TOTP configured — prompt for the
                # 6-digit code and complete auth via
                # respond_to_auth_challenge in _respond_to_mfa_challenge.
                st.session_state["mfa_challenge_pending"] = True
                st.session_state["mfa_challenge_session"] = cognito_session
                st.session_state["mfa_challenge_email"] = email
                st.rerun()

            elif challenge == "MFA_SETUP":
                # MFA is required by the user pool but this user has
                # not configured a TOTP factor yet. Kick off
                # AssociateSoftwareToken to obtain a shared secret and
                # route the UI to the setup form (QR + verify input).
                try:
                    assoc = client.associate_software_token(Session=cognito_session)
                except Exception as e:
                    logger.error(f"AssociateSoftwareToken failed: {e}")
                    st.session_state["auth_error"] = f"MFA setup initialization failed: {str(e)}"
                    return None
                st.session_state["mfa_setup_pending"] = True
                st.session_state["mfa_setup_session"] = assoc.get("Session")
                st.session_state["mfa_setup_secret"] = assoc.get("SecretCode", "")
                st.session_state["mfa_setup_email"] = email
                st.rerun()

            else:
                st.session_state["auth_error"] = (
                    f"Unsupported authentication challenge: {challenge}"
                )
                return None

        # No-challenge path (rare — MFA=ON forces a challenge for all
        # users). Use the shared extractor so refresh_token capture is
        # consistent with the MFA and new-password paths.
        user = _extract_user_from_auth_result(
            response.get("AuthenticationResult", {}),
            fallback_email=email,
        )
        if not user:
            st.session_state["auth_error"] = "Authentication succeeded but no token received."
            return None
        return user

    except client.exceptions.NotAuthorizedException:
        st.session_state["auth_error"] = "Incorrect email or password."
        return None
    except client.exceptions.UserNotFoundException:
        st.session_state["auth_error"] = "Incorrect email or password."
        return None
    except client.exceptions.UserNotConfirmedException:
        st.session_state["auth_error"] = "Account not confirmed. Contact your administrator."
        return None
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        st.session_state["auth_error"] = f"Authentication error: {str(e)}"
        return None


# ── Set-permanent-password flow (T-06) ──────────────────────────────
# Cognito returns a NEW_PASSWORD_REQUIRED challenge on the first sign-in
# of an admin-created user (see AdminCreateUserConfig on the UserPool
# resource in deploy/cognito-stack.yaml). _authenticate_user routes here
# via session state; on success we typically get an MFA_SETUP challenge
# next, which we hand off to the existing MFA flows below.


def _show_new_password_form():
    """Prompt an admin-invited user to set a permanent password.

    Runs when session state has new_password_pending set (from
    _authenticate_user hitting a NEW_PASSWORD_REQUIRED challenge).
    Collects a new password that meets the pool policy, then calls
    _respond_to_new_password_challenge to complete the challenge.
    """
    email = st.session_state.get("new_password_email", "")
    st.markdown("### 🔑 Set a permanent password")
    st.markdown(
        f"Your temporary password worked. Set a permanent password for "
        f"**{email}** to continue. The pool policy requires at least "
        f"12 characters with uppercase, lowercase, number, and symbol."
    )
    st.markdown("")

    with st.form("new_password_form"):
        new_password = st.text_input(
            "New password", type="password", key="new_password_input"
        )
        confirm = st.text_input(
            "Confirm new password",
            type="password",
            key="new_password_confirm_input",
        )
        col1, col2 = st.columns([1, 1])
        with col1:
            submit = st.form_submit_button(
                "Set password", use_container_width=True, type="primary"
            )
        with col2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

    if cancel:
        for k in (
            "new_password_pending",
            "new_password_session",
            "new_password_email",
        ):
            st.session_state.pop(k, None)
        st.rerun()

    if submit:
        if not new_password or not confirm:
            st.error("Please fill in both password fields.")
            return
        if new_password != confirm:
            st.error("Passwords do not match.")
            return
        _respond_to_new_password_challenge(
            email=email,
            new_password=new_password,
            session=st.session_state["new_password_session"],
        )


def _respond_to_new_password_challenge(
    email: str, new_password: str, session: str
):
    """Submit the permanent password for a NEW_PASSWORD_REQUIRED challenge.

    Cognito's response may be:
      - Another challenge (typically MFA_SETUP for admin-invited users
        in a pool with MfaConfiguration=ON, which is our case). Route
        the follow-up challenge into the existing MFA flows.
      - AuthenticationResult (rare — only if MFA is not required for
        this user). Extract the ID token and complete sign-in.
    """
    client = boto3.client("cognito-idp", region_name=AWS_REGION)
    try:
        response = client.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=session,
            ChallengeResponses={
                "USERNAME": email,
                "NEW_PASSWORD": new_password,
            },
        )
    except client.exceptions.InvalidPasswordException as e:
        st.error(f"Password does not meet pool policy: {str(e)}")
        return
    except client.exceptions.NotAuthorizedException:
        st.error("Session expired. Please sign in again with your temporary password.")
        for k in (
            "new_password_pending",
            "new_password_session",
            "new_password_email",
        ):
            st.session_state.pop(k, None)
        return
    except Exception as e:
        logger.error(f"NEW_PASSWORD_REQUIRED response failed: {e}")
        st.error(f"Setting password failed: {str(e)}")
        return

    # Clear the new-password session state before routing further.
    for k in (
        "new_password_pending",
        "new_password_session",
        "new_password_email",
    ):
        st.session_state.pop(k, None)

    # If Cognito returned another challenge, route it to the appropriate
    # existing flow.
    if "ChallengeName" in response:
        next_challenge = response["ChallengeName"]
        next_session = response.get("Session")
        if next_challenge == "MFA_SETUP":
            try:
                assoc = client.associate_software_token(Session=next_session)
            except Exception as e:
                logger.error(f"AssociateSoftwareToken failed after new password: {e}")
                st.error(f"MFA setup initialization failed: {str(e)}")
                return
            st.session_state["mfa_setup_pending"] = True
            st.session_state["mfa_setup_session"] = assoc.get("Session")
            st.session_state["mfa_setup_secret"] = assoc.get("SecretCode", "")
            st.session_state["mfa_setup_email"] = email
            st.rerun()
        elif next_challenge == "SOFTWARE_TOKEN_MFA":
            st.session_state["mfa_challenge_pending"] = True
            st.session_state["mfa_challenge_session"] = next_session
            st.session_state["mfa_challenge_email"] = email
            st.rerun()
        else:
            st.error(
                f"Unsupported challenge after password set: {next_challenge}"
            )
            return

    # No follow-up challenge — auth complete.
    user = _extract_user_from_auth_result(
        response.get("AuthenticationResult", {}), fallback_email=email
    )
    if user:
        st.session_state["user"] = user
        _log_action(
            "login",
            f"User logged in (permanent password set): {user['email']}",
        )
        st.rerun()
    else:
        st.error("Password set succeeded but no session token was received.")


# ── MFA (TOTP) flows ─────────────────────────────────────────────────
# Cognito MfaConfiguration=ON forces every user through one of two paths
# on sign-in: SOFTWARE_TOKEN_MFA (already-configured user, prompt for
# code) or MFA_SETUP (first-time, must set up TOTP now). Both are
# implemented as multi-step Streamlit flows using session_state to
# persist the Cognito Session token across reruns.


def _show_mfa_challenge_form():
    """MFA code prompt for users who already have TOTP configured."""
    email = st.session_state.get("mfa_challenge_email", "")
    st.markdown("### 🔐 Two-Factor Authentication")
    st.markdown(
        f"Enter the 6-digit code from your authenticator app for **{email}**."
    )
    st.markdown("")

    with st.form("mfa_challenge_form"):
        code = st.text_input(
            "6-digit code",
            max_chars=6,
            placeholder="123456",
            key="mfa_challenge_code",
        )
        col1, col2 = st.columns([1, 1])
        with col1:
            submit = st.form_submit_button(
                "Verify", use_container_width=True, type="primary"
            )
        with col2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

    if cancel:
        for k in (
            "mfa_challenge_pending",
            "mfa_challenge_session",
            "mfa_challenge_email",
        ):
            st.session_state.pop(k, None)
        st.rerun()

    if submit:
        c = (code or "").strip()
        if not (c.isdigit() and len(c) == 6):
            st.error("Enter the 6-digit code from your authenticator app.")
            return
        _respond_to_mfa_challenge(
            email=email,
            code=c,
            session=st.session_state["mfa_challenge_session"],
        )


def _show_mfa_setup_form():
    """MFA setup form: QR code + secret + verification prompt.

    Shown when Cognito returns MFA_SETUP (pool requires MFA but the
    user has not yet configured a TOTP factor). On successful verify,
    completes the original sign-in in one step.
    """
    email = st.session_state.get("mfa_setup_email", "")
    secret = st.session_state.get("mfa_setup_secret", "")

    st.markdown("### 🔐 Set up Two-Factor Authentication")
    st.markdown(
        "This app requires MFA. Scan the QR code below with an "
        "authenticator app (Google Authenticator, Authy, 1Password, "
        "Microsoft Authenticator, Duo, etc.), then enter the 6-digit "
        "code the app generates."
    )
    st.markdown("")

    col1, col2 = st.columns([1, 2])
    with col1:
        try:
            qr_img = _generate_totp_qr(secret, email)
            st.image(qr_img, caption="Scan with authenticator app", width=220)
        except Exception as e:
            logger.error(f"QR generation failed: {e}")
            st.warning("Could not generate the QR image; use the manual code below.")
    with col2:
        st.markdown("**Manual entry (if QR won't scan):**")
        st.code(secret or "(no secret available)", language=None)
        st.markdown(f"Account name: `{email}`")
        st.markdown("Issuer: `video-analytic-agent`")
        st.markdown("Type: `Time-based (TOTP)` — 6 digits, 30-second period")

    st.markdown("")

    with st.form("mfa_setup_form"):
        code = st.text_input(
            "6-digit code from your authenticator",
            max_chars=6,
            placeholder="123456",
            key="mfa_setup_code",
        )
        col1, col2 = st.columns([1, 1])
        with col1:
            submit = st.form_submit_button(
                "Verify and sign in", use_container_width=True, type="primary"
            )
        with col2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

    if cancel:
        for k in (
            "mfa_setup_pending",
            "mfa_setup_session",
            "mfa_setup_secret",
            "mfa_setup_email",
        ):
            st.session_state.pop(k, None)
        st.rerun()

    if submit:
        c = (code or "").strip()
        if not (c.isdigit() and len(c) == 6):
            st.error("Enter the 6-digit code shown in your authenticator app.")
            return
        _complete_mfa_setup(
            email=email,
            code=c,
            session=st.session_state["mfa_setup_session"],
        )


def _respond_to_mfa_challenge(email: str, code: str, session: str):
    """Submit the TOTP code for a SOFTWARE_TOKEN_MFA challenge.

    On success, extracts user info from the ID token, populates
    session_state, clears MFA flags, and reruns to enter the app.
    """
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        response = client.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=session,
            ChallengeResponses={
                "USERNAME": email,
                "SOFTWARE_TOKEN_MFA_CODE": code,
            },
        )
    except Exception as e:
        _display_mfa_error(e, kind="challenge")
        return

    user = _extract_user_from_auth_result(
        response.get("AuthenticationResult", {}), fallback_email=email
    )
    if user:
        for k in (
            "mfa_challenge_pending",
            "mfa_challenge_session",
            "mfa_challenge_email",
        ):
            st.session_state.pop(k, None)
        st.session_state["user"] = user
        _log_action("login", f"User logged in (MFA verified): {user['email']}")
        st.rerun()
    else:
        st.error("Authentication succeeded but no token was received.")


def _complete_mfa_setup(email: str, code: str, session: str):
    """Verify the user's first TOTP code, then complete sign-in.

    Two Cognito calls: verify_software_token then respond_to_auth_challenge
    with the MFA_SETUP challenge name.
    """
    client = boto3.client("cognito-idp", region_name=AWS_REGION)

    try:
        verify_resp = client.verify_software_token(
            Session=session,
            UserCode=code,
        )
    except Exception as e:
        _display_mfa_error(e, kind="setup")
        return

    if verify_resp.get("Status") != "SUCCESS":
        st.error(
            f"MFA verification returned unexpected status: "
            f"{verify_resp.get('Status')}"
        )
        return

    try:
        final_resp = client.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName="MFA_SETUP",
            Session=verify_resp.get("Session"),
            ChallengeResponses={"USERNAME": email},
        )
    except Exception as e:
        logger.error(f"MFA_SETUP respond_to_auth_challenge failed: {e}")
        st.error(f"MFA setup verified but sign-in completion failed: {str(e)}")
        return

    user = _extract_user_from_auth_result(
        final_resp.get("AuthenticationResult", {}), fallback_email=email
    )
    if user:
        for k in (
            "mfa_setup_pending",
            "mfa_setup_session",
            "mfa_setup_secret",
            "mfa_setup_email",
        ):
            st.session_state.pop(k, None)
        st.session_state["user"] = user
        _log_action("mfa_setup", f"User completed MFA setup: {user['email']}")
        _log_action("login", f"User logged in via MFA setup: {user['email']}")
        st.rerun()
    else:
        st.error("MFA setup verified but no token was received.")


def _extract_user_from_auth_result(
    auth_result: dict, fallback_email: str
) -> Optional[Dict]:
    """Decode the Cognito ID token and capture refresh state.

    T-03: also captures RefreshToken and AccessToken from the outer
    AuthenticationResult so _refresh_tokens() can rotate the ID/access
    tokens silently before they expire. The refresh token stays in
    Streamlit's server-side session_state — the browser never sees it.
    """
    id_token = auth_result.get("IdToken") if auth_result else None
    if not id_token:
        return None
    try:
        import base64
        payload = id_token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        user_info = json.loads(base64.b64decode(payload))
        return {
            "email": user_info.get("email", fallback_email),
            "sub": user_info.get("sub", ""),
            "username": user_info.get("cognito:username", ""),
            "token_expiry": user_info.get("exp", 0),
            # T-03: keep the refresh token server-side so we can rotate
            # the ID/access tokens without user interaction. Refresh
            # tokens have a longer lifetime than access tokens (30d
            # default in Cognito) and are what makes the "silent renewal"
            # pattern work.
            "refresh_token": auth_result.get("RefreshToken", ""),
            "access_token": auth_result.get("AccessToken", ""),
        }
    except Exception as e:
        logger.error(f"Failed to decode id_token: {e}")
        return None


def _refresh_tokens() -> bool:
    """T-03: silently rotate the ID/access tokens using the refresh token.

    Called from is_authenticated() when the ID token is within
    REFRESH_SAFETY_WINDOW of expiring. Returns True on success (updates
    session_state["user"] with fresh tokens in place). Returns False if:

      - the current session has no user or no refresh token (unrecoverable
        — session was probably not established with T-03-aware code and
        needs a fresh login),
      - Cognito rejects the refresh (revoked user or expired refresh
        token — typically NotAuthorizedException), or
      - any other Cognito error (network, throttling — treated as
        unrecoverable at this call site; user will be re-prompted).

    The refresh flow does NOT rotate the RefreshToken itself. Cognito's
    default is to keep the same refresh token valid for 30 days from
    last activity, and successive REFRESH_TOKEN_AUTH calls do not return
    a new one in AuthenticationResult. We preserve the existing one.
    """
    user = st.session_state.get("user")
    if not user:
        return False
    refresh_token = user.get("refresh_token")
    if not refresh_token:
        return False

    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        response = client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={
                "REFRESH_TOKEN": refresh_token,
            },
        )
    except Exception as e:
        # NotAuthorizedException typically means the user was disabled
        # in Cognito or the refresh token has expired. Any other error
        # (network, service issue) also falls here — safer to force a
        # re-authentication than to keep the session on assumptions.
        logger.info(
            "Token refresh failed for %s: %s",
            user.get("email", "<unknown>"),
            e,
        )
        return False

    fresh = _extract_user_from_auth_result(
        response.get("AuthenticationResult", {}),
        fallback_email=user.get("email", ""),
    )
    if not fresh:
        logger.warning(
            "Token refresh returned an empty AuthenticationResult for %s",
            user.get("email", "<unknown>"),
        )
        return False

    # REFRESH_TOKEN_AUTH does not return a new RefreshToken; keep the
    # existing one so the next refresh has something to work with.
    fresh["refresh_token"] = refresh_token
    st.session_state["user"] = fresh
    return True


def _display_mfa_error(exc: Exception, kind: str):
    """Translate a Cognito MFA-related exception into a friendly error.

    kind: 'challenge' for SOFTWARE_TOKEN_MFA flow, 'setup' for MFA_SETUP.
    """
    cls = exc.__class__.__name__
    if "CodeMismatchException" in cls or "EnableSoftwareTokenMFAException" in cls:
        st.error(
            "Incorrect code. Wait for the next code in your authenticator "
            "and try again."
        )
    elif "ExpiredCodeException" in cls:
        st.error(
            "Code expired. Enter the current code shown in your "
            "authenticator app."
        )
    elif "NotAuthorizedException" in cls:
        st.error("Session expired. Please sign in again.")
        # Bounce back to the sign-in tabs
        prefix = "mfa_challenge_" if kind == "challenge" else "mfa_setup_"
        for k in list(st.session_state.keys()):
            if k.startswith(prefix):
                st.session_state.pop(k, None)
    else:
        logger.error(f"MFA {kind} failed: {exc}")
        st.error(f"MFA verification failed: {str(exc)}")


def _generate_totp_qr(secret: str, email: str, issuer: str = "video-analytic-agent"):
    """Return PNG bytes of a QR code encoding the otpauth:// TOTP URI.

    Any RFC 6238 authenticator (Google Authenticator, Authy, 1Password,
    Microsoft Authenticator, Duo, etc.) can scan this to register the
    account.

    Returns bytes rather than the PIL image wrapper because Streamlit's
    st.image() does not accept qrcode's PilImage subclass directly.
    """
    import io
    import qrcode
    otpauth = (
        f"otpauth://totp/"
        f"{urllib.parse.quote(issuer)}:{urllib.parse.quote(email)}"
        f"?secret={secret}&issuer={urllib.parse.quote(issuer)}"
    )
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(otpauth)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _show_hosted_ui_login():
    """Display the login page with Cognito Hosted UI redirect (requires HTTPS)."""
    st.markdown("## 🔐 ProServe Discovery Service")
    st.markdown("Please sign in to continue.")
    st.markdown("")

    # Check if we have a callback code in the URL
    query_params = st.query_params
    auth_code = query_params.get("code")

    if auth_code:
        # Exchange code for tokens
        user = _exchange_code_for_tokens(auth_code)
        if user:
            st.session_state["user"] = user
            st.query_params.clear()
            _log_action("login", f"User logged in: {user['email']}")
            st.rerun()
        else:
            st.error("Authentication failed. Please try again.")
            st.query_params.clear()

    # Show login button
    login_url = _build_login_url()
    st.markdown(f'<a href="{login_url}" target="_self">'
                f'<button style="background-color:#FF4B4B;color:white;padding:12px 24px;'
                f'border:none;border-radius:8px;font-size:16px;cursor:pointer;">'
                f'Sign In / Sign Up</button></a>',
                unsafe_allow_html=True)

    st.markdown("")
    st.markdown("---")
    st.markdown("*Don't have an account? Click above to sign up.*")
    st.stop()


def logout():
    """Clear the session and optionally redirect to Cognito logout."""
    user = get_current_user()
    if user:
        _log_action("logout", f"User logged out: {user.get('email', 'unknown')}")

    st.session_state["user"] = None
    # Clear all session state
    for key in list(st.session_state.keys()):
        if key != "user":
            del st.session_state[key]

    if USE_HOSTED_UI:
        logout_url = (
            f"https://{COGNITO_DOMAIN}/logout?"
            f"client_id={COGNITO_CLIENT_ID}&"
            f"logout_uri={urllib.parse.quote(APP_URL)}"
        )
        st.markdown(f'<meta http-equiv="refresh" content="0;url={logout_url}">',
                    unsafe_allow_html=True)
    st.rerun()


def _build_login_url() -> str:
    """Build the Cognito Hosted UI login URL."""
    callback_url = APP_URL.rstrip("/")
    return (
        f"https://{COGNITO_DOMAIN}/login?"
        f"client_id={COGNITO_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=openid+email+profile&"
        f"redirect_uri={urllib.parse.quote(callback_url)}"
    )


def _exchange_code_for_tokens(code: str) -> Optional[Dict]:
    """Exchange the authorization code for tokens and extract user info."""
    import requests

    token_url = f"https://{COGNITO_DOMAIN}/oauth2/token"
    callback_url = APP_URL.rstrip("/")

    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": COGNITO_CLIENT_ID,
                "code": code,
                "redirect_uri": callback_url,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )

        if resp.status_code != 200:
            logger.error(f"Token exchange failed: {resp.status_code} {resp.text}")
            return None

        tokens = resp.json()
        id_token = tokens.get("id_token")

        if not id_token:
            logger.error("No id_token in response")
            return None

        # Decode JWT payload (no verification needed — Cognito already validated)
        import base64
        payload = id_token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        user_info = json.loads(base64.b64decode(payload))

        return {
            "email": user_info.get("email", "unknown"),
            "sub": user_info.get("sub", ""),
            "username": user_info.get("cognito:username", ""),
            "token_expiry": user_info.get("exp", 0),
        }

    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None


def _log_action(action: str, detail: str = ""):
    """Log a user action for audit trail."""
    user = get_current_user()
    email = user.get("email", "unknown") if user else "unknown"
    timestamp = _time_now()
    log_entry = f"[{timestamp}] user={email} action={action}"
    if detail:
        log_entry += f" detail={detail}"
    logger.info(log_entry)
    print(log_entry)  # Also print to stdout for CloudWatch


def _time_now() -> str:
    """Return current UTC timestamp string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Audit logging helper (importable by other modules) ────────────────────

def log_user_action(action: str, detail: str = ""):
    """Public interface for logging user actions from other modules."""
    _log_action(action, detail)
