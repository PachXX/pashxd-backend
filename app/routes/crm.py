from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
from datetime import datetime
from bson import ObjectId
from app.middleware.auth import get_current_user
from app.utils.audit import log_audit

router = APIRouter(prefix="/api/crm", tags=["crm"])

# ==================== MODELS ====================

class ContactCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = ""
    company: Optional[str] = ""
    role: Optional[str] = ""
    industry: Optional[str] = ""
    source: str = "manual"
    status: Optional[str] = "new"
    notes: Optional[str] = ""

class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    industry: Optional[str] = None
    source: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None

class DealCreate(BaseModel):
    title: str
    contact_id: str
    value: float = 0
    currency: str = "EUR"
    stage: Literal["lead", "qualified", "proposal", "negotiation", "won", "lost"] = "lead"
    probability: int = 10
    notes: Optional[str] = ""
    source: str = "manual"

class DealUpdate(BaseModel):
    stage: Optional[str] = None
    value: Optional[float] = None
    notes: Optional[str] = None
    probability: Optional[int] = None
    title: Optional[str] = None

class ActivityCreate(BaseModel):
    type: str
    title: str
    description: Optional[str] = ""
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None

# ==================== CONTACTS ====================

@router.get("/contacts")
async def get_contacts(search: Optional[str] = None, status: Optional[str] = None, limit: int = 100, user=Depends(get_current_user)):
    """Get all contacts with optional search and filter"""
    from app.config import database

    query = {}

    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"company": {"$regex": search, "$options": "i"}},
        ]

    if status and status != "all":
        query["status"] = status

    contacts = await database.db.contacts.find(query).limit(limit).to_list(limit)

    return {
        "contacts": [
            {
                "id": str(c["_id"]),
                "name": c.get("name", ""),
                "email": c.get("email", ""),
                "phone": c.get("phone", ""),
                "company": c.get("company", ""),
                "role": c.get("role", ""),
                "industry": c.get("industry", ""),
                "source": c.get("source", "manual"),
                "status": c.get("status", "new"),
                "notes": c.get("notes", ""),
                "outreach_status": c.get("outreach_status"),
                "outreach_summary": c.get("outreach_summary"),
                "outreach_next_followup_at": c.get("outreach_next_followup_at").isoformat() if isinstance(c.get("outreach_next_followup_at"), datetime) else c.get("outreach_next_followup_at"),
                "outreach_last_sent_at": c.get("outreach_last_sent_at").isoformat() if isinstance(c.get("outreach_last_sent_at"), datetime) else c.get("outreach_last_sent_at"),
                "created_at": c.get("created_at", datetime.utcnow()).isoformat() if isinstance(c.get("created_at"), datetime) else c.get("created_at"),
                "updated_at": c.get("updated_at", datetime.utcnow()).isoformat() if isinstance(c.get("updated_at"), datetime) else c.get("updated_at"),
            }
            for c in contacts
        ]
    }

