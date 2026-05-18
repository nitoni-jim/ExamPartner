"""
app.py — ExamPartner API entry point.

Responsibilities:
- App init and middleware
- Mount static files
- Register all routers
- Health check

All business logic lives in routes/ and services/.
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import DB_PATH, logger
from db import init_db
from paystack_routes import router as paystack_router
from routes.admin import router as admin_router
from routes.auth import router as auth_router
from routes.cbt import router as cbt_router
from routes.feedback import router as feedback_router
from routes.questions import router as questions_router
from routes.theory import router as theory_router

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="ExamPartner API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
DIAGRAMS_DIR = Path(os.getenv("DIAGRAMS_DIR", str(Path(__file__).resolve().parent / "diagrams")))
DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/diagrams", StaticFiles(directory=str(DIAGRAMS_DIR)), name="diagrams")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(feedback_router)
app.include_router(questions_router)
app.include_router(cbt_router)
app.include_router(admin_router)
app.include_router(paystack_router)   # payments — already uses APIRouter
app.include_router(theory_router)     # AI theory grading

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    logger.info("Starting ExamPartner API v2")
    init_db()
    logger.info("Database initialized OK")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "ExamPartner API",
        "version": "2.0.0",
        "db_path": DB_PATH,
        "db_mode": ("postgres" if os.getenv("DATABASE_URL") else "sqlite"),
    }
