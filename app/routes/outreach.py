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
import html
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
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

# ─── AI REGENERATION ──────────────────────────────────────────────────────────
# Mirrors mcp-server/outreach_agent.py's generate_copy()/render_html() exactly,
# so a regenerated draft reads in the same voice as one the agent drafted.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SENDER_NAME = os.getenv("OUTREACH_SENDER_NAME", "Shahil")
CALENDLY_URL = os.getenv("CALENDLY_URL", "https://calendly.com/shahil-talenlio-letstalk/letstalk")
CRM_URL = os.getenv("CRM_URL", "https://crm.pashx.com")
BRAND_COLOR = "#2ECC71"

STEP_BRIEF = {
    "intro":    "First cold email. Open with a specific hook drawn from their business context. State one concrete PashxD benefit relevant to them. Soft CTA for a 15-min call. Warm, human, no corporate fluff.",
    "followup": "Second touch, they didn't reply to the intro. Short, friendly bump. Add ONE new angle or question — do not repeat the intro. Very brief.",
    "value":    "Third touch. Lead with proof: a concrete result, mini case-study style, or a sharp insight about their industry. Show don't tell. Still short.",
    "breakup":  "Final email. Low-pressure 'closing the loop' note. Gracious, leaves the door open. Two sentences max in the body.",
}

BANNED_PHRASES = [
    "game-changer", "in today's fast-paced world", "unlock", "leverage",
    "synergy", "revolutionize", "seamless", "seamlessly", "elevate",
    "supercharge", "cutting-edge", "next-level", "paradigm shift",
    "I hope this finds you well", "dear valued", "esteemed", "delve",
    "moreover", "furthermore", "in conclusion", "plethora", "myriad",
    "robust", "empower", "empowering", "testament to", "boasts",
]


# ─── MODELS ───────────────────────────────────────────────────────────────────

class DraftCreate(BaseModel):
    contact_id: str
    step: int = Field(ge=0, le=MAX_STEP)
    subject: str
    body_html: str
    personalization_notes: Optional[str] = None
    cc_emails: Optional[list[str]] = None


class DraftUpdate(BaseModel):
    subject: Optional[str] = None
    body_html: Optional[str] = None


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


@router.patch("/drafts/{draft_id}")
async def update_draft(draft_id: str, updates: DraftUpdate, user=Depends(require_admin)):
    """Manual edit of a pending draft's subject/body_html."""
    from app.config import database
    db = database

    draft = await db.db.outreach_drafts.find_one({"_id": _oid(draft_id)})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Draft is {draft.get('status')}, not pending")

    patch = {k: v for k, v in updates.dict().items() if v is not None}
    if not patch:
        return _serialize_draft(draft)

    await db.db.outreach_drafts.update_one({"_id": draft["_id"]}, {"$set": patch})
    updated = await db.db.outreach_drafts.find_one({"_id": draft["_id"]})
    return _serialize_draft(updated)


async def _generate_copy(kind: str, contact_name: str, company: str, notes: str) -> dict:
    """Ask Claude for hyper-personalised copy. Returns {subject, preview, paragraphs, cta_label}.

    Mirrors mcp-server/outreach_agent.py's generate_copy() — same prompt,
    same voice — so a regenerated draft doesn't read differently from one
    the agent originally drafted.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on backend")

    prompt = f"""You are {SENDER_NAME}, founder of PashxD (pashx.com) — an AI-native trading & retail platform (CRM, quotations, multi-branch stock, VAT/ZATCA e-invoicing) for SMBs.

Write a {kind} cold outreach email to this prospect. Be HYPER-PERSONALISED using their specific context — never generic.

PROSPECT:
- Name: {contact_name}
- Company: {company}
- Contact notes (score, pain, current software, ZATCA phase): {notes or 'none on file'}

EMAIL TYPE: {kind}
BRIEF: {STEP_BRIEF.get(kind, STEP_BRIEF['intro'])}

