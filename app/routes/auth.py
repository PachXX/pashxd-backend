from fastapi import APIRouter, HTTPException, status, Depends
from app.models.schemas import UserLogin, UserCreate, TokenResponse, MessageResponse
from app.utils.jwt import create_token
from app.utils.hash import hash_password, verify_password
from app.middleware.auth import require_admin
from app.config.database import get_db
from datetime import datetime

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    db = get_db()
    user = await db.users.find_one({"email": body.email})

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

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