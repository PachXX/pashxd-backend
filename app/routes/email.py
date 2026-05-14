from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timedelta
from bson import ObjectId
import httpx
import os
import csv
import io
import uuid
import logging
import re

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/email", tags=["email"])

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "info@pashx.com")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME", "PashxD")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Global db reference - will be set by app.main in lifespan
_db = None

def set_db(database):
    """Called by main.py lifespan to set the database instance"""
    global _db
    _db = database

def get_db():
    """Get the database instance"""
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized - lifespan may not have completed")
    return _db

# ==================== HELPERS ====================

def obj_id(id_str: str):
    """Convert string to ObjectId with validation"""
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid ID: {id_str}")

def serialize_doc(doc, fields=None):
    """Convert MongoDB doc to JSON-safe dict"""
    if doc is None:
        return None
    result = {"id": str(doc["_id"])}
    for key, val in doc.items():
        if key == "_id":
            continue
        if isinstance(val, datetime):
            result[key] = val.isoformat()
        elif isinstance(val, ObjectId):
            result[key] = str(val)
        else:
            result[key] = val
    return result

def extract_contact_variables(contact: dict) -> dict:
    """
    Extract all available variables from a contact document.
    Handles both 'name' and 'first_name'/'last_name' formats.
    """
    # Get first name
    first_name = contact.get("first_name", "")
    if not first_name and contact.get("name"):
        # If no first_name, try to split from name field
        parts = contact.get("name", "").split()
        first_name = parts[0] if parts else ""

    # Get last name
    last_name = contact.get("last_name", "")
    if not last_name and contact.get("name"):
        # If no last_name, try to extract from name field
        parts = contact.get("name", "").split()
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Get full name
    full_name = contact.get("full_name", "")
    if not full_name:
        if first_name and last_name:
            full_name = f"{first_name} {last_name}"
        elif first_name or last_name:
            full_name = f"{first_name} {last_name}".strip()
        else:
            full_name = contact.get("name", "")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "company_name": contact.get("company_name") or contact.get("company") or "",
        "job_title": contact.get("job_title") or contact.get("role") or "",
        "email": contact.get("email", ""),
        "meeting_date": contact.get("meeting_date", ""),
        "deal_value": contact.get("deal_value", ""),
    }

def replace_variables_in_text(text: str, variables: dict) -> str:
    """
    Replace all {{variable}} placeholders with actual values.
    Handles both single and double braces.
    """
    if not text:
        return text

    result = text
    for key, value in variables.items():
        if value is None:
            value = ""
        value_str = str(value).strip()
        # Replace {{key}} format
        result = result.replace(f"{{{{{key}}}}}", value_str)
        # Also replace {{key}} with single braces (just in case)
        result = result.replace(f"{{{key}}}", value_str)

    return result

def inject_tracking(html_body: str, campaign_id: str, contact_email: str) -> str:
    """Inject open-tracking pixel and wrap links for click tracking"""
    # 1) Open tracking pixel
    pixel_url = f"{BACKEND_URL}/api/email/track/open/{campaign_id}?email={contact_email}"
    pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="" />'
    if "</body>" in html_body.lower():
        html_body = html_body.replace("</body>", f"{pixel}</body>")
    else:
        html_body += pixel

    # 2) Click tracking — wrap all href links
    def replace_link(match):
        original_url = match.group(1)
        if "track/open" in original_url or "track/click" in original_url:
            return match.group(0)
        tracked = f'{BACKEND_URL}/api/email/track/click/{campaign_id}?email={contact_email}&url={original_url}'
        return f'href="{tracked}"'

    html_body = re.sub(r'href="([^"]+)"', replace_link, html_body)
    return html_body

# ==================== MODELS ====================

class EmailTemplate(BaseModel):
    name: str
    subject: str
    body: str
    category: str = "custom"
    variables: List[str] = []
    is_default: bool = False

class SendEmailRequest(BaseModel):
    to_email: EmailStr
    to_name: str = ""
    cc: Optional[str] = ""
    bcc: Optional[str] = ""
    subject: str
    body: str
    template_id: Optional[str] = None
    deal_id: Optional[str] = None
    campaign_id: Optional[str] = None
    variables: dict = {}

class CampaignCreate(BaseModel):
    name: str
    template_id: str
    audience: str = "all"
    selected_contact_ids: List[str] = []
    schedule_at: Optional[str] = None
    status: str = "draft"

class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    schedule_at: Optional[str] = None

class AutomationCreate(BaseModel):
    name: str
    trigger: str
    template_id: str
    delay_minutes: int = 0
    status: str = "draft"

