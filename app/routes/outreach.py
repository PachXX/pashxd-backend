"""
Outreach router — draft-for-approval cold email sequencing.

Flow:
  1. Agent calls GET /due -> tasks (new intros + followups that are due).
  2. Agent generates hyper-personalised HTML per task, POSTs to /drafts.
  3. Operator reviews drafts in the dashboard:
       - POST /drafts/{id}/approve -> sends via SendGrid, logs, advances sequence.
       - POST /drafts/{id}/skip    -> advances sequence without sending.
  4. Engagement: if a prior email was clicked, the sequence is marked "hot"
     and no further drafts are generated (operator handles personally).

Nothing is ever auto-sent. Sequence state lives here (dashboard-authoritative).
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middleware.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/outreach", tags=["outreach"])

# ─── CADENCE ──────────────────────────────────────────────────────────────────
# 4 touches: Day 0 / 3 / 7 / 12. gap_days = days after previous send.
CADENCE = [
    {"step": 0, "kind": "intro",    "gap_days": 0,  "label": "Intro"},
    {"step": 1, "kind": "followup", "gap_days": 3,  "label": "Follow-up"},
    {"step": 2, "kind": "value",    "gap_days": 4,  "label": "Value / proof"},
    {"step": 3, "kind": "breakup",  "gap_days": 5,  "label": "Breakup"},
]
MAX_STEP = len(CADENCE) - 1

# Which CRM contact sources are eligible for enrollment.
LEAD_SOURCES = ["saudi-lead-agent", "uk-lead-agent", "lead-agent"]

NOTIFY_CC = os.getenv("OUTREACH_CC", "moideenshahil2@gmail.com")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "shahil@pashx.com")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME", "Shahil from PashxD")


# ─── MODELS ───────────────────────────────────────────────────────────────────

class DraftCreate(BaseModel):
    contact_id: str
    step: int = Field(ge=0, le=MAX_STEP)
    subject: str
    body_html: str
    personalization_notes: Optional[str] = None
    cc_emails: Optional[list[str]] = None


class EnrollRequest(BaseModel):
    company_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sid(doc: dict) -> str:
    return str(doc.get("_id", ""))


def _serialize_seq(s: dict) -> dict:
    return {
        "id": _sid(s),
        "contact_id": s.get("contact_id"),
        "contact_name": s.get("contact_name"),
        "contact_email": s.get("contact_email"),
        "company": s.get("company"),
        "status": s.get("status"),
        "current_step": s.get("current_step"),
        "started_at": s.get("started_at"),
        "last_sent_at": s.get("last_sent_at"),
        "next_due_at": s.get("next_due_at"),
    }


def _serialize_draft(d: dict) -> dict:
    return {
        "id": _sid(d),
        "sequence_id": d.get("sequence_id"),
        "contact_id": d.get("contact_id"),
        "contact_name": d.get("contact_name"),
        "contact_email": d.get("contact_email"),
        "company": d.get("company"),
        "step": d.get("step"),
        "kind": d.get("kind"),
        "subject": d.get("subject"),
        "body_html": d.get("body_html"),
        "status": d.get("status"),
        "personalization_notes": d.get("personalization_notes"),
        "cc_emails": d.get("cc_emails") or [],
        "created_at": d.get("created_at"),
        "sent_at": d.get("sent_at"),
    }


async def _engagement(db, email: str) -> dict:
    """Any opens/clicks across this contact's prior logged emails."""
    if not email:
        return {"opened": False, "clicked": False}
    opened = await db.db.email_logs.count_documents({"to_email": email, "opened_at": {"$ne": None}})
    clicked = await db.db.email_logs.count_documents({"to_email": email, "clicked_at": {"$ne": None}})
    return {"opened": opened > 0, "clicked": clicked > 0}


# ─── DUE (agent pulls work) ───────────────────────────────────────────────────

