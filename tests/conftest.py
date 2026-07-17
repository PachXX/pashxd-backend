"""Shared fixtures: in-memory Mongo (mongomock-motor) + ASGI test client.

No network, no real Mongo — tests run anywhere `pip install -r requirements.txt
pytest pytest-asyncio mongomock-motor` has run.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

# Make `import main` work when pytest runs from repo root or backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

# Local test cookies: no https in the ASGI transport
os.environ["COOKIE_SECURE"] = "false"
os.environ["COOKIE_SAMESITE"] = "lax"
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789abcdef"

from app.utils.hash import hash_password  # noqa: E402
import main  # noqa: E402
from app.config import database  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture()
async def db():
    """Fresh in-memory DB per test, wired into both db access paths."""
    mock_client = AsyncMongoMockClient()
    mock_db = mock_client["pashxd_test"]

    database.db = mock_db          # app.config.database.get_db()
    main.db_instance = mock_db     # main.py module-level handle

    await mock_db.users.insert_one({
        "email": "admin@test.com",
        "password": hash_password("correct-password"),
        "role": "admin",
    })
    yield mock_db


@pytest_asyncio.fixture()
async def client(db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client, email="admin@test.com", password="correct-password"):
    return await client.post("/api/auth/login", json={"email": email, "password": password})
