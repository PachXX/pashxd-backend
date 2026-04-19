from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path
import os
import logging
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import json

# Import new admin routes
from app.routes.auth import router as auth_router
from app.routes.blog import router as blog_router
from app.routes.crm import router as crm_router
from app.routes.seo import router as seo_router
from app.routes.dashboard import sitemap_router, dashboard_router
from app.utils.hash import hash_password

# Import insights router (KEEP THIS ONLY ONCE)
from app.api.routes.insights import router as insights_router

# ─── SETUP ────────────────────────────────────────────────

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db_name = os.environ.get('DB_NAME', 'pashxd')
db_instance = client[db_name]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─── LIFESPAN ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from app.config import database
    database.db = db_instance
    logger.info(f"✅ Connected to MongoDB: {db_name}")

    # Seed admin user
    await seed_admin()

    yield

    # Shutdown
    client.close()
    logger.info("❌ MongoDB connection closed")


async def seed_admin():
    """Create default admin user if none exists"""
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
        logger.info(f"✅ Admin user created: {admin_email}")
    else:
        logger.info(f"ℹ️  Admin user already exists: {admin_email}")


# ─── APP SETUP ────────────────────────────────────────────

app = FastAPI(
    title="PashxD API",
    description="AI-Powered Industrial OS & Construction ERP — Backend API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ─── CORS ─────────────────────────────────────────────────

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:3000",
    "https://pashx.com",
    "https://www.pashx.com",
    "https://admin.pashx.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── EXISTING ROUTES (YOUR ORIGINAL CODE) ────────────────

api_router = APIRouter(prefix="/api")


# Define Models
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


# Routes
@api_router.get("/")
async def root():
    return {"message": "PashxD API", "version": "1.0.0"}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    _ = await db_instance.status_checks.insert_one(doc)
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db_instance.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


@api_router.post("/demo-requests", response_model=DemoRequest)
async def create_demo_request(input: DemoRequestCreate):
    demo_dict = input.model_dump()
    demo_obj = DemoRequest(**demo_dict)
    doc = demo_obj.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    _ = await db_instance.demo_requests.insert_one(doc)
    return demo_obj


@api_router.get("/demo-requests", response_model=List[DemoRequest])
async def get_demo_requests():
    requests = await db_instance.demo_requests.find({}, {"_id": 0}).to_list(1000)
    for req in requests:
        if isinstance(req.get('created_at'), str):
            req['created_at'] = datetime.fromisoformat(req['created_at'])
    return requests


# Include existing API router
app.include_router(api_router)


# ─── NEW ADMIN ROUTES ─────────────────────────────────────

app.include_router(auth_router)
app.include_router(blog_router)
app.include_router(crm_router)
app.include_router(seo_router)
app.include_router(sitemap_router)
app.include_router(dashboard_router)
app.include_router(insights_router)  # ← MOVED HERE, ONLY ONCE


@app.get("/health")
async def health():
    return {"status": "healthy"}