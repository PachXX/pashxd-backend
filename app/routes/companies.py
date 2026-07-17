from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from bson import ObjectId
from app.middleware.auth import get_current_user
from app.services.company_resolver import resolve_company_key

router = APIRouter(prefix="/api/crm/companies", tags=["companies"])

# ==================== MODELS ====================

class CompanyCreate(BaseModel):
    name: str
    domain: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    notes: Optional[str] = ""

class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    notes: Optional[str] = None

STAGE_RANK = {"lead": 0, "qualified": 1, "proposal": 2, "negotiation": 3, "won": 4}


def _serialize_company(c: dict, contact_count: int = 0, deal_count: int = 0,
                        open_deal_value: float = 0, won_value: float = 0, stage: Optional[str] = None) -> dict:
    return {
        "id": str(c["_id"]),
        "name": c.get("name", ""),
        "domain": c.get("domain"),
        "industry": c.get("industry"),
        "website": c.get("website"),
        "notes": c.get("notes", ""),
        "is_singleton": c.get("is_singleton", False),
        "contact_count": contact_count,
        "deal_count": deal_count,
        "open_deal_value": open_deal_value,
        "won_value": won_value,
        "stage": stage,
        "created_at": c.get("created_at", datetime.utcnow()).isoformat() if isinstance(c.get("created_at"), datetime) else c.get("created_at"),
        "updated_at": c.get("updated_at", datetime.utcnow()).isoformat() if isinstance(c.get("updated_at"), datetime) else c.get("updated_at"),
    }


# ==================== COMPANIES ====================

@router.get("")
async def list_companies(search: Optional[str] = None, limit: int = 200, user=Depends(get_current_user)):
    """List companies with rollup stats — batch-fetched to avoid N+1, same
    pattern as get_pipeline's contact batching."""
    from app.config import database

    query = {}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"domain": {"$regex": search, "$options": "i"}},
        ]

    companies = await database.db.companies.find(query).limit(limit).to_list(limit)
    company_ids = [str(c["_id"]) for c in companies]

    contacts_by_company = {}
    if company_ids:
        cursor = database.db.contacts.find({"company_id": {"$in": company_ids}}, {"company_id": 1})
        async for c in cursor:
            contacts_by_company.setdefault(c["company_id"], []).append(c)

    deals_by_company = {}
    if company_ids:
        cursor = database.db.deals.find({"company_id": {"$in": company_ids}}, {"company_id": 1, "stage": 1, "value": 1})
        async for d in cursor:
            deals_by_company.setdefault(d["company_id"], []).append(d)

    result = []
    for c in companies:
        cid = str(c["_id"])
        deals = deals_by_company.get(cid, [])
        live_deals = [d for d in deals if d.get("stage") != "lost"]
        stage = None
        if live_deals:
            stage = max(live_deals, key=lambda d: STAGE_RANK.get(d.get("stage", "lead"), 0)).get("stage", "lead")
        elif deals:
            stage = "lost"

        open_value = sum(float(d.get("value") or 0) for d in deals if d.get("stage") not in ("won", "lost"))
        won_value = sum(float(d.get("value") or 0) for d in deals if d.get("stage") == "won")

        result.append(_serialize_company(
            c,
            contact_count=len(contacts_by_company.get(cid, [])),
            deal_count=len(deals),
            open_deal_value=open_value,
            won_value=won_value,
            stage=stage,
        ))

    return {"companies": result}


@router.post("")
async def create_company(company: CompanyCreate, user=Depends(get_current_user)):
    """Manual company creation."""
    from app.config import database

    if company.domain:
        normalized_key = company.domain.strip().lower()
    else:
        info = resolve_company_key("", company.name)
        normalized_key = info["normalized_key"]

    existing = await database.db.companies.find_one({"normalized_key": normalized_key})
    if existing:
        raise HTTPException(status_code=400, detail="A company with this domain/name already exists")

    now = datetime.utcnow()
    doc = {
        "name": company.name,
        "domain": company.domain.strip().lower() if company.domain else None,
        "normalized_key": normalized_key,
        "is_singleton": False,
        "industry": company.industry,
        "website": company.website or (f"https://{company.domain}" if company.domain else None),
        "notes": company.notes or "",
        "created_at": now,
        "updated_at": now,
        "created_from": "manual",
    }
    result = await database.db.companies.insert_one(doc)
    return _serialize_company({**doc, "_id": result.inserted_id})


