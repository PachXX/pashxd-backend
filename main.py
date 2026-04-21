from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path
import os
import logging
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional
import uuid
from datetime import datetime, timezone

# ✅ CRM FUNCTIONS
from app.services.crm_bridge import create_or_update_contact, create_deal_if_not_exists

# ─── SAFE IMPORT HELPER (prevents app crash) ──────────────
def safe_import_router(import_path, name="router"):
    try:
        module = __import__(import_path, fromlist=[name])
        return getattr(module, name)
    except Exception as e:
        print(f"❌ Failed to import {import_path}: {e}")
        return None

# ─── LOAD ENV ─────────────────────────────────────────────

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ─── LOGGING ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── DATABASE ─────────────────────────────────────────────

mongo_url = os.getenv("MONGO_URL")
if not mongo_url:
    raise ValueError("❌ MONGO_URL not set")

client = AsyncIOMotorClient(mongo_url)
db_name = os.getenv("DB_NAME", "pashxd")
db_instance = client[db_name]

# ─── LIFESPAN ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from app.config import database
        database.db = db_instance
        logger.info(f"✅ Connected to MongoDB: {db_name}")
    except Exception as e:
        logger.error(f"❌ DB setup failed: {e}")

    await seed_admin()
    yield

    client.close()
    logger.info("❌ MongoDB closed")

async def seed_admin():
    try:
        from app.utils.hash import hash_password
        admin_email = os.getenv("ADMIN_EMAIL", "admin@pashx.com")
        admin_password = os.getenv("ADMIN_PASSWORD", "changeme123")

        existing = await db_instance.users.find_one({"email": admin_email})
        if not existing:
            await db_instance.users.insert_one({
                "email": admin_email,
                "password": hash_password(admin_password),
                "role": "admin",
                "created_at": datetime.utcnow(),
            })
            logger.info(f"✅ Admin created: {admin_email}")
        else:
            logger.info("ℹ️ Admin exists")
    except Exception as e:
        logger.error(f"❌ Admin seed failed: {e}")

# ─── APP ─────────────────────────────────────────────────

app = FastAPI(
    title="PashxD API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# ─── CORS ────────────────────────────────────────────────

def get_allowed_origins():
    origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]

    cors_env = os.getenv("CORS_ORIGINS")
    if cors_env:
        origins.extend([o.strip() for o in cors_env.split(",") if o.strip()])

    for key in ["FRONTEND_URL", "ADMIN_URL"]:
        val = os.getenv(key)
        if val and val.startswith("http"):
            origins.append(val)

    return list(set(origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── API ROUTER ──────────────────────────────────────────

api_router = APIRouter(prefix="/api")

# ─── MODELS ──────────────────────────────────────────────

class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

class DemoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    email: str
    company: str
    role: Optional[str] = ""
    industry: Optional[str] = ""
    message: Optional[str] = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class DemoRequestCreate(BaseModel):
    name: str
    email: str
    company: str
    role: Optional[str] = ""
    industry: Optional[str] = ""
    message: Optional[str] = ""

# ─── BASIC ROUTES ────────────────────────────────────────

@api_router.get("/")
async def root():
    return {"message": "PashxD API running"}

@api_router.get("/status")
async def get_status():
    return {"status": "ok"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status(input: StatusCheckCreate):
    obj = StatusCheck(**input.model_dump())
    doc = obj.model_dump()
    doc["timestamp"] = doc["timestamp"].isoformat()
    await db_instance.status_checks.insert_one(doc)
    return obj

# ─── DEMO → CRM ──────────────────────────────────────────

@api_router.post("/demo-requests", response_model=DemoRequest)
async def create_demo(input: DemoRequestCreate):
    demo_dict = input.model_dump()
    demo_obj = DemoRequest(**demo_dict)

    doc = demo_obj.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()

    await db_instance.demo_requests.insert_one(doc)

    contact = await create_or_update_contact(db_instance, doc)
    await create_deal_if_not_exists(db_instance, contact, doc)

    return demo_obj

@api_router.get("/demo-requests")
async def get_demo_requests():
    requests = await db_instance.demo_requests.find({}, {"_id": 0}).to_list(1000)

    for req in requests:
        if isinstance(req.get("created_at"), str):
            req["created_at"] = datetime.fromisoformat(req["created_at"])

    return requests

app.include_router(api_router)

# ─── SAFE ROUTE REGISTRATION ─────────────────────────────

for route_path in [
    "app.routes.auth",
    "app.routes.blog",
    "app.routes.crm",
    "app.routes.seo",
    "app.routes.dashboard",
    "app.api.routes.insights",
]:
    router = safe_import_router(route_path)
    if router:
        app.include_router(router)

# ─── HEALTH ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy"}