@router.post("/contacts")
async def create_contact(contact: ContactCreate, user=Depends(get_current_user)):
    """Create new contact"""
    from app.config import database

    existing = await database.db.contacts.find_one({"email": contact.email})
    if existing:
        raise HTTPException(status_code=400, detail="Contact with this email already exists")

    contact_doc = {
        **contact.dict(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    result = await database.db.contacts.insert_one(contact_doc)

    await database.db.activities.insert_one({
        "type": "contact_created",
        "title": f"New contact: {contact.name}",
        "description": f"Added {contact.name} from {contact.company or 'Unknown'}",
        "contact_id": str(result.inserted_id),
        "created_at": datetime.utcnow(),
    })

    return {
        "id": str(result.inserted_id),
        **contact.dict(),
        "created_at": contact_doc["created_at"].isoformat(),
        "updated_at": contact_doc["updated_at"].isoformat(),
    }

@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str, user=Depends(get_current_user)):
    """Get single contact"""
    from app.config import database

    try:
        obj_id = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid contact ID")

    contact = await database.db.contacts.find_one({"_id": obj_id})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    return {
        "id": str(contact["_id"]),
        "name": contact.get("name", ""),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "company": contact.get("company", ""),
        "role": contact.get("role", ""),
        "industry": contact.get("industry", ""),
        "source": contact.get("source", "manual"),
        "status": contact.get("status", "new"),
        "notes": contact.get("notes", ""),
        "outreach_status": contact.get("outreach_status"),
        "outreach_summary": contact.get("outreach_summary"),
        "outreach_touches_sent": contact.get("outreach_touches_sent"),
        "outreach_next_followup_at": contact.get("outreach_next_followup_at").isoformat() if isinstance(contact.get("outreach_next_followup_at"), datetime) else contact.get("outreach_next_followup_at"),
        "outreach_last_sent_at": contact.get("outreach_last_sent_at").isoformat() if isinstance(contact.get("outreach_last_sent_at"), datetime) else contact.get("outreach_last_sent_at"),
        "created_at": contact.get("created_at", datetime.utcnow()).isoformat() if isinstance(contact.get("created_at"), datetime) else contact.get("created_at"),
        "updated_at": contact.get("updated_at", datetime.utcnow()).isoformat() if isinstance(contact.get("updated_at"), datetime) else contact.get("updated_at"),
    }

@router.get("/contacts/{contact_id}/export")
async def export_contact(contact_id: str, request: Request, user=Depends(get_current_user)):
    """
    GDPR Art. 15/20 — data subject access & portability. Returns the
    complete record we hold on this contact (all fields, no summary
    truncation) as a single downloadable JSON document. Admin-triggered:
    the operator runs this on behalf of a data subject request, since
    PashxD contacts don't have their own login to self-serve.
    """
    from app.config import database

    try:
        obj_id = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid contact ID")

    contact = await database.db.contacts.find_one({"_id": obj_id})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    def _serialize(doc: dict) -> dict:
        out = {}
        for k, v in doc.items():
            if k == "_id":
                out["id"] = str(v)
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, ObjectId):
                out[k] = str(v)
            else:
                out[k] = v
        return out

    deals = await database.db.deals.find({"contact_id": contact_id}).to_list(1000)
    activities = await database.db.activities.find({"contact_id": contact_id}).sort("created_at", -1).to_list(1000)

    email_logs = []
    contact_email = contact.get("email", "")
    if contact_email:
        email_logs = await database.db.email_logs.find(
            {"to_email": contact_email},
            {"body": 0},  # full HTML body omitted for size; subject/status/timestamps kept
        ).sort("created_at", -1).to_list(1000)

    await log_audit(
        request, user, action="export", resource_type="contact", resource_id=contact_id,
    )

    return {
        "export_generated_at": datetime.utcnow().isoformat(),
        "export_reason": "GDPR data subject access/portability request",
        "contact": _serialize(contact),
        "deals": [_serialize(d) for d in deals],
        "activities": [_serialize(a) for a in activities],
        "email_communications": [_serialize(e) for e in email_logs],
    }