RULES:
- Under 90 words in the body. Real sentences, no buzzwords.
- NEVER use these phrases or close variants: {", ".join(BANNED_PHRASES)}.
- Vary sentence length — one short line, one longer one. Uniform sentence length reads as AI-generated.
- Use contractions naturally (it's, don't, that's) — write like {SENDER_NAME} personally typed this, not a brand voice.
- Reference something specific to THEM in the first line — a real detail, not a generic compliment.
- One clear soft CTA (a 15-min call). Do not paste the link in the body text; a button is added separately.
- Sign as {SENDER_NAME}.

Return ONLY JSON (no markdown), exactly:
{{"subject": "<subject line, under 60 chars, personalised>",
  "preview": "<preheader, under 90 chars>",
  "paragraphs": ["<para 1>", "<para 2>", "..."],
  "cta_label": "<short button label e.g. 'Grab 15 minutes'>"}}"""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 900,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Claude API error {resp.status_code}")

    raw = resp.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _render_html(contact_name: str, company: str, copy: dict) -> str:
    """Wrap personalised copy in the same branded template outreach_agent.py uses."""
    name = html.escape((contact_name or "there").split(" ")[0] or "there")
    company_esc = html.escape(company or "your business")
    paras = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:#2b2f36;">{html.escape(p)}</p>'
        for p in copy.get("paragraphs", [])
    )
    cta_label = html.escape(copy.get("cta_label", "Book 15 minutes"))
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:28px 0;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(16,24,40,0.08);">
    <tr><td style="padding:22px 32px 8px;">
      <span style="font-family:'Segoe UI',Arial,sans-serif;font-size:19px;font-weight:800;letter-spacing:-0.02em;color:#0D1117;">Pash<span style="color:{BRAND_COLOR};">xD</span></span>
    </td></tr>
    <tr><td style="padding:8px 32px 4px;font-family:'Segoe UI',Arial,sans-serif;">
      <p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:#2b2f36;">Hi {name},</p>
      {paras}
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 12px;"><tr>
        <td style="border-radius:8px;background:{BRAND_COLOR};">
          <a href="{CALENDLY_URL}" style="display:inline-block;padding:11px 22px;font-family:'Segoe UI',Arial,sans-serif;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">{cta_label} →</a>
        </td>
      </tr></table>
      <p style="margin:0 0 20px;font-size:13px;line-height:1.6;color:#8a9099;">Prefer to look around first? See it live at <a href="{CRM_URL}" style="color:{BRAND_COLOR};text-decoration:none;font-weight:600;">crm.pashx.com</a></p>
      <p style="margin:0 0 4px;font-size:15px;line-height:1.6;color:#2b2f36;">— {SENDER_NAME}</p>
      <p style="margin:0 0 18px;font-size:13px;color:#8a9099;">PashxD · <a href="https://pashx.com" style="color:{BRAND_COLOR};text-decoration:none;">pashx.com</a></p>
    </td></tr>
    <tr><td style="padding:14px 32px;border-top:1px solid #eef0f2;font-family:'Segoe UI',Arial,sans-serif;">
      <p style="margin:0;font-size:11px;color:#aab0b8;">You're receiving this because PashxD may be a fit for {company_esc}. Reply "no thanks" and I won't follow up.</p>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


@router.post("/drafts/{draft_id}/regenerate")
async def regenerate_draft(draft_id: str, user=Depends(require_admin)):
    """Re-generate a pending draft's copy via Claude, same voice as the agent."""
    from app.config import database
    db = database

    draft = await db.db.outreach_drafts.find_one({"_id": _oid(draft_id)})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Draft is {draft.get('status')}, not pending")

    contact = await db.db.contacts.find_one({"_id": _oid(draft["contact_id"])}) if draft.get("contact_id") else None
    notes = contact.get("notes", "") if contact else ""

    copy = await _generate_copy(
        kind=draft.get("kind", "intro"),
        contact_name=draft.get("contact_name", ""),
        company=draft.get("company", ""),
        notes=notes,
    )
    body_html = _render_html(draft.get("contact_name", ""), draft.get("company", ""), copy)

    await db.db.outreach_drafts.update_one(
        {"_id": draft["_id"]},
        {"$set": {
            "subject": copy.get("subject", draft.get("subject")),
            "body_html": body_html,
            "personalization_notes": copy.get("preview", ""),
        }},
    )
    updated = await db.db.outreach_drafts.find_one({"_id": draft["_id"]})
    return _serialize_draft(updated)


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
