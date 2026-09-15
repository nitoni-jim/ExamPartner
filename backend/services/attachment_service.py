"""
services/attachment_service.py — ownership and lifecycle for candidate
diagram attachments.

Sprint A. storage_service.py moves bytes; this module decides whose bytes
they are, which attempt they belong to, when they are replaced and when
they are deleted.

--- Why a separate table rather than a column on theory_attempts ---

theory_attempts.student_answer is TEXT and one row is written AFTER grading
completes. An attachment exists BEFORE grading — it has to, because the
upload must finish before the grading request is sent, or a slow connection
burns a credit on a file that never arrived. So the attachment cannot hang
off a row that does not exist yet. It gets its own table, keyed to the
attempt by a client-supplied attempt_key, and is linked back to the
theory_attempts row once grading produces one.

--- Status lifecycle ---

  pending   uploaded, not yet graded against
  consumed  a grading attempt has read it; theory_attempt_id is set
  deleted   object removed by the retention sweep; row kept as a tombstone

The tombstone matters for support: "my diagram vanished" is answerable from
a deleted row and unanswerable from a missing one.
"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config
from config import db_conn, logger
from services import storage_service
from services.question_utils import row_get

# Retention for candidate scripts. Defined in config.py
# (ATTACHMENT_RETENTION_DAYS); the sweep reads it, nothing else depends on
# the number.
RETENTION_DAYS = config.ATTACHMENT_RETENTION_DAYS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_attachment(
    identifier: str,
    question_id: str,
    sub_question_label: Optional[str],
    attempt_key: str,
    raw: bytes,
) -> Dict[str, Any]:
    """
    Stores one upload and records it.

    Supersedes any existing PENDING attachment for the same
    (user, question_id, sub_question_label, attempt_key). That is the
    retake path: a student who photographs the page again replaces the
    previous shot rather than appending a second one, which is both what
    the UI promises and what stops a grading request finding two candidate
    images for one sub-question.

    A CONSUMED attachment is never superseded — it belongs to a grading
    that already happened and is part of that attempt's record.
    """
    meta = storage_service.store_upload(
        raw=raw,
        identifier=identifier,
        question_id=question_id,
    )

    superseded = _supersede_pending(
        identifier=identifier,
        question_id=question_id,
        sub_question_label=sub_question_label,
        attempt_key=attempt_key,
    )

    now = _now()
    row_id = secrets.token_hex(16)

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO theory_attachments
              (id, user_id, question_id, sub_question_label, attempt_key,
               storage_key, content_type, byte_size, width, height, sha256,
               status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                identifier,
                question_id,
                sub_question_label,
                attempt_key,
                meta["storage_key"],
                meta["content_type"],
                meta["byte_size"],
                meta["width"],
                meta["height"],
                meta["sha256"],
                "pending",
                _iso(now + timedelta(days=RETENTION_DAYS)),
            ),
        )
        db.commit()
    except Exception as exc:
        # The object is already in storage but the row failed. Remove the
        # orphan rather than leaving bytes nothing will ever reference or
        # sweep — the retention sweep walks rows, not the bucket.
        logger.exception("theory_attachments insert failed; deleting orphaned object")
        storage_service.delete_object(meta["storage_key"])
        raise storage_service.StorageError(
            f"Attachment record could not be saved: {type(exc).__name__}"
        ) from exc
    finally:
        db.close()

    # Only drop the superseded objects once the replacement is durably
    # recorded. Deleting first would lose the student's earlier photograph
    # if the insert above failed.
    for old_key in superseded:
        storage_service.delete_object(old_key)

    logger.info(
        "Attachment stored: user=%s question=%s label=%s bytes=%d superseded=%d",
        identifier, question_id, sub_question_label, meta["byte_size"], len(superseded),
    )

    return {
        "attachment_key": meta["storage_key"],
        "content_type":   meta["content_type"],
        "byte_size":      meta["byte_size"],
        "width":          meta["width"],
        "height":         meta["height"],
        "expires_at":     _iso(now + timedelta(days=RETENTION_DAYS)),
        "durable":        storage_service.is_durable(),
    }


def _supersede_pending(
    identifier: str,
    question_id: str,
    sub_question_label: Optional[str],
    attempt_key: str,
) -> List[str]:
    """Marks prior pending rows for this slot as deleted; returns their keys."""
    db = db_conn()
    cur = db.cursor()
    keys: List[str] = []
    try:
        if sub_question_label is None:
            cur.execute(
                """
                SELECT storage_key FROM theory_attachments
                WHERE user_id = ? AND question_id = ? AND attempt_key = ?
                  AND sub_question_label IS NULL AND status = 'pending'
                """,
                (identifier, question_id, attempt_key),
            )
        else:
            cur.execute(
                """
                SELECT storage_key FROM theory_attachments
                WHERE user_id = ? AND question_id = ? AND attempt_key = ?
                  AND sub_question_label = ? AND status = 'pending'
                """,
                (identifier, question_id, attempt_key, sub_question_label),
            )
        keys = [row_get(r, "storage_key") for r in (cur.fetchall() or [])]

        for key in keys:
            cur.execute(
                "UPDATE theory_attachments SET status = 'deleted' WHERE storage_key = ?",
                (key,),
            )
        db.commit()
    except Exception:
        logger.exception("Supersede query failed — continuing with the new upload")
        return []
    finally:
        db.close()
    return keys


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_attachment_record(storage_key: str, identifier: str) -> Optional[Dict[str, Any]]:
    """
    Returns the row for this key IF it belongs to this user.

    Ownership is checked here, in SQL, on every read. The unguessable key is
    not the access control — a student must never be able to grade against
    another student's script by pasting a key, and that guarantee should not
    rest on the key staying secret in logs, crash reports or support tickets.
    """
    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT id, user_id, question_id, sub_question_label, attempt_key,
                   storage_key, content_type, byte_size, width, height, sha256,
                   status, theory_attempt_id, consumed_at, expires_at, created_at
            FROM theory_attachments
            WHERE storage_key = ? AND user_id = ?
            """,
            (storage_key, identifier),
        )
        row = cur.fetchone()
    except Exception:
        logger.exception("Attachment lookup failed for key=%s", storage_key)
        return None
    finally:
        db.close()

    if not row:
        return None

    return {
        "id":                 row_get(row, "id"),
        "user_id":            row_get(row, "user_id"),
        "question_id":        row_get(row, "question_id"),
        "sub_question_label": row_get(row, "sub_question_label"),
        "attempt_key":        row_get(row, "attempt_key"),
        "storage_key":        row_get(row, "storage_key"),
        "content_type":       row_get(row, "content_type"),
        "byte_size":          row_get(row, "byte_size"),
        "width":              row_get(row, "width"),
        "height":             row_get(row, "height"),
        "sha256":             row_get(row, "sha256"),
        "status":             row_get(row, "status"),
        "theory_attempt_id":  row_get(row, "theory_attempt_id"),
        "consumed_at":        row_get(row, "consumed_at"),
        "expires_at":         row_get(row, "expires_at"),
        "created_at":         row_get(row, "created_at"),
    }


