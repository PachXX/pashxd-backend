import jwt
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    # Fail loud, not quiet: a hardcoded fallback here would mean every
    # deployment missing this env var silently signs tokens with a secret
    # published in this file's git history — anyone could forge an admin
    # session. Matches the fail-closed RBAC policy in app/middleware/auth.py.
    raise RuntimeError(
        "JWT_SECRET environment variable is not set. Refusing to start with "
        "a default signing secret. Set JWT_SECRET (Render: render.yaml "
        "generateValue; local: add to backend/.env)."
    )
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", 24))


def create_token(data: dict) -> str:
    payload = data.copy()
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    payload.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")