"""
Audit log read endpoint — admin-only view into app/utils/audit.py entries.
"""
from fastapi import APIRouter, Depends, Query
from typing import Optional

from app.middleware.auth import require_admin

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("")
@router.get("/")
async def list_audit_logs(
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = Query(100, le=500),
    user=Depends(require_admin),
):
    from app.config import database

    query = {}
    if resource_type:
        query["resource_type"] = resource_type
    if resource_id:
        query["resource_id"] = resource_id
    if action:
        query["action"] = action

    cursor = database.db.audit_logs.find(query).sort("created_at", -1).limit(limit)
    entries = await cursor.to_list(limit)
    for e in entries:
        e["id"] = str(e.pop("_id"))
    return {"entries": entries, "count": len(entries)}
