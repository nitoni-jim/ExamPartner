"""
config.py — centralised env/config for ExamPartner.
Import from here instead of reading os.getenv in multiple places.
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

DB_PATH: str = os.getenv("DB_PATH", "exam_partner.db")
JWT_SECRET: str = os.getenv("JWT_SECRET", "dev_secret_change_me")
JWT_TTL_SECONDS: int = int(os.getenv("JWT_TTL_SECONDS", "86400"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

ADMIN_IDENTIFIERS: frozenset = frozenset(
    item.strip().lower()
    for item in os.getenv("ADMIN_IDENTIFIERS", "admin@exampartner.com").split(",")
    if item.strip()
)

FOUNDING_CAP: int = int(os.getenv("FOUNDING_CAP", "500"))

# Free access: oldest year per subject only (no flat question caps).
# Enforced in questions and CBT routes via access_control.get_free_year_for_subject.
FREE_SAMPLE_LIMIT_OBJ: int = int(os.getenv("FREE_SAMPLE_LIMIT_OBJ", "10"))
FREE_SAMPLE_LIMIT_THEORY: int = int(os.getenv("FREE_SAMPLE_LIMIT_THEORY", "2"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("exampartner")


def db_conn():
    """Return a DB connection (Postgres if DATABASE_URL is set, else SQLite)."""
    from db import get_db
    return get_db(DB_PATH)
