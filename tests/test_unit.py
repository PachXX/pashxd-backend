"""Unit tests — no server or database required."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest

from app.utils.jwt import create_token, decode_token
from app.utils.hash import hash_password, verify_password


def test_jwt_roundtrip():
    token = create_token({"sub": "u1", "email": "a@b.c", "role": "admin"})
    payload = decode_token(token)
    assert payload["sub"] == "u1"
    assert payload["email"] == "a@b.c"
    assert payload["role"] == "admin"
    assert "exp" in payload and "iat" in payload


def test_jwt_invalid_token_rejected():
    with pytest.raises(ValueError):
        decode_token("not-a-real-token")


def test_jwt_tampered_token_rejected():
    token = create_token({"sub": "u1", "role": "admin"})
    with pytest.raises(ValueError):
        decode_token(token[:-2] + "xx")


def test_password_hash_roundtrip():
    hashed = hash_password("s3cret!")
    assert hashed != "s3cret!"
    assert verify_password("s3cret!", hashed)
    assert not verify_password("wrong", hashed)


def test_firebase_disabled_without_project():
    for var in ("FIREBASE_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        os.environ.pop(var, None)
    from app.config import firebase

    assert not firebase.firebase_enabled()
    # verify must fail closed (None), never raise
    assert firebase.verify_firebase_token("junk") is None


def test_cloud_logging_json_format():
    import json
    import logging

    from app.utils.cloud_logging import CloudRunJsonFormatter

    rec = logging.LogRecord(
        "test", logging.ERROR, "file.py", 42, "boom %s", ("x",), None
    )
    out = json.loads(CloudRunJsonFormatter().format(rec))
    assert out["severity"] == "ERROR"
    assert out["message"] == "boom x"
