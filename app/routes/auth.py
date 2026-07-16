import os

from fastapi import APIRouter, HTTPException, status, Depends, Request
from app.models.schemas import UserLogin, UserCreate, TokenResponse, MessageResponse
from app.utils.jwt import create_token
from app.utils.hash import hash_password, verify_password
from app.middleware.auth import require_admin
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
async def login(body: UserLogin, request: Request):
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
async def get_me(user=Depends(require_admin)):
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
