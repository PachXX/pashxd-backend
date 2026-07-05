"""Firebase Admin SDK integration (optional).

The Admin SDK is initialised lazily and never crashes app startup: on Cloud Run
it picks up Application Default Credentials from the attached service account, so
no key file is shipped. Locally it uses GOOGLE_APPLICATION_CREDENTIALS if set.

Currently exposed for Firebase ID-token verification (e.g. to accept Firebase
Authentication logins alongside the existing JWT auth). Importing this module has
no side effects; call ``init_firebase()`` or ``verify_firebase_token()`` when needed.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_initialised = False
_app = None


def init_firebase() -> bool:
    """Initialise the Firebase Admin SDK once. Returns True if available.

    Safe to call repeatedly. Returns False (and logs a warning) if the SDK is
    not installed or credentials cannot be resolved, so callers can degrade
    gracefully rather than take the process down.
    """
    global _initialised, _app
    if _initialised:
        return _app is not None

    _initialised = True
    try:
        import firebase_admin
        from firebase_admin import credentials

        project_id = os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        options = {"projectId": project_id} if project_id else None

        # Application Default Credentials: the Cloud Run service account, or
        # GOOGLE_APPLICATION_CREDENTIALS locally.
        cred = credentials.ApplicationDefault()
        _app = firebase_admin.initialize_app(cred, options)
        logger.info("Firebase Admin SDK initialised (project=%s)", project_id or "default")
        return True
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash startup
        logger.warning("Firebase Admin SDK unavailable: %s", exc)
        _app = None
        return False


def verify_firebase_token(id_token: str) -> Optional[dict]:
    """Verify a Firebase ID token. Returns the decoded claims, or None if invalid
    or the SDK is unavailable."""
    if not init_firebase():
        return None
    try:
        from firebase_admin import auth as fb_auth

        return fb_auth.verify_id_token(id_token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Firebase token verification failed: %s", exc)
        return None
