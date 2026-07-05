"""Firebase Authentication integration tests (Emulator Suite).

Requires the Firebase Auth emulator and an API instance wired to it:

    firebase emulators:start --only auth --project demo-pashxd
    # API needs: FIREBASE_PROJECT_ID=demo-pashxd
    #            FIREBASE_AUTH_EMULATOR_HOST=<reachable host>:9099

    BASE_URL=http://localhost:8080 \
    FIREBASE_AUTH_EMULATOR_URL=http://localhost:9099 \
    pytest tests/test_firebase_auth.py -v

Skipped when either env var is missing.
"""
import json
import os
import uuid

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL")
EMULATOR_URL = os.getenv("FIREBASE_AUTH_EMULATOR_URL")
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "demo-pashxd")

pytestmark = pytest.mark.skipif(
    not (BASE_URL and EMULATOR_URL),
    reason="BASE_URL / FIREBASE_AUTH_EMULATOR_URL not set",
)


def _signup(email: str, password: str) -> dict:
    r = httpx.post(
        f"{EMULATOR_URL}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fake",
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    r.raise_for_status()
    return r.json()


def _set_admin_claim(local_id: str) -> None:
    # "Bearer owner" is the emulator's built-in admin credential.
    r = httpx.post(
        f"{EMULATOR_URL}/identitytoolkit.googleapis.com/v1/projects/{PROJECT_ID}/accounts:update",
        headers={"Authorization": "Bearer owner"},
        json={"localId": local_id, "customAttributes": json.dumps({"role": "admin"})},
    )
    r.raise_for_status()


def _sign_in(email: str, password: str) -> str:
    r = httpx.post(
        f"{EMULATOR_URL}/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=fake",
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    r.raise_for_status()
    return r.json()["idToken"]


def test_firebase_admin_token_grants_admin_access():
    email = f"fb-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = _signup(email, "test-pass-123")
    _set_admin_claim(user["localId"])
    token = _sign_in(email, "test-pass-123")  # fresh token includes the claim

    r = httpx.get(
        f"{BASE_URL}/api/demo-requests",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_firebase_token_without_admin_claim_forbidden():
    email = f"fb-user-{uuid.uuid4().hex[:8]}@example.com"
    user = _signup(email, "test-pass-123")
    token = user["idToken"]

    r = httpx.get(
        f"{BASE_URL}/api/demo-requests",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