@router.get("/due")
async def get_due(limit: int = 15, user=Depends(get_current_user)):
    """
    Tasks the outreach agent should draft now:
      - new intros: eligible lead contacts with no sequence yet
      - followups: active sequences past next_due_at with no pending draft
    Contacts that clicked a prior email are marked 'hot' and skipped.
    """
    from app.config import database
    db = database
    limit = max(1, min(limit, 50))
    tasks: list[dict] = []

    # 1) Followups on active sequences that are due
    active = await db.db.outreach_sequences.find(
        {"status": "active", "next_due_at": {"$lte": _now()}}
    ).sort("next_due_at", 1).to_list(limit)

    for seq in active:
        step = seq.get("current_step", 0)
        if step > MAX_STEP:
            continue
        # engagement gate: click -> mark hot, stop
        eng = await _engagement(db, seq.get("contact_email", ""))
        if eng["clicked"]:
            await db.db.outreach_sequences.update_one(
                {"_id": seq["_id"]},
                {"$set": {"status": "hot", "updated_at": _now()}},
            )
            seq["status"] = "hot"
            await _sync_contact(db, seq)
            continue
        # skip if a draft already exists for this step (avoid dupes on re-run)
        dup = await db.db.outreach_drafts.count_documents(
            {"sequence_id": _sid(seq), "step": step, "status": {"$in": ["pending", "approved", "sent"]}}
        )
        if dup:
            continue
        c = CADENCE[step]
        tasks.append({
            "sequence_id": _sid(seq),
            "contact_id": seq.get("contact_id"),
            "contact_name": seq.get("contact_name"),
            "contact_email": seq.get("contact_email"),
            "company": seq.get("company"),
            "notes": seq.get("notes", ""),
            "step": step,
            "kind": c["kind"],
            "engagement": eng,
            "is_new": False,
        })

    # 2) New intros: eligible lead contacts not yet enrolled
    remaining = limit - len(tasks)
    if remaining > 0:
        enrolled_ids = set(
            s.get("contact_id") for s in await db.db.outreach_sequences.find({}, {"contact_id": 1}).to_list(5000)
        )
        candidates = await db.db.contacts.find(
            {"source": {"$in": LEAD_SOURCES}, "email": {"$nin": ["", None]}}
        ).sort("created_at", -1).to_list(500)
        for c in candidates:
            if remaining <= 0:
                break
            cid = str(c["_id"])
            if cid in enrolled_ids:
                continue
            tasks.append({
                "sequence_id": None,
                "contact_id": cid,
                "contact_name": c.get("name", ""),
                "contact_email": c.get("email", ""),
                "company": c.get("company", ""),
                "notes": c.get("notes", ""),
                "step": 0,
                "kind": "intro",
                "engagement": {"opened": False, "clicked": False},
                "is_new": True,
            })
            remaining -= 1

    return {"tasks": tasks, "cadence": CADENCE}


# ─── DRAFTS ───────────────────────────────────────────────────────────────────

