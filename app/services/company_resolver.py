"""Resolves a contact's email/company text into a shared `companies` document.

Single source of truth for company grouping — used by contact/deal creation
routes, CSV import, the demo-request auto-conversion, and the backfill
migration script, so grouping logic never drifts between call sites.
"""
import re
from datetime import datetime
from typing import Optional

from pymongo.errors import DuplicateKeyError

PERSONAL_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "protonmail.com", "proton.me", "live.com", "msn.com",
    "yandex.com", "gmx.com", "mail.com", "zoho.com", "me.com",
})

# Longest-match-first so " llc." doesn't get partially eaten by " llc".
_COMPANY_SUFFIX_STOPWORDS = sorted([
    " establishment", " corporation", " trading est", " company",
    " est.", " est", " group", " corp.", " corp", " l.l.c", " llc.",
    " llc", " ltd.", " ltd", " gmbh", " plc", " inc.", " inc",
    " co.", " co", " llp", " fze", " fzc", " bv", " sa",
], key=len, reverse=True)


def normalize_company_text(raw: str) -> str:
    """lowercase, trim, strip common legal suffixes, collapse whitespace."""
    text = (raw or "").strip().lower()
    for suffix in _COMPANY_SUFFIX_STOPWORDS:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_email_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


def resolve_company_key(email: str, company_text: Optional[str]) -> dict:
    """Returns {normalized_key, domain, display_name}.

    domain is None unless the email is on a real (non-personal) corporate
    domain — used for display and the sparse-unique domain index.
    """
    domain = extract_email_domain(email)
    is_real_domain = bool(domain) and domain not in PERSONAL_EMAIL_DOMAINS

    if is_real_domain:
        normalized_company = normalize_company_text(company_text or "")
        display_name = (
            normalized_company.title() if normalized_company
            else domain.split(".")[0].replace("-", " ").title()
        )
        return {"normalized_key": domain, "domain": domain, "display_name": display_name}

    normalized_company = normalize_company_text(company_text or "")
    if normalized_company:
        return {
            "normalized_key": f"name:{normalized_company}",
            "domain": None,
            "display_name": normalized_company.title(),
        }

    # Personal/blank domain + blank company text: one singleton company per
    # contact, keyed by their own email so re-resolving is idempotent instead
    # of creating a fresh duplicate every time.
    email_key = (email or "").strip().lower()
    return {
        "normalized_key": f"singleton:{email_key}",
        "domain": None,
        "display_name": email_key or "Unknown",
    }


async def resolve_or_create_company(
    db,
    *,
    email: str,
    company_text: Optional[str],
    industry: Optional[str] = None,
    created_from: str = "auto_contact",
    contact_name: Optional[str] = None,
) -> str:
    """Idempotent: finds an existing company by normalized_key, else creates one."""
    info = resolve_company_key(email, company_text)

    existing = await db.companies.find_one({"normalized_key": info["normalized_key"]})
    if existing:
        return str(existing["_id"])

    display_name = info["display_name"]
    if info["normalized_key"].startswith("singleton:") and contact_name:
        display_name = contact_name

    now = datetime.utcnow()
    doc = {
        "name": display_name,
        "domain": info["domain"],
        "normalized_key": info["normalized_key"],
        "is_singleton": info["normalized_key"].startswith("singleton:"),
        "industry": industry or None,
        "website": f"https://{info['domain']}" if info["domain"] else None,
        "notes": "",
        "created_at": now,
        "updated_at": now,
        "created_from": created_from,
    }
    try:
        result = await db.companies.insert_one(doc)
        return str(result.inserted_id)
    except DuplicateKeyError:
        # Lost a race with a concurrent insert on the same normalized_key.
        winner = await db.companies.find_one({"normalized_key": info["normalized_key"]})
        return str(winner["_id"])
