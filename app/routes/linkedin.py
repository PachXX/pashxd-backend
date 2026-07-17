"""
LinkedIn router — draft-for-approval company-page posting.

Flow (mirrors outreach.py):
  1. Agent calls POST /posts with a generated company-page post draft, and
     POST /brief with the day's read-only advisory brief.
  2. Operator reviews in the dashboard (/linkedin):
       - PATCH /posts/{id}            -> edit hook / body / hashtags / link
       - POST  /posts/{id}/regenerate -> re-generate copy via Claude
       - POST  /posts/{id}/approve    -> publish to the company Page via the
                                         LinkedIn API, or (until tokens are
                                         configured) mark approved and return
                                         copy-paste-ready text — graceful skip.
       - POST  /posts/{id}/skip       -> discard.

Only company-page POSTS are ever published. The brief is advisory only.
Publishing happens backend-side on approve (like outreach's SendGrid send),
so the agent never holds LinkedIn credentials.
"""
import os
import json
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middleware.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
# TODO(2026-07): LINKEDIN_ORG_ID secret was intentionally unset on the Cloud Run
# service while LinkedIn's Community Management API review is pending (org-page
# posting 403s until approved — see _publish_to_linkedin). Once approved,
# re-add the org-id secret via `gcloud run services update --update-secrets`
# and _publish_to_linkedin will automatically prefer the org URN again.
LINKEDIN_ORG_ID = os.getenv("LINKEDIN_ORG_ID", "")  # numeric organization id
LINKEDIN_MEMBER_URN = os.getenv("LINKEDIN_MEMBER_URN", "")  # urn:li:person:{id} — personal-profile fallback while org review is pending
LINKEDIN_API_VERSION = os.getenv("LINKEDIN_API_VERSION", "202606")  # LinkedIn versions expire after ~12mo — bump periodically

CONTENT_TYPES = {"product_value", "industry_insight", "blog_reshare", "thought_leadership"}

PRODUCT_CONTEXT = (
    "PashX (pashx.com) is an AI-native Industrial Operating System — it unifies "
    "procurement, operations, CRM, ERP, finance, and execution into one intelligent "
    "platform for companies that operate in the physical world: manufacturing, "
    "construction, industrial equipment, retail chains, energy, trading, logistics, "
    "and infrastructure. Not 'AI-added' to old software — AI-native from the ground "
    "up. Philosophy: traditional ERP records transactions, PashX recommends actions; "
    "traditional CRM tracks customers, PashX helps win them; traditional procurement "
    "manages purchases, PashX predicts what's needed next."
)
PERSONA = (
    "You are Shahil Mohideen, Founder & Managing Director of PashX, posting on your "
    "personal LinkedIn profile as an industry voice on industrial AI and operations "
    "— not a company brand account. Your job on LinkedIn is to bring your audience "
    "real value and spark genuine discussion, not to pitch. Product mentions are "
    "earned, not default — most posts should stand on their own as useful analysis "
    "or a sharp opinion even if PashX is never named.\n\n"
    "Voice: professional, direct, optimistic, practical, curious. Explain technology "
    "in business language, not engineering language — always lead with the customer "
    "outcome, not the feature. Avoid buzzwords unless they add real clarity. Use "
    "simple language and concrete, practical examples over abstractions.\n\n"
    "Core beliefs that should surface naturally across posts (don't state them as a "
    "list — show them through specific examples): software should reduce complexity, "
    "not create it. AI should augment people, not replace them. Every operational "
    "decision should be backed by data. Industrial companies deserve consumer-grade "
    "software experiences. Automation should save hours, not create more process.\n\n"
    "Recurring themes to draw from: digital transformation, industrial AI, "
    "procurement intelligence, workflow automation, execution intelligence, "
    "operational excellence, data-driven operations, AI agents as every employee's "
    "operational assistant (drafting RFQs, predicting delays, generating reports, "
    "automating repetitive work) — not replacing the team."
)
BANNED_PHRASES = [
    "game-changer", "in today's fast-paced world", "unlock", "leverage",
    "synergy", "revolutionize", "seamless", "seamlessly", "elevate",
    "supercharge", "cutting-edge", "next-level", "paradigm shift",
    "I hope this finds you well", "dear valued", "esteemed", "delve",
    "delve into", "dive into", "dive deep", "moreover", "furthermore",
    "in conclusion", "plethora", "myriad", "tapestry", "landscape",
    "realm", "embark", "foster", "holistic", "unparalleled", "robust",
    "empower", "empowering", "testament to", "boasts",
]