@router.put("/contacts/{contact_id}")
async def update_contact(contact_id: str, updates: ContactUpdate, request: Request, user=Depends(get_current_user)):
    """Update contact"""
    from app.config import database

    try:
        obj_id = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid contact ID")

    contact = await database.db.contacts.find_one({"_id": obj_id})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Check if new email already exists
    if updates.email and updates.email != contact.get("email"):
        existing = await database.db.contacts.find_one({"email": updates.email})
        if existing:
            raise HTTPException(status_code=400, detail="Contact with this email already exists")

    update_data = {k: v for k, v in updates.dict().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow()

    result = await database.db.contacts.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")

    await log_audit(
        request, user, action="update", resource_type="contact",
        resource_id=contact_id, before=contact, after=update_data,
    )

    return {
        "success": True,
        "id": contact_id,
        "updated": update_data
    }

@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: str, request: Request, user=Depends(get_current_user)):
    """Delete contact"""
    from app.config import database

    try:
        obj_id = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid contact ID")

    contact = await database.db.contacts.find_one({"_id": obj_id})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Delete associated deals
    await database.db.deals.delete_many({"contact_id": contact_id})

    # Delete the contact
    result = await database.db.contacts.delete_one({"_id": obj_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")

    await log_audit(
        request, user, action="delete", resource_type="contact",
        resource_id=contact_id, before=contact,
    )

    await database.db.activities.insert_one({
        "type": "contact_deleted",
        "title": f"Contact deleted: {contact.get('name', 'Unknown')}",
        "description": f"Deleted {contact.get('name', 'Unknown')} ({contact.get('email', 'Unknown')})",
        "created_at": datetime.utcnow(),
    })

    return {"success": True, "message": "Contact deleted successfully"}

# ==================== DEALS/PIPELINE ====================

@router.get("/pipeline")
async def get_pipeline(user=Depends(get_current_user)):
    """Get all deals organized by stage"""
    from app.config import database

    deals = await database.db.deals.find({}).to_list(1000)

    pipeline = {
        "lead": [],
        "qualified": [],
        "proposal": [],
        "negotiation": [],
        "won": [],
        "lost": [],
    }

    # Batch-fetch every referenced contact in ONE query. The previous
    # per-deal find_one was an N+1 that took ~21s at 125 deals on Render.
    contact_oids = []
    for d in deals:
        cid = d.get("contact_id")
        if cid:
            try:
                contact_oids.append(ObjectId(cid))
            except Exception:
                pass
    contacts_by_id = {}
    if contact_oids:
        cursor = database.db.contacts.find(
            {"_id": {"$in": contact_oids}},
            {"name": 1, "email": 1, "company": 1},
        )
        async for c in cursor:
            contacts_by_id[str(c["_id"])] = c

    for deal in deals:
        contact = contacts_by_id.get(str(deal.get("contact_id", "")))

        deal_data = {
            "id": str(deal["_id"]),
            "title": deal.get("title", ""),
            "value": deal.get("value", 0),
            "currency": deal.get("currency", "EUR"),
            "stage": deal.get("stage", "lead"),
            "probability": deal.get("probability", 10),
            "notes": deal.get("notes", ""),
            "source": deal.get("source", "manual"),
            "created_at": deal.get("created_at", datetime.utcnow()).isoformat(),
            "updated_at": deal.get("updated_at", datetime.utcnow()).isoformat(),
            "contact": {
                "id": str(contact["_id"]) if contact else "",
                "name": contact.get("name", "") if contact else "",
                "email": contact.get("email", "") if contact else "",
                "company": contact.get("company", "") if contact else "",
            }
        }

        stage = deal.get("stage", "lead")
        if stage in pipeline:
            pipeline[stage].append(deal_data)

    return pipeline

@router.post("/deals")
async def create_deal(deal: DealCreate, user=Depends(get_current_user)):
    """Create new deal"""
    from app.config import database

    try:
        contact = await database.db.contacts.find_one({"_id": ObjectId(deal.contact_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid contact ID")

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    deal_doc = {
        **deal.dict(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    result = await database.db.deals.insert_one(deal_doc)

    await database.db.activities.insert_one({
        "type": "deal_created",
        "title": f"New deal: {deal.title}",
        "description": f"Deal created for {contact.get('name', 'Unknown')} - €{deal.value:,.2f}",
        "contact_id": deal.contact_id,
        "deal_id": str(result.inserted_id),
        "created_at": datetime.utcnow(),
    })

    return {
        "id": str(result.inserted_id),
        **deal.dict(),
        "contact_name": contact.get("name", ""),
        "contact_email": contact.get("email", ""),
        "company": contact.get("company", ""),
        "created_at": deal_doc["created_at"].isoformat(),
        "updated_at": deal_doc["updated_at"].isoformat(),
    }

@router.put("/deals/{deal_id}")
async def update_deal(deal_id: str, updates: DealUpdate, request: Request, user=Depends(get_current_user)):
    """Update deal stage, value, notes - uses PUT method for drag and drop"""
    from app.config import database

    try:
        obj_id = ObjectId(deal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deal ID")

    old_deal = await database.db.deals.find_one({"_id": obj_id})
    if not old_deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    update_data = {k: v for k, v in updates.dict().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow()

    # Track exact timestamp when deal moves to won (for accurate revenue trending)
    if update_data.get("stage") == "won" and old_deal.get("stage") != "won":
        update_data["won_at"] = datetime.utcnow()

    result = await database.db.deals.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Deal not found")

    if "stage" in update_data and update_data["stage"] != old_deal.get("stage"):
        await database.db.activities.insert_one({
            "type": "deal_stage_changed",
            "title": f"Deal moved to {update_data['stage']}",
            "description": f"{old_deal.get('title', 'Deal')} moved from {old_deal.get('stage', 'unknown')} to {update_data['stage']}",
            "contact_id": old_deal["contact_id"],
            "deal_id": deal_id,
            "created_at": datetime.utcnow(),
        })

    await log_audit(
        request, user, action="update", resource_type="deal",
        resource_id=deal_id, before=old_deal, after=update_data,
    )

    return {"success": True, "updated": update_data}

@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: str, request: Request, user=Depends(get_current_user)):
    """Delete deal"""
    from app.config import database

    try:
        obj_id = ObjectId(deal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deal ID")

    deal = await database.db.deals.find_one({"_id": obj_id})
    result = await database.db.deals.delete_one({"_id": obj_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deal not found")

    await log_audit(
        request, user, action="delete", resource_type="deal",
        resource_id=deal_id, before=deal,
    )

    return {"success": True}

@router.get("/deals/{deal_id}")
async def get_deal(deal_id: str, user=Depends(get_current_user)):
    """Get single deal with contact info"""
    from app.config import database

    try:
        obj_id = ObjectId(deal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deal ID")

    deal = await database.db.deals.find_one({"_id": obj_id})
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    contact = await database.db.contacts.find_one({"_id": ObjectId(deal["contact_id"])})

    return {
        "id": str(deal["_id"]),
        "title": deal.get("title", ""),
        "value": deal.get("value", 0),
        "currency": deal.get("currency", "EUR"),
        "stage": deal.get("stage", "lead"),
        "probability": deal.get("probability", 10),
        "notes": deal.get("notes", ""),
        "source": deal.get("source", "manual"),
        "created_at": deal.get("created_at", datetime.utcnow()).isoformat(),
        "updated_at": deal.get("updated_at", datetime.utcnow()).isoformat(),
        "contact": {
            "id": str(contact["_id"]) if contact else "",
            "name": contact.get("name", "") if contact else "",
            "email": contact.get("email", "") if contact else "",
            "company": contact.get("company", "") if contact else "",
        }
    }

# ==================== ACTIVITIES ====================

@router.get("/activities")
async def get_activities(limit: int = 100, user=Depends(get_current_user)):
    """Get recent activities"""
    from app.config import database

    try:
        activities = await database.db.activities.find({}).sort("created_at", -1).limit(limit).to_list(limit)

        return {
            "activities": [
                {
                    "id": str(a["_id"]),
                    "type": a.get("type", ""),
                    "title": a.get("title", ""),
                    "description": a.get("description", ""),
                    "contact_id": a.get("contact_id"),
                    "deal_id": a.get("deal_id"),
                    "created_at": a.get("created_at", datetime.utcnow()).isoformat(),
                }
                for a in activities
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/activities")
async def create_activity(activity: ActivityCreate, user=Depends(get_current_user)):
    """Create new activity log"""
    from app.config import database

    activity_doc = {
        **activity.dict(),
        "created_at": datetime.utcnow(),
    }

    result = await database.db.activities.insert_one(activity_doc)

    return {
        "id": str(result.inserted_id),
        **activity.dict(),
        "created_at": activity_doc["created_at"].isoformat(),
    }


# ==================== DASHBOARD STATS ====================

@router.get("/stats")
async def get_dashboard_stats(days: int = 30, user=Depends(get_current_user)):
    """
    Single aggregated endpoint for the Overview dashboard.
    Returns contacts, deals, revenue, and pre-bucketed trend data.
    """
    from app.config import database
    from datetime import timedelta

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)

    # Fetch deals, contacts, and today's activity count concurrently
    import asyncio
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    all_deals, all_contacts, activities_today = await asyncio.gather(
        database.db.deals.find({}).to_list(10000),
        database.db.contacts.find({}, {"created_at": 1}).to_list(10000),
        database.db.activities.count_documents({"created_at": {"$gte": today_start}}),
    )

    # ── Contacts ──────────────────────────────────────────────
    total_contacts = len(all_contacts)
    contacts_this_month = sum(
        1 for c in all_contacts
        if isinstance(c.get("created_at"), datetime) and c["created_at"] >= month_start
    )
    contacts_last_month = sum(
        1 for c in all_contacts
        if isinstance(c.get("created_at"), datetime)
        and last_month_start <= c["created_at"] < month_start
    )

    # ── Deals ─────────────────────────────────────────────────
    won_deals = [d for d in all_deals if d.get("stage") == "won"]
    lost_deals = [d for d in all_deals if d.get("stage") == "lost"]
    active_deals = [d for d in all_deals if d.get("stage") not in ("won", "lost")]

    by_stage = {}
    for d in all_deals:
        s = d.get("stage", "lead")
        by_stage[s] = by_stage.get(s, 0) + 1

    closed_count = len(won_deals) + len(lost_deals)
    win_rate = round(len(won_deals) / closed_count * 100) if closed_count > 0 else 0

    # ── Revenue ───────────────────────────────────────────────
    def deal_value(d):
        return float(d.get("value") or 0)

    def won_date(d):
        """Best timestamp for when deal was won."""
        return d.get("won_at") or d.get("updated_at") or d.get("created_at")

    total_revenue = sum(deal_value(d) for d in won_deals)
    pipeline_value = sum(deal_value(d) for d in active_deals)

    revenue_this_month = sum(
        deal_value(d) for d in won_deals
        if isinstance(won_date(d), datetime) and won_date(d) >= month_start
    )
    revenue_last_month = sum(
        deal_value(d) for d in won_deals
        if isinstance(won_date(d), datetime)
        and last_month_start <= won_date(d) < month_start
    )

    avg_deal_value = round(total_revenue / len(won_deals)) if won_deals else 0

    # ── Revenue trend (N days, pre-bucketed) ──────────────────
    window_start = now - timedelta(days=days)
    trend = []
    for i in range(days):
        day = window_start + timedelta(days=i)
        day_key = day.date()
        value = sum(
            deal_value(d) for d in won_deals
            if isinstance(won_date(d), datetime) and won_date(d).date() == day_key
        )
        trend.append({"date": day_key.isoformat(), "value": value})

    # ── MoM deltas ────────────────────────────────────────────
    def pct_change(curr, prev):
        if prev == 0:
            return None  # can't compute — insufficient history
        return round((curr - prev) / prev * 100, 1)

    return {
        "contacts": {
            "total": total_contacts,
            "this_month": contacts_this_month,
            "last_month": contacts_last_month,
            "mom_pct": pct_change(contacts_this_month, contacts_last_month),
        },
        "deals": {
            "total": len(all_deals),
            "active": len(active_deals),
            "won": len(won_deals),
            "lost": len(lost_deals),
            "by_stage": by_stage,
            "closing_soon": sum(1 for d in active_deals if d.get("stage") in ("negotiation", "proposal")),
        },
        "revenue": {
            "total": total_revenue,
            "this_month": revenue_this_month,
            "last_month": revenue_last_month,
            "pipeline": pipeline_value,
            "avg_deal": avg_deal_value,
            "mom_pct": pct_change(revenue_this_month, revenue_last_month),
        },
        "activities": {
            "today": activities_today,
        },
        "win_rate": win_rate,
        "trend": trend,
    }