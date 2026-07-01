"""
Agents router — tracks background automation agents that run on the operator's
machine (launchd) and push run records here after each execution.

Static registry defines the known agents + their schedules; the agent_runs
collection stores each execution (status, summary, log tail).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9 fallback, not expected on Render
    ZoneInfo = None

from app.middleware.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/agents", tags=["agents"])

# ─── STATIC AGENT REGISTRY ────────────────────────────────────────────────────
# Schedules are the launchd times on the operator's Mac (Europe/Berlin, CEST).
SCHEDULE_TZ = "Europe/Berlin"

AGENTS = [
    {
        "id": "token-refresh",
        "label": "Token Refresh",
        "description": "Logs into the backend daily and rotates the JWT all agents use.",
        "icon": "key",
        "schedule": {"hour": 7, "minute": 0},
        "schedule_label": "Daily · 07:00",
    },
    {
        "id": "saudi-lead",
        "label": "Saudi Lead Agent",
        "description": "Sources KSA retail/trading prospects, qualifies, pushes to pipeline, sends outreach.",
        "icon": "target",
        "schedule": {"hour": 7, "minute": 30},
        "schedule_label": "Daily · 07:30",
    },
    {
        "id": "uk-lead",
        "label": "UK & EU Lead Agent",
        "description": "Sources UK/EU prospects, qualifies, pushes to pipeline, sends outreach.",
        "icon": "globe",
        "schedule": {"hour": 8, "minute": 0},
        "schedule_label": "Daily · 08:00",
    },
    {
        "id": "blog",
        "label": "Blog Publisher",
        "description": "Generates an SEO blog post with Claude and publishes it to the site.",
        "icon": "file-text",
        "schedule": {"hour": 9, "minute": 0},
        "schedule_label": "Daily · 09:00",
    },
    {
        "id": "outreach",
        "label": "Outreach Agent",
        "description": "Drafts hyper-personalised cold emails + follow-ups for pipeline contacts. Draft-for-approval; nothing auto-sends.",
        "icon": "send",
        "schedule": {"hour": 8, "minute": 30},
        "schedule_label": "Daily · 08:30",
    },
]

AGENT_IDS = {a["id"] for a in AGENTS}
AGENT_BY_ID = {a["id"]: a for a in AGENTS}


# ─── MODELS ───────────────────────────────────────────────────────────────────

class RunReport(BaseModel):
    agent: str
    status: str = Field(default="success")  # success | error | partial
    summary: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    log_tail: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _next_run(hour: int, minute: int) -> Optional[str]:
    """Next occurrence of hour:minute in the schedule tz, returned as UTC ISO."""
    if ZoneInfo is None:
        return None
    try:
        tz = ZoneInfo(SCHEDULE_TZ)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc).isoformat()


def _serialize_run(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id", "")),
        "agent": doc.get("agent", ""),
        "status": doc.get("status", "unknown"),
        "summary": doc.get("summary", {}),
        "error": doc.get("error"),
        "log_tail": doc.get("log_tail"),
        "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "duration_seconds": doc.get("duration_seconds"),
        "created_at": doc.get("created_at"),
    }


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def list_agents(user=Depends(get_current_user)):
    """List all agents with last run, next scheduled run, and run stats."""
    from app.config import database

    result = []
    for a in AGENTS:
        recent = await database.db.agent_runs.find({"agent": a["id"]}).sort("created_at", -1).to_list(1)

        last_run = _serialize_run(recent[0]) if recent else None
        total = await database.db.agent_runs.count_documents({"agent": a["id"]})
        successes = await database.db.agent_runs.count_documents(
            {"agent": a["id"], "status": "success"}
        )
        success_rate = round((successes / total) * 100) if total else None

        result.append({
            "id": a["id"],
            "label": a["label"],
            "description": a["description"],
            "icon": a["icon"],
            "schedule_label": a["schedule_label"],
            "next_run": _next_run(a["schedule"]["hour"], a["schedule"]["minute"]),
            "last_run": last_run,
            "total_runs": total,
            "success_rate": success_rate,
        })

    return {"agents": result}


@router.get("/runs")
async def list_runs(agent: Optional[str] = None, limit: int = 50, user=Depends(get_current_user)):
    """Run history across all agents, or filtered to one agent."""
    from app.config import database

    query = {}
    if agent:
        if agent not in AGENT_IDS:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
        query["agent"] = agent

    limit = max(1, min(limit, 200))
    runs = await database.db.agent_runs.find(query).sort("created_at", -1).to_list(limit)
    return {"runs": [_serialize_run(r) for r in runs]}


@router.post("/runs")
async def report_run(report: RunReport, user=Depends(require_admin)):
    """Ingest a run record from an agent (admin token required)."""
    from app.config import database

    if report.agent not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {report.agent}")

    now = datetime.now(timezone.utc)
    duration = report.duration_seconds
    if duration is None and report.started_at and report.finished_at:
        duration = (report.finished_at - report.started_at).total_seconds()

    doc = {
        "agent": report.agent,
        "status": report.status,
        "summary": report.summary,
        "error": report.error,
        "log_tail": (report.log_tail or "")[-8000:],  # cap stored log
        "started_at": report.started_at,
        "finished_at": report.finished_at or now,
        "duration_seconds": duration,
        "created_at": now,
    }
    res = await database.db.agent_runs.insert_one(doc)
    return {"ok": True, "id": str(res.inserted_id)}
