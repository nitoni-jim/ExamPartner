import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import logging
import random
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple

from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header, Request, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


from db import get_db, init_db
from paystack_routes import router as paystack_router

# -----------------------------
# ENV / CONFIG
# -----------------------------
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "exam_partner.db")
JWT_SECRET = os.getenv("JWT_SECRET", "dev_secret_change_me")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "86400"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DIAGRAMS_DIR = Path(os.getenv("DIAGRAMS_DIR", str(Path(__file__).resolve().parent / "diagrams")))
# Ensure diagrams dir exists (local + deployed)
DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)

cors_origins_raw = os.getenv("CORS_ORIGINS", "http://127.0.0.1:5173,http://127.0.0.1:5500")
CORS_ORIGINS = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

FREE_SAMPLE_LIMIT_OBJ = int(os.getenv("FREE_SAMPLE_LIMIT_OBJ", "10"))
FREE_SAMPLE_LIMIT_THEORY = int(os.getenv("FREE_SAMPLE_LIMIT_THEORY", "2"))
ADMIN_IDENTIFIERS = {
    item.strip().lower()
    for item in os.getenv("ADMIN_IDENTIFIERS", "admin@exampartner.com").split(",")
    if item.strip()
}


# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("exampartner")

# -----------------------------
# APP
# -----------------------------
app = FastAPI(title="ExamPartner API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # ✅ MVP: allow all
    allow_credentials=False,      # ✅ must be False when using "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


# Serve diagrams (served at /static/diagrams/<filename>)
app.mount("/static/diagrams", StaticFiles(directory=str(DIAGRAMS_DIR)), name="diagrams")

# Payments routes
app.include_router(paystack_router)


@app.on_event("startup")
def startup():
    logger.info("Starting ExamPartner API")
    init_db()  # <-- Postgres if DATABASE_URL set, else SQLite
    logger.info("Database initialized OK")


# -----------------------------
# HEALTH
# -----------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "ExamPartner API",
        "db_path": DB_PATH,
        "db_mode": ("postgres" if os.getenv("DATABASE_URL") else "sqlite"),
    }


@app.get("/founding/status")
def founding_status():
    """
    Returns whether Founding (₦1,000) is still open for NEW users.
    Existing founders can still renew; frontend can decide that.
    """
    FOUNDING_CAP = int(os.getenv("FOUNDING_CAP", "100"))
    using_pg = bool(os.getenv("DATABASE_URL"))

    db = db_conn()
    try:
        cur = db.cursor()
        # Postgres uses TRUE/FALSE, SQLite uses 1/0
        cur.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_founding = " + ("TRUE" if using_pg else "1")
        )
        row = cur.fetchone()

        # handle dict-like or tuple rows
        try:
            count = int(row.get("c") if hasattr(row, "get") else row[0])
        except Exception:
            count = int(row[0])

        return {"cap": FOUNDING_CAP, "count": count, "open": count < FOUNDING_CAP}
    finally:
        db.close()


# -----------------------------
# DB helper
# -----------------------------
def db_conn():
    """Return a DB connection (Postgres if DATABASE_URL is set, else SQLite)."""
    return get_db(DB_PATH)


# -----------------------------
# AUTH (JWT-ish minimal)
# -----------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _sign(data: bytes, secret: str) -> str:
    return _b64url(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest())


def make_token(sub: str, ttl_seconds: int = JWT_TTL_SECONDS) -> str:
    payload = {"sub": sub, "exp": int(time.time()) + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(raw, JWT_SECRET)
    return f"{_b64url(raw)}.{sig}"


def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        b64, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "==")
        if _sign(raw, JWT_SECRET) != sig:
            return None
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def get_current_user(authorization: Optional[str] = Header(default=None)) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return read_token(token)


# -----------------------------
# MODELS
# -----------------------------
class AuthReq(BaseModel):
    identifier: str
    password: str


class AuthResp(BaseModel):
    token: str
    identifier: str
    is_paid: bool
    is_admin: bool = False


class PlatformFeedbackReq(BaseModel):
    category: str
    message: str
    source_area: str = "footer"


