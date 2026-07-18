"""Integration tests for auth: login, cookies, rate limiting, RBAC."""
import pytest

from tests.conftest import login

pytestmark = pytest.mark.asyncio


# ─── LOGIN ───────────────────────────────────────────────

async def test_login_success_returns_user_and_sets_cookie(client):
    res = await login(client)
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["email"] == "admin@test.com"
    assert body["user"]["role"] == "admin"
    # httpOnly session cookie set
    set_cookie = res.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "HttpOnly" in set_cookie


async def test_login_wrong_password_401(client):
    res = await login(client, password="wrong")
    assert res.status_code == 401


async def test_login_unknown_email_401(client):
    res = await login(client, email="nobody@test.com")
    assert res.status_code == 401


# ─── COOKIE SESSION ──────────────────────────────────────

async def test_me_with_cookie_session(client):
    await login(client)  # AsyncClient carries cookies forward
    res = await client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json()["email"] == "admin@test.com"


async def test_me_without_auth_401(client):
    res = await client.get("/api/auth/me")
    assert res.status_code == 401


async def test_me_with_bearer_fallback(client):
    res = await login(client)
    token = res.json()["access_token"]
    client.cookies.clear()
    res = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


async def test_logout_clears_cookie(client):
    await login(client)
    res = await client.post("/api/auth/logout")
    assert res.status_code == 200
    # Cookie deleted → subsequent /me is unauthenticated
    res = await client.get("/api/auth/me")
    assert res.status_code == 401


# ─── RATE LIMITING ───────────────────────────────────────

async def test_login_rate_limit_trips_after_5_failures(client):
    for _ in range(5):
        res = await login(client, password="wrong")
        assert res.status_code == 401
    res = await login(client, password="wrong")
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    # Correct password is also blocked while tripped
    res = await login(client)
    assert res.status_code == 429


async def test_login_success_resets_rate_limit(client):
    for _ in range(3):
        await login(client, password="wrong")
    res = await login(client)  # success below threshold
    assert res.status_code == 200
    # Counter cleared: 5 fresh failures needed to trip again
    for _ in range(4):
        res = await login(client, password="wrong")
        assert res.status_code == 401


# ─── PROTECTED ROUTES ────────────────────────────────────

async def test_demo_requests_requires_auth(client):
    res = await client.get("/api/demo-requests")
    assert res.status_code == 401


async def test_demo_requests_with_admin_session(client):
    await login(client)
    res = await client.get("/api/demo-requests")
    assert res.status_code == 200


# ─── RBAC ────────────────────────────────────────────────
# These four need POST/GET /api/auth/users and PATCH /api/auth/users/{id}/role
# — user-management-with-roles, a separate feature from cookie sessions.
# require_role()/ROLE_HIERARCHY already exist in middleware/auth.py so the
# access-control building block is there, but there's currently no way to
# create anything but an admin user, and no endpoint to list/patch users.
# Tracked on the roadmap as its own item (see CEO audit — multi-user RBAC);
# not building it as a side effect of finishing cookie sessions.

@pytest.mark.skip(reason="POST /api/auth/users not built yet — separate user-management feature")
async def test_admin_creates_user_with_role(client, db):
    await login(client)
    res = await client.post(
        "/api/auth/users",
        json={"email": "viewer@test.com", "password": "viewer-pass-123", "role": "viewer"},
    )
    assert res.status_code == 201
    assert res.json()["role"] == "viewer"


@pytest.mark.skip(reason="POST /api/auth/users not built yet — separate user-management feature")
async def test_create_user_rejects_unknown_role(client):
    await login(client)
    res = await client.post(
        "/api/auth/users",
        json={"email": "x@test.com", "password": "pass", "role": "superuser"},
    )
    assert res.status_code == 400


@pytest.mark.skip(reason="POST/GET /api/auth/users not built yet — separate user-management feature")
async def test_viewer_cannot_access_admin_routes(client, db):
    await login(client)
    await client.post(
        "/api/auth/users",
        json={"email": "viewer@test.com", "password": "viewer-pass-123", "role": "viewer"},
    )
    client.cookies.clear()

    res = await login(client, email="viewer@test.com", password="viewer-pass-123")
    assert res.status_code == 200
    # /me works for any authenticated role
    res = await client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json()["role"] == "viewer"
    # admin-only route → 403
    res = await client.get("/api/auth/users")
    assert res.status_code == 403


@pytest.mark.skip(reason="PATCH /api/auth/users/{id}/role not built yet — separate user-management feature")
async def test_admin_cannot_change_own_role(client):
    res = await login(client)
    my_id = res.json()["user"]["id"]
    res = await client.patch(f"/api/auth/users/{my_id}/role", json={"role": "viewer"})
    assert res.status_code == 400


# ─── HEALTH ──────────────────────────────────────────────

async def test_health_reports_metrics(client):
    res = await client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "pashxd-api"
    assert "uptime_seconds" in body
    assert "db" in body
