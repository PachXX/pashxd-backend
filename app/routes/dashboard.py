from fastapi import APIRouter, Depends
from fastapi.responses import Response
from app.middleware.auth import require_admin
from app.config.database import get_db
from datetime import datetime
from bson import ObjectId

# Single router that exports as "router" for main.py to import
router = APIRouter()

# ─── SITEMAP ──────────────────────────────────────────────

@router.get("/sitemap.xml", include_in_schema=False, tags=["Sitemap"])
async def generate_sitemap():
    """Generate XML sitemap for SEO"""
    db = get_db()
    blogs = await db.blogs.find(
        {"status": "published"},
        {"slug": 1, "updated_at": 1}
    ).to_list(length=500)

    static_pages = [
        "/", "/product", "/pricing", "/industries",
        "/about", "/resources", "/contact", "/marketplace",
    ]

    urls = []

    # Static pages
    for page in static_pages:
        urls.append(f"""
  <url>
    <loc>https://pashx.com{page}</loc>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>""")

    # Blog posts
    for blog in blogs:
        updated = blog.get("updated_at", datetime.utcnow()).strftime("%Y-%m-%d")
        urls.append(f"""
  <url>
    <loc>https://pashx.com/blog/{blog["slug"]}</loc>
    <lastmod>{updated}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.6</priority>
  </url>""")

    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{"".join(urls)}
</urlset>"""

    return Response(content=sitemap, media_type="application/xml")


# ─── DASHBOARD ────────────────────────────────────────────

@router.get("/api/dashboard/stats", tags=["Dashboard"])
async def get_dashboard_stats(user=Depends(require_admin)):
    """Get dashboard statistics for admin panel"""
    db = get_db()

    # Blog stats
    total_blogs = await db.blogs.count_documents({})
    published_blogs = await db.blogs.count_documents({"status": "published"})
    draft_blogs = total_blogs - published_blogs

    # CRM stats
    total_contacts = await db.contacts.count_documents({})
    new_contacts = await db.contacts.count_documents({"status": "new"})
    total_deals = await db.deals.count_documents({})

    # Deals by stage
    pipeline = await db.deals.aggregate([
        {"$group": {"_id": "$stage", "count": {"$sum": 1}, "value": {"$sum": "$value"}}}
    ]).to_list(length=10)

    deals_by_stage = {p["_id"]: {"count": p["count"], "value": p["value"] or 0} for p in pipeline}
    total_deal_value = sum(p.get("value") or 0 for p in pipeline)

    # Recent activities
    recent = await db.activities.find({}).sort("created_at", -1).limit(5).to_list(length=5)
    for a in recent:
        try:
            contact = await db.contacts.find_one({"_id": ObjectId(a["contact_id"])}, {"name": 1})
            a["contact_name"] = contact["name"] if contact else "Unknown"
        except Exception:
            a["contact_name"] = "Unknown"
        a["id"] = str(a.pop("_id"))

    # Monthly contacts (last 6 months)
    monthly = await db.contacts.aggregate([
        {"$group": {
            "_id": {
                "year": {"$year": "$created_at"},
                "month": {"$month": "$created_at"}
            },
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id.year": -1, "_id.month": -1}},
        {"$limit": 6}
    ]).to_list(length=6)

    return {
        "blogs": {
            "total": total_blogs,
            "published": published_blogs,
            "draft": draft_blogs,
        },
        "crm": {
            "total_contacts": total_contacts,
            "new_contacts": new_contacts,
            "total_deals": total_deals,
            "total_deal_value": total_deal_value,
            "deals_by_stage": deals_by_stage,
        },
        "recent_activities": recent,
        "monthly_contacts": list(reversed(monthly)),
    }