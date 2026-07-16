"""
Audit trail for admin-privileged mutations — who changed what, when.

SOC2 Processing Integrity / incident-response requirement: deletions and
updates to customer records (contacts, deals, email templates, ...) must be
attributable to a specific admin, with enough detail to reconstruct what
changed.

Usage:
    from app.utils.audit import log_audit

    await log_audit(
        request, user,
        action="delete", resource_type="contact", resource_id=contact_id,
        before=existing_contact,
    )
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for") if request else None
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request and request.client else "unknown"


def _redact(doc: Optional[dict]) -> Optional[dict]:
    """Drop fields that should never sit in an audit trail (password hashes etc)."""
    if not doc:
        return doc
    redacted = dict(doc)
    for field in ("password", "password_hash", "token"):
        redacted.pop(field, None)
    if "_id" in redacted:
        redacted["_id"] = str(redacted["_id"])
    return redacted


async def log_audit(
    request,
    user: dict,
    action: str,
    resource_type: str,
    resource_id: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """
    Record one audit entry. Never raises — a logging failure must not block
    the actual mutation (matches the fail-open style used by rate_limit).

    action:        "create" | "update" | "delete"
    resource_type: "contact" | "deal" | "email_template" | ...
    """
    try:
        from app.config import database
        db = database.db

        entry = {
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "actor_id": str(user.get("sub") or user.get("id") or ""),
            "actor_email": user.get("email", ""),
            "ip": _client_ip(request),
            "before": _redact(before),
            "after": _redact(after),
            "extra": extra or {},
            "created_at": datetime.now(timezone.utc),
        }
        await db.audit_logs.insert_one(entry)
    except Exception as e:
        logger.warning(f"⚠️ audit log write failed ({resource_type}/{action}): {e}")
