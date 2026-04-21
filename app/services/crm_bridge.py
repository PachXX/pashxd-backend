from datetime import datetime, timedelta


async def create_or_update_contact(db, lead):
    email = (lead.get("email") or "").strip().lower()

    if not email:
        return None

    existing = await db.contacts.find_one({"email": email})

    if existing:
        await db.contacts.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "name": lead.get("name") or existing.get("name"),
                    "company": lead.get("company") or existing.get("company"),
                    "phone": lead.get("phone") or existing.get("phone", ""),
                    "updated_at": datetime.utcnow(),
                }
            }
        )
        return await db.contacts.find_one({"_id": existing["_id"]})

    contact = {
        "name": lead.get("name", ""),
        "email": email,
        "company": lead.get("company", ""),
        "phone": lead.get("phone", ""),
        "source": "demo_form",
        "created_at": datetime.utcnow(),
    }

    res = await db.contacts.insert_one(contact)
    contact["_id"] = res.inserted_id
    return contact


# ✅ THIS IS THE MISSING FUNCTION
async def create_deal_if_not_exists(db, contact, lead):
    if not contact:
        return

    contact_id = str(contact["_id"])
    recent_time = datetime.utcnow() - timedelta(hours=24)

    existing = await db.deals.find_one({
        "contact_id": contact_id,
        "created_at": {"$gte": recent_time}
    })

    if existing:
        return

    deal = {
        "title": f"{lead.get('company') or lead.get('name') or 'New Lead'} — Demo",
        "contact_id": contact_id,
        "value": 0,
        "currency": "EUR",
        "stage": "lead",
        "notes": lead.get("message", ""),
        "created_at": datetime.utcnow(),
    }

    await db.deals.insert_one(deal)