class QuestionFeedbackReq(BaseModel):
    question_id: str
    category: str
    message: str
    source_area: Optional[str] = None


# -----------------------------
# USERS
# -----------------------------
def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


@app.post("/auth/register", response_model=AuthResp)
def register(body: AuthReq):
    identifier = body.identifier.strip().lower()
    if not identifier or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Invalid identifier/password")

    salt = secrets.token_hex(16)
    pw_hash = _hash_pw(body.password, salt)

    db = db_conn()
    cur = db.cursor()
    try:
        # ✅ Use a real boolean for Postgres (works in SQLite too)
        cur.execute(
            "INSERT INTO users (identifier, salt, pw_hash, is_paid) VALUES (?, ?, ?, ?)",
            (identifier, salt, pw_hash, False),
        )
        db.commit()
    except Exception as e:
        # ✅ Only claim "already exists" when it's truly a unique/duplicate error
        msg = (str(e) or "").lower()
        logger.exception("Register failed for identifier=%s", identifier)

        if "unique" in msg or "duplicate" in msg or "already exists" in msg:
            raise HTTPException(status_code=409, detail="User already exists")

        # Any other DB error is NOT "user exists"
        raise HTTPException(status_code=500, detail="Registration failed. Server DB error.")
    finally:
        db.close()

    token = make_token(identifier)
    return {"token": token, "identifier": identifier, "is_paid": False, "is_admin": _is_admin_identifier(identifier)}


