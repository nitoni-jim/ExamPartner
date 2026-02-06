
import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import logging
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
    return {"token": token, "identifier": identifier, "is_paid": False}


@app.post("/auth/login", response_model=AuthResp)
def login(body: AuthReq):
    identifier = body.identifier.strip().lower()
    db = db_conn()
    cur = db.cursor()
    cur.execute("SELECT identifier, salt, pw_hash, is_paid FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    salt = row["salt"]
    pw_hash = row["pw_hash"]
    if _hash_pw(body.password, salt) != pw_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = make_token(identifier)
    return {"token": token, "identifier": identifier, "is_paid": bool(row["is_paid"])}


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
        "SELECT is_paid, paid_until, plan, is_founding, email FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    is_paid_active = _is_paid_user({"sub": identifier})

    paid_until = row.get("paid_until")
    return {
        "identifier": identifier,
        # legacy flag (kept for compatibility)
        "is_paid": bool(row.get("is_paid")),
        # preferred flag for access gating
        "is_paid_active": bool(is_paid_active),
        "paid_until": paid_until.isoformat() if paid_until else None,
        "plan": row.get("plan") or "free",
        "is_founding": bool(row.get("is_founding") or False),
        "email": row.get("email"),
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
        where_y.append("qtype = ?"); params_y.append(qtype)
    if exam:
        where_y.append("exam = ?"); params_y.append(exam)
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
def _jloads(x: Optional[str]):
    return json.loads(x) if x else None


def _row_to_question(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "exam": row.get("exam"),
        "year": row.get("year"),
        "subject": row.get("subject"),
        "paper": row.get("paper"),
        "section": row.get("section"),
        "type": row["qtype"],
        "page": row.get("page"),
        "marks": row.get("marks"),
        "question_text": row["question_text"],
        "options": _jloads(row.get("options_json")),
        "answer": row.get("answer"),
        "explanation": row.get("explanation"),
        "sub_questions": _jloads(row.get("sub_questions_json")),
        "solution_steps": _jloads(row.get("solution_steps_json")),
        "diagrams": _jloads(row.get("diagrams_json")) or [],
    }


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

    db = db_conn()
    cur = db.cursor()
    cur.execute("SELECT is_paid, paid_until FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    db.close()
    if not row:
        return False

    paid_until = row.get("paid_until")
    if paid_until is not None:
        now = datetime.now(timezone.utc)
        return paid_until > now

    # legacy fallback
    return bool(row.get("is_paid"))



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

    # ✅ Objective preview cap (unpaid): max 10 total
    if not is_paid:
        if offset >= FREE_SAMPLE_LIMIT_OBJ:
            raise HTTPException(status_code=402, detail="Free preview limit reached. Upgrade to continue.")
        remaining = FREE_SAMPLE_LIMIT_OBJ - offset
        limit = min(limit, remaining)

    where_sql, params = _build_filters("objective", exam, year, subject)

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT id, exam, year, subject, paper, section, qtype, page, marks, question_text,
               options_json, answer, explanation, sub_questions_json,
               solution_steps_json, diagrams_json
        FROM questions
        WHERE {where_sql}
        ORDER BY COALESCE(sort_key, 999999999), id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = cur.fetchall()
    db.close()

    return {"items": [_row_to_question(r) for r in rows], "limit": limit, "offset": offset}

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
        SELECT id, exam, year, subject, paper, section, qtype, page, marks, question_text,
               options_json, answer, explanation, sub_questions_json,
               solution_steps_json, diagrams_json
        FROM questions
        WHERE {where_sql}
        ORDER BY COALESCE(sort_key, 999999999), id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = cur.fetchall()
    db.close()

    return {"items": [_row_to_question(r) for r in rows], "limit": limit, "offset": offset}


@app.get("/question/{qid}")
def get_question(qid: str, user: Optional[Dict[str, Any]] = Depends(get_current_user)):
    db = db_conn()
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, exam, year, subject, paper, section, qtype, page, marks, question_text,
               options_json, answer, explanation, sub_questions_json,
               solution_steps_json, diagrams_json
        FROM questions
        WHERE id = ?
        """,
        (qid,),
    )
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Question not found")

    return _row_to_question(row)
