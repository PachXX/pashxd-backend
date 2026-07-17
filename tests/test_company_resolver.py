"""Unit + integration tests for company grouping logic."""
import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from app.services.company_resolver import (
    normalize_company_text,
    resolve_company_key,
    resolve_or_create_company,
)


def test_normalize_strips_legal_suffixes():
    assert normalize_company_text("Acme Inc.") == "acme"
    assert normalize_company_text("Beta LLC") == "beta"
    assert normalize_company_text("  Gamma   Group  ") == "gamma"


def test_resolve_key_real_domain():
    info = resolve_company_key("khalid@acme.com", "Acme Trading Est.")
    assert info["normalized_key"] == "acme.com"
    assert info["domain"] == "acme.com"


def test_resolve_key_personal_domain_with_company_text():
    info = resolve_company_key("jane@gmail.com", "Beta Corp")
    assert info["normalized_key"] == "name:beta"
    assert info["domain"] is None


def test_resolve_key_personal_domain_blank_company_is_singleton():
    info = resolve_company_key("bob@gmail.com", "")
    assert info["normalized_key"] == "singleton:bob@gmail.com"
    assert info["domain"] is None


@pytest_asyncio.fixture()
async def resolver_db():
    client = AsyncMongoMockClient()
    db = client["resolver_test"]
    await db.companies.create_index("normalized_key", unique=True)
    yield db


@pytest.mark.asyncio
async def test_same_domain_contacts_share_one_company(resolver_db):
    id1 = await resolve_or_create_company(resolver_db, email="a@acme.com", company_text="Acme Inc.")
    id2 = await resolve_or_create_company(resolver_db, email="b@acme.com", company_text="Acme")
    assert id1 == id2
    assert await resolver_db.companies.count_documents({}) == 1


@pytest.mark.asyncio
async def test_singleton_contacts_do_not_collide(resolver_db):
    id1 = await resolve_or_create_company(resolver_db, email="x@gmail.com", company_text="")
    id2 = await resolve_or_create_company(resolver_db, email="y@gmail.com", company_text="")
    assert id1 != id2
    assert await resolver_db.companies.count_documents({}) == 2


@pytest.mark.asyncio
async def test_resolver_is_idempotent(resolver_db):
    id1 = await resolve_or_create_company(resolver_db, email="a@acme.com", company_text="Acme")
    id2 = await resolve_or_create_company(resolver_db, email="a@acme.com", company_text="Acme")
    assert id1 == id2
    assert await resolver_db.companies.count_documents({}) == 1
