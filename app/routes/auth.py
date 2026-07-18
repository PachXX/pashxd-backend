import os

from fastapi import APIRouter, HTTPException, status, Depends, Request, Response
from app.models.schemas import UserLogin, UserCreate, TokenResponse, MessageResponse
from app.utils.jwt import create_token, JWT_EXPIRE_HOURS
from app.utils.hash import hash_password, verify_password
from app.middleware.auth import require_admin, get_current_user, COOKIE_NAME, COOKIE_SECURE, COOKIE_SAMESITE
from app.config.database import get_db
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# ─── Login rate limiting ─────────────────────────────────
# Mongo-backed so the limit holds across multiple backend instances. Docs
# expire via TTL index on expires_at (created in main.py lifespan).
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_MINUTES = int(os.getenv("LOGIN_WINDOW_MINUTES", "15"))


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin, request: Request, response: Response):
    db = get_db()

    attempt_key = f"{_client_ip(request)}:{body.email.lower()}"
    now = datetime.utcnow()

    attempt = await db.login_attempts.find_one({"key": attempt_key})
    # TTL monitor only sweeps ~every 60s — treat an expired doc as gone.
    if attempt and attempt.get("expires_at", now) <= now:
        await db.login_attempts.delete_one({"_id": attempt["_id"]})
        attempt = None
    if attempt and attempt.get("count", 0) >= LOGIN_MAX_ATTEMPTS and attempt.get("expires_at", now) > now:
        retry_after = int((attempt["expires_at"] - now).total_seconds())
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(max(retry_after, 1))},
        )

    user = await db.users.find_one({"email": body.email})

    if not user or not verify_password(body.password, user["password"]):
        # Count the failure; window starts at first failed attempt.
        await db.login_attempts.update_one(
            {"key": attempt_key},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "key": attempt_key,
                    "expires_at": now + timedelta(minutes=LOGIN_WINDOW_MINUTES),
                },
            },
            upsert=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Successful login clears the counter.
    await db.login_attempts.delete_one({"key": attempt_key})

    token = create_token({
        "sub": str(user["_id"]),
        "email": user["email"],
        "role": user.get("role", "admin"),
    })

    # httpOnly session cookie — the dashboard and marketing site live on a
    # different origin than the API, so SameSite=None; Secure is required
    # for the browser to send it back cross-site. Bearer header still works
    # unchanged (see get_current_user) for anything not switched over yet.
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path="/",
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user["_id"]),
            "email": user["email"],
            "role": user.get("role", "admin"),
        },
    }


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    db = get_db()
    from bson import ObjectId
    doc = await db.users.find_one({"_id": ObjectId(user["sub"])})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": str(doc["_id"]),
        "email": doc["email"],
        "role": doc.get("role", "admin"),
        "created_at": doc.get("created_at"),
    }


@router.post("/logout", response_model=MessageResponse)
async def logout(response: Response):
    """Auth is stateless Bearer JWT + an optional session cookie — nothing
    to invalidate server-side either way, so this just clears the cookie.
    delete_cookie must repeat the secure/samesite/path attributes it was
    set with, or some browsers silently keep it."""
    response.delete_cookie(COOKIE_NAME, path="/", secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    return {"message": "Logged out successfully"}


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
        body: dict,
        user=Depends(require_admin)
):
    db = get_db()
    from bson import ObjectId

    doc = await db.users.find_one({"_id": ObjectId(user["sub"])})
    if not doc or not verify_password(body.get("current_password", ""), doc["password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_hashed = hash_password(body.get("new_password", ""))
    await db.users.update_one(
        {"_id": ObjectId(user["sub"])},
        {"$set": {"password": new_hashed, "updated_at": datetime.utcnow()}}
    )
    return {"message": "Password updated successfully"}
