from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.schemas import (
    ContactCreate, ContactUpdate,
    DealCreate, DealUpdate,
    ActivityCreate
)
from app.middleware.auth import require_admin
from app.config.database import get_db
from bson import ObjectId
from datetime import datetime

router = APIRouter(prefix="/api/crm", tags=["CRM"])


def fmt(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


# ── CONTACTS ──────────────────────────────────────────────

@router.get("/contacts")
async def list_contacts(
        search: str = None,
        status: str = None,
        limit: int = Query(20, le=100),
        skip: int = 0,
        user=Depends(require_admin),
):
    db = get_db()
    query = {}
    if status:
        query["status"] = status
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"company": {"$regex": search, "$options": "i"}},
        ]

    cursor = db.contacts.find(query).sort("created_at", -1).skip(skip).limit(limit)
    contacts = await cursor.to_list(length=limit)
    total = await db.contacts.count_documents(query)

    return {
        "contacts": [fmt(c) for c in contacts],
        "total": total,
    }


@router.get("/contacts/{id}")
async def get_contact(id: str, user=Depends(require_admin)):
    db = get_db()
    doc = await db.contacts.find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Attach deals + activities
    deals = await db.deals.find({"contact_id": id}).to_list(length=50)
    activities = await db.activities.find({"contact_id": id}).sort("created_at", -1).to_list(length=50)

    contact = fmt(doc)
    contact["deals"] = [fmt(d) for d in deals]
    contact["activities"] = [fmt(a) for a in activities]
    return contact


@router.post("/contacts")
async def create_contact(body: ContactCreate, user=Depends(require_admin)):
    db = get_db()
    now = datetime.utcnow()
    doc = {**body.model_dump(), "created_at": now, "updated_at": now}
    result = await db.contacts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return fmt(doc)


@router.put("/contacts/{id}")
async def update_contact(id: str, body: ContactUpdate, user=Depends(require_admin)):
    db = get_db()
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow()

    result = await db.contacts.update_one({"_id": ObjectId(id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")

    doc = await db.contacts.find_one({"_id": ObjectId(id)})
    return fmt(doc)


@router.delete("/contacts/{id}")
async def delete_contact(id: str, user=Depends(require_admin)):
    db = get_db()
    result = await db.contacts.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")
    # Clean up related deals + activities
    await db.deals.delete_many({"contact_id": id})
    await db.activities.delete_many({"contact_id": id})
    return {"message": "Contact and related records deleted"}


# ── DEALS / PIPELINE ──────────────────────────────────────

@router.get("/deals")
async def list_deals(
        stage: str = None,
        contact_id: str = None,
        user=Depends(require_admin),
):
    db = get_db()
    query = {}
    if stage:
        query["stage"] = stage
    if contact_id:
        query["contact_id"] = contact_id

    deals = await db.deals.find(query).sort("created_at", -1).to_list(length=200)

    # Attach contact name
    for d in deals:
        contact = await db.contacts.find_one(
            {"_id": ObjectId(d["contact_id"])},
            {"name": 1}
        )
        d["contact_name"] = contact["name"] if contact else "Unknown"

    return {"deals": [fmt(d) for d in deals]}


@router.get("/pipeline")
async def get_pipeline(user=Depends(require_admin)):
    """Returns deals grouped by stage for kanban board"""
    db = get_db()
    stages = ["lead", "qualified", "proposal", "negotiation", "won", "lost"]
    pipeline = {}

    for stage in stages:
        deals = await db.deals.find({"stage": stage}).sort("updated_at", -1).to_list(length=100)
        for d in deals:
            contact = await db.contacts.find_one(
                {"_id": ObjectId(d["contact_id"])}, {"name": 1}
            )
            d["contact_name"] = contact["name"] if contact else "Unknown"
        pipeline[stage] = [fmt(d) for d in deals]

    return pipeline


@router.post("/deals")
async def create_deal(body: DealCreate, user=Depends(require_admin)):
    db = get_db()
    # Verify contact exists
    contact = await db.contacts.find_one({"_id": ObjectId(body.contact_id)})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    now = datetime.utcnow()
    doc = {**body.model_dump(), "created_at": now, "updated_at": now}
    result = await db.deals.insert_one(doc)
    doc["_id"] = result.inserted_id
    doc["contact_name"] = contact["name"]
    return fmt(doc)


@router.put("/deals/{id}")
async def update_deal(id: str, body: DealUpdate, user=Depends(require_admin)):
    db = get_db()
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow()

    result = await db.deals.update_one({"_id": ObjectId(id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Deal not found")

    doc = await db.deals.find_one({"_id": ObjectId(id)})
    contact = await db.contacts.find_one(
        {"_id": ObjectId(doc["contact_id"])}, {"name": 1}
    )
    doc["contact_name"] = contact["name"] if contact else "Unknown"
    return fmt(doc)


@router.delete("/deals/{id}")
async def delete_deal(id: str, user=Depends(require_admin)):
    db = get_db()
    result = await db.deals.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deal not found")
    return {"message": "Deal deleted"}


# ── ACTIVITY LOG ──────────────────────────────────────────

@router.get("/activities")
async def list_activities(
        contact_id: str = None,
        limit: int = Query(20, le=100),
        user=Depends(require_admin),
):
    db = get_db()
    query = {}
    if contact_id:
        query["contact_id"] = contact_id

    activities = await db.activities.find(query).sort("created_at", -1).limit(limit).to_list(length=limit)

    for a in activities:
        contact = await db.contacts.find_one(
            {"_id": ObjectId(a["contact_id"])}, {"name": 1}
        )
        a["contact_name"] = contact["name"] if contact else "Unknown"

    return {"activities": [fmt(a) for a in activities]}


@router.post("/activities")
async def log_activity(body: ActivityCreate, user=Depends(require_admin)):
    db = get_db()
    contact = await db.contacts.find_one({"_id": ObjectId(body.contact_id)})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    doc = {**body.model_dump(), "created_at": datetime.utcnow()}
    result = await db.activities.insert_one(doc)
    doc["_id"] = result.inserted_id
    doc["contact_name"] = contact["name"]
    return fmt(doc)


@router.delete("/activities/{id}")
async def delete_activity(id: str, user=Depends(require_admin)):
    db = get_db()
    result = await db.activities.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Activity not found")
    return {"message": "Activity deleted"}