@router.post("/drafts")
async def create_draft(draft: DraftCreate, user=Depends(require_admin)):
    """Agent stores a generated draft. Enrolls the contact if not already."""
    from app.config import database
    db = database

    contact = await db.db.contacts.find_one({"_id": _oid(draft.contact_id)})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    seq = await db.db.outreach_sequences.find_one({"contact_id": draft.contact_id})
    if not seq:
        seq_doc = {
            "contact_id": draft.contact_id,
            "contact_name": contact.get("name", ""),
            "contact_email": contact.get("email", ""),
            "company": contact.get("company", ""),
            "company_id": contact.get("company_id"),
            "notes": contact.get("notes", ""),
            "status": "active",
            "current_step": draft.step,
            "started_at": _now(),
            "last_sent_at": None,
            "next_due_at": _now(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        res = await db.db.outreach_sequences.insert_one(seq_doc)
        seq_id = str(res.inserted_id)
        seq = {**seq_doc, "_id": res.inserted_id}
    else:
        if seq.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=f"Sequence is {seq.get('status')}, not active — cannot draft a new touch",
            )
        if draft.step != seq.get("current_step"):
            raise HTTPException(
                status_code=409,
                detail=f"Sequence is at step {seq.get('current_step')}, draft targets step {draft.step}",
            )
        seq_id = _sid(seq)

    c = CADENCE[draft.step] if draft.step <= MAX_STEP else CADENCE[MAX_STEP]
    doc = {
        "sequence_id": seq_id,
        "contact_id": draft.contact_id,
        "contact_name": seq.get("contact_name"),
        "contact_email": seq.get("contact_email"),
        "company": seq.get("company"),
        "company_id": seq.get("company_id"),
        "step": draft.step,
        "kind": c["kind"],
        "subject": draft.subject,
        "body_html": draft.body_html,
        "personalization_notes": draft.personalization_notes,
        "cc_emails": draft.cc_emails or [],
        "status": "pending",
        "created_at": _now(),
        "sent_at": None,
    }
    res = await db.db.outreach_drafts.insert_one(doc)
    return {"ok": True, "id": str(res.inserted_id), "sequence_id": seq_id}


@router.get("/drafts")
async def list_drafts(status: str = "pending", limit: int = 100, user=Depends(get_current_user)):
    from app.config import database
    db = database
    limit = max(1, min(limit, 200))
    q = {} if status == "all" else {"status": status}
    drafts = await db.db.outreach_drafts.find(q).sort("created_at", -1).to_list(limit)
    return {"drafts": [_serialize_draft(d) for d in drafts]}


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str, user=Depends(require_admin)):
    """Send the drafted email via SendGrid, log it, advance the sequence."""
    from app.config import database
    db = database

    draft = await db.db.outreach_drafts.find_one({"_id": _oid(draft_id)})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Draft is {draft.get('status')}, not pending")

    to_email = draft.get("contact_email", "")
    if not to_email:
        raise HTTPException(status_code=400, detail="Draft has no recipient email")

    # Log first so tracking id exists
    log_doc = {
        "to_email": to_email,
        "to_name": draft.get("contact_name", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body_html", ""),
        "campaign_id": "outreach",
        "status": "pending",
        "sent_at": None, "opened_at": None, "clicked_at": None,
        "open_count": 0, "click_count": 0,
        "created_at": _now(),
    }
    log_res = await db.db.email_logs.insert_one(log_doc)

    from app.routes.email import inject_tracking, send_via_resend, RESEND_API_KEY, SENDGRID_REPLY_TO
    tracked_body = inject_tracking(draft.get("body_html", ""), str(log_res.inserted_id), to_email)

    sent_ok = False
    error = None
    if RESEND_API_KEY:
        cc_list = [NOTIFY_CC]
        for e in (draft.get("cc_emails") or []):
            if e and e.lower() != NOTIFY_CC.lower():
                cc_list.append(e)
        sent_ok, error = await send_via_resend(
            to_email=to_email, to_name=draft.get("contact_name", ""),
            subject=draft.get("subject", ""), html=tracked_body,
            from_email=SENDGRID_FROM_EMAIL, from_name=SENDGRID_FROM_NAME,
            reply_to=SENDGRID_REPLY_TO, cc=cc_list,
        )
    else:
        error = "RESEND_API_KEY not configured"

    if not sent_ok:
        await db.db.email_logs.update_one({"_id": log_res.inserted_id}, {"$set": {"status": "failed"}})
        raise HTTPException(status_code=502, detail=error or "Send failed")

    await db.db.email_logs.update_one(
        {"_id": log_res.inserted_id}, {"$set": {"status": "sent", "sent_at": _now()}}
    )
    await db.db.outreach_drafts.update_one(
        {"_id": draft["_id"]}, {"$set": {"status": "sent", "sent_at": _now()}}
    )
    await _advance(db, draft)
    # write outreach state back onto the CRM contact
    seq = await db.db.outreach_sequences.find_one({"_id": _oid(draft["sequence_id"])})
    if seq:
        await _sync_contact(db, seq)
    return {"ok": True}


@router.post("/drafts/{draft_id}/skip")
async def skip_draft(draft_id: str, user=Depends(require_admin)):
    """Discard this draft and advance the sequence without sending."""
    from app.config import database
    db = database
    draft = await db.db.outreach_drafts.find_one({"_id": _oid(draft_id)})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Draft is {draft.get('status')}, not pending")
    await db.db.outreach_drafts.update_one({"_id": draft["_id"]}, {"$set": {"status": "skipped"}})
    await _advance(db, draft)
    return {"ok": True}


@router.get("/sent")
async def list_sent(limit: int = 50, company_id: Optional[str] = None, user=Depends(get_current_user)):
    """Recently sent outreach emails, enriched with each sequence's next follow-up."""
    from app.config import database
    db = database
    limit = max(1, min(limit, 200))
    q = {"status": "sent"}
    if company_id:
        q["company_id"] = company_id
    sent = await db.db.outreach_drafts.find(q).sort("sent_at", -1).to_list(limit)

    seq_ids = list({d.get("sequence_id") for d in sent if d.get("sequence_id")})
    seq_map = {}
    for sid in seq_ids:
        try:
            s = await db.db.outreach_sequences.find_one({"_id": _oid(sid)})
            if s:
                seq_map[sid] = s
        except Exception:
            continue

    items = []
    for d in sent:
        s = seq_map.get(d.get("sequence_id"), {})
        seq_status = s.get("status")
        items.append({
            "id": _sid(d),
            "contact_name": d.get("contact_name"),
            "contact_email": d.get("contact_email"),
            "company": d.get("company"),
            "step": d.get("step"),
            "kind": d.get("kind"),
            "subject": d.get("subject"),
            "sent_at": d.get("sent_at"),
            "sequence_status": seq_status,
            "next_followup_at": s.get("next_due_at") if seq_status == "active" else None,
        })
    return {"sent": items}


@router.post("/reset")
async def reset_outreach(pending_only: bool = True, user=Depends(require_admin)):
    """
    Clear outreach state so sequences re-draft from scratch.
    pending_only=True (default): delete only pending drafts and reset their
    sequences back to step 0 (safe — never touches already-sent history).
    pending_only=False: wipe ALL drafts + sequences.
    """
    from app.config import database
    db = database

    if not pending_only:
        d = await db.db.outreach_drafts.delete_many({})
        s = await db.db.outreach_sequences.delete_many({})
        return {"ok": True, "drafts_deleted": d.deleted_count, "sequences_deleted": s.deleted_count}

    pending = await db.db.outreach_drafts.find({"status": "pending"}).to_list(1000)
    seq_ids = {p.get("sequence_id") for p in pending if p.get("sequence_id")}
    d = await db.db.outreach_drafts.delete_many({"status": "pending"})
    reset = 0
    for sid in seq_ids:
        # only reset sequences that have no sent/approved drafts left
        sent = await db.db.outreach_drafts.count_documents(
            {"sequence_id": sid, "status": {"$in": ["sent", "approved"]}}
        )
        if sent == 0:
            await db.db.outreach_sequences.delete_one({"_id": _oid(sid)})
            reset += 1
    return {"ok": True, "drafts_deleted": d.deleted_count, "sequences_reset": reset}


@router.get("/sequences")
async def list_sequences(
    status: Optional[str] = None, company_id: Optional[str] = None,
    limit: int = 200, user=Depends(get_current_user),
):
    from app.config import database
    db = database
    limit = max(1, min(limit, 500))
    q = {}
    if status:
        q["status"] = status
    if company_id:
        q["company_id"] = company_id
    seqs = await db.db.outreach_sequences.find(q).sort("updated_at", -1).to_list(limit)
    counts = {}
    for st in ["active", "hot", "completed", "stopped"]:
        counts[st] = await db.db.outreach_sequences.count_documents({"status": st})
    return {"sequences": [_serialize_seq(s) for s in seqs], "counts": counts}


@router.post("/enroll")
async def enroll_company(req: EnrollRequest, user=Depends(require_admin)):
    """Manually start outreach for every contact at a company that isn't
    already enrolled — for CRM entries that didn't come through a
    lead-sourcing agent and so never hit the LEAD_SOURCES auto-enrollment
    in get_due()."""
    from app.config import database
    db = database

    contacts = await db.db.contacts.find(
        {"company_id": req.company_id, "email": {"$nin": ["", None]}}
    ).to_list(500)
    if not contacts:
        raise HTTPException(status_code=404, detail="No contacts with an email found for this company")

    enrolled_ids = set(
        s.get("contact_id") for s in await db.db.outreach_sequences.find({}, {"contact_id": 1}).to_list(5000)
    )

    enrolled, skipped = [], []
    for c in contacts:
        cid = str(c["_id"])
        if cid in enrolled_ids:
            skipped.append(cid)
            continue
        seq_doc = {
            "contact_id": cid,
            "contact_name": c.get("name", ""),
            "contact_email": c.get("email", ""),
            "company": c.get("company", ""),
            "company_id": c.get("company_id"),
            "notes": c.get("notes", ""),
            "status": "active",
            "current_step": 0,
            "started_at": _now(),
            "last_sent_at": None,
            "next_due_at": _now(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        res = await db.db.outreach_sequences.insert_one(seq_doc)
        enrolled.append(str(res.inserted_id))

    return {"ok": True, "enrolled": len(enrolled), "skipped_already_enrolled": len(skipped)}


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _oid(id_str: str):
    from bson import ObjectId
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


TOUCH_LABELS = ["intro", "follow-up 1", "follow-up 2", "breakup"]


async def _sync_contact(db, seq: dict):
    """Mirror sequence state onto the CRM contact so the pipeline shows outreach status."""
    cid = seq.get("contact_id")
    if not cid:
        return
    status = seq.get("status")
    touches_sent = seq.get("current_step", 0)  # after advance, == number of touches sent
    next_due = seq.get("next_due_at") if status == "active" else None
    if status == "completed":
        summary = f"Outreach complete — {touches_sent} email(s) sent, no next follow-up"
    elif status == "hot":
        summary = "Outreach paused — lead engaged (hot), handle personally"
    else:
        last = TOUCH_LABELS[min(touches_sent - 1, len(TOUCH_LABELS) - 1)] if touches_sent > 0 else "intro"
        due_str = next_due.strftime("%Y-%m-%d") if hasattr(next_due, "strftime") else str(next_due)[:10]
        summary = f"Outreach: {last} sent · next follow-up {due_str}"
    try:
        await db.db.contacts.update_one(
            {"_id": _oid(cid)},
            {"$set": {
                "outreach_status": status,
                "outreach_touches_sent": touches_sent,
                "outreach_last_sent_at": seq.get("last_sent_at"),
                "outreach_next_followup_at": next_due,
                "outreach_summary": summary,
                "updated_at": _now(),
            }},
        )
    except Exception:
        pass


async def _advance(db, draft: dict):
    """Advance the sequence after a touch is sent or skipped."""
    seq = await db.db.outreach_sequences.find_one({"_id": _oid(draft["sequence_id"])})
    if not seq:
        return
    step = draft.get("step", seq.get("current_step", 0))
    next_step = step + 1
    if next_step > MAX_STEP:
        await db.db.outreach_sequences.update_one(
            {"_id": seq["_id"]},
            {"$set": {"status": "completed", "current_step": next_step,
                      "last_sent_at": _now(), "updated_at": _now()}},
        )
        return
    gap = CADENCE[next_step]["gap_days"]
    await db.db.outreach_sequences.update_one(
        {"_id": seq["_id"]},
        {"$set": {
            "current_step": next_step,
            "last_sent_at": _now(),
            "next_due_at": _now() + timedelta(days=gap),
            "updated_at": _now(),
        }},
    )