# ─── MODELS ───────────────────────────────────────────────────────────────────

class LinkedInPostCreate(BaseModel):
    content_type: str = Field(default="product_value")
    hook: str = ""
    body: str
    hashtags: list[str] = Field(default_factory=list)
    link_url: Optional[str] = None
    source_blog_slug: Optional[str] = None


class PostUpdate(BaseModel):
    hook: Optional[str] = None
    body: Optional[str] = None
    hashtags: Optional[list[str]] = None
    link_url: Optional[str] = None


class LinkedInBriefCreate(BaseModel):
    industry_topic: str = ""
    prospect_profiles: str = ""
    page_tip: str = ""
    engagement_tips: str = ""


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _oid(id_str: str):
    from bson import ObjectId
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def _plain(text: Optional[str]) -> str:
    """LinkedIn commentary is plain text — strip any tags defensively."""
    if not text:
        return ""
    import nh3
    return nh3.clean(text, tags=set(), attributes={}).strip()


def _norm_hashtags(tags: Optional[list[str]]) -> list[str]:
    out = []
    for t in (tags or []):
        t = (t or "").strip().lstrip("#").strip()
        if t:
            out.append(t)
    return out[:8]


def _serialize_post(d: dict) -> dict:
    return {
        "id": str(d.get("_id", "")),
        "content_type": d.get("content_type"),
        "hook": d.get("hook", ""),
        "body": d.get("body", ""),
        "hashtags": d.get("hashtags", []),
        "link_url": d.get("link_url"),
        "source_blog_slug": d.get("source_blog_slug"),
        "status": d.get("status"),
        "linkedin_urn": d.get("linkedin_urn"),
        "permalink": d.get("permalink"),
        "created_at": d.get("created_at"),
        "published_at": d.get("published_at"),
    }


def _serialize_brief(d: dict) -> dict:
    return {
        "id": str(d.get("_id", "")),
        "industry_topic": d.get("industry_topic", ""),
        "prospect_profiles": d.get("prospect_profiles", ""),
        "page_tip": d.get("page_tip", ""),
        "engagement_tips": d.get("engagement_tips", ""),
        "created_at": d.get("created_at"),
    }


def _compose_commentary(post: dict) -> str:
    """The exact text that goes on LinkedIn (also what the operator copy-pastes
    when the API isn't connected yet)."""
    parts = []
    body = _plain(post.get("body"))
    if body:
        parts.append(body)
    if post.get("link_url"):
        parts.append(post["link_url"])
    tags = _norm_hashtags(post.get("hashtags"))
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    return "\n\n".join(parts)


async def _generate_post_copy(content_type: str, source_blog: Optional[dict], recent_titles: list[str]) -> dict:
    """Ask Claude for a company-page post. Returns {hook, body, hashtags, link_url?}."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on backend")

    avoid = "; ".join(recent_titles[:15]) or "none yet"
    banned = ", ".join(BANNED_PHRASES)
    blog_ctx = ""
    if content_type == "blog_reshare" and source_blog:
        blog_ctx = (
            f"\nRESHARE THIS BLOG POST:\n- Title: {source_blog.get('title','')}\n"
            f"- URL: https://pashx.com/blog/{source_blog.get('slug','')}\n"
            f"- Summary: {source_blog.get('excerpt') or source_blog.get('meta_description') or ''}\n"
            "Write commentary that teases the post and drives clicks. Set link_url to the URL above."
        )

    type_brief = {
        "product_value": "Highlight one concrete PashX capability and the real operational problem it removes for an industrial/physical-world business (manufacturing, construction, retail, energy, trading, logistics). Frame it as traditional-software-vs-AI-native (e.g. 'traditional ERP records transactions, PashX recommends actions'). Specific, not salesy.",
        "industry_insight": "Share one sharp, current insight about industrial operations, procurement, or AI-native software replacing fragmented tools (CRM+ERP+procurement+BI as separate systems). Take a clear point of view.",
        "blog_reshare": "Tease a PashX blog post and drive readers to it.",
        "thought_leadership": "Share a genuine, useful piece of analysis or opinion about running a physical-world business, using AI as an operational assistant (not a replacement), or digital transformation in industrial/procurement/execution — the kind of post a founder posts to build their own voice, not to sell anything. PashX may go unmentioned; if it comes up, it's one line, not the point.",
    }.get(content_type, "Highlight one concrete PashX capability.")

    prompt = f"""{PERSONA}

{PRODUCT_CONTEXT}

POST TYPE: {content_type}
BRIEF: {type_brief}{blog_ctx}