@router.get("/{company_id}")
async def get_company(company_id: str, user=Depends(get_current_user)):
    """Company detail: profile + nested contacts, deals, and recent activities."""
    from app.config import database

    try:
        obj_id = ObjectId(company_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid company ID")

    company = await database.db.companies.find_one({"_id": obj_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    contacts = await database.db.contacts.find({"company_id": company_id}).to_list(1000)
    contact_ids = [str(c["_id"]) for c in contacts]

    deals = await database.db.deals.find({"company_id": company_id}).to_list(1000)
    contacts_by_id = {str(c["_id"]): c for c in contacts}

    activities = []
    if contact_ids:
        activities = await database.db.activities.find(
            {"contact_id": {"$in": contact_ids}}
        ).sort("created_at", -1).limit(50).to_list(50)

    live_deals = [d for d in deals if d.get("stage") != "lost"]
    stage = None
    if live_deals:
        stage = max(live_deals, key=lambda d: STAGE_RANK.get(d.get("stage", "lead"), 0)).get("stage", "lead")
    elif deals:
        stage = "lost"

    return {
        "company": _serialize_company(
            company,
            contact_count=len(contacts),
            deal_count=len(deals),
            open_deal_value=sum(float(d.get("value") or 0) for d in deals if d.get("stage") not in ("won", "lost")),
            won_value=sum(float(d.get("value") or 0) for d in deals if d.get("stage") == "won"),
            stage=stage,
        ),
        "contacts": [
            {
                "id": str(c["_id"]),
                "name": c.get("name", ""),
                "email": c.get("email", ""),
                "phone": c.get("phone", ""),
                "role": c.get("role", ""),
                "status": c.get("status", "new"),
                "created_at": c.get("created_at").isoformat() if isinstance(c.get("created_at"), datetime) else c.get("created_at"),
            }
            for c in contacts
        ],
        "deals": [
            {
                "id": str(d["_id"]),
                "title": d.get("title", ""),
                "value": d.get("value", 0),
                "currency": d.get("currency", "EUR"),
                "stage": d.get("stage", "lead"),
                "probability": d.get("probability", 10),
                "notes": d.get("notes", ""),
                "created_at": d.get("created_at").isoformat() if isinstance(d.get("created_at"), datetime) else d.get("created_at"),
                "contact": {
                    "id": d.get("contact_id", ""),
                    "name": contacts_by_id.get(d.get("contact_id", ""), {}).get("name", ""),
                    "email": contacts_by_id.get(d.get("contact_id", ""), {}).get("email", ""),
                },
            }
            for d in deals
        ],
        "activities": [
            {
                "id": str(a["_id"]),
                "type": a.get("type", ""),
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "contact_id": a.get("contact_id"),
                "created_at": a.get("created_at").isoformat() if isinstance(a.get("created_at"), datetime) else a.get("created_at"),
            }
            for a in activities
        ],
    }


@router.put("/{company_id}")
async def update_company(company_id: str, updates: CompanyUpdate, user=Depends(get_current_user)):
    """Partial update; recomputes normalized_key if domain changes."""
    from app.config import database

    try:
        obj_id = ObjectId(company_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid company ID")

    company = await database.db.companies.find_one({"_id": obj_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    update_data = {k: v for k, v in updates.dict().items() if v is not None}

    if "domain" in update_data:
        new_domain = update_data["domain"].strip().lower()
        if new_domain != company.get("domain"):
            existing = await database.db.companies.find_one({"normalized_key": new_domain, "_id": {"$ne": obj_id}})
            if existing:
                raise HTTPException(status_code=400, detail="Another company already uses this domain")
            update_data["domain"] = new_domain
            update_data["normalized_key"] = new_domain

    update_data["updated_at"] = datetime.utcnow()

    await database.db.companies.update_one({"_id": obj_id}, {"$set": update_data})
    return {"success": True, "id": company_id, "updated": update_data}


@router.delete("/{company_id}")
async def delete_company(company_id: str, user=Depends(get_current_user)):
    """Refuses to delete a company that still has contacts — no cascade,
    unlike contact delete, since silently orphaning/destroying people records
    on a company delete would be too destructive to do implicitly."""
    from app.config import database

    try:
        obj_id = ObjectId(company_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid company ID")

    contact_count = await database.db.contacts.count_documents({"company_id": company_id})
    if contact_count > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete company with existing contacts — reassign or delete them first.",
        )

    result = await database.db.companies.delete_one({"_id": obj_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Company not found")

    return {"success": True}
