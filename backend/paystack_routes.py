# paystack_routes.py (Neon Postgres-ready, cleaned, feature-complete for MVP)

import os
import hmac
import json
import hashlib
import base64
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from db import get_db, _using_postgres  # uses Postgres if DATABASE_URL is set; else SQLite

load_dotenv()
# -----------------------------
# Token helper (same scheme as app.py)
# -----------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _sign(data: bytes, secret: str) -> str:
    return _b64url(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest())

def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        b64, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "==")
        if _sign(raw, JWT_SECRET) != sig:
            return None
        payload = json.loads(raw.decode("utf-8"))
        exp = int(payload.get("exp", 0) or 0)
        if exp and int(time.time()) > exp:
            return None
        return payload
    except Exception:
        return None

def require_user(request: Request) -> Dict[str, Any]:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    payload = read_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return payload

def is_email(v: str) -> bool:
    v = (v or "").strip().lower()
    return ("@" in v) and ("." in v.split("@", 1)[-1])


router = APIRouter(prefix="/payments", tags=["payments"])


# -----------------------------
# ENV helpers
# -----------------------------
def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


PAYSTACK_SECRET_KEY = env_str("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = env_str("PAYSTACK_PUBLIC_KEY", "")
ADMIN_SECRET = env_str("ADMIN_SECRET", "")
AUTO_DOWNGRADE_ON_REFUND = env_bool("AUTO_DOWNGRADE_ON_REFUND", False)

# Price gate (₦1,000 in kobo)
MIN_AMOUNT_KOBO = int(env_str("MIN_AMOUNT_KOBO", "100000"))

JWT_SECRET = env_str("JWT_SECRET", "dev_secret_change_me")

# Platform-specific callback URLs for Paystack hosted checkout.
# Web redirects to Cloudflare Pages; Android uses a deep link.
# Set both on Render — do not use a single shared callback URL.
PAYSTACK_WEB_CALLBACK_URL = env_str(
    "PAYSTACK_WEB_CALLBACK_URL",
    "https://exampartner.pages.dev/?payment=callback"
)
# Android App Link: Paystack redirects to this HTTPS URL after payment.
# Android OS intercepts it at the system level (Digital Asset Links verification),
# closes Chrome Custom Tab automatically, and opens MainActivity.
# No website page is shown to the user.
PAYSTACK_ANDROID_CALLBACK_URL = env_str(
    "PAYSTACK_ANDROID_CALLBACK_URL",
    "https://exampartner.app/payment-callback"
)



# -----------------------------
# Admin auth + audit logging
# -----------------------------
def require_admin(request: Request) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET not set on server")
    provided = (request.headers.get("x-admin-key") or "").strip()
    if not provided or provided != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def audit_admin_action(
    request: Request,
    action: str,
    reference: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Writes a small audit record for admin actions.
    Never raises (so it won't break admin calls).
    """
    try:
        actor_ip = None
        try:
            actor_ip = request.client.host if request.client else None
        except Exception:
            actor_ip = None

        user_agent = (request.headers.get("user-agent") or "").strip()

        payload_json = None
        if payload is not None:
            try:
                payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                payload_json = None

        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO admin_audit_log (action, reference, actor_ip, user_agent, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (action or "").strip(),
                    (reference or "").strip() or None,
                    actor_ip,
                    user_agent,
                    payload_json,
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        return


# -----------------------------
# Paystack helpers
# -----------------------------
def _paystack_headers() -> Dict[str, str]:
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="PAYSTACK_SECRET_KEY not configured")
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ExamPartner/1.0",
    }


def paystack_api_get(path: str) -> Dict[str, Any]:
    url = "https://api.paystack.co" + path
    try:
        r = requests.get(url, headers=_paystack_headers(), timeout=30)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=f"Paystack HTTP {r.status_code}: {r.text}")
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Paystack request error: {e}")


def paystack_api_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = "https://api.paystack.co" + path
    try:
        r = requests.post(url, headers=_paystack_headers(), json=payload, timeout=30)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=f"Paystack HTTP {r.status_code}: {r.text}")
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Paystack request error: {e}")


def verify_paystack_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """
    Paystack webhook signature verification (HMAC-SHA512 of raw request body).
    """
    if not signature or not PAYSTACK_SECRET_KEY:
        return False
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -----------------------------
# Replay protection helpers
# -----------------------------
def is_webhook_reference_seen(reference: str) -> bool:
    reference = (reference or "").strip()
    if not reference:
        return False
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT reference FROM webhook_receipts WHERE reference = ?", (reference,))
        return cur.fetchone() is not None
    finally:
        db.close()


def remember_webhook_reference(reference: str, event_type: str, body_hash: str) -> None:
    """
    Idempotency record: Postgres-safe.
    """
    reference = (reference or "").strip()
    if not reference:
        return
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO webhook_receipts (reference, event_type, body_hash, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (reference) DO NOTHING
            """,
            (reference, event_type or "", body_hash or "", datetime.utcnow().isoformat()),
        )
        db.commit()
    finally:
        db.close()


# -----------------------------
# Payment state helpers
# -----------------------------
def extract_paystack_channel(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    channel = payload.get("channel")
    if channel is None and isinstance(payload.get("data"), dict):
        channel = payload["data"].get("channel")

    if channel is None:
        return None

    channel = str(channel).strip()
    return channel or None


def update_payment_status(reference: str, status: str, raw_json: Optional[Dict[str, Any]] = None, channel: Optional[str] = None) -> None:
    ref = (reference or "").strip()
    if not ref:
        return

    raw_str = None
    if raw_json is not None:
        try:
            raw_str = json.dumps(raw_json, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            raw_str = None

    normalized_channel = (channel or "").strip() or None

    db = get_db()
    try:
        cur = db.cursor()
        if raw_str is not None:
            cur.execute("UPDATE payments SET status = ?, raw_json = ?, channel = COALESCE(?, channel) WHERE reference = ?", (status, raw_str, normalized_channel, ref))
        else:
            cur.execute("UPDATE payments SET status = ?, channel = COALESCE(?, channel) WHERE reference = ?", (status, normalized_channel, ref))
        db.commit()
    finally:
        db.close()


def persist_payment_payload_by_identifier(
    identifier: str,
    reference: str,
    source: str,
    pay_data: Optional[Dict[str, Any]] = None,
) -> None:
    identifier = (identifier or "").strip().lower()
    ref = (reference or "").strip()
    if not identifier or not ref:
        return

    pay_data = pay_data or {}
    raw_json = None
    try:
        raw_json = json.dumps(pay_data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw_json = None

    channel = extract_paystack_channel(pay_data)
    amount_kobo = int(pay_data.get("amount") or 0)
    currency = (pay_data.get("currency") or "NGN").strip().upper()
    status = (pay_data.get("status") or "unknown").strip() or "unknown"

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM users WHERE lower(identifier) = ?", (identifier,))
        urow = cur.fetchone()
        if not urow:
            return

        try:
            user_id = int(urow["id"])
        except Exception:
            user_id = int(urow[0])

        cur.execute("SELECT id FROM payments WHERE reference = ?", (ref,))
        prow = cur.fetchone()
        if prow:
            cur.execute(
                "UPDATE payments SET user_id = ?, provider = ?, amount_kobo = ?, currency = ?, status = ?, channel = ?, raw_json = ? WHERE reference = ?",
                (user_id, source or "paystack", amount_kobo, currency, status, channel, raw_json, ref),
            )
        else:
            cur.execute(
                """
                INSERT INTO payments (user_id, provider, reference, amount_kobo, currency, status, channel, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source or "paystack",
                    ref,
                    amount_kobo,
                    currency,
                    status,
                    channel,
                    raw_json,
                    datetime.utcnow().isoformat(),
                ),
            )
        db.commit()
    finally:
        db.close()


def maybe_downgrade_user_on_refund(reference: str) -> None:
    if not AUTO_DOWNGRADE_ON_REFUND:
        return

    ref = (reference or "").strip()
    if not ref:
        return

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT user_id FROM payments WHERE reference = ?", (ref,))
        row = cur.fetchone()
        if not row:
            return
        user_id = int(row["user_id"])
        # ✅ boolean
        cur.execute("UPDATE users SET is_paid = ? WHERE id = ?", (False, user_id))
        db.commit()
    finally:
        db.close()


def mark_user_paid_by_identifier(
    identifier: str,
    reference: str,
    source: str,
    pay_data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Apply access based on the transaction amount:
    - ₦1,000  => Founding (365 days) [first 500 users only]
    - ₦2,000+ => Core (365 days)
    Extends paid_until from max(now, paid_until).
    Founding status (is_founding) is assigned once and never removed.
    """
    identifier = (identifier or "").strip().lower()
    ref = (reference or "").strip()
    if not identifier or not ref:
        return

    pay_data = pay_data or {}
    amount_kobo = int(pay_data.get("amount") or 0)
    currency = (pay_data.get("currency") or "NGN").strip().upper()
    status = (pay_data.get("status") or "success").strip()

    # Plan thresholds (kobo)
    FOUNDING_AMOUNT_KOBO = 1000 * 100   # ₦1,000
    CORE_AMOUNT_KOBO = 2000 * 100       # ₦2,000
    FOUNDING_CAP = 500                  # limited to 500 users
    FOUNDING_DAYS = 365                 # 1 year (same as Core)
    CORE_DAYS = 365                     # 1 year

    # Decide plan from amount
    if amount_kobo >= CORE_AMOUNT_KOBO:
        plan = "core"
        duration_days = CORE_DAYS
    elif amount_kobo >= FOUNDING_AMOUNT_KOBO:
        plan = "founding"
        duration_days = FOUNDING_DAYS
    else:
        # too small / ignore
        return

    raw_json = None
    try:
        raw_json = json.dumps(pay_data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw_json = None

    channel = extract_paystack_channel(pay_data)

    # Placeholder style: Postgres uses %s, SQLite uses ?
    ph = "%s" if _using_postgres() else "?"

    db = get_db()
    try:
        cur = db.cursor()

        # Load user state
        cur.execute(f"SELECT id, is_founding, paid_until FROM users WHERE lower(identifier) = {ph}", (identifier,))
        urow = cur.fetchone()
        if not urow:
            return

        # Support both dict-like and tuple rows
        try:
            user_id = int(urow.get("id"))
            is_founding = bool(urow.get("is_founding") or False)
            paid_until = urow.get("paid_until")
        except Exception:
            user_id = int(urow[0])
            is_founding = bool(urow[1]) if len(urow) > 1 else False
            paid_until = urow[2] if len(urow) > 2 else None

        now = datetime.now(timezone.utc)

        # Normalize paid_until if returned as string (SQLite stores ISO strings)
        if isinstance(paid_until, str):
            try:
                paid_until_dt = datetime.fromisoformat(paid_until.replace("Z", "+00:00"))
            except Exception:
                paid_until_dt = None
        else:
            paid_until_dt = paid_until

        base = paid_until_dt if (paid_until_dt and paid_until_dt > now) else now
        new_paid_until = base + timedelta(days=duration_days)

        # Founding assignment (only when buying founding and not already founder)
        if plan == "founding" and not is_founding:
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_founding = " + ("TRUE" if _using_postgres() else "1"))
            crow = cur.fetchone()
            try:
                founding_count = int(crow.get("c") if hasattr(crow, "get") else crow[0])
            except Exception:
                founding_count = int(crow[0])

            if founding_count >= FOUNDING_CAP:
                # Founding is closed for new users. (Frontend should hide it, but keep backend safe.)
                raise HTTPException(status_code=403, detail="Founding is full (500 students). Please upgrade to Core.")

            # qualify as founder
            cur.execute(f"UPDATE users SET is_founding = " + ("TRUE" if _using_postgres() else "1") + f", plan = {ph} WHERE id = {ph}", ("founding", user_id))
            is_founding = True

        # If user buys Core, set plan=core (keep is_founding if they already have it)
        if plan == "core":
            cur.execute(f"UPDATE users SET plan = {ph} WHERE id = {ph}", ("core", user_id))

        # Mark paid + extend expiry
        # Store paid_until as TIMESTAMPTZ in Postgres, or ISO in SQLite
        paid_until_store = new_paid_until if _using_postgres() else new_paid_until.isoformat()
        cur.execute(
            f"UPDATE users SET is_paid = " + ("TRUE" if _using_postgres() else "1") + f", paid_until = {ph} WHERE id = {ph}",
            (paid_until_store, user_id),
        )

        # If identifier looks like an email, store it as receipt email (helps phone-number accounts)
        if is_email(identifier):
            try:
                cur.execute(f"UPDATE users SET email = COALESCE(email, {ph}) WHERE id = {ph}", (identifier, user_id))
            except Exception:
                try:
                    cur.execute(f"UPDATE users SET email = {ph} WHERE id = {ph}", (identifier, user_id))
                except Exception:
                    pass

        # Log payment (idempotent on reference)
        cur.execute(f"SELECT id FROM payments WHERE reference = {ph}", (ref,))
        prow = cur.fetchone()
        if not prow:
            cur.execute(
                """
                INSERT INTO payments (user_id, provider, reference, amount_kobo, currency, status, channel, raw_json, created_at)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                """.format(ph=ph),
                (
                    user_id,
                    source or "paystack",
                    ref,
                    amount_kobo,
                    currency,
                    status,
                    channel,
                    raw_json,
                    datetime.utcnow().isoformat(),
                ),
            )

        db.commit()
    finally:
        db.close()

def admin_grant_access_by_identifier(
    identifier: str,
    plan: str,
    duration_days: Optional[int] = None,
) -> dict:
    """
    Admin-only manual access grant.
    Sets the complete membership state so no manual SQL is needed.
    """
    identifier = (identifier or "").strip().lower()
    plan = (plan or "").strip().lower()

    if not identifier:
        raise HTTPException(status_code=400, detail="Missing identifier")

    if plan not in ("founding", "core"):
        raise HTTPException(status_code=400, detail="Invalid plan. Use 'founding' or 'core'.")

    if duration_days is None:
        duration_days = 365

    ph = "%s" if _using_postgres() else "?"

    db = get_db()
    try:
        cur = db.cursor()

        cur.execute(f"SELECT id, is_founding, paid_until FROM users WHERE lower(identifier) = {ph}", (identifier,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            user_id = int(row["id"])
            existing_is_founding = bool(row["is_founding"] or False)
            paid_until = row["paid_until"]
        except Exception:
            user_id = int(row[0])
            existing_is_founding = bool(row[1]) if len(row) > 1 else False
            paid_until = row[2] if len(row) > 2 else None

        now = datetime.now(timezone.utc)

        if isinstance(paid_until, str):
            try:
                paid_until_dt = datetime.fromisoformat(paid_until.replace("Z", "+00:00"))
            except Exception:
                paid_until_dt = None
        else:
            paid_until_dt = paid_until

        base = paid_until_dt if (paid_until_dt and paid_until_dt > now) else now
        new_paid_until = base + timedelta(days=int(duration_days))
        paid_until_store = new_paid_until if _using_postgres() else new_paid_until.isoformat()

        if plan == "founding":
            cur.execute(
                f"UPDATE users SET is_paid = {ph}, is_founding = {ph}, plan = {ph}, paid_until = {ph} WHERE id = {ph}",
                (True, True, "founding", paid_until_store, user_id),
            )
        else:
            cur.execute(
                f"UPDATE users SET is_paid = {ph}, is_founding = {ph}, plan = {ph}, paid_until = {ph} WHERE id = {ph}",
                (True, existing_is_founding, "core", paid_until_store, user_id),
            )

        db.commit()

        return {
            "ok": True,
            "identifier": identifier,
            "plan": plan,
            "is_founding": True if plan == "founding" else existing_is_founding,
            "paid_until": new_paid_until.isoformat(),
        }
    finally:
        db.close()


# -----------------------------
# API models
# -----------------------------
class InitializeReq(BaseModel):
    """
    Request body for POST /payments/initialize.
    Amount is decided server-side from plan — client must not send amount.
    platform: "web" | "android" — determines which callback_url is used.
    """
    email: str        # for Paystack customer record
    identifier: str   # account identifier (email or phone)
    plan: str         # "founding" | "core"
    platform: str = "web"  # "web" | "android"


# FIX: email is Optional — Android sends blank email when profile cache is empty at verify time.
# Identifier is resolved from Paystack metadata (meta_identifier) and customer email first;
# Android email field is only a last resort fallback.
class VerifyReq(BaseModel):
    reference: str
    email: Optional[str] = None


class AdminRefundReq(BaseModel):
    reference: str
    amount_kobo: Optional[int] = None
    customer_note: Optional[str] = None
    merchant_note: Optional[str] = None


class AdminGrantAccessReq(BaseModel):
    identifier: str
    plan: str  # founding | core
    duration_days: Optional[int] = None


# -----------------------------
# Routes
# -----------------------------
@router.get("/public-key")
def paystack_public_key():
    if not PAYSTACK_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="PAYSTACK_PUBLIC_KEY not configured")
    return {"ok": True, "public_key": PAYSTACK_PUBLIC_KEY}

@router.post("/initialize")
def initialize_payment(req: InitializeReq, request: Request):
    """
    Create a Paystack hosted checkout transaction.

    Used by Web (redirect) and Android (Chrome Custom Tab).
    Replaces the old inline iframe / SDK chargeCard flow.

    Amount is decided server-side — plan determines kobo:
      founding = 100,000 kobo (₦1,000)
      core     = 200,000 kobo (₦2,000)

    Includes channels: card, bank, ussd, bank_transfer.
    """
    user       = require_user(request)
    identifier = (req.identifier or "").strip().lower()
    email      = (req.email or "").strip().lower()
    plan       = (req.plan or "").strip().lower()

    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    if not email or not is_email(email):
        email = identifier if is_email(identifier) else f"{identifier}@exampartner.app"
    if plan not in ("founding", "core"):
        raise HTTPException(status_code=400, detail="plan must be founding or core")

    # Server-side amount — never trust client
    PLAN_AMOUNTS = {"founding": 100_000, "core": 200_000}
    amount_kobo = PLAN_AMOUNTS[plan]

    payload = {
        "email":        email,
        "amount":       amount_kobo,
        "currency":     "NGN",
        "callback_url": PAYSTACK_ANDROID_CALLBACK_URL
        if (req.platform or "web").strip().lower() == "android"
        else PAYSTACK_WEB_CALLBACK_URL,
        "channels":     ["card", "bank", "ussd", "bank_transfer"],
        "metadata": {
            "identifier": identifier,
            "plan":       plan,
            "custom_fields": [
                {"display_name": "ExamPartner Identifier", "variable_name": "identifier", "value": identifier},
                {"display_name": "Plan", "variable_name": "plan", "value": plan},
            ],
        },
    }

    resp = paystack_api_post("/transaction/initialize", payload)
    data = resp.get("data") or {}

    authorization_url = data.get("authorization_url") or ""
    reference         = data.get("reference") or ""
    access_code       = data.get("access_code") or ""

    if not authorization_url or not reference:
        raise HTTPException(status_code=502, detail="Paystack did not return a checkout URL")

    return {
        "ok":                True,
        "authorization_url": authorization_url,
        "reference":         reference,
        "access_code":       access_code,
    }


@router.get("/history")
def payment_history(request: Request, limit: int = 20):
    """Logged-in user's payment history (latest first)."""
    user = require_user(request)
    identifier = (user.get("sub") or "").strip().lower()

    try:
        limit = int(limit)
    except Exception:
        limit = 20
    limit = max(1, min(200, limit))

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM users WHERE lower(identifier) = ?", (identifier,))
        urow = cur.fetchone()
        if not urow:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            user_id = int(urow["id"])
        except Exception:
            user_id = int(urow[0])

        cur.execute(
            """
            SELECT provider, reference, amount_kobo, currency, status, channel, raw_json, created_at
            FROM payments
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    items: List[Dict[str, Any]] = []
    for r in rows:
        try:
            provider = r["provider"]
            reference = r["reference"]
            amount_kobo = int(r["amount_kobo"] or 0)
            currency = r["currency"]
            status = r["status"]
            channel = r["channel"]
            raw_json = r["raw_json"]
            created_at = r["created_at"]
        except Exception:
            provider, reference, amount_kobo, currency, status, channel, raw_json, created_at = r

        if not channel and raw_json:
            try:
                channel = extract_paystack_channel(json.loads(raw_json))
            except Exception:
                channel = None

        items.append(
            {
                "provider": provider,
                "reference": reference,
                "amount": int(amount_kobo // 100),
                "currency": currency,
                "status": status,
                "channel": channel or None,
                "created_at": created_at,
            }
        )

    return {"ok": True, "limit": limit, "items": items}


@router.post("/verify")
def verify_payment(req: VerifyReq):
    ref = (req.reference or "").strip()
    email = (req.email or "").strip().lower()

    if not ref:
        raise HTTPException(status_code=400, detail="Missing reference")
    # FIX: removed hard email guard — Android sends blank email when profile cache is empty.
    # Identifier is resolved from Paystack metadata and customer email first; email is last resort only.

    resp = paystack_api_get(f"/transaction/verify/{ref}")
    if not resp.get("status"):
        raise HTTPException(status_code=400, detail="Paystack verification failed")

    tx = resp.get("data") or {}
    status = (tx.get("status") or "").strip().lower()
    amount = int(tx.get("amount") or 0)

    customer = tx.get("customer") or {}
    customer_email = (customer.get("email") or "").strip().lower()
    metadata = tx.get("metadata") or {}
    meta_identifier = (metadata.get("identifier") or "").strip().lower()

    # beneficiary first, payer email second, request email last
    final_identifier = meta_identifier or customer_email or email

    persist_payment_payload_by_identifier(final_identifier, ref, source="paystack:verify", pay_data=tx)

    if status != "success":
        raise HTTPException(status_code=400, detail=f"Payment not successful: {status}")

    if amount < MIN_AMOUNT_KOBO:
        raise HTTPException(status_code=400, detail="Amount too low")

    mark_user_paid_by_identifier(final_identifier, ref, source="paystack:verify", pay_data=tx)
    update_payment_status(ref, status, raw_json=tx, channel=extract_paystack_channel(tx))

    return {"ok": True, "reference": ref, "email": final_identifier, "amount_kobo": amount}


@router.post("/webhook")
async def paystack_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-paystack-signature")

    if not verify_paystack_signature(raw, signature):
        raise HTTPException(status_code=401, detail="Invalid Paystack signature")

    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = (event.get("event") or "").strip()
    data = event.get("data") or {}

    reference = (data.get("reference") or "").strip()
    if not reference:
        tx = data.get("transaction")
        if isinstance(tx, dict):
            reference = (tx.get("reference") or "").strip()

    if not reference:
        return {"ok": True, "ignored": "no_reference", "event": event_type}

    body_hash = sha256_hex(raw)
    if is_webhook_reference_seen(reference):
        return {"ok": True, "ignored": "replay", "event": event_type, "reference": reference}

    remember_webhook_reference(reference, event_type, body_hash)

    if "refund" in event_type.lower():
        update_payment_status(reference, "refunded", raw_json=event, channel=extract_paystack_channel(event))
        maybe_downgrade_user_on_refund(reference)
        return {"ok": True, "event": event_type, "reference": reference, "refunded": True}

    verify = paystack_api_get(f"/transaction/verify/{reference}")
    if not verify.get("status"):
        return {"ok": True, "event": event_type, "reference": reference, "verified": False}

    tx = verify.get("data") or {}
    paid = (tx.get("status") == "success")

    customer = tx.get("customer") or {}
    email = (customer.get("email") or "").strip().lower()

    metadata = tx.get("metadata") or {}
    meta_identifier = (metadata.get("identifier") or "").strip().lower()
    # beneficiary first
    final_identifier = meta_identifier or email

    if final_identifier:
        persist_payment_payload_by_identifier(final_identifier, reference, source=f"webhook:{event_type}", pay_data=tx)

    if paid:
        amount = int(tx.get("amount") or 0)
        if amount >= MIN_AMOUNT_KOBO and final_identifier:
            mark_user_paid_by_identifier(final_identifier, reference, source=f"webhook:{event_type}", pay_data=tx)
        update_payment_status(reference, tx.get("status") or "success", raw_json=tx, channel=extract_paystack_channel(tx))

    if not paid:
        update_payment_status(reference, (tx.get("status") or event_type or "unknown").strip() or "unknown", raw_json=tx, channel=extract_paystack_channel(tx))

    return {"ok": True, "event": event_type, "reference": reference, "paid": bool(paid)}


# -----------------------------
# Admin endpoints
# -----------------------------
@router.post("/admin/reconcile/{reference}")
def admin_reconcile(reference: str, request: Request):
    require_admin(request)
    ref = (reference or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="Missing reference")

    audit_admin_action(request, action="admin_reconcile", reference=ref, payload={"reference": ref})

    resp = paystack_api_get(f"/transaction/verify/{ref}")
    if not resp.get("status"):
        raise HTTPException(status_code=400, detail="Paystack verification failed")

    tx = resp.get("data") or {}
    paid = (tx.get("status") == "success")

    customer = tx.get("customer") or {}
    email = (customer.get("email") or "").strip().lower()
    metadata = tx.get("metadata") or {}
    meta_identifier = (metadata.get("identifier") or "").strip().lower()
    # beneficiary first
    final_identifier = meta_identifier or email

    if final_identifier:
        persist_payment_payload_by_identifier(final_identifier, ref, source="admin:reconcile", pay_data=tx)

    if paid and final_identifier:
        mark_user_paid_by_identifier(final_identifier, ref, source="admin:reconcile", pay_data=tx)
        update_payment_status(ref, tx.get("status") or "success", raw_json=tx, channel=extract_paystack_channel(tx))
    else:
        update_payment_status(ref, (tx.get("status") or "unknown").strip() or "unknown", raw_json=tx, channel=extract_paystack_channel(tx))

    return {"ok": True, "reference": ref, "paid": bool(paid), "identifier": final_identifier or None, "channel": extract_paystack_channel(tx)}


@router.post("/admin/topup/credit/{reference}")
def admin_topup_credit(reference: str, request: Request):
    """
    Admin endpoint to manually credit AI top-up for a paid but unverified reference.

    Use when:
      - User paid successfully but network dropped before verify completed
      - The ep_topup_ reference is confirmed successful in Paystack dashboard

    Uses same x-admin-key auth as other admin endpoints.
    Idempotent — safe to call multiple times for the same reference.

    Usage:
      curl -X POST https://exampartner-backend.onrender.com/payments/admin/topup/credit/ep_topup_xxx
           -H "x-admin-key: your-admin-key"
    """
    require_admin(request)
    ref = (reference or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="Missing reference")

    audit_admin_action(request, action="admin_topup_credit", reference=ref, payload={"reference": ref})

    # Idempotency check — already credited?
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id, user_identifier, credits_total, credits_used FROM ai_grading_credit_purchases "
            "WHERE payment_reference = ?",
            (ref,),
        )
        existing = cur.fetchone()
    finally:
        db.close()

    if existing:
        identifier = existing["user_identifier"] if hasattr(existing, "keys") else existing[1]
        remaining  = _topup_extra_remaining(identifier)
        return {
            "ok":                      True,
            "already_credited":        True,
            "identifier":              identifier,
            "extra_credits_remaining": remaining,
            "message":                 f"Reference already processed for {identifier}.",
        }

    # Verify with Paystack
    resp = paystack_api_get(f"/transaction/verify/{ref}")
    if not resp.get("status"):
        raise HTTPException(status_code=400, detail="Paystack verification failed")

    tx     = resp.get("data") or {}
    status = (tx.get("status") or "").strip().lower()
    amount = int(tx.get("amount") or 0)

    if status != "success":
        raise HTTPException(status_code=400, detail=f"Payment not successful on Paystack: {status}")

    if amount < TOPUP_EXTRA_50_AMOUNT_KOBO:
        raise HTTPException(status_code=400, detail=f"Amount too low: {amount} kobo (expected {TOPUP_EXTRA_50_AMOUNT_KOBO})")

    # Get identifier from metadata or customer email
    customer        = tx.get("customer") or {}
    email           = (customer.get("email") or "").strip().lower()
    metadata        = tx.get("metadata") or {}
    meta_identifier = (metadata.get("identifier") or "").strip().lower()
    identifier      = meta_identifier or email

    if not identifier:
        raise HTTPException(status_code=400, detail="Could not determine user identifier from transaction metadata")

    # Credit 50 markings
    now         = datetime.now(timezone.utc)
    expires_at  = (now + timedelta(days=365)).isoformat()
    purchase_id = secrets.token_hex(16)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ai_grading_credit_purchases
              (id, user_identifier, credits_total, credits_used, amount_paid,
               currency, payment_reference, status, expires_at)
            VALUES (?, ?, ?, 0, ?, 'NGN', ?, 'active', ?)
            """,
            (purchase_id, identifier, TOPUP_EXTRA_50_CREDITS, amount, ref, expires_at),
        )
        db.commit()
    except Exception as e:
        db.rollback()
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            remaining = _topup_extra_remaining(identifier)
            return {
                "ok":                      True,
                "already_credited":        True,
                "identifier":              identifier,
                "extra_credits_remaining": remaining,
                "message":                 f"Reference already processed for {identifier}.",
            }
        raise HTTPException(status_code=500, detail=f"Failed to record credits: {e}")
    finally:
        db.close()

    remaining = _topup_extra_remaining(identifier)
    return {
        "ok":                      True,
        "already_credited":        False,
        "identifier":              identifier,
        "credits_added":           TOPUP_EXTRA_50_CREDITS,
        "extra_credits_remaining": remaining,
        "expires_at":              expires_at,
        "message":                 f"50 extra AI theory markings credited to {identifier}.",
    }


@router.post("/admin/refund")
def admin_refund(req: AdminRefundReq, request: Request):
    require_admin(request)

    ref = (req.reference or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="Missing reference")

    payload: Dict[str, Any] = {"transaction": ref}
    if req.amount_kobo is not None:
        payload["amount"] = int(req.amount_kobo)
    if req.customer_note:
        payload["customer_note"] = req.customer_note
    if req.merchant_note:
        payload["merchant_note"] = req.merchant_note

    audit_admin_action(request, action="admin_refund", reference=ref, payload=payload)

    out = paystack_api_post("/refund", payload)

    update_payment_status(ref, "refund_queued", raw_json=out, channel=extract_paystack_channel(out))

    return {"ok": True, "reference": ref, "paystack": out}


@router.get("/admin/audit")
def admin_audit(request: Request, limit: int = 20):
    require_admin(request)

    try:
        limit = int(limit)
    except Exception:
        limit = 20
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    audit_admin_action(request, action="admin_view_audit", payload={"limit": limit})

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, action, reference, actor_ip, user_agent, payload_json, created_at
            FROM admin_audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "action": r["action"],
                "reference": r["reference"],
                "actor_ip": r["actor_ip"],
                "user_agent": r["user_agent"],
                "payload_json": r["payload_json"],
                "created_at": r["created_at"],
            }
        )

    return {"ok": True, "limit": limit, "items": items}


@router.post("/admin/grant-access")
def admin_grant_access(req: AdminGrantAccessReq, request: Request):
    require_admin(request)

    result = admin_grant_access_by_identifier(
        identifier=req.identifier,
        plan=req.plan,
        duration_days=req.duration_days,
    )

    audit_admin_action(
        request,
        action="admin_grant_access",
        reference=(req.identifier or "").strip().lower(),
        payload={
            "identifier": (req.identifier or "").strip().lower(),
            "plan": (req.plan or "").strip().lower(),
            "duration_days": req.duration_days or 365,
        },
    )

    return result


@router.post("/admin/mark-paid")
def admin_mark_paid(
    request: Request,
    identifier: str = Query(...),
    plan: str = Query("core"),
    duration_days: int = Query(365),
):
    """
    Backward-compatible admin route — now delegates to admin_grant_access_by_identifier
    for full membership state update (is_paid, is_founding, plan, paid_until).
    Example: /payments/admin/mark-paid?identifier=0703...&plan=founding
    """
    require_admin(request)

    result = admin_grant_access_by_identifier(
        identifier=identifier,
        plan=plan,
        duration_days=duration_days,
    )

    audit_admin_action(
        request,
        action="admin_mark_paid",
        reference=(identifier or "").strip().lower(),
        payload={
            "identifier": (identifier or "").strip().lower(),
            "plan": (plan or "").strip().lower(),
            "duration_days": duration_days,
        },
    )

    return result


# =============================================================================
# AI Theory Marking Top-up — Phase 3
# =============================================================================
# Top-up package: 50 extra AI theory markings for ₦1,000, valid 1 year.
# These are SEPARATE from subscription — they only affect AI grading quota.
# Top-up can be purchased by free or paid users alike.
# =============================================================================

TOPUP_EXTRA_50_AMOUNT_KOBO = 100000   # ₦1,000 in kobo
TOPUP_EXTRA_50_CREDITS     = 50
TOPUP_PACKAGE_KEY          = "extra_50"


class TopupInitReq(BaseModel):
    package: str  # must be "extra_50"


class TopupVerifyReq(BaseModel):
    reference: str


@router.post("/ai-grading/topup/init")
def topup_init(req: TopupInitReq, request: Request):
    """
    Initialise a Paystack transaction for an AI theory marking top-up.

    Request:  { "package": "extra_50" }
    Response: { "ok": true, "authorization_url": "...", "reference": "..." }

    Requires Bearer token. Platform is detected from User-Agent to select
    the correct callback URL (Android deep link vs web redirect).
    """
    user = require_user(request)
    identifier = user.get("sub", "").strip().lower()
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    package = (req.package or "").strip().lower()
    if package != TOPUP_PACKAGE_KEY:
        raise HTTPException(status_code=400, detail=f"Unknown top-up package: {package}")

    # Detect platform for callback URL
    ua = (request.headers.get("user-agent") or "").lower()
    is_android = "android" in ua or "okhttp" in ua or "dalvik" in ua
    callback_url = PAYSTACK_ANDROID_CALLBACK_URL if is_android else PAYSTACK_WEB_CALLBACK_URL

    reference = f"ep_topup_{secrets.token_hex(12)}"

    payload = {
        "amount":       TOPUP_EXTRA_50_AMOUNT_KOBO,
        "email":        identifier if is_email(identifier) else f"{identifier}@exampartner.internal",
        "reference":    reference,
        "callback_url": callback_url,
        "metadata": {
            "identifier": identifier,
            "package":    TOPUP_PACKAGE_KEY,
            "credits":    TOPUP_EXTRA_50_CREDITS,
        },
    }

    resp = paystack_api_post("/transaction/initialize", payload)
    data = resp.get("data") or {}
    authorization_url = data.get("authorization_url") or ""
    if not authorization_url:
        raise HTTPException(status_code=502, detail="Paystack did not return an authorization URL")

    return {
        "ok":               True,
        "authorization_url": authorization_url,
        "reference":        reference,
    }


@router.post("/ai-grading/topup/verify")
def topup_verify(req: TopupVerifyReq, request: Request):
    """
    Verify a Paystack top-up transaction and credit 50 AI markings.

    Idempotent: if the reference has already been processed, returns the
    existing purchase info without adding credits again.

    Request:  { "reference": "ep_topup_..." }
    Response: {
        "ok": true,
        "credits_added": 50,
        "extra_credits_remaining": 50,
        "expires_at": "...",
        "message": "50 extra AI theory markings added successfully."
    }
    """
    user = require_user(request)
    identifier = user.get("sub", "").strip().lower()
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    reference = (req.reference or "").strip()
    if not reference:
        raise HTTPException(status_code=400, detail="reference is required")

    # ── Idempotency check — do not add credits twice ──────────────────────
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id, credits_total, credits_used, expires_at FROM ai_grading_credit_purchases "
            "WHERE payment_reference = ?",
            (reference,),
        )
        existing = cur.fetchone()
    finally:
        db.close()

    if existing:
        # Already processed — return current state without adding credits
        existing_id      = existing["id"] if hasattr(existing, "keys") else existing[0]
        credits_total    = existing["credits_total"] if hasattr(existing, "keys") else existing[1]
        credits_used     = existing["credits_used"] if hasattr(existing, "keys") else existing[2]
        expires_at_str   = existing["expires_at"] if hasattr(existing, "keys") else existing[3]
        remaining = _topup_extra_remaining(identifier)
        return {
            "ok":                      True,
            "credits_added":           0,
            "extra_credits_remaining": remaining,
            "expires_at":              expires_at_str,
            "message":                 "This reference has already been processed.",
        }

    # ── Verify with Paystack ──────────────────────────────────────────────
    resp = paystack_api_get(f"/transaction/verify/{reference}")
    if not resp.get("status"):
        raise HTTPException(status_code=400, detail="Paystack verification failed")

    tx     = resp.get("data") or {}
    status = (tx.get("status") or "").strip().lower()
    amount = int(tx.get("amount") or 0)

    if status != "success":
        raise HTTPException(status_code=400, detail=f"Payment not successful: {status}")

    if amount < TOPUP_EXTRA_50_AMOUNT_KOBO:
        raise HTTPException(status_code=400, detail="Payment amount too low for this package")

    # Confirm package from metadata
    metadata = tx.get("metadata") or {}
    meta_pkg = (metadata.get("package") or "").strip().lower()
    if meta_pkg and meta_pkg != TOPUP_PACKAGE_KEY:
        raise HTTPException(status_code=400, detail="Reference is not a top-up transaction")

    # ── Credit the user ───────────────────────────────────────────────────
    now   = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=365)).isoformat()
    purchase_id = secrets.token_hex(16)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO ai_grading_credit_purchases
              (id, user_identifier, credits_total, credits_used, amount_paid,
               currency, payment_reference, status, expires_at)
            VALUES (?, ?, ?, 0, ?, 'NGN', ?, 'active', ?)
            """,
            (purchase_id, identifier, TOPUP_EXTRA_50_CREDITS,
             amount, reference, expires_at),
        )
        db.commit()
    except Exception as e:
        db.rollback()
        # If a UNIQUE constraint fires here, another request beat us — idempotent
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            remaining = _topup_extra_remaining(identifier)
            return {
                "ok":                      True,
                "credits_added":           0,
                "extra_credits_remaining": remaining,
                "expires_at":              expires_at,
                "message":                 "This reference has already been processed.",
            }
        raise HTTPException(status_code=500, detail="Failed to record top-up credits")
    finally:
        db.close()

    remaining = _topup_extra_remaining(identifier)
    return {
        "ok":                      True,
        "credits_added":           TOPUP_EXTRA_50_CREDITS,
        "extra_credits_remaining": remaining,
        "expires_at":              expires_at,
        "message":                 f"{TOPUP_EXTRA_50_CREDITS} extra AI theory markings added successfully.",
    }


def _topup_extra_remaining(identifier: str) -> int:
    """Returns total valid non-expired top-up credits for a user."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT COALESCE(SUM(credits_total - credits_used), 0) AS remaining
            FROM ai_grading_credit_purchases
            WHERE user_identifier = ?
              AND status = 'active'
              AND credits_used < credits_total
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (identifier, now_iso),
        )
        row = cur.fetchone()
        return int((row["remaining"] if hasattr(row, "keys") else row[0]) or 0)
    finally:
        db.close()
