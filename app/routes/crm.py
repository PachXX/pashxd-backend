from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
from datetime import datetime
from bson import ObjectId

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
async def get_contacts():
    """Get all contacts"""
    from app.config import database

    contacts = await database.db.contacts.find({}).to_list(1000)

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
                "created_at": c.get("created_at", datetime.utcnow()).isoformat(),
            }
            for c in contacts
        ]
    }

@router.post("/contacts")
async def create_contact(contact: ContactCreate):
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
    }

@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str):
    """Get single contact"""
    from app.config import database

    contact = await database.db.contacts.find_one({"_id": ObjectId(contact_id)})
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
        "created_at": contact.get("created_at", datetime.utcnow()).isoformat(),
    }

# ==================== DEALS/PIPELINE ====================

@router.get("/pipeline")
async def get_pipeline():
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

    for deal in deals:
        contact = await database.db.contacts.find_one({"_id": ObjectId(deal["contact_id"])})

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
async def create_deal(deal: DealCreate):
    """Create new deal"""
    from app.config import database

    contact = await database.db.contacts.find_one({"_id": ObjectId(deal.contact_id)})
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
async def update_deal(deal_id: str, updates: DealUpdate):
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

    return {"success": True, "updated": update_data}

@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: str):
    """Delete deal"""
    from app.config import database

    try:
        obj_id = ObjectId(deal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deal ID")

    result = await database.db.deals.delete_one({"_id": obj_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deal not found")

    return {"success": True}

@router.get("/deals/{deal_id}")
async def get_deal(deal_id: str):
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
async def get_activities(limit: int = 100):
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
async def create_activity(activity: ActivityCreate):
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