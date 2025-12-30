
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

FREE_SAMPLE_LIMIT = int(os.getenv("FREE_SAMPLE_LIMIT", "10"))

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
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
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
        cur.execute(
            "INSERT INTO users (identifier, salt, pw_hash, is_paid) VALUES (?, ?, ?, 0)",
            (identifier, salt, pw_hash),
        )
        db.commit()
    except Exception:
        raise HTTPException(status_code=409, detail="User already exists")
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
    cur.execute("SELECT is_paid FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return {"identifier": identifier, "is_paid": bool(row["is_paid"])}


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
    if not user:
        return False
    identifier = user.get("sub")
    if not identifier:
        return False
    db = db_conn()
    cur = db.cursor()
    cur.execute("SELECT is_paid FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    db.close()
    return bool(row["is_paid"]) if row else False


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
    if not _is_paid_user(user) and offset >= FREE_SAMPLE_LIMIT:
        raise HTTPException(status_code=402, detail="Free preview limit reached. Upgrade to continue.")

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
    if not _is_paid_user(user) and offset >= FREE_SAMPLE_LIMIT:
        raise HTTPException(status_code=402, detail="Free preview limit reached. Upgrade to continue.")

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

