from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from bson import ObjectId
import httpx
import os

router = APIRouter(prefix="/api/email", tags=["email"])

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "info@pashx.com")

# ==================== MODELS ====================

class EmailTemplate(BaseModel):
    name: str
    subject: str
    body: str  # HTML content
    category: str  # "proposal", "followup", "quote", "custom"
    variables: List[str] = []  # {{client_name}}, {{deal_value}}, etc
    is_default: bool = False

class EmailCampaign(BaseModel):
    deal_id: str
    contact_email: EmailStr
    template_id: str
    subject: str
    body: str
    sent_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None

class SendEmailRequest(BaseModel):
    deal_id: str
    contact_email: EmailStr
    contact_name: str
    template_id: str
    variables: dict = {}  # {"client_name": "John", "deal_value": "€50,000"}

# ==================== EMAIL TEMPLATES ====================

@router.get("/templates")
async def get_templates():
    """Get all email templates"""
    from app.config import database

    templates = await database.db.email_templates.find({}).to_list(100)

    return {
        "templates": [
            {
                "id": str(t["_id"]),
                "name": t.get("name", ""),
                "subject": t.get("subject", ""),
                "category": t.get("category", "custom"),
                "variables": t.get("variables", []),
                "is_default": t.get("is_default", False),
                "created_at": t.get("created_at", datetime.utcnow()).isoformat(),
            }
            for t in templates
        ]
    }

@router.post("/templates")
async def create_template(template: EmailTemplate):
    """Create new email template"""
    from app.config import database

    template_doc = {
        **template.dict(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    result = await database.db.email_templates.insert_one(template_doc)

    return {
        "id": str(result.inserted_id),
        **template.dict(),
        "created_at": template_doc["created_at"].isoformat(),
    }

@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """Get single template"""
    from app.config import database

    template = await database.db.email_templates.find_one({"_id": ObjectId(template_id)})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return {
        "id": str(template["_id"]),
        "name": template.get("name", ""),
        "subject": template.get("subject", ""),
        "body": template.get("body", ""),
        "category": template.get("category", "custom"),
        "variables": template.get("variables", []),
        "is_default": template.get("is_default", False),
    }

@router.put("/templates/{template_id}")
async def update_template(template_id: str, template: EmailTemplate):
    """Update email template"""
    from app.config import database

    try:
        obj_id = ObjectId(template_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid template ID")

    update_data = {
        **template.dict(),
        "updated_at": datetime.utcnow(),
    }

    result = await database.db.email_templates.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")

    return {"success": True, "updated": update_data}

@router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    """Delete email template"""
    from app.config import database

    try:
        obj_id = ObjectId(template_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid template ID")

    result = await database.db.email_templates.delete_one({"_id": obj_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")

    return {"success": True}

# ==================== SEND EMAIL ====================

@router.post("/send")
async def send_email(request: SendEmailRequest):
    """Send email from template"""
    from app.config import database

    # Get template
    template = await database.db.email_templates.find_one({"_id": ObjectId(request.template_id)})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Replace variables in subject and body
    subject = template.get("subject", "")
    body = template.get("body", "")

    for key, value in request.variables.items():
        subject = subject.replace(f"{{{{{key}}}}}", str(value))
        body = body.replace(f"{{{{{key}}}}}", str(value))

    # Send via SendGrid
    if SENDGRID_API_KEY:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {SENDGRID_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "personalizations": [
                            {
                                "to": [{"email": request.contact_email, "name": request.contact_name}],
                                "subject": subject,
                            }
                        ],
                        "from": {"email": SENDGRID_FROM_EMAIL, "name": "PashxD"},
                        "content": [
                            {
                                "type": "text/html",
                                "value": body
                            }
                        ],
                        "tracking_settings": {
                            "open": {"enable": True},
                            "click": {"enable": True}
                        }
                    }
                )

                if response.status_code not in [200, 201, 202]:
                    raise HTTPException(status_code=500, detail="Failed to send email")

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SendGrid error: {str(e)}")
    else:
        # Fallback: just log it (for testing)
        print(f"📧 Email to {request.contact_email}: {subject}")

    # Log campaign
    campaign = {
        "deal_id": request.deal_id,
        "contact_email": request.contact_email,
        "template_id": request.template_id,
        "subject": subject,
        "body": body,
        "sent_at": datetime.utcnow(),
        "opened_at": None,
        "clicked_at": None,
        "status": "sent",
    }

    result = await database.db.email_campaigns.insert_one(campaign)

    return {
        "success": True,
        "campaign_id": str(result.inserted_id),
        "sent_to": request.contact_email,
        "sent_at": campaign["sent_at"].isoformat(),
    }

# ==================== EMAIL TRACKING ====================

@router.get("/campaigns/{deal_id}")
async def get_deal_emails(deal_id: str):
    """Get all emails sent for a deal"""
    from app.config import database

    campaigns = await database.db.email_campaigns.find({"deal_id": deal_id}).to_list(100)

    return {
        "campaigns": [
            {
                "id": str(c["_id"]),
                "contact_email": c.get("contact_email", ""),
                "subject": c.get("subject", ""),
                "status": c.get("status", "sent"),
                "sent_at": c.get("sent_at", datetime.utcnow()).isoformat(),
                "opened_at": c.get("opened_at", "").isoformat() if c.get("opened_at") else None,
                "clicked_at": c.get("clicked_at", "").isoformat() if c.get("clicked_at") else None,
            }
            for c in campaigns
        ]
    }

@router.get("/stats")
async def get_email_stats():
    """Get email campaign statistics"""
    from app.config import database

    total = await database.db.email_campaigns.count_documents({})
    opened = await database.db.email_campaigns.count_documents({"opened_at": {"$exists": True, "$ne": None}})
    clicked = await database.db.email_campaigns.count_documents({"clicked_at": {"$exists": True, "$ne": None}})

    return {
        "total_sent": total,
        "total_opened": opened,
        "total_clicked": clicked,
        "open_rate": round((opened / total * 100), 2) if total > 0 else 0,
        "click_rate": round((clicked / total * 100), 2) if total > 0 else 0,
    }