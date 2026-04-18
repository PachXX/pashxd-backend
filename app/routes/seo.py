from fastapi import APIRouter, Depends
from app.middleware.auth import require_admin
from app.config.database import get_db
from app.utils.readability import get_readability, get_keyword_density
from bson import ObjectId

router = APIRouter(prefix="/api/seo", tags=["SEO"])


@router.post("/analyze")
async def analyze_content(body: dict, user=Depends(require_admin)):
    """Analyze content and return readability + keyword scores"""
    content = body.get("content", "")
    title = body.get("title", "")

    scores = get_readability(content)
    keywords = get_keyword_density(content)

    # Basic SEO checks
    checks = []

    if title:
        if len(title) < 30:
            checks.append({"type": "warning", "msg": "Title is too short. Aim for 40-60 characters."})
        elif len(title) > 60:
            checks.append({"type": "warning", "msg": "Title is too long. Keep it under 60 characters."})
        else:
            checks.append({"type": "success", "msg": "Title length is good."})

    meta_desc = body.get("meta_description", "")
    if meta_desc:
        if len(meta_desc) < 120:
            checks.append({"type": "warning", "msg": "Meta description is too short. Aim for 140-160 characters."})
        elif len(meta_desc) > 160:
            checks.append({"type": "warning", "msg": "Meta description is too long. Keep under 160 characters."})
        else:
            checks.append({"type": "success", "msg": "Meta description length is perfect."})
    else:
        checks.append({"type": "error", "msg": "Meta description is missing."})

    if scores["word_count"] < 300:
        checks.append({"type": "error", "msg": "Content too short. Aim for at least 600 words."})
    elif scores["word_count"] >= 600:
        checks.append({"type": "success", "msg": f"Good content length: {scores['word_count']} words."})

    return {
        "readability": scores,
        "keywords": keywords[:10],
        "seo_checks": checks,
        "overall_score": scores["seo_score"],
    }


@router.get("/scores")
async def get_all_seo_scores(user=Depends(require_admin)):
    """Get readability scores for all blog posts"""
    db = get_db()
    blogs = await db.blogs.find(
        {},
        {"title": 1, "slug": 1, "status": 1, "readability_score": 1,
         "word_count": 1, "reading_time": 1, "meta_description": 1,
         "meta_title": 1, "keywords": 1, "created_at": 1}
    ).to_list(length=200)

    results = []
    for b in blogs:
        has_meta = bool(b.get("meta_description"))
        has_title = bool(b.get("meta_title"))
        score = 100
        if not has_meta:
            score -= 25
        if not has_title:
            score -= 15
        if (b.get("word_count") or 0) < 300:
            score -= 20

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
            "seo_score": max(0, score),
        })

    return {"posts": results, "total": len(results)}