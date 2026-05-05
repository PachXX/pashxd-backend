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
from bson import ObjectId

# ─── LOAD ENV FIRST ──────────────────────────────────────

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ─── LOGGING ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── DATABASE ─────────────────────────────────────────────

mongo_url = os.getenv("MONGO_URL")
if not mongo_url:
    logger.warning("⚠️ MONGO_URL not set, using default")
    mongo_url = "mongodb://localhost:27017"

client = AsyncIOMotorClient(mongo_url)
db_name = os.getenv("DB_NAME", "pashxd")
db_instance = client[db_name]

# ─── LIFESPAN ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup and shutdown"""
    try:
        from app.config import database
        database.db = db_instance
        logger.info(f"✅ Connected to MongoDB: {db_name}")
    except Exception as e:
        logger.warning(f"⚠️ DB config setup skipped: {e}")

    # Try to seed admin
    try:
        await seed_admin()
    except Exception as e:
        logger.warning(f"⚠️ Admin seed skipped: {e}")

    yield

    try:
        client.close()
        logger.info("🔴 MongoDB closed")
    except Exception:
        pass

async def seed_admin():
    """Create default admin user if not exists"""
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

# ─── APP ─────────────────────────────────────────────────

app = FastAPI(
    title="PashxD API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# ✅ ADD MIDDLEWARE HERE
@app.middleware("http")
async def add_process_time_header(request, call_next):
    response = await call_next(request)
    response.headers["X-App-Name"] = "PashxD"
    return response


# ─── CORS ────────────────────────────────────────────────

def get_allowed_origins():
    origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "https://pashx.com",
        "https://www.pashx.com",
        "https://admin.pashx.com",
        "https://pashxd-admin.vercel.app"
    ]

    # Add from ENV
    cors_env = os.getenv("CORS_ORIGINS")
    if cors_env:
        origins.extend([o.strip() for o in cors_env.split(",") if o.strip()])

    # Add deployed frontend URLs
    frontend_url = os.getenv("FRONTEND_URL")
    admin_url = os.getenv("ADMIN_URL")

    if frontend_url:
        origins.append(frontend_url)

    if admin_url:
        origins.append(admin_url)

    return list(set(origins))


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_origin_regex=r"https://.*\.vercel\.app",  # ✅ allow ALL Vercel previews
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# ─── API ROUTER ──────────────────────────────────────────

api_router = APIRouter(prefix="/api")

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

# ─── DEMO REQUESTS ───────────────────────────────────────

@api_router.post("/demo-requests", response_model=DemoRequest)
async def create_demo(input: DemoRequestCreate):
    """Create demo request AND auto-convert to contact + deal"""
    from bson import ObjectId

    # 1. Save demo request
    demo_dict = input.model_dump()
    demo_obj = DemoRequest(**demo_dict)
    doc = demo_obj.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()

    await db_instance.demo_requests.insert_one(doc)
    logger.info(f"✅ Demo request created: {input.email}")

    # 2. AUTO-CREATE CONTACT (or get existing)
    try:
        existing_contact = await db_instance.contacts.find_one({"email": input.email})

        if not existing_contact:
            contact_doc = {
                "name": input.name,
                "email": input.email,
                "phone": "",
                "company": input.company,
                "role": input.role or "",
                "industry": input.industry or "",
                "source": "demo_request",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            contact_result = await db_instance.contacts.insert_one(contact_doc)
            contact_id = str(contact_result.inserted_id)
            logger.info(f"✅ Contact created: {contact_id}")
        else:
            contact_id = str(existing_contact["_id"])
            logger.info(f"ℹ️ Contact exists: {contact_id}")

        # 3. AUTO-CREATE DEAL
        deal_doc = {
            "title": f"{input.company} - Demo Request",
            "contact_id": contact_id,
            "value": 0,
            "currency": "EUR",
            "stage": "lead",
            "probability": 10,
            "notes": input.message or "",
            "source": "demo_request",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        deal_result = await db_instance.deals.insert_one(deal_doc)
        logger.info(f"✅ Deal created: {str(deal_result.inserted_id)}")

    except Exception as e:
        logger.error(f"❌ CRM auto-conversion failed: {e}")
        # Don't fail the demo request - it's already saved

    return demo_obj

@api_router.get("/demo-requests")
async def get_demo_requests():
    """Get all demo requests"""
    requests = await db_instance.demo_requests.find({}, {"_id": 0}).to_list(1000)

    for req in requests:
        if isinstance(req.get("created_at"), str):
            req["created_at"] = datetime.fromisoformat(req["created_at"])

    return requests

# Include main API router
app.include_router(api_router)

# ─── SAFE ROUTE IMPORT ───────────────────────────────────

def safe_import_router(import_path, name="router"):
    """Safely import a router without crashing the app"""
    try:
        module = __import__(import_path, fromlist=[name])
        router = getattr(module, name, None)
        if router is None:
            logger.error(f"❌ {import_path} has no '{name}' attribute")
            return None
        logger.info(f"✅ Loaded {import_path}")
        return router
    except ImportError as e:
        logger.error(f"❌ Cannot import {import_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error loading {import_path}: {e}")
        return None

# ─── LOAD APP ROUTES ─────────────────────────────────────

route_modules = [
    "app.routes.auth",
    "app.routes.blog",
    "app.routes.crm",
    "app.routes.email",  # ← ADD EMAIL ROUTE HERE
    "app.routes.seo",
    "app.routes.dashboard",
]

for route_path in route_modules:
    router = safe_import_router(route_path)
    if router:
        app.include_router(router)

# Try insights route (may be in different locations)
for insights_path in ["app.api.routes.insights", "app.routes.insights"]:
    router = safe_import_router(insights_path)
    if router:
        app.include_router(router)
        break

# ─── HEALTH CHECK ────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "service": "pashxd-api"}

logger.info("🚀 Application initialized")