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

# ---------------------------------------------------------------------------
# Object storage — candidate diagram attachments (Sprint A)
# ---------------------------------------------------------------------------
# Cloudflare R2 rather than S3: the grading path re-reads every candidate
# image on each grading call, and on S3 each of those reads is billed
# egress. R2 zero-rates egress. Data cost is already a live constraint for
# this user base on the client side; the same argument applies server-side.
#
# R2_ENDPOINT_URL must NOT include the bucket name. Cloudflare's bucket
# settings page displays the S3 API URL with the bucket appended
# (https://<account>.r2.cloudflarestorage.com/<bucket>); pasting that form
# makes boto3 append the bucket a second time and every request 404s.
# The correct value stops at .com.
#
# R2_REGION is "auto" for R2 regardless of the bucket's physical location —
# it is not the same field as the bucket's Location setting.
#
# These are read by services/storage_service.py, which falls back to a local
# directory when they are unset so the upload path is developable without a
# bucket. That fallback is NOT durable on Render — a deploy wipes the disk
# and every stored attachment with it — so production must set all four.
# /health reports which backend is live.
R2_BUCKET: str            = os.getenv("R2_BUCKET", "")
R2_ENDPOINT_URL: str      = os.getenv("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY_ID: str     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_REGION: str            = os.getenv("R2_REGION", "auto")

# Upload cap, applied to the ORIGINAL bytes before canonicalisation.
# 12 MB: Android downscales to roughly 1600px before upload, which lands
# well under 1 MB for a photo of a notebook page. The headroom covers the
# case where downscaling is skipped and a raw 12-megapixel capture is sent.
ATTACHMENT_MAX_BYTES: int = int(os.getenv("ATTACHMENT_MAX_BYTES", str(12 * 1024 * 1024)))

# Longest edge of the stored raster — a server-side backstop only. Set above
# the client downscale target deliberately, so the server never becomes what
# destroys the faint-pencil letterform detail the naming criteria depend on.
ATTACHMENT_MAX_EDGE_PX: int = int(os.getenv("ATTACHMENT_MAX_EDGE_PX", "2000"))

# Retention for candidate scripts, in days. Long enough that a student
# revisiting last term's practice still sees what they submitted; short
# enough that the platform is not indefinitely holding photographs taken by
# secondary-school candidates.
ATTACHMENT_RETENTION_DAYS: int = int(os.getenv("ATTACHMENT_RETENTION_DAYS", "90"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("exampartner")


def db_conn():
    """Return a DB connection (Postgres if DATABASE_URL is set, else SQLite)."""
    from db import get_db
    return get_db(DB_PATH)
