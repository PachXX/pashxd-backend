from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.schemas import BlogCreate, BlogUpdate
from app.middleware.auth import require_admin
from app.config.database import get_db
from app.utils.slug import slugify
from app.utils.readability import get_readability, get_keyword_density
from bson import ObjectId
from datetime import datetime

router = APIRouter(prefix="/api/blogs", tags=["Blog"])


def blog_out(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


# ── PUBLIC: get published blogs (for frontend) ──────────────

@router.get("/")
async def list_published_blogs(
        limit: int = Query(10, le=50),
        skip: int = 0,
        category: str = None,
):
    db = get_db()
    query = {"status": "published"}
    if category:
        query["category"] = category

    cursor = db.blogs.find(query, {"content": 0}).sort("created_at", -1).skip(skip).limit(limit)
    blogs = await cursor.to_list(length=limit)
    total = await db.blogs.count_documents(query)

    return {
        "blogs": [blog_out(b) for b in blogs],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/slug/:slug")
async def get_blog_by_slug(slug: str):
    db = get_db()
    doc = await db.blogs.find_one({"slug": slug, "status": "published"})
    if not doc:
        raise HTTPException(status_code=404, detail="Blog post not found")
    return blog_out(doc)


# ── ADMIN: full CRUD ──────────────────────────────────────

@router.get("/admin/all")
async def list_all_blogs(
        limit: int = Query(20, le=100),
        skip: int = 0,
        status: str = None,
        user=Depends(require_admin),
):
    db = get_db()
    query = {}
    if status:
        query["status"] = status

    cursor = db.blogs.find(query, {"content": 0}).sort("created_at", -1).skip(skip).limit(limit)
    blogs = await cursor.to_list(length=limit)
    total = await db.blogs.count_documents(query)

    return {
        "blogs": [blog_out(b) for b in blogs],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/admin/:id")
async def get_blog_admin(id: str, user=Depends(require_admin)):
    db = get_db()
    doc = await db.blogs.find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Blog not found")
    return blog_out(doc)


@router.post("/")
async def create_blog(body: BlogCreate, user=Depends(require_admin)):
    db = get_db()

    # Auto-generate slug
    base_slug = slugify(body.title)
    slug = base_slug
    counter = 1
    while await db.blogs.find_one({"slug": slug}):
        slug = f"{base_slug}-{counter}"
        counter += 1

    # Readability scores
    scores = get_readability(body.content)
    keywords_density = get_keyword_density(body.content)

    now = datetime.utcnow()
    doc = {
        **body.model_dump(),
        "slug": slug,
        "readability_score": scores["flesch_reading_ease"],
        "reading_grade": scores["reading_grade"],
        "word_count": scores["word_count"],
        "reading_time": scores["reading_time_minutes"],
        "keyword_density": keywords_density,
        "created_at": now,
        "updated_at": now,
    }

    result = await db.blogs.insert_one(doc)
    doc["_id"] = result.inserted_id
    return blog_out(doc)


@router.put("/{id}")
async def update_blog(id: str, body: BlogUpdate, user=Depends(require_admin)):
    db = get_db()

    update_data = {k: v for k, v in body.model_dump().items() if v is not None}

    # Recalculate scores if content updated
    if "content" in update_data:
        scores = get_readability(update_data["content"])
        keywords_density = get_keyword_density(update_data["content"])
        update_data.update({
            "readability_score": scores["flesch_reading_ease"],
            "reading_grade": scores["reading_grade"],
            "word_count": scores["word_count"],
            "reading_time": scores["reading_time_minutes"],
            "keyword_density": keywords_density,
        })

    # Regenerate slug if title changed
    if "title" in update_data:
        base_slug = slugify(update_data["title"])
        slug = base_slug
        counter = 1
        while await db.blogs.find_one({"slug": slug, "_id": {"$ne": ObjectId(id)}}):
            slug = f"{base_slug}-{counter}"
            counter += 1
        update_data["slug"] = slug

    update_data["updated_at"] = datetime.utcnow()

    result = await db.blogs.update_one(
        {"_id": ObjectId(id)},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Blog not found")

    doc = await db.blogs.find_one({"_id": ObjectId(id)})
    return blog_out(doc)


@router.patch("/{id}/publish")
async def toggle_publish(id: str, user=Depends(require_admin)):
    db = get_db()
    doc = await db.blogs.find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Blog not found")

    new_status = "published" if doc["status"] == "draft" else "draft"
    await db.blogs.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
    )
    return {"message": f"Blog {new_status}", "status": new_status}


@router.delete("/{id}")
async def delete_blog(id: str, user=Depends(require_admin)):
    db = get_db()
    result = await db.blogs.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Blog not found")
    return {"message": "Blog deleted successfully"}