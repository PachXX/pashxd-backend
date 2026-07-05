"""End-to-end integration tests.

Run against a LIVE deployment (local Docker container or Cloud Run):

    BASE_URL=http://localhost:8080 \
    TEST_ADMIN_EMAIL=admin@pashx.com TEST_ADMIN_PASSWORD=... \
    pytest tests/test_integration.py -v

Skipped entirely when BASE_URL is not set.
"""
import os
import uuid

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL")
ADMIN_EMAIL = os.getenv("TEST_ADMIN_EMAIL", "admin@pashx.com")
ADMIN_PASSWORD = os.getenv("TEST_ADMIN_PASSWORD", "changeme123")

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="BASE_URL not set — no live server to test against"
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return r.json()["access_token"]


# ── Health / liveness ────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_readiness_database_connected(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["database"] == "connected"


def test_api_root(client):
    r = client.get("/api/")
    assert r.status_code == 200
    assert "PashxD" in r.json()["message"]


# ── Database round-trip ──────────────────────────────────────────────

def test_status_check_roundtrip(client):
    name = f"itest-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/status", json={"client_name": name})
    assert r.status_code == 200
    body = r.json()
    assert body["client_name"] == name
    assert body["id"]


# ── Authentication ───────────────────────────────────────────────────

def test_login_rejects_bad_credentials(client):
    r = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "definitely-wrong"},
    )
    assert r.status_code == 401


def test_admin_me(client, admin_token):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "admin"


def test_protected_route_requires_token(client):
    r = client.get("/api/demo-requests")
    assert r.status_code in (401, 403)


def test_protected_route_rejects_garbage_token(client):
    r = client.get(
        "/api/demo-requests", headers={"Authorization": "Bearer garbage"}
    )
    assert r.status_code == 401


# ── Demo requests + CRM auto-conversion ──────────────────────────────

def test_demo_request_flow(client, admin_token):
    email = f"itest-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/api/demo-requests",
        json={
            "name": "Integration Test",
            "email": email,
            "company": "TestCo",
            "role": "QA",
            "industry": "Software",
            "message": "integration test",
        },
    )
    assert r.status_code == 200
    assert r.json()["email"] == email

    # Admin can list demo requests and sees the new one
    r = client.get(
        "/api/demo-requests", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert any(d["email"] == email for d in r.json())

    # CRM auto-conversion created a contact
    r = client.get(
        "/api/crm/contacts", headers={"Authorization": f"Bearer {admin_token}"}
    )
    if r.status_code == 200:
        payload = r.json()
        contacts = payload if isinstance(payload, list) else payload.get("contacts", [])
        assert any(c.get("email") == email for c in contacts)


# ── CORS ─────────────────────────────────────────────────────────────

def test_cors_allows_frontend_origin(client):
    r = client.options(
        "/api/status",
        headers={
            "Origin": "https://pashx.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://pashx.com"


# ── Cloud Run headers ────────────────────────────────────────────────

def test_app_header_middleware(client):
    r = client.get("/health")
    assert r.headers.get("x-app-name") == "PashxD"
