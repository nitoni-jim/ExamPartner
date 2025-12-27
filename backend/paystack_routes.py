
# paystack_routes.py (Neon Postgres-ready, cleaned, feature-complete for MVP)

import os
import hmac
import json
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from db import get_db  # uses Postgres if DATABASE_URL is set; else SQLite

load_dotenv()

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
def update_payment_status(reference: str, status: str, raw_json: Optional[Dict[str, Any]] = None) -> None:
    ref = (reference or "").strip()
    if not ref:
        return

    raw_str = None
    if raw_json is not None:
        try:
            raw_str = json.dumps(raw_json, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            raw_str = None

    db = get_db()
    try:
        cur = db.cursor()
        if raw_str is not None:
            cur.execute("UPDATE payments SET status = ?, raw_json = ? WHERE reference = ?", (status, raw_str, ref))
        else:
            cur.execute("UPDATE payments SET status = ? WHERE reference = ?", (status, ref))
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
        cur.execute("UPDATE users SET is_paid = 0 WHERE id = ?", (user_id,))
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
    Marks user as paid and logs payment if not already logged.
    identifier is typically the user's email.
    """
    identifier = (identifier or "").strip().lower()
    ref = (reference or "").strip()
    if not identifier or not ref:
        return

    pay_data = pay_data or {}
    amount_kobo = int(pay_data.get("amount") or 0)
    currency = (pay_data.get("currency") or "NGN").strip().upper()
    status = (pay_data.get("status") or "success").strip()

    raw_json = None
    try:
        raw_json = json.dumps(pay_data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw_json = None

    db = get_db()
    try:
        cur = db.cursor()

        cur.execute("SELECT id FROM users WHERE lower(identifier) = ?", (identifier,))
        urow = cur.fetchone()
        if not urow:
            return
        user_id = int(urow["id"])

        # mark paid
        cur.execute("UPDATE users SET is_paid = 1 WHERE id = ?", (user_id,))

        # insert payment only if not exists
        cur.execute("SELECT id FROM payments WHERE reference = ?", (ref,))
        prow = cur.fetchone()
        if not prow:
            cur.execute(
                """
                INSERT INTO payments (user_id, provider, reference, amount_kobo, currency, status, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source or "paystack",
                    ref,
                    amount_kobo,
                    currency,
                    status,
                    raw_json,
                    datetime.utcnow().isoformat(),
                ),
            )

        db.commit()
    finally:
        db.close()


# -----------------------------
# API models
# -----------------------------
class VerifyReq(BaseModel):
    reference: str
    email: str


class AdminRefundReq(BaseModel):
    reference: str
    amount_kobo: Optional[int] = None  # omit for full refund
    customer_note: Optional[str] = None
    merchant_note: Optional[str] = None


# -----------------------------
# Routes
# -----------------------------
@router.get("/public-key")
def paystack_public_key():
    if not PAYSTACK_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="PAYSTACK_PUBLIC_KEY not configured")
    return {"ok": True, "public_key": PAYSTACK_PUBLIC_KEY}


@router.post("/verify")
def verify_payment(req: VerifyReq):
    """
    Frontend calls this after Paystack success callback.
    Verifies reference with Paystack and marks the user as paid.
    """
    ref = (req.reference or "").strip()
    email = (req.email or "").strip().lower()

    if not ref:
        raise HTTPException(status_code=400, detail="Missing reference")
    if not email:
        raise HTTPException(status_code=400, detail="Missing email")

    resp = paystack_api_get(f"/transaction/verify/{ref}")
    if not resp.get("status"):
        raise HTTPException(status_code=400, detail="Paystack verification failed")

    tx = resp.get("data") or {}
    status = (tx.get("status") or "").strip().lower()
    amount = int(tx.get("amount") or 0)

    if status != "success":
        raise HTTPException(status_code=400, detail=f"Payment not successful: {status}")

    if amount < MIN_AMOUNT_KOBO:
        raise HTTPException(status_code=400, detail="Amount too low")

    # prefer Paystack customer email if present
    customer = tx.get("customer") or {}
    customer_email = (customer.get("email") or "").strip().lower()

    final_identifier = customer_email or email

    # mark paid + store payment
    mark_user_paid_by_identifier(final_identifier, ref, source="paystack:verify", pay_data=tx)

    return {"ok": True, "reference": ref, "email": final_identifier, "amount_kobo": amount}


@router.post("/webhook")
async def paystack_webhook(request: Request):
    """
    Paystack webhook:
    - verify signature
    - replay protection by reference
    - handle refunds (optional downgrade)
    - for other events: authoritative verify by reference and mark paid if success
    """
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
        # sometimes nested
        tx = data.get("transaction")
        if isinstance(tx, dict):
            reference = (tx.get("reference") or "").strip()

    if not reference:
        return {"ok": True, "ignored": "no_reference", "event": event_type}

    # replay protection
    body_hash = sha256_hex(raw)
    if is_webhook_reference_seen(reference):
        return {"ok": True, "ignored": "replay", "event": event_type, "reference": reference}

    remember_webhook_reference(reference, event_type, body_hash)

    # refund handling
    if "refund" in event_type.lower():
        update_payment_status(reference, "refunded", raw_json=event)
        maybe_downgrade_user_on_refund(reference)
        return {"ok": True, "event": event_type, "reference": reference, "refunded": True}

    # authoritative verify
    verify = paystack_api_get(f"/transaction/verify/{reference}")
    if not verify.get("status"):
        return {"ok": True, "event": event_type, "reference": reference, "verified": False}

    tx = verify.get("data") or {}
    paid = (tx.get("status") == "success")

    if paid:
        amount = int(tx.get("amount") or 0)
        if amount >= MIN_AMOUNT_KOBO:
            customer = tx.get("customer") or {}
            email = (customer.get("email") or "").strip().lower()

            metadata = tx.get("metadata") or {}
            meta_identifier = (metadata.get("identifier") or "").strip().lower()

            final_identifier = email or meta_identifier
            if final_identifier:
                mark_user_paid_by_identifier(final_identifier, reference, source=f"webhook:{event_type}", pay_data=tx)

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
    final_identifier = email or meta_identifier

    if paid and final_identifier:
        mark_user_paid_by_identifier(final_identifier, ref, source="admin:reconcile", pay_data=tx)

    return {"ok": True, "reference": ref, "paid": bool(paid), "identifier": final_identifier or None}


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

    audit_admin_action(
        request,
        action="admin_refund",
        reference=ref,
        payload=payload,
    )

    out = paystack_api_post("/refund", payload)

    # set local status as queued; webhook may later flip to refunded
    update_payment_status(ref, "refund_queued", raw_json=out)

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


@router.post("/admin/mark-paid")
def admin_mark_paid(request: Request, email: str):
    require_admin(request)
    identifier = (email or "").strip().lower()
    if not identifier:
        raise HTTPException(status_code=400, detail="Missing email")

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET is_paid = 1 WHERE lower(identifier) = ?", (identifier,))
        db.commit()
    finally:
        db.close()

    audit_admin_action(request, action="admin_mark_paid", reference=identifier, payload={"email": identifier})
    return {"ok": True, "email": identifier}
