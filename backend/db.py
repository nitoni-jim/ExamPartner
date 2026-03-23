import os
import sqlite3
from typing import Any, Optional

QUESTIONS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("exam", "TEXT"),
    ("year", "INTEGER"),
    ("subject", "TEXT"),
    ("paper", "TEXT"),
    ("section", "TEXT"),
    ("qtype", "TEXT NOT NULL"),
    ("sort_key", "INTEGER"),
    ("page", "INTEGER"),
    ("marks", "INTEGER"),
    ("question_text", "TEXT NOT NULL"),
    ("options_json", "TEXT"),
    ("answer", "TEXT"),
    ("explanation", "TEXT"),
    ("sub_questions_json", "TEXT"),
    ("solution_steps_json", "TEXT"),
    ("diagrams_json", "TEXT"),
    ("answer_diagrams_json", "TEXT"),
    ("explanation_diagrams_json", "TEXT"),
    ("tables_json", "TEXT"),
    ("section_instruction", "TEXT"),
    ("topic", "TEXT"),
    ("subtopic", "TEXT"),
    ("difficulty", "TEXT"),
    ("learning_objective", "TEXT"),
    ("examiner_tip", "TEXT"),
    ("keywords_json", "TEXT"),
    ("tags_json", "TEXT"),
    ("examiner_points_json", "TEXT"),
    ("concepts_json", "TEXT"),
    ("common_traps_json", "TEXT"),
    ("references_json", "TEXT"),
    ("metadata_json", "TEXT"),
    ("passage_id", "TEXT"),
    ("passage_snapshot", "TEXT"),
]

PASSAGES_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("exam", "TEXT"),
    ("year", "INTEGER"),
    ("subject", "TEXT"),
    ("paper", "TEXT"),
    ("section", "TEXT"),
    ("title", "TEXT"),
    ("passage_type", "TEXT"),
    ("passage_text", "TEXT"),
    ("metadata_json", "TEXT"),
    ("created_at", "TEXT"),
]

PASSAGES_SQLITE_COLUMNS = [
    *PASSAGES_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

PASSAGES_POSTGRES_COLUMNS = [
    *PASSAGES_COLUMNS[:-1],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

FEEDBACK_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("feedback_type", "TEXT NOT NULL"),
    ("question_id", "TEXT"),
    ("source_area", "TEXT NOT NULL"),
    ("category", "TEXT"),
    ("message", "TEXT"),
    ("user_identifier", "TEXT"),
    ("created_at", "TEXT"),
]

FEEDBACK_SQLITE_COLUMNS = [
    *FEEDBACK_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

FEEDBACK_POSTGRES_COLUMNS = [
    *FEEDBACK_COLUMNS[:-1],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]



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


def _table_sql(table_name: str, columns: list[tuple[str, str]]) -> str:
    columns_sql = ",\n              ".join(f"{name} {ddl}" for name, ddl in columns)
    return (
        f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
              """
        + columns_sql
        + """
            );
            """
    )


def _questions_table_sql() -> str:
    return _table_sql("questions", QUESTIONS_COLUMNS)


def _passages_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("passages", columns)


def _feedback_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("feedback", columns)


def _sqlite_add_missing_columns(cur: sqlite3.Cursor, table_name: str, columns: list[tuple[str, str]]) -> None:
    cur.execute(f"PRAGMA table_info({table_name});")
    cols = {row[1] for row in cur.fetchall()}
    for col, ddl in columns:
        if col in cols:
            continue

        col_type = ddl.replace(" PRIMARY KEY", "")
        col_type = col_type.replace(" NOT NULL", "")
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type};")


def _sqlite_add_missing_question_columns(cur: sqlite3.Cursor) -> None:
    _sqlite_add_missing_columns(cur, "questions", QUESTIONS_COLUMNS)


def _postgres_add_missing_columns(cur, table_name: str, columns: list[tuple[str, str]]) -> None:
    for col, ddl in columns:
        if "PRIMARY KEY" in ddl:
            continue

        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col} {ddl};")


def _postgres_add_missing_question_columns(cur) -> None:
    _postgres_add_missing_columns(cur, "questions", QUESTIONS_COLUMNS)


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
              is_admin INTEGER NOT NULL DEFAULT 0,
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
              channel TEXT,
              raw_json TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        cur.execute(_questions_table_sql())
        cur.execute(_passages_table_sql(PASSAGES_SQLITE_COLUMNS))
        cur.execute(_feedback_table_sql(FEEDBACK_SQLITE_COLUMNS))

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

        # lightweight migrations for older SQLite DBs
        _sqlite_add_missing_columns(cur, "users", [("email", "TEXT"), ("paid_until", "TEXT"), ("plan", "TEXT"), ("is_founding", "INTEGER"), ("is_admin", "INTEGER NOT NULL DEFAULT 0")])
        _sqlite_add_missing_question_columns(cur)
        _sqlite_add_missing_columns(cur, "passages", PASSAGES_COLUMNS)
        _sqlite_add_missing_columns(cur, "feedback", FEEDBACK_COLUMNS)
        _sqlite_add_missing_columns(cur, "payments", [("channel", "TEXT")])

        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_passage_id ON questions(passage_id);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_question_id ON feedback(question_id);")

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
              is_admin BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # --- migrations (Postgres) ---
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMPTZ;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_founding BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;")
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
              channel TEXT,
              raw_json TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute(_questions_table_sql())
        cur.execute(_passages_table_sql(PASSAGES_POSTGRES_COLUMNS))
        cur.execute(_feedback_table_sql(FEEDBACK_POSTGRES_COLUMNS))
        _postgres_add_missing_question_columns(cur)
        _postgres_add_missing_columns(cur, "passages", PASSAGES_COLUMNS)
        _postgres_add_missing_columns(cur, "feedback", FEEDBACK_COLUMNS)
        _postgres_add_missing_columns(cur, "payments", [("channel", "TEXT")])

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_passage_id ON questions(passage_id);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_question_id ON feedback(question_id);")

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