class AutomationUpdate(BaseModel):
    name: Optional[str] = None
    trigger: Optional[str] = None
    template_id: Optional[str] = None
    delay_minutes: Optional[int] = None
    status: Optional[str] = None

class ContactTagRequest(BaseModel):
    contact_ids: List[str]
    tags: List[str] = []
    list_name: Optional[str] = None

# ==================== EMAIL TEMPLATES ====================

@router.get("/templates")
async def get_templates():
    """Get all email templates"""
    db = get_db()
    templates = await db.db.email_templates.find({}).sort("created_at", -1).to_list(200)
    return {"templates": [serialize_doc(t) for t in templates]}

@router.post("/templates")
async def create_template(template: EmailTemplate):
    """Create new email template"""
    db = get_db()
    doc = {
        **template.dict(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = await db.db.email_templates.insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize_doc(doc)

@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """Get single template"""
    db = get_db()
    template = await db.db.email_templates.find_one({"_id": obj_id(template_id)})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return serialize_doc(template)

@router.put("/templates/{template_id}")
async def update_template(template_id: str, template: EmailTemplate):
    """Update email template"""
    db = get_db()
    update_data = {**template.dict(), "updated_at": datetime.utcnow()}
    result = await db.db.email_templates.update_one(
        {"_id": obj_id(template_id)}, {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    updated = await db.db.email_templates.find_one({"_id": obj_id(template_id)})
    return serialize_doc(updated)

@router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    """Delete email template"""
    db = get_db()
    result = await db.db.email_templates.delete_one({"_id": obj_id(template_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"success": True}

# ==================== SEND SINGLE EMAIL ====================

@router.post("/send")
async def send_email(request: SendEmailRequest):
    """Send a single email via SendGrid with tracking"""
    db = get_db()

    # Replace variables in subject and body
    subject = request.subject
    body = request.body
    for key, value in request.variables.items():
        subject = replace_variables_in_text(subject, {key: value})
        body = replace_variables_in_text(body, {key: value})

    # Create email log entry first so we have an ID for tracking
    email_log = {
        "to_email": request.to_email,
        "to_name": request.to_name,
        "cc": request.cc,
        "bcc": request.bcc,
        "subject": subject,
        "body": body,
        "template_id": request.template_id,
        "deal_id": request.deal_id,
        "campaign_id": request.campaign_id,
        "variables": request.variables,
        "status": "pending",
        "sent_at": None,
        "opened_at": None,
        "clicked_at": None,
        "open_count": 0,
        "click_count": 0,
        "created_at": datetime.utcnow(),
    }
    result = await db.db.email_logs.insert_one(email_log)
    log_id = str(result.inserted_id)

    # Inject tracking pixel and click tracking
    tracked_body = inject_tracking(body, log_id, request.to_email)

    # Send via SendGrid
    sent = False
    error_msg = None
    if SENDGRID_API_KEY:
        try:
            personalizations = {
                "to": [{"email": request.to_email, "name": request.to_name or request.to_email}],
                "subject": subject,
            }
            if request.cc:
                personalizations["cc"] = [{"email": e.strip()} for e in request.cc.split(",") if e.strip()]
            if request.bcc:
                personalizations["bcc"] = [{"email": e.strip()} for e in request.bcc.split(",") if e.strip()]

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {SENDGRID_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "personalizations": [personalizations],
                        "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
                        "content": [{"type": "text/html", "value": tracked_body}],
                        "tracking_settings": {
                            "open_tracking": {"enable": False},
                            "click_tracking": {"enable": False},
                        },
                    },
                )
                if response.status_code in [200, 201, 202]:
                    sent = True
                else:
                    error_msg = f"SendGrid returned {response.status_code}"
        except Exception as e:
            error_msg = str(e)
    else:
        # No API key - log as sent for testing
        sent = True
        error_msg = "SENDGRID_API_KEY not configured - logged locally"

    # Update log
    now = datetime.utcnow()
    status = "sent" if sent else "failed"
    await db.db.email_logs.update_one(
        {"_id": result.inserted_id},
        {"$set": {"status": status, "sent_at": now if sent else None, "error": error_msg}},
    )

    # If part of a campaign, update campaign counters
    if request.campaign_id:
        await db.db.email_campaigns.update_one(
            {"_id": obj_id(request.campaign_id)},
            {"$inc": {"sent_count": 1 if sent else 0, "failed_count": 0 if sent else 1}},
        )

    return {
        "success": sent,
        "email_log_id": log_id,
        "status": status,
        "sent_at": now.isoformat() if sent else None,
        "error": error_msg,
    }

# ==================== TRACKING ====================

TRACKING_PIXEL = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00,
    0x01, 0x00, 0x80, 0x00, 0x00, 0xFF, 0xFF, 0xFF,
    0x00, 0x00, 0x00, 0x21, 0xF9, 0x04, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x2C, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
    0x01, 0x00, 0x3B,
])

@router.get("/track/open/{email_log_id}")
async def track_open(email_log_id: str, email: str = ""):
    """Track email open via invisible pixel"""
    db = get_db()
    try:
        now = datetime.utcnow()
        await db.db.email_logs.update_one(
            {"_id": obj_id(email_log_id)},
            {
                "$set": {"opened_at": now},
                "$inc": {"open_count": 1},
            },
        )
        await db.db.email_events.insert_one({
            "email_log_id": email_log_id,
            "type": "open",
            "email": email,
            "timestamp": now,
        })
    except Exception as e:
        logger.error(f"Track open error: {e}")
    return Response(content=TRACKING_PIXEL, media_type="image/gif")

@router.get("/track/click/{email_log_id}")
async def track_click(email_log_id: str, email: str = "", url: str = ""):
    """Track email click and redirect"""
    db = get_db()
    try:
        now = datetime.utcnow()
        await db.db.email_logs.update_one(
            {"_id": obj_id(email_log_id)},
            {
                "$set": {"clicked_at": now},
                "$inc": {"click_count": 1},
            },
        )
        await db.db.email_events.insert_one({
            "email_log_id": email_log_id,
            "type": "click",
            "email": email,
            "url": url,
            "timestamp": now,
        })
    except Exception as e:
        logger.error(f"Track click error: {e}")

    if url:
        return RedirectResponse(url=url)
    return {"tracked": True}

# ==================== LOGS & ACTIVITY ====================

@router.get("/logs")
async def get_email_logs(
        limit: int = Query(50, le=200),
        skip: int = Query(0),
        status: Optional[str] = None,
        campaign_id: Optional[str] = None,
):
    """Get email logs"""
    db = get_db()
    query = {}
    if status:
        query["status"] = status
    if campaign_id:
        query["campaign_id"] = campaign_id

    total = await db.db.email_logs.count_documents(query)
    logs = await db.db.email_logs.find(query).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return {"logs": [serialize_doc(l) for l in logs], "total": total}

@router.get("/activity")
async def get_recent_activity(limit: int = Query(20, le=50)):
    """Get recent email events"""
    db = get_db()
    events = await db.db.email_events.find({}).sort("timestamp", -1).limit(limit).to_list(limit)

    activity = [
        {
            "type": e.get("type"),
            "email": e.get("email", ""),
            "email_log_id": e.get("email_log_id", ""),
            "url": e.get("url", ""),
            "timestamp": e.get("timestamp", datetime.utcnow()).isoformat(),
        }
        for e in events
    ]

    return {"activity": activity}

# ==================== STATS ====================

@router.get("/stats")
async def get_email_stats():
    """Get comprehensive email statistics"""
    db = get_db()

    total = await db.db.email_logs.count_documents({})
    sent = await db.db.email_logs.count_documents({"status": "sent"})
    failed = await db.db.email_logs.count_documents({"status": "failed"})
    opened = await db.db.email_logs.count_documents({"opened_at": {"$ne": None}})
    clicked = await db.db.email_logs.count_documents({"clicked_at": {"$ne": None}})

    # Monthly data for chart (last 6 months)
    six_months_ago = datetime.utcnow() - timedelta(days=180)
    monthly_pipeline = [
        {"$match": {"created_at": {"$gte": six_months_ago}}},
        {
            "$group": {
                "_id": {
                    "year": {"$year": "$created_at"},
                    "month": {"$month": "$created_at"},
                },
                "sent": {"$sum": {"$cond": [{"$eq": ["$status", "sent"]}, 1, 0]}},
                "opened": {"$sum": {"$cond": [{"$ne": ["$opened_at", None]}, 1, 0]}},
                "clicked": {"$sum": {"$cond": [{"$ne": ["$clicked_at", None]}, 1, 0]}},
            }
        },
        {"$sort": {"_id.year": 1, "_id.month": 1}},
    ]
    monthly_data = await db.db.email_logs.aggregate(monthly_pipeline).to_list(12)
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    chart_data = [
        {
            "month": month_names[m["_id"]["month"]],
            "year": m["_id"]["year"],
            "sent": m["sent"],
            "opened": m["opened"],
            "clicked": m["clicked"],
        }
        for m in monthly_data
    ]

    return {
        "total_sent": sent,
        "total_opened": opened,
        "total_clicked": clicked,
        "open_rate": round((opened / sent * 100), 2) if sent > 0 else 0,
        "click_rate": round((clicked / sent * 100), 2) if sent > 0 else 0,
        "bounce_rate": round((failed / total * 100), 2) if total > 0 else 0,
        "unique_opened": opened,
        "chart_data": chart_data,
    }

# ==================== CAMPAIGNS ====================

@router.get("/campaigns")
async def get_campaigns():
    db = get_db()
    campaigns = await db.db.email_campaigns.find({}).sort("created_at", -1).to_list(200)
    return {"campaigns": [serialize_doc(c) for c in campaigns]}

@router.post("/campaigns")
async def create_campaign(campaign: CampaignCreate):
    db = get_db()
    template = await db.db.email_templates.find_one({"_id": obj_id(campaign.template_id)})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    doc = {
        "name": campaign.name,
        "template_id": campaign.template_id,
        "template_name": template.get("name", ""),
        "audience": campaign.audience,
        "selected_contact_ids": campaign.selected_contact_ids,
        "schedule_at": campaign.schedule_at,
        "status": campaign.status,
        "sent_count": 0,
        "opened_count": 0,
        "clicked_count": 0,
        "failed_count": 0,
        "total_recipients": 0,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = await db.db.email_campaigns.insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize_doc(doc)

@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    db = get_db()
    result = await db.db.email_campaigns.delete_one({"_id": obj_id(campaign_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await db.db.email_logs.delete_many({"campaign_id": campaign_id})
    return {"success": True}

@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(campaign_id: str):
    """Execute a campaign - with proper variable replacement"""
    db = get_db()
    campaign = await db.db.email_campaigns.find_one({"_id": obj_id(campaign_id)})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    template = await db.db.email_templates.find_one({"_id": obj_id(campaign.get("template_id", ""))})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Resolve audience
    audience = campaign.get("audience", "all")
    contact_query = {}
    if audience.startswith("tag:"):
        tag = audience.split(":", 1)[1]
        contact_query = {"tags": tag}
    elif audience.startswith("list:"):
        list_name = audience.split(":", 1)[1]
        contact_query = {"lists": list_name}
    elif audience == "selected":
        ids = campaign.get("selected_contact_ids", [])
        if ids:
            contact_query = {"_id": {"$in": [obj_id(i) for i in ids]}}

    contacts = await db.db.contacts.find(contact_query).to_list(10000)
    if not contacts:
        return {"success": False, "error": "No contacts found", "sent": 0, "failed": 0}

    await db.db.email_campaigns.update_one(
        {"_id": obj_id(campaign_id)},
        {"$set": {"status": "sending", "total_recipients": len(contacts), "updated_at": datetime.utcnow()}},
    )

    sent_count = 0
    failed_count = 0

    for contact in contacts:
        contact_email = contact.get("email", "")
        if not contact_email:
            failed_count += 1
            continue

        # 🔥 FIX: Use the new extract_contact_variables function
        variables = extract_contact_variables(contact)

        # Get template content
        subject = template.get("subject", "")
        body = template.get("body", "")

        # Replace variables using the new function
        subject = replace_variables_in_text(subject, variables)
        body = replace_variables_in_text(body, variables)

        email_log = {
            "to_email": contact_email,
            "to_name": variables.get("full_name", ""),
            "subject": subject,
            "body": body,
            "template_id": campaign.get("template_id", ""),
            "campaign_id": campaign_id,
            "status": "pending",
            "sent_at": None,
            "opened_at": None,
            "clicked_at": None,
            "open_count": 0,
            "click_count": 0,
            "created_at": datetime.utcnow(),
        }
        log_result = await db.db.email_logs.insert_one(email_log)
        log_id = str(log_result.inserted_id)

        tracked_body = inject_tracking(body, log_id, contact_email)

        # Send via SendGrid
        if SENDGRID_API_KEY:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        "https://api.sendgrid.com/v3/mail/send",
                        headers={
                            "Authorization": f"Bearer {SENDGRID_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "personalizations": [
                                {"to": [{"email": contact_email, "name": variables.get("full_name", "")}], "subject": subject}
                            ],
                            "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
                            "content": [{"type": "text/html", "value": tracked_body}],
                        },
                    )
                    if response.status_code in [200, 201, 202]:
                        await db.db.email_logs.update_one(
                            {"_id": log_result.inserted_id},
                            {"$set": {"status": "sent", "sent_at": datetime.utcnow()}},
                        )
                        sent_count += 1
                    else:
                        await db.db.email_logs.update_one(
                            {"_id": log_result.inserted_id},
                            {"$set": {"status": "failed", "error": response.text}},
                        )
                        failed_count += 1
            except Exception as e:
                await db.db.email_logs.update_one(
                    {"_id": log_result.inserted_id},
                    {"$set": {"status": "failed", "error": str(e)}},
                )
                failed_count += 1
        else:
            await db.db.email_logs.update_one(
                {"_id": log_result.inserted_id},
                {"$set": {"status": "sent", "sent_at": datetime.utcnow()}},
            )
            sent_count += 1

    final_status = "completed"
    await db.db.email_campaigns.update_one(
        {"_id": obj_id(campaign_id)},
        {
            "$set": {
                "status": final_status,
                "sent_count": sent_count,
                "failed_count": failed_count,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    return {
        "success": True,
        "campaign_id": campaign_id,
        "total_recipients": len(contacts),
        "sent": sent_count,
        "failed": failed_count,
        "status": final_status,
    }

# ==================== AUTOMATIONS ====================

@router.get("/automations")
async def get_automations():
    db = get_db()
    automations = await db.db.email_automations.find({}).sort("created_at", -1).to_list(100)
    result = []
    for a in automations:
        doc = serialize_doc(a)
        if a.get("template_id"):
            tmpl = await db.db.email_templates.find_one({"_id": obj_id(a["template_id"])})
            doc["template_name"] = tmpl.get("name", "") if tmpl else ""
        count = await db.db.email_logs.count_documents({"automation_id": str(a["_id"])})
        doc["emails_sent"] = count
        result.append(doc)
    return {"automations": result}

@router.post("/automations")
async def create_automation(automation: AutomationCreate):
    db = get_db()
    template = await db.db.email_templates.find_one({"_id": obj_id(automation.template_id)})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    doc = {
        "name": automation.name,
        "trigger": automation.trigger,
        "template_id": automation.template_id,
        "template_name": template.get("name", ""),
        "delay_minutes": automation.delay_minutes,
        "status": automation.status,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = await db.db.email_automations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize_doc(doc)

@router.delete("/automations/{automation_id}")
async def delete_automation(automation_id: str):
    db = get_db()
    result = await db.db.email_automations.delete_one({"_id": obj_id(automation_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Automation not found")
    return {"success": True}

# ==================== CONTACTS ====================

@router.get("/contacts")
async def get_email_contacts(
        search: str = "",
        tag: str = "",
        limit: int = Query(100, le=500),
        skip: int = 0,
):
    """Get contacts for email"""
    db = get_db()
    query = {}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"company": {"$regex": search, "$options": "i"}},
        ]
    if tag:
        query["tags"] = tag

    total = await db.db.contacts.count_documents(query)
    contacts = await db.db.contacts.find(query).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return {"contacts": [serialize_doc(c) for c in contacts], "total": total}

@router.post("/contacts/import-csv")
async def import_contacts_csv(file: UploadFile = File(...)):
    """Import contacts from CSV"""
    db = get_db()
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    imported = 0
    skipped = 0
    errors = []

    for row in reader:
        email = row.get("email", "").strip()
        if not email:
            skipped += 1
            continue

        existing = await db.db.contacts.find_one({"email": email})
        if existing:
            skipped += 1
            continue

        try:
            contact_doc = {
                "name": row.get("name", "").strip(),
                "email": email,
                "phone": row.get("phone", "").strip(),
                "company": row.get("company", "").strip(),
                "role": row.get("role", row.get("job_title", "")).strip(),
                "industry": row.get("industry", "").strip(),
                "tags": [t.strip() for t in row.get("tags", "").split(",") if t.strip()],
                "lists": ["Imported"],
                "source": "csv_import",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            await db.db.contacts.insert_one(contact_doc)
            imported += 1
        except Exception as e:
            errors.append(f"Row {imported + skipped + 1}: {str(e)}")

    return {"imported": imported, "skipped": skipped, "errors": errors}

@router.get("/contacts/lists")
async def get_contact_lists():
    """Get all distinct lists and tags"""
    db = get_db()
    tags = await db.db.contacts.distinct("tags")
    lists = await db.db.contacts.distinct("lists")
    return {"tags": [t for t in tags if t], "lists": [l for l in lists if l]}