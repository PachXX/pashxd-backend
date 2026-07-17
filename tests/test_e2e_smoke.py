"""E2E smoke tests against a deployed backend (migration-critical flows).

Skipped unless E2E_BASE_URL is set — keeps unit runs offline. Usage:

    E2E_BASE_URL=https://pashxd-api-q2ccs4ytaq-ew.a.run.app \
        python -m pytest tests/test_e2e_smoke.py -q

Read-only: no logins are attempted, nothing is created.
"""
import os

import httpx
import pytest

BASE_URL = os.getenv("E2E_BASE_URL", "")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL not set")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=15) as c:
        yield c


def test_health_ok(http):
    res = http.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "pashxd-api"
    assert body["status"] in ("healthy", "degraded")


def test_health_db_reachable(http):
    body = http.get("/health").json()
    # After deploy of the metrics change this reports db latency;
    # older deployments won't have the key yet.
    if "db" in body:
        assert body["db"]["status"] == "ok", "MongoDB unreachable from Cloud Run"


def test_public_blogs_list(http):
    res = http.get("/api/blogs/")
    assert res.status_code == 200
    assert isinstance(res.json().get("blogs"), list)


def test_login_rejects_bad_credentials(http):
    res = http.post(
        "/api/auth/login",
        json={"email": "smoke-test@invalid.example", "password": "definitely-wrong"},
    )
    # 401 = rejected, 429 = rate limiter already tripped for this IP — both prove auth is on
    assert res.status_code in (401, 429)


def test_protected_routes_require_auth(http):
    for path in ["/api/demo-requests", "/api/auth/me", "/api/email/logs", "/api/blogs/admin/all", "/api/crm/companies"]:
        res = http.get(path)
        # 401 = current code; 403 = pre-cookie deploys (HTTPBearer auto_error).
        # Either way the route refused an unauthenticated request.
        assert res.status_code in (401, 403), f"{path} not auth-protected (got {res.status_code})"


def test_cors_allows_production_frontend(http):
    res = http.options(
        "/api/auth/login",
        headers={
            "Origin": "https://pashx.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.headers.get("access-control-allow-origin") == "https://pashx.com"
    assert res.headers.get("access-control-allow-credentials") == "true"