@app.post("/auth/login", response_model=AuthResp)
def login(body: AuthReq):
    identifier = body.identifier.strip().lower()
    db = db_conn()
    cur = db.cursor()
    cur.execute("SELECT identifier, salt, pw_hash, is_paid, is_admin FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    salt = row["salt"]
    pw_hash = row["pw_hash"]
    if _hash_pw(body.password, salt) != pw_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = make_token(identifier)
    return {"token": token, "identifier": identifier, "is_paid": bool(row["is_paid"]), "is_admin": _is_admin_identifier(identifier) or bool(_row_get(row, "is_admin") or False)}


@app.get("/me")
def me(user: Optional[Dict[str, Any]] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        "SELECT is_paid, paid_until, plan, is_founding, email, is_admin FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    is_paid_active = _is_paid_user({"sub": identifier})

    paid_until = _row_get(row, "paid_until")
    return {
        "identifier": identifier,
        # legacy flag (kept for compatibility)
        "is_paid": bool(_row_get(row, "is_paid")),
        # preferred flag for access gating
        "is_paid_active": bool(is_paid_active),
        "paid_until": paid_until.isoformat() if paid_until else None,
        "plan": _row_get(row, "plan") or "free",
        "is_founding": bool(_row_get(row, "is_founding") or False),
        "email": _row_get(row, "email"),
        "is_admin": _is_admin_identifier(identifier) or bool(_row_get(row, "is_admin") or False),
    }


@app.post("/me/email")
def update_email(
    payload: Dict[str, str],
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    # very light validation (frontend already checks)
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    db = db_conn()
    cur = db.cursor()

    cur.execute(
        "UPDATE users SET email = ? WHERE identifier = ?",
        (email, identifier),
    )
    db.commit()

    return {"ok": True, "email": email}


def _require_feedback_value(value: Optional[str], field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return normalized


@app.post("/feedback/platform")
def submit_platform_feedback(
    body: PlatformFeedbackReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    feedback_id = secrets.token_hex(16)
    category = _require_feedback_value(body.category, "category")
    message = _require_feedback_value(body.message, "message")
    source_area = (body.source_area or "footer").strip() or "footer"
    user_identifier = user.get("sub") if user else None

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO feedback (id, feedback_type, question_id, source_area, category, message, user_identifier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, "platform", None, source_area, category, message, user_identifier),
        )
        db.commit()
    finally:
        db.close()

    return {"ok": True, "id": feedback_id}


@app.post("/feedback/question")
def submit_question_feedback(
    body: QuestionFeedbackReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    feedback_id = secrets.token_hex(16)
    question_id = _require_feedback_value(body.question_id, "question_id")
    category = _require_feedback_value(body.category, "category")
    message = _require_feedback_value(body.message, "message")
    source_area = (body.source_area or "").strip() or "question_detail"
    user_identifier = user.get("sub") if user else None

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO feedback (id, feedback_type, question_id, source_area, category, message, user_identifier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, "question", question_id, source_area, category, message, user_identifier),
        )
        db.commit()
    finally:
        db.close()

    return {"ok": True, "id": feedback_id}


# -----------------------------
# FILTER OPTIONS (dynamic)
# -----------------------------
@app.get("/filters")
def filters(
    qtype: Optional[str] = Query(default=None),
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
):
    where: List[str] = []
    params: List[Any] = []

    if qtype:
        where.append("qtype = ?")
        params.append(qtype)

    if exam:
        where.append("exam = ?")
        params.append(exam)

    if year is not None:
        where.append("year = ?")
        params.append(year)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    db = db_conn()
    cur = db.cursor()

    cur.execute(
        f"""SELECT DISTINCT exam FROM questions
        {('WHERE qtype = ?' if qtype else '')}
        AND exam IS NOT NULL AND TRIM(exam) <> ''""" if qtype else
        """SELECT DISTINCT exam FROM questions
        WHERE exam IS NOT NULL AND TRIM(exam) <> ''""",
        (qtype,) if qtype else None,
    )
    exams_rows = cur.fetchall()
    exams = sorted([r["exam"] for r in exams_rows if r.get("exam")])

    where_y: List[str] = []
    params_y: List[Any] = []
    if qtype:
        where_y.append("qtype = ?")
        params_y.append(qtype)
    if exam:
        where_y.append("exam = ?")
        params_y.append(exam)
    where_y_sql = ("WHERE " + " AND ".join(where_y)) if where_y else ""
    cur.execute(
        f"""SELECT DISTINCT year FROM questions
        {where_y_sql}
        {'AND' if where_y_sql else 'WHERE'} year IS NOT NULL""",
        tuple(params_y) if params_y else None,
    )
    years_rows = cur.fetchall()
    years = sorted([int(r["year"]) for r in years_rows if r.get("year") is not None], reverse=True)

    cur.execute(
        f"""SELECT DISTINCT subject FROM questions
        {where_sql}
        {'AND' if where_sql else 'WHERE'} subject IS NOT NULL AND TRIM(subject) <> ''""",
        tuple(params) if params else None,
    )
    subs_rows = cur.fetchall()
    subjects = sorted([r["subject"] for r in subs_rows if r.get("subject")])

    db.close()

    return {
        "ok": True,
        "exams": exams,
        "years": years,
        "subjects": subjects,
    }


# -----------------------------
# QUESTIONS
# -----------------------------



def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default

def _jloads(x: Optional[str]):
    try:
        return json.loads(x) if x else None
    except Exception:
        return None


def _normalize_explanation(qtype: Optional[str], raw_explanation: Optional[str]):
    if qtype == "objective":
        return _jloads(raw_explanation) if raw_explanation else []
    return raw_explanation or ""


def _normalize_passage_snapshot(raw_passage_snapshot: Optional[str]):
    parsed = _jloads(raw_passage_snapshot)
    if parsed is not None:
        return parsed
    return raw_passage_snapshot or None


def _row_to_question(row) -> Dict[str, Any]:
    qtype = row["qtype"]

    raw_passage_snapshot = _normalize_passage_snapshot(_row_get(row, "passage_snapshot"))

    joined_passage_text = _row_get(row, "passage_text")
    joined_passage_title = _row_get(row, "passage_title")
    joined_passage_type = _row_get(row, "passage_type")
    joined_passage_metadata = _jloads(_row_get(row, "passage_metadata_json"))

    if _row_get(row, "passage_id"):
        merged_passage_snapshot = {}

        if isinstance(raw_passage_snapshot, dict):
            merged_passage_snapshot.update(raw_passage_snapshot)

        if joined_passage_title and not merged_passage_snapshot.get("title"):
            merged_passage_snapshot["title"] = joined_passage_title

        if joined_passage_type and not merged_passage_snapshot.get("passage_type"):
            merged_passage_snapshot["passage_type"] = joined_passage_type

        if joined_passage_text and not merged_passage_snapshot.get("passage_text"):
            merged_passage_snapshot["passage_text"] = joined_passage_text

        if isinstance(joined_passage_metadata, dict):
            for k, v in joined_passage_metadata.items():
                if k not in merged_passage_snapshot:
                    merged_passage_snapshot[k] = v

        normalized_passage_snapshot = merged_passage_snapshot or raw_passage_snapshot
    else:
        normalized_passage_snapshot = raw_passage_snapshot

    return {
        "id": row["id"],
        "exam": _row_get(row, "exam"),
        "year": _row_get(row, "year"),
        "subject": _row_get(row, "subject"),
        "paper": _row_get(row, "paper"),
        "section": _row_get(row, "section"),
        "type": qtype,
        "page": _row_get(row, "page"),
        "marks": _row_get(row, "marks"),
        "question_text": row["question_text"],
        "options": _jloads(_row_get(row, "options_json")),
        "answer": _row_get(row, "answer"),
        "explanation": _normalize_explanation(qtype, _row_get(row, "explanation")),
        "sub_questions": _jloads(_row_get(row, "sub_questions_json")),
        "solution_steps": _jloads(_row_get(row, "solution_steps_json")),
        "diagrams": _jloads(_row_get(row, "diagrams_json")) or [],
        "answer_diagrams": _jloads(_row_get(row, "answer_diagrams_json")) or [],
        "explanation_diagrams": _jloads(_row_get(row, "explanation_diagrams_json")) or [],
        "tables": _jloads(_row_get(row, "tables_json")) or {},
        "passage_id": _row_get(row, "passage_id"),
        "passage_snapshot": normalized_passage_snapshot,
    }


def _is_admin_identifier(identifier: Optional[str]) -> bool:
    normalized = (identifier or "").strip().lower()
    return bool(normalized and normalized in ADMIN_IDENTIFIERS)


def _is_admin_user(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False

    identifier = (user.get("sub") or "").strip().lower()
    if _is_admin_identifier(identifier):
        return True
    if not identifier:
        return False

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute("SELECT is_admin FROM users WHERE identifier = ?", (identifier,))
        row = cur.fetchone()
    finally:
        db.close()

    return bool(row and _row_get(row, "is_admin"))


def _is_paid_user(user: Optional[Dict[str, Any]]) -> bool:
    """Paid access check.
    - If paid_until exists and is in the future => active
    - Else fallback to legacy is_paid (for older accounts)
    """
    if not user:
        return False
    identifier = user.get("sub")
    if not identifier:
        return False
    if _is_admin_identifier(identifier):
        return True

    db = db_conn()
    cur = db.cursor()
    cur.execute("SELECT is_paid, paid_until FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    db.close()
    if not row:
        return False

    paid_until = _row_get(row, "paid_until")
    if paid_until is not None:
        now = datetime.now(timezone.utc)
        return paid_until > now

    # legacy fallback
    return bool(_row_get(row, "is_paid"))



def _build_filters(
    qtype: str,
    exam: Optional[str],
    year: Optional[int],
    subject: Optional[str],
) -> Tuple[str, List[Any]]:
    where = ["qtype = ?"]
    params: List[Any] = [qtype]

    if exam:
        where.append("exam = ?")
        params.append(exam)

    if year is not None:
        where.append("year = ?")
        params.append(year)

    if subject:
        where.append("subject = ?")
        params.append(subject)

    return " AND ".join(where), params


@app.get("/questions/objective")
def list_objective(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default="NECO"),
    year: Optional[int] = Query(default=2023),
    subject: Optional[str] = Query(default="Mathematics"),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    is_paid = _is_paid_user(user)

    # Objective preview cap (unpaid): max 10 total
    if not is_paid:
        if offset >= FREE_SAMPLE_LIMIT_OBJ:
            raise HTTPException(status_code=402, detail="Free preview limit reached. Upgrade to continue.")
        remaining = FREE_SAMPLE_LIMIT_OBJ - offset
        limit = min(limit, remaining)

    where_sql, params = _build_filters("objective", exam, year, subject)

    where_sql = (
        where_sql
        .replace("qtype", "q.qtype")
        .replace("exam", "q.exam")
        .replace("year", "q.year")
        .replace("subject", "q.subject")
    )

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT q.id, q.exam, q.year, q.subject, q.paper, q.section, q.qtype, q.page, q.marks, q.question_text,
               q.options_json, q.answer, q.explanation, q.sub_questions_json,
               q.solution_steps_json, q.diagrams_json, q.answer_diagrams_json, q.explanation_diagrams_json, q.tables_json,
               q.passage_id, q.passage_snapshot,
               p.title AS passage_title,
               p.passage_type,
               p.passage_text,
               p.metadata_json AS passage_metadata_json
        FROM questions q
        LEFT JOIN passages p ON q.passage_id = p.id
        WHERE {where_sql}
        ORDER BY COALESCE(q.sort_key, 999999999), q.id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = cur.fetchall()
    db.close()

    return {
        "items": [_row_to_question(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }

# -----------------------------
# CBT — JAMB full-simulation endpoint
# -----------------------------
CBT_ENGLISH_SUBJECT = "Use of English"
CBT_ENGLISH_CAP = 60
CBT_OTHER_CAP = 40


@app.get("/cbt/questions")
def cbt_questions(
    subject: str = Query(...),
    exam: str = Query(default="JAMB"),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns a shuffled, deduplicated set of objective questions for one subject.
    - Pools ALL years for that subject (no year filter).
    - Deduplicates by exact question_text match (so repeated questions across
      years only appear once per session).
    - Caps at 60 for Use of English, 40 for all other subjects.
    - Requires paid access or admin.
    - Never reuses practice viewer preview caps or paywall logic.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for CBT.")

    if not (_is_paid_user(user) or _is_admin_user(user)):
        raise HTTPException(status_code=402, detail="CBT requires an active subscription.")

    subject = (subject or "").strip()
    exam = (exam or "JAMB").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required.")

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
    """
    SELECT q.id, q.exam, q.year, q.subject, q.paper, q.section, q.qtype, q.page, q.marks, q.question_text,
           q.options_json, q.answer, q.explanation, q.sub_questions_json,
           q.solution_steps_json, q.diagrams_json, q.answer_diagrams_json, q.explanation_diagrams_json,
           q.tables_json, q.passage_id, q.passage_snapshot,
           p.title AS passage_title,
           p.passage_type,
           p.passage_text,
           p.metadata_json AS passage_metadata_json
    FROM questions q
    LEFT JOIN passages p ON q.passage_id = p.id
    WHERE q.qtype = ? AND q.exam = ? AND q.subject = ?
    ORDER BY q.id
    """,
    ("objective", exam, subject),
)
        rows = cur.fetchall()
    finally:
        db.close()

    total_available = len(rows)

    # Deduplicate by exact question_text — keep first occurrence per text
    seen_texts: set = set()
    deduped = []
    for row in rows:
        text = (row["question_text"] or "").strip()
        if text and text in seen_texts:
            continue
        seen_texts.add(text)
        deduped.append(row)

    # Shuffle
    random.shuffle(deduped)

    # Cap by subject
    cap = CBT_ENGLISH_CAP if subject == CBT_ENGLISH_SUBJECT else CBT_OTHER_CAP
    capped = deduped[:cap]

    return {
        "items": [_row_to_question(r) for r in capped],
        "subject": subject,
        "total_available": total_available,
        "returned": len(capped),
    }


@app.get("/questions/theory")
def list_theory(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default="NECO"),
    year: Optional[int] = Query(default=2023),
    subject: Optional[str] = Query(default="Mathematics"),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    is_paid = _is_paid_user(user)
    # ✅ Theory preview cap (unpaid): max 2 total
    if not is_paid:
        if offset >= FREE_SAMPLE_LIMIT_THEORY:
            raise HTTPException(status_code=402, detail="Free preview limit reached. Upgrade to continue.")
        remaining = FREE_SAMPLE_LIMIT_THEORY - offset
        limit = min(limit, remaining)

    where_sql, params = _build_filters("theory", exam, year, subject)

    db = db_conn()
    cur = db.cursor()
    cur.execute(
    f"""
    SELECT q.id, q.exam, q.year, q.subject, q.paper, q.section, q.qtype, q.page, q.marks, q.question_text,
           q.options_json, q.answer, q.explanation, q.sub_questions_json,
           q.solution_steps_json, q.diagrams_json, q.answer_diagrams_json, q.explanation_diagrams_json, q.tables_json,
           q.passage_id, q.passage_snapshot,
           p.title AS passage_title,
           p.passage_type,
           p.passage_text,
           p.metadata_json AS passage_metadata_json
    FROM questions q
    LEFT JOIN passages p ON q.passage_id = p.id
    WHERE {where_sql.replace('qtype', 'q.qtype').replace('exam', 'q.exam').replace('year', 'q.year').replace('subject', 'q.subject')}
    ORDER BY q.year DESC, q.exam, q.subject, COALESCE(q.sort_key, 999999999), q.id
    LIMIT ? OFFSET ?
    """,
    (*params, limit, offset),
)
    rows = cur.fetchall()
    db.close()
    return {"items": [_row_to_question(r) for r in rows], "limit": limit, "offset": offset}


def _require_admin(user: Optional[Dict[str, Any]]) -> str:
    if not user or not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return str(user.get("sub") or "").strip().lower()


@app.get("/admin/questions")
def admin_list_questions(
    limit: int = 100,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    qtype: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    _require_admin(user)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where = []
    params: List[Any] = []
    if exam:
        where.append("exam = ?")
        params.append(exam)
    if year is not None:
        where.append("year = ?")
        params.append(year)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if qtype:
        where.append("qtype = ?")
        params.append(qtype)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS total FROM questions {where_sql}", tuple(params) if params else None)
        total_row = cur.fetchone()
        total = int(_row_get(total_row, "total", 0) if total_row else 0)

        cur.execute(
            f"""
            SELECT id, exam, year, subject, paper, section, qtype, page, marks, question_text,
                   options_json, answer, explanation, sub_questions_json,
                   solution_steps_json, diagrams_json, answer_diagrams_json, explanation_diagrams_json, tables_json,
                   passage_id, passage_snapshot
            FROM questions
            {where_sql}
            ORDER BY year DESC, exam, subject, COALESCE(sort_key, 999999999), id
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    return {"items": [_row_to_question(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/admin/feedback")
def admin_list_feedback(
    limit: int = 100,
    offset: int = 0,
    feedback_type: Optional[str] = Query(default=None),
    source_area: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    _require_admin(user)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where = []
    params: List[Any] = []
    if feedback_type:
        where.append("feedback_type = ?")
        params.append(feedback_type)
    if source_area:
        where.append("source_area = ?")
        params.append(source_area)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS total FROM feedback {where_sql}", tuple(params) if params else None)
        total_row = cur.fetchone()
        total = int(_row_get(total_row, "total", 0) if total_row else 0)
        cur.execute(
            f"""
            SELECT id, feedback_type, question_id, category, message, source_area, user_identifier, created_at
            FROM feedback
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/question/{qid}")
def get_question(qid: str, user: Optional[Dict[str, Any]] = Depends(get_current_user)):
    db = db_conn()
    cur = db.cursor()
    cur.execute(
    """
    SELECT q.id, q.exam, q.year, q.subject, q.paper, q.section, q.qtype, q.page, q.marks, q.question_text,
           q.options_json, q.answer, q.explanation, q.sub_questions_json,
           q.solution_steps_json, q.diagrams_json, q.answer_diagrams_json, q.explanation_diagrams_json, q.tables_json,
           q.passage_id, q.passage_snapshot,
           p.title AS passage_title,
           p.passage_type,
           p.passage_text,
           p.metadata_json AS passage_metadata_json
    FROM questions q
    LEFT JOIN passages p ON q.passage_id = p.id
    WHERE q.id = ?
    """,
    (qid,),
)
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Question not found")

    return _row_to_question(row)
