from fastapi import APIRouter, Depends
from app.middleware.auth import require_admin
from app.config.database import get_db
from app.utils.readability import get_readability, get_keyword_density, build_seo_checks
from bson import ObjectId

router = APIRouter(prefix="/api/seo", tags=["SEO"])


@router.post("/analyze")
async def analyze_content(body: dict, user=Depends(require_admin)):
    """Analyze content and return readability + keyword scores"""
    content = body.get("content", "")
    title = body.get("title", "")
    meta_desc = body.get("meta_description", "")

    scores = get_readability(content)
    keywords = get_keyword_density(content)
    checks = build_seo_checks(title, content, meta_desc)

    return {
        "readability": scores,
        "keywords": keywords[:10],
        "seo_checks": checks,
        "overall_score": scores["seo_score"],
    }


@router.get("/scores")
async def get_all_seo_scores(user=Depends(require_admin)):
    """SEO scores for all blog posts — populated automatically on publish
    (see blog.py's toggle_publish) for anything published from now on.
    Posts published before that shipped have no stored seo_score/seo_checks
    yet, so those are computed live here as a fallback."""
    db = get_db()
    blogs = await db.blogs.find(
        {},
        {"title": 1, "slug": 1, "status": 1, "readability_score": 1,
         "word_count": 1, "reading_time": 1, "meta_description": 1,
         "meta_title": 1, "keywords": 1, "content": 1, "created_at": 1,
         "seo_checks": 1, "seo_score": 1, "seo_checked_at": 1}
    ).to_list(length=200)

    results = []
    for b in blogs:
        has_meta = bool(b.get("meta_description"))
        has_title = bool(b.get("meta_title"))

        seo_score = b.get("seo_score")
        seo_checks = b.get("seo_checks")
        if seo_score is None or seo_checks is None:
            live = get_readability(b.get("content", ""))
            seo_score = live["seo_score"]
            seo_checks = build_seo_checks(b.get("meta_title", ""), b.get("content", ""), b.get("meta_description", ""))

        results.append({
            "id": str(b["_id"]),
            "title": b["title"],
            "slug": b["slug"],
            "status": b["status"],
            "readability_score": b.get("readability_score"),
            "word_count": b.get("word_count"),
            "reading_time": b.get("reading_time"),
            "has_meta_description": has_meta,
            "has_meta_title": has_title,
            "seo_score": seo_score,
            "seo_checks": seo_checks,
            "seo_checked_at": b.get("seo_checked_at"),
        })

    return {"posts": results, "total": len(results)}