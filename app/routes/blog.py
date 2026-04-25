from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from bson import ObjectId
import re

router = APIRouter(prefix="/api/blogs", tags=["blog"])

# ==================== MODELS ====================

class BlogCreate(BaseModel):
    title: str
    content: str
    excerpt: Optional[str] = ""
    category: Optional[str] = ""
    tags: List[str] = []
    cover_image: Optional[str] = ""
    meta_title: Optional[str] = ""
    meta_description: Optional[str] = ""
    og_image: Optional[str] = ""
    keywords: List[str] = []
    status: str = "draft"  # draft, published

class BlogUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    excerpt: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    cover_image: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image: Optional[str] = None
    keywords: Optional[List[str]] = None
    status: Optional[str] = None

# ==================== HELPERS ====================

def calculate_reading_time(content: str) -> int:
    """Calculate reading time in minutes (200 words per minute)"""
    words = len(re.findall(r'\w+', content))
    return max(1, round(words / 200))

def generate_slug(title: str) -> str:
    """Generate URL-friendly slug from title"""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    return slug[:100]

# ==================== PUBLIC ENDPOINTS ====================

@router.get("/")
async def get_published_blogs():
    """Get all published blogs (for frontend)"""
    from app.config import database

    blogs = await database.db.blogs.find({"status": "published"}).sort("created_at", -1).to_list(100)

    return {
        "blogs": [
            {
                "id": str(b["_id"]),
                "title": b.get("title", ""),
                "slug": b.get("slug", ""),
                "excerpt": b.get("excerpt", ""),
                "category": b.get("category", ""),
                "tags": b.get("tags", []),
                "cover_image": b.get("cover_image", ""),
                "author": b.get("author", "PashxD Team"),
                "reading_time": b.get("reading_time", 5),
                "created_at": b.get("created_at", datetime.utcnow()).isoformat(),
                "updated_at": b.get("updated_at", datetime.utcnow()).isoformat(),
            }
            for b in blogs
        ]
    }

@router.get("/{slug}")
async def get_blog_by_slug(slug: str):
    """Get single blog post by slug (for frontend)"""
    from app.config import database

    blog = await database.db.blogs.find_one({"slug": slug, "status": "published"})
    if not blog:
        raise HTTPException(status_code=404, detail="Blog post not found")

    return {
        "id": str(blog["_id"]),
        "title": blog.get("title", ""),
        "slug": blog.get("slug", ""),
        "content": blog.get("content", ""),
        "excerpt": blog.get("excerpt", ""),
        "category": blog.get("category", ""),
        "tags": blog.get("tags", []),
        "cover_image": blog.get("cover_image", ""),
        "author": blog.get("author", "PashxD Team"),
        "reading_time": blog.get("reading_time", 5),
        "word_count": blog.get("word_count", 0),
        "meta_title": blog.get("meta_title", ""),
        "meta_description": blog.get("meta_description", ""),
        "og_image": blog.get("og_image", ""),
        "keywords": blog.get("keywords", []),
        "created_at": blog.get("created_at", datetime.utcnow()).isoformat(),
        "updated_at": blog.get("updated_at", datetime.utcnow()).isoformat(),
    }

# ==================== ADMIN ENDPOINTS ====================

@router.get("/admin/all")
async def get_all_blogs_admin():
    """Get all blogs including drafts (for admin dashboard)"""
    from app.config import database

    blogs = await database.db.blogs.find({}).sort("created_at", -1).to_list(200)

    return {
        "blogs": [
            {
                "id": str(b["_id"]),
                "title": b.get("title", ""),
                "slug": b.get("slug", ""),
                "excerpt": b.get("excerpt", ""),
                "content": b.get("content", ""),
                "category": b.get("category", ""),
                "tags": b.get("tags", []),
                "cover_image": b.get("cover_image", ""),
                "status": b.get("status", "draft"),
                "reading_time": b.get("reading_time", 0),
                "word_count": b.get("word_count", 0),
                "meta_title": b.get("meta_title", ""),
                "meta_description": b.get("meta_description", ""),
                "og_image": b.get("og_image", ""),
                "keywords": b.get("keywords", []),
                "created_at": b.get("created_at", datetime.utcnow()).isoformat(),
                "updated_at": b.get("updated_at", datetime.utcnow()).isoformat(),
            }
            for b in blogs
        ]
    }

@router.post("/")
async def create_blog(blog: BlogCreate):
    """Create new blog post"""
    from app.config import database

    slug = generate_slug(blog.title)
    word_count = len(re.findall(r'\w+', blog.content))
    reading_time = calculate_reading_time(blog.content)

    # Check if slug exists
    existing = await database.db.blogs.find_one({"slug": slug})
    if existing:
        # Add timestamp to make unique
        slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

    blog_doc = {
        **blog.dict(),
        "slug": slug,
        "word_count": word_count,
        "reading_time": reading_time,
        "author": "PashxD Team",
        "views": 0,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    result = await database.db.blogs.insert_one(blog_doc)

    return {
        "id": str(result.inserted_id),
        "slug": slug,
        "message": "Blog created successfully"
    }

@router.put("/{blog_id}")
async def update_blog(blog_id: str, updates: BlogUpdate):
    """Update blog post"""
    from app.config import database

    try:
        obj_id = ObjectId(blog_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid blog ID")

    update_data = {k: v for k, v in updates.dict().items() if v is not None}

    # Recalculate if content changed
    if "content" in update_data:
        update_data["word_count"] = len(re.findall(r'\w+', update_data["content"]))
        update_data["reading_time"] = calculate_reading_time(update_data["content"])

    # Regenerate slug if title changed
    if "title" in update_data:
        update_data["slug"] = generate_slug(update_data["title"])

    update_data["updated_at"] = datetime.utcnow()

    result = await database.db.blogs.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Blog not found")

    return {"success": True, "message": "Blog updated"}

@router.patch("/{blog_id}/publish")
async def toggle_publish(blog_id: str):
    """Toggle publish/unpublish status"""
    from app.config import database

    try:
        obj_id = ObjectId(blog_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid blog ID")

    blog = await database.db.blogs.find_one({"_id": obj_id})
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    new_status = "draft" if blog.get("status") == "published" else "published"

    await database.db.blogs.update_one(
        {"_id": obj_id},
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
    )

    return {"success": True, "status": new_status}

@router.delete("/{blog_id}")
async def delete_blog(blog_id: str):
    """Delete blog post"""
    from app.config import database

    try:
        obj_id = ObjectId(blog_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid blog ID")

    result = await database.db.blogs.delete_one({"_id": obj_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Blog not found")

    return {"success": True, "message": "Blog deleted"}