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
JWT_TTL_SECONDS: int = int(os.getenv("JWT_TTL_SECONDS", str(365 * 24 * 60 * 60)))  # 1 year default
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

ADMIN_IDENTIFIERS: frozenset = frozenset(
    item.strip().lower()
    for item in os.getenv("ADMIN_IDENTIFIERS", "admin@exampartner.com").split(",")
    if item.strip()
)

FOUNDING_CAP: int = int(os.getenv("FOUNDING_CAP", "500"))

# ---------------------------------------------------------------------------
# Candidate countries — ISO 3166-1 alpha-2
# ---------------------------------------------------------------------------
# Lives here rather than in a service module because two unrelated write paths
# validate against it: paper_rules.country (which country a paper rule applies
# to) and users.country (which country a candidate sits in). If those two lists
# ever diverged the failure would be silent — a rule authored for a code the
# profile can never hold simply never matches, and the country-agnostic row is
# served instead, which is indistinguishable from correct fallback behaviour.
#
# alpha-2 specifically: it matches the device locale used to pre-select the
# country on the client, and it sidesteps "Gambia" / "The Gambia" naming.
#
# Scope is the four non-Ghana WAEC/NECO countries. Ghana is deliberately
# absent — it is the platform's exclusion boundary, not an unsupported-yet
# entry. Only extend this alongside a decision to serve that country.
SUPPORTED_COUNTRIES: frozenset = frozenset({
    "NG",  # Nigeria
    "GM",  # The Gambia
    "LR",  # Liberia
    "SL",  # Sierra Leone
})

# Free access: oldest year per subject only (no flat question caps).
# Enforced in questions and CBT routes via access_control.get_free_year_for_subject.
FREE_SAMPLE_LIMIT_OBJ: int = int(os.getenv("FREE_SAMPLE_LIMIT_OBJ", "10"))
FREE_SAMPLE_LIMIT_THEORY: int = int(os.getenv("FREE_SAMPLE_LIMIT_THEORY", "2"))

# ---------------------------------------------------------------------------
# Email — password reset
# ---------------------------------------------------------------------------
# RESEND_API_KEY:      Set in Render environment. No default — email is
#                      silently skipped in dev if this is absent.
# SUPPORT_EMAIL_FROM:  The "From" address shown to the user.
#                      Must be a verified sender domain in your Resend account.
# ---------------------------------------------------------------------------
RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
SUPPORT_EMAIL_FROM: str = os.getenv("SUPPORT_EMAIL_FROM", "ExamPartner <noreply@exampartner.com>")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("exampartner")


def db_conn():
    """Return a DB connection (Postgres if DATABASE_URL is set, else SQLite)."""
    from db import get_db
    return get_db(DB_PATH)
