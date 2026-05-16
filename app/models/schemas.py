from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ─── USER MODELS ─────────────────────────────────────────

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime


# ─── BLOG MODELS ─────────────────────────────────────────

class ContentType(str, Enum):
    markdown = "markdown"
    html = "html"

class BlogStatus(str, Enum):
    draft = "draft"
    published = "published"

class BlogCreate(BaseModel):
    title: str
    content: str
    excerpt: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = []
    cover_image: Optional[str] = None
    status: BlogStatus = BlogStatus.draft
    # Content type support
    content_type: ContentType = ContentType.markdown
    custom_html: Optional[str] = None
    custom_css: Optional[str] = None
    # SEO fields (manual)
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image: Optional[str] = None
    keywords: List[str] = []
    word_count: Optional[int] = None

class BlogUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    excerpt: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    cover_image: Optional[str] = None
    status: Optional[BlogStatus] = None
    content_type: Optional[ContentType] = None
    custom_html: Optional[str] = None
    custom_css: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image: Optional[str] = None
    keywords: Optional[List[str]] = None
    word_count: Optional[int] = None

class BlogOut(BaseModel):
    id: str
    title: str
    slug: str
    content: str
    excerpt: Optional[str]
    category: Optional[str]
    tags: List[str]
    cover_image: Optional[str]
    status: str
    content_type: str
    custom_html: Optional[str]
    custom_css: Optional[str]
    meta_title: Optional[str]
    meta_description: Optional[str]
    og_image: Optional[str]
    keywords: List[str]
    readability_score: Optional[float]
    word_count: Optional[int]
    reading_time: Optional[int]
    created_at: datetime
    updated_at: datetime


# ─── CRM MODELS ─────────────────────────────────────────

class ContactStatus(str, Enum):
    new = "new"
    qualified = "qualified"
    customer = "customer"
    lost = "lost"

class ContactCreate(BaseModel):
    name: str
    email: EmailStr
    company: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    source: Optional[str] = None   # website | marketplace | referral
    status: ContactStatus = ContactStatus.new
    notes: Optional[str] = None

class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    source: Optional[str] = None
    status: Optional[ContactStatus] = None
    notes: Optional[str] = None

class ContactOut(BaseModel):
    id: str
    name: str
    email: str
    company: Optional[str]
    phone: Optional[str]
    country: Optional[str]
    source: Optional[str]
    status: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


# ─── DEAL MODELS ─────────────────────────────────────────

class DealStage(str, Enum):
    lead = "lead"
    qualified = "qualified"
    proposal = "proposal"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"

class DealCreate(BaseModel):
    title: str
    contact_id: str
    value: Optional[float] = None
    currency: str = "EUR"
    stage: DealStage = DealStage.lead
    notes: Optional[str] = None

class DealUpdate(BaseModel):
    title: Optional[str] = None
    value: Optional[float] = None
    currency: Optional[str] = None
    stage: Optional[DealStage] = None
    notes: Optional[str] = None

class DealOut(BaseModel):
    id: str
    title: str
    contact_id: str
    contact_name: Optional[str]
    value: Optional[float]
    currency: str
    stage: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


# ─── ACTIVITY MODELS ─────────────────────────────────────

class ActivityType(str, Enum):
    email = "email"
    call = "call"
    meeting = "meeting"
    note = "note"
    demo = "demo"

class ActivityCreate(BaseModel):
    contact_id: str
    type: ActivityType
    title: str
    body: Optional[str] = None

class ActivityOut(BaseModel):
    id: str
    contact_id: str
    contact_name: Optional[str]
    type: str
    title: str
    body: Optional[str]
    created_at: datetime


# ─── RESPONSE MODELS ─────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

class MessageResponse(BaseModel):
    message: str

class DashboardStats(BaseModel):
    total_blogs: int
    published_blogs: int
    draft_blogs: int
    total_contacts: int
    new_contacts: int
    total_deals: int
    deals_by_stage: dict
    total_deal_value: float
    recent_activities: list