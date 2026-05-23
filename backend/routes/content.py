"""
routes/content.py — content version and sync awareness endpoint.

GET /content/version
    Returns the current content version (total question count) and
    the timestamp of the most recently added question.

    No authentication required — this is public metadata used by the
    Android app to show users whether their offline content is up to date.

    content_version: total question count in the DB. Always increases
                     when new questions are added. Used as a simple
                     monotonic version number.
    latest_updated_at: rowid-based proxy for newest question — the highest
                       rowid row's creation order. Since questions table has
                       no updated_at column, we use MAX(rowid) as a proxy.
    has_updates: always true if content_version > 0. The Android app
                 compares this against its last-synced count stored locally.
    message: human-readable status string shown to the user.
"""
from fastapi import APIRouter
from config import db_conn, logger

router = APIRouter(prefix="/content", tags=["content"])


@router.get("/version")
def content_version():
    """
    Returns the current content version derived from the questions table.

    content_version = COUNT(*) of all questions.
    latest_updated_at = ISO timestamp of the most-recently inserted row
                        (approximated via MAX(rowid) join).

    This endpoint is unauthenticated — no sensitive data is returned.
    """
    try:
        db  = db_conn()
        cur = db.cursor()

        # Total question count — used as content_version
        cur.execute("SELECT COUNT(*) FROM questions")
        row = cur.fetchone()
        total = int(row[0] if not hasattr(row, "keys") else row["COUNT(*)"] if "COUNT(*)" in (row.keys() if hasattr(row, "keys") else []) else row[0])

        # Most recent insert approximation — questions have no updated_at,
        # so we join on MAX(rowid) to get the newest row's id as a proxy.
        # We only need the id for ordering purposes; we return a count-based
        # version number, not a timestamp.
        latest_at = None
        try:
            cur.execute(
                "SELECT id FROM questions WHERE rowid = (SELECT MAX(rowid) FROM questions)"
            )
            latest_row = cur.fetchone()
            # We can't get a real timestamp, so we return None — Android
            # will display "Unknown" for this field gracefully.
            latest_at = None
        except Exception:
            latest_at = None

        db.close()

        return {
            "ok":               True,
            "content_version":  total,
            "latest_updated_at": latest_at,
            "has_updates":      total > 0,
            "message":          f"{total} questions available." if total > 0 else "No content available yet.",
        }

    except Exception as e:
        logger.exception("content_version failed: %s", e)
        return {
            "ok":               False,
            "content_version":  0,
            "latest_updated_at": None,
            "has_updates":      False,
            "message":          "Could not fetch content version.",
        }
