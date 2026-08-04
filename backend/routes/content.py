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
    latest_updated_at: always null. The questions table has no updated_at
                       column and there is no portable substitute, so this
                       has never carried a real value. Kept in the response
                       shape for client compatibility.
    has_updates: always true if content_version > 0. The Android app
                 compares this against its last-synced count stored locally.
    message: human-readable status string shown to the user.
"""
from fastapi import APIRouter
from config import db_conn, logger
from services.question_utils import row_get

router = APIRouter(prefix="/content", tags=["content"])


@router.get("/version")
def content_version():
    """
    Returns the current content version derived from the questions table.

    content_version = COUNT(*) of all questions.
    latest_updated_at = always null (see module docstring).

    This endpoint is unauthenticated — no sensitive data is returned.
    """
    db = None
    try:
        db  = db_conn()
        cur = db.cursor()

        # Total question count — used as content_version.
        #
        # The column is ALIASED rather than read positionally. Postgres runs
        # with RealDictCursor, whose rows are dict-like and reject integer
        # indexing with KeyError rather than IndexError — so `row[0]` raised
        # on every Postgres request, and the handler below quietly returned
        # content_version 0 with HTTP 200. ProfileScreen took the success
        # branch and showed the server as holding zero questions.
        #
        # Guessing the unaliased column name does not work either: SQLite
        # calls it "COUNT(*)" and Postgres calls it "count". row_get() is the
        # codebase's existing helper for exactly this dialect difference, so
        # use it against a name we control.
        cur.execute("SELECT COUNT(*) AS total FROM questions")
        row = cur.fetchone()
        total = int(row_get(row, "total") or 0)

        # latest_updated_at is always None: the questions table has no
        # updated_at column, and there is no portable substitute. A previous
        # MAX(rowid) block was removed rather than fixed — it discarded its
        # own result and set this to None on every path, so it computed
        # nothing, while being invalid SQL on Postgres (rowid is SQLite-only).
        # The field is kept in the response for client compatibility.
        latest_at = None

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

    finally:
        # Previously closed only on the success path, so any failure leaked
        # the connection.
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