Do NOT repeat the angle of any of these recent posts: {avoid}

STYLE:
- Human, direct, confident. Short lines and line breaks (LinkedIn native).
- Under 130 words in the body. A strong first line (the hook) that stops the scroll.
- Vary line/sentence length on purpose — some lines are 3 words, some are 15. Uniform rhythm reads as AI-generated.
- Use contractions naturally (it's, don't, that's) — one specific person wrote this, not a brand account.
- Avoid perfectly parallel three-item lists ("faster, smarter, better") — real writing is lumpier.
- End on something that earns a real reply — a specific question, an invitation to disagree, or a concrete "here's what I'd do" — not a generic "thoughts?" tack-on.
- No emoji spam (0-2 max). No hashtags inside the body.
- Banned phrases — never use: {banned}.

Return ONLY JSON (no markdown), exactly:
{{"hook": "<first line, under 100 chars>",
  "body": "<full post text incl the hook as its first line, plain text with \\n line breaks>",
  "hashtags": ["<5 relevant hashtags without the # sign>"],
  "link_url": "<url or empty string>"}}"""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 900,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Claude API error {resp.status_code}")

    raw = resp.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    data["hashtags"] = _norm_hashtags(data.get("hashtags"))[:5]
    return data


async def _publish_to_linkedin(post: dict) -> Optional[dict]:
    """Publish a post via the LinkedIn Posts API.
    Prefers the company Page (org URN) once Community Management API is
    approved; until then falls back to the personal profile (member URN) so
    posting still works. Returns {urn, permalink, author} on success, or None
    when no credentials are configured at all (graceful skip — the caller
    then falls back to copy-paste). Raises only on a real API failure."""
    if LINKEDIN_ORG_ID:
        author = f"urn:li:organization:{LINKEDIN_ORG_ID}"
    elif LINKEDIN_MEMBER_URN:
        author = LINKEDIN_MEMBER_URN
    else:
        return None
    if not LINKEDIN_ACCESS_TOKEN:
        return None

    commentary = _compose_commentary(post)
    payload = {
        "author": author,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.linkedin.com/rest/posts",
            headers={
                "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "LinkedIn-Version": LINKEDIN_API_VERSION,
                "X-Restli-Protocol-Version": "2.0.0",
            },
            json=payload,
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"LinkedIn API {resp.status_code}: {resp.text[:200]}")
    urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id") or ""
    permalink = f"https://www.linkedin.com/feed/update/{urn}" if urn else ""
    return {"urn": urn, "permalink": permalink, "author": author}


# ─── POST ENDPOINTS ───────────────────────────────────────────────────────────

@router.get("/posts")
async def list_posts(status: str = "pending", limit: int = 50, user=Depends(get_current_user)):
    from app.config import database
    db = database
    limit = max(1, min(limit, 200))
    q = {} if status == "all" else {"status": status}
    posts = await db.db.linkedin_posts.find(q).sort("created_at", -1).to_list(limit)
    return {"posts": [_serialize_post(p) for p in posts]}


@router.post("/posts")
async def create_post(post: LinkedInPostCreate, user=Depends(require_admin)):
    """Agent stores a generated company-page post draft."""
    from app.config import database
    db = database
    ct = post.content_type if post.content_type in CONTENT_TYPES else "product_value"
    doc = {
        "content_type": ct,
        "hook": _plain(post.hook),
        "body": _plain(post.body),
        "hashtags": _norm_hashtags(post.hashtags)[:5],
        "link_url": (post.link_url or "").strip() or None,
        "source_blog_slug": (post.source_blog_slug or "").strip() or None,
        "status": "pending",
        "linkedin_urn": None,
        "permalink": None,
        "created_at": _now(),
        "published_at": None,
    }
    res = await db.db.linkedin_posts.insert_one(doc)
    return {"ok": True, "id": str(res.inserted_id)}


@router.patch("/posts/{post_id}")
async def update_post(post_id: str, update: PostUpdate, user=Depends(require_admin)):
    """Edit a pending post's hook / body / hashtags / link."""
    from app.config import database
    db = database
    post = await db.db.linkedin_posts.find_one({"_id": _oid(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only pending posts can be edited")
    patch: dict = {"updated_at": _now()}
    if update.hook is not None:
        patch["hook"] = _plain(update.hook)
    if update.body is not None:
        patch["body"] = _plain(update.body)
    if update.hashtags is not None:
        patch["hashtags"] = _norm_hashtags(update.hashtags)[:5]
    if update.link_url is not None:
        patch["link_url"] = update.link_url.strip() or None
    await db.db.linkedin_posts.update_one({"_id": post["_id"]}, {"$set": patch})
    updated = await db.db.linkedin_posts.find_one({"_id": post["_id"]})
    return _serialize_post(updated)


@router.post("/posts/{post_id}/regenerate")
async def regenerate_post(post_id: str, user=Depends(require_admin)):
    """Re-generate the post copy via Claude and update in place."""
    from app.config import database
    db = database
    post = await db.db.linkedin_posts.find_one({"_id": _oid(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only pending posts can be regenerated")

    source_blog = None
    if post.get("content_type") == "blog_reshare" and post.get("source_blog_slug"):
        try:
            source_blog = await db.db.blogs.find_one({"slug": post["source_blog_slug"]})
        except Exception:
            source_blog = None

    recent = await db.db.linkedin_posts.find(
        {"_id": {"$ne": post["_id"]}}, {"hook": 1}
    ).sort("created_at", -1).to_list(20)
    recent_titles = [r.get("hook", "") for r in recent if r.get("hook")]

    copy = await _generate_post_copy(post.get("content_type", "product_value"), source_blog, recent_titles)
    await db.db.linkedin_posts.update_one(
        {"_id": post["_id"]},
        {"$set": {
            "hook": _plain(copy.get("hook")),
            "body": _plain(copy.get("body")),
            "hashtags": _norm_hashtags(copy.get("hashtags"))[:5],
            "link_url": (copy.get("link_url") or "").strip() or None if "link_url" in copy else post.get("link_url"),
            "updated_at": _now(),
        }},
    )
    updated = await db.db.linkedin_posts.find_one({"_id": post["_id"]})
    return _serialize_post(updated)


@router.post("/posts/{post_id}/approve")
async def approve_post(post_id: str, user=Depends(require_admin)):
    """Publish to the company Page. Falls back to copy-paste text when the
    LinkedIn API isn't connected yet (graceful skip, not an error)."""
    from app.config import database
    db = database
    post = await db.db.linkedin_posts.find_one({"_id": _oid(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.get("status") not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail=f"Post is {post.get('status')}, cannot approve")

    result = await _publish_to_linkedin(post)  # None => not connected
    if result is None:
        await db.db.linkedin_posts.update_one(
            {"_id": post["_id"]}, {"$set": {"status": "approved", "updated_at": _now()}}
        )
        return {
            "ok": True,
            "published": False,
            "manual": True,
            "commentary": _compose_commentary(post),
            "message": "LinkedIn not connected yet — copy the text and post it manually. It will auto-publish once tokens are configured.",
        }

    await db.db.linkedin_posts.update_one(
        {"_id": post["_id"]},
        {"$set": {
            "status": "published",
            "linkedin_urn": result["urn"],
            "permalink": result["permalink"],
            "published_at": _now(),
            "updated_at": _now(),
        }},
    )
    return {"ok": True, "published": True, "permalink": result["permalink"]}


@router.post("/posts/{post_id}/skip")
async def skip_post(post_id: str, user=Depends(require_admin)):
    """Discard a pending post."""
    from app.config import database
    db = database
    post = await db.db.linkedin_posts.find_one({"_id": _oid(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.get("status") not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail=f"Post is {post.get('status')}, cannot skip")
    await db.db.linkedin_posts.update_one({"_id": post["_id"]}, {"$set": {"status": "skipped", "updated_at": _now()}})
    return {"ok": True}


# ─── BRIEF ENDPOINTS ──────────────────────────────────────────────────────────

@router.get("/brief")
async def get_brief(user=Depends(get_current_user)):
    """Latest advisory brief (read-only intelligence)."""
    from app.config import database
    db = database
    doc = await db.db.linkedin_briefs.find_one(sort=[("created_at", -1)])
    return {"brief": _serialize_brief(doc) if doc else None}


@router.post("/brief")
async def create_brief(brief: LinkedInBriefCreate, user=Depends(require_admin)):
    """Agent stores the day's advisory brief."""
    from app.config import database
    db = database
    doc = {
        "industry_topic": (brief.industry_topic or "").strip(),
        "prospect_profiles": (brief.prospect_profiles or "").strip(),
        "page_tip": (brief.page_tip or "").strip(),
        "engagement_tips": (brief.engagement_tips or "").strip(),
        "created_at": _now(),
    }
    res = await db.db.linkedin_briefs.insert_one(doc)
    return {"ok": True, "id": str(res.inserted_id)}
