
import os
import sqlite3
from typing import Optional, Any

# ----------------------------
# Detect Postgres
# ----------------------------
def _using_postgres() -> bool:
    url = (os.getenv("DATABASE_URL") or "").strip()
    return url.lower().startswith("postgres")


# ----------------------------
# Public API
# ----------------------------
def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize DB schema.
    - If DATABASE_URL is set => Postgres
    - Else => SQLite using DB_PATH
    Safe to call multiple times.
    """
    if _using_postgres():
        _init_db_postgres()
    else:
        _init_db_sqlite(db_path=db_path)


def get_db(db_path: Optional[str] = None):
    """
    Get a DB connection.
    - If DATABASE_URL is set => psycopg2 connection (RealDictCursor)
    - Else => sqlite3 connection (Row)
    """
    if _using_postgres():
        return _get_pg()
    return _get_sqlite(db_path=db_path)


# ----------------------------
# SQLite implementation (keeps your current schema)
# ----------------------------
def _init_db_sqlite(db_path: Optional[str] = None) -> None:
    db_path = db_path or os.getenv("DB_PATH", "exam_partner.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              identifier TEXT UNIQUE NOT NULL,
              salt TEXT NOT NULL,
              pw_hash TEXT NOT NULL,
              is_paid INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              reference TEXT UNIQUE NOT NULL,
              amount_kobo INTEGER NOT NULL,
              currency TEXT NOT NULL,
              status TEXT NOT NULL,
              raw_json TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
              id TEXT PRIMARY KEY,
              exam TEXT,
              year INTEGER,
              subject TEXT,
              paper TEXT,
              section TEXT,
              qtype TEXT NOT NULL,
              sort_key INTEGER,
              page INTEGER,
              marks INTEGER,
              question_text TEXT NOT NULL,
              options_json TEXT,
              answer TEXT,
              explanation TEXT,
              sub_questions_json TEXT,
              solution_steps_json TEXT,
              diagrams_json TEXT
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_receipts (
              reference TEXT PRIMARY KEY,
              event_type TEXT,
              body_hash TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action TEXT NOT NULL,
              reference TEXT,
              actor_ip TEXT,
              user_agent TEXT,
              payload_json TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        # lightweight migration for older SQLite DBs
        cur.execute("PRAGMA table_info(questions);")
        cols = {row[1] for row in cur.fetchall()}
        for col, ddl in [
            ("exam", "ALTER TABLE questions ADD COLUMN exam TEXT;"),
            ("year", "ALTER TABLE questions ADD COLUMN year INTEGER;"),
            ("subject", "ALTER TABLE questions ADD COLUMN subject TEXT;"),
        ]:
            if col not in cols:
                cur.execute(ddl)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created_at ON admin_audit_log(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);")

        conn.commit()
    finally:
        conn.close()


def _get_sqlite(db_path: Optional[str] = None) -> sqlite3.Connection:
    db_path = db_path or os.getenv("DB_PATH", "exam_partner.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------------------
# Postgres implementation
# ----------------------------
def _get_pg():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    url = (os.getenv("DATABASE_URL") or "").strip()
    # Neon uses SSL; your URL already includes sslmode=require
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    return _PGConn(conn)


def _init_db_postgres() -> None:
    db = _get_pg()
    try:
        cur = db.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
              identifier TEXT UNIQUE NOT NULL,
              salt TEXT NOT NULL,
              pw_hash TEXT NOT NULL,
              is_paid BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        

        # --- migrations (Postgres) ---
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMPTZ;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_founding BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
              id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id),
              provider TEXT NOT NULL,
              reference TEXT UNIQUE NOT NULL,
              amount_kobo BIGINT NOT NULL,
              currency TEXT NOT NULL,
              status TEXT NOT NULL,
              raw_json TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
              id TEXT PRIMARY KEY,
              exam TEXT,
              year INTEGER,
              subject TEXT,
              paper TEXT,
              section TEXT,
              qtype TEXT NOT NULL,
              sort_key INTEGER,
              page INTEGER,
              marks INTEGER,
              question_text TEXT NOT NULL,
              options_json TEXT,
              answer TEXT,
              explanation TEXT,
              sub_questions_json TEXT,
              solution_steps_json TEXT,
              diagrams_json TEXT
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_receipts (
              reference TEXT PRIMARY KEY,
              event_type TEXT,
              body_hash TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
              id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
              action TEXT NOT NULL,
              reference TEXT,
              actor_ip TEXT,
              user_agent TEXT,
              payload_json TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created_at ON admin_audit_log(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);")

        db.commit()
    finally:
        db.close()


# ----------------------------
# Adapter: keep SQLite-style "?" placeholders on Postgres
# ----------------------------
class _PGConn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


class _PGCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, query: str, params: Any = None):
        q = query.replace("?", "%s")
        return self._cur.execute(q, params)

    def executemany(self, query: str, seq_of_params):
        q = query.replace("?", "%s")
        return self._cur.executemany(q, seq_of_params)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()