def load_attachment_bytes(storage_key: str, identifier: str) -> Optional[bytes]:
    """
    Ownership-checked byte read. Returns None when the key is unknown, not
    this user's, or already swept. Raises StorageError on a backend fault —
    which the caller must treat as no-charge.
    """
    record = get_attachment_record(storage_key, identifier)
    if not record or record["status"] == "deleted":
        return None
    return storage_service.get_object(storage_key)


# ---------------------------------------------------------------------------
# Consume
# ---------------------------------------------------------------------------

def mark_consumed(storage_keys: List[str], identifier: str, theory_attempt_id: str) -> None:
    """
    Links attachments to the grading attempt that read them.

    Swallows its own errors, matching _store_attempt(): a bookkeeping
    failure must not turn a successful grading into an error the student
    sees after being charged.
    """
    if not storage_keys:
        return

    db = db_conn()
    cur = db.cursor()
    try:
        for key in storage_keys:
            cur.execute(
                """
                UPDATE theory_attachments
                SET status = 'consumed', theory_attempt_id = ?, consumed_at = ?
                WHERE storage_key = ? AND user_id = ? AND status = 'pending'
                """,
                (theory_attempt_id, _iso(_now()), key, identifier),
            )
        db.commit()
    except Exception:
        logger.exception("Failed to mark attachments consumed for attempt=%s", theory_attempt_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def sweep_expired(limit: int = 500) -> Dict[str, int]:
    """
    Deletes objects past expires_at and tombstones their rows.

    Batched and idempotent, so it is safe to run from a cron ping, a
    startup hook or by hand. Object deletion is best-effort inside
    storage_service; anything that fails is retried on the next run because
    the row is only tombstoned after the delete call returns.
    """
    now_iso = _iso(_now())
    db = db_conn()
    cur = db.cursor()
    rows: List[Any] = []
    try:
        cur.execute(
            """
            SELECT storage_key FROM theory_attachments
            WHERE status != 'deleted' AND expires_at IS NOT NULL AND expires_at < ?
            LIMIT ?
            """,
            (now_iso, limit),
        )
        rows = cur.fetchall() or []
    except Exception:
        logger.exception("Retention sweep query failed")
        db.close()
        return {"examined": 0, "deleted": 0}

    deleted = 0
    try:
        for r in rows:
            key = row_get(r, "storage_key")
            storage_service.delete_object(key)
            cur.execute(
                "UPDATE theory_attachments SET status = 'deleted' WHERE storage_key = ?",
                (key,),
            )
            deleted += 1
        db.commit()
    except Exception:
        logger.exception("Retention sweep update failed after %d deletions", deleted)
    finally:
        db.close()

    if deleted:
        logger.info("Retention sweep removed %d attachment(s)", deleted)
    return {"examined": len(rows), "deleted": deleted}
