from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.utils.jwt import decode_token
from app.config.firebase import verify_firebase_token

security = HTTPBearer()


async def get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token = credentials.credentials

    # 1) Legacy JWT issued by /api/auth/login (primary auth path)
    try:
        payload = decode_token(token)
        return payload
    except ValueError as jwt_error:
        # 2) Firebase Auth ID token (available once Firebase is enabled;
        #    role comes from custom claims set via the Admin SDK)
        fb_claims = verify_firebase_token(token)
        if fb_claims:
            return {
                "sub": fb_claims.get("uid") or fb_claims.get("user_id"),
                "email": fb_claims.get("email"),
                "role": fb_claims.get("role", "user"),
                "auth_provider": "firebase",
            }

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(jwt_error),
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_admin(user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
