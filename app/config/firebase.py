"""Firebase Admin SDK initialization.

On Cloud Run the SDK authenticates via Application Default Credentials
(the service's runtime service account) — no key file is needed or
wanted. Locally you can either:
  * set GOOGLE_APPLICATION_CREDENTIALS to a service-account key file, or
  * run `gcloud auth application-default login`, or
  * leave Firebase disabled (the app runs fine without it; MongoDB
    remains the system of record and legacy JWT auth keeps working).
"""
import logging
import os

logger = logging.getLogger(__name__)

_app = None
_init_attempted = False


def firebase_enabled() -> bool:
    """Firebase is on when a project is configured (Cloud Run sets none
    of these automatically, so this is always an explicit opt-in)."""
    return bool(
        os.getenv("FIREBASE_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
    )


def init_firebase():
    """Initialize firebase_admin once. Safe to call multiple times.

    Returns the App instance, or None when Firebase is not configured
    or initialization fails (the API must never crash because of an
    optional integration).
    """
    global _app, _init_attempted
    if _app is not None or _init_attempted:
        return _app
    _init_attempted = True

    if not firebase_enabled():
        logger.info("Firebase disabled (no FIREBASE_PROJECT_ID/GOOGLE_CLOUD_PROJECT set)")
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials

        project_id = (
            os.getenv("FIREBASE_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCLOUD_PROJECT")
        )
        options = {"projectId": project_id}
        bucket = os.getenv("FIREBASE_STORAGE_BUCKET")
        if bucket:
            options["storageBucket"] = bucket

        _app = firebase_admin.initialize_app(
            credentials.ApplicationDefault(), options
        )
        logger.info(f"✅ Firebase Admin initialized for project {project_id}")
    except Exception as e:
        logger.error(f"❌ Firebase Admin init failed: {e}")
        _app = None
    return _app


def verify_firebase_token(id_token: str) -> dict | None:
    """Verify a Firebase Auth ID token.

    Returns the decoded claims, or None if Firebase is unavailable or
    the token is not a valid Firebase token (callers fall back to the
    legacy JWT path).
    """
    if init_firebase() is None:
        return None
    try:
        from firebase_admin import auth as fb_auth

        return fb_auth.verify_id_token(id_token)
    except Exception:
        return None
