import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.utils.jwt import decode_token

# auto_error=False so a missing Authorization header falls through to the
# cookie check instead of an immediate 403.
security = HTTPBearer(auto_error=False)

# ─── Auth cookie settings ────────────────────────────────
# Frontend (Vercel) and API (Cloud Run) live on different sites, so the
# cookie must be SameSite=None; Secure. Override via env for local dev.
COOKIE_NAME = "access_token"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none")


async def get_current_user(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    # Explicit Bearer header wins (API clients, legacy frontends),
    # then the httpOnly cookie set by /api/auth/login.
    token = credentials.credentials if credentials else None
    if not token:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_token(token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


# ─── RBAC ────────────────────────────────────────────────
# Hierarchical roles: each level includes everything below it.
ROLE_HIERARCHY = {"viewer": 0, "editor": 1, "admin": 2}


def require_role(minimum: str):
    """Dependency factory: require_role('editor') admits editors and admins.

    Tokens without a known role are rejected (fail closed).
    """
    if minimum not in ROLE_HIERARCHY:
        raise ValueError(f"Unknown role: {minimum}")
    minimum_level = ROLE_HIERARCHY[minimum]

    async def checker(user=Depends(get_current_user)):
        user_level = ROLE_HIERARCHY.get(user.get("role"), -1)
        if user_level < minimum_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{minimum.capitalize()} access required",
            )
        return user

    return checker


# Existing routes keep importing require_admin unchanged.
require_admin = require_role("admin")
require_editor = require_role("editor")
require_viewer = require_role("viewer")
