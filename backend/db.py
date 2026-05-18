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
# topics — canonical curriculum grouping
# ----------------------------
TOPICS_COLUMNS = [
    ("topic_id", "TEXT PRIMARY KEY"),
    ("exam", "TEXT"),
    ("subject", "TEXT NOT NULL"),
    ("topic", "TEXT NOT NULL"),
    ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
    ("is_active", "TEXT"),          # stored as "1"/"0" for SQLite, TRUE/FALSE for PG
    ("metadata_json", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
]

TOPICS_SQLITE_COLUMNS = [
    *TOPICS_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

TOPICS_POSTGRES_COLUMNS = [
    *TOPICS_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

# ----------------------------
# subtopics — syllabus structure
# ----------------------------
SUBTOPICS_COLUMNS = [
    ("subtopic_id", "TEXT PRIMARY KEY"),
    ("topic_id", "TEXT"),
    ("exam", "TEXT"),
    ("subject", "TEXT NOT NULL"),
    ("topic", "TEXT NOT NULL"),
    ("subtopic", "TEXT NOT NULL"),
    ("lesson_note_id", "TEXT"),
    ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
    ("is_active", "TEXT"),          # stored as "1"/"0" for SQLite, TRUE/FALSE for PG
    ("metadata_json", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
]

SUBTOPICS_SQLITE_COLUMNS = [
    *SUBTOPICS_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

SUBTOPICS_POSTGRES_COLUMNS = [
    *SUBTOPICS_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

# ----------------------------
# lesson_notes — lesson content
# ----------------------------
LESSON_NOTES_COLUMNS = [
    ("lesson_note_id", "TEXT PRIMARY KEY"),
    ("subtopic_id", "TEXT"),
    ("exam", "TEXT"),
    ("subject", "TEXT NOT NULL"),
    ("topic", "TEXT NOT NULL"),
    ("title", "TEXT NOT NULL"),
    ("content", "TEXT"),
    ("summary", "TEXT"),
    ("is_published", "TEXT"),       # "1"/"0" / TRUE/FALSE
    ("metadata_json", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
]

LESSON_NOTES_SQLITE_COLUMNS = [
    *LESSON_NOTES_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

LESSON_NOTES_POSTGRES_COLUMNS = [
    *LESSON_NOTES_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

# ----------------------------
# cbt_sessions — per CBT attempt
# ----------------------------
# Note: product modes are Study / CBT / Game. CBT session mode values should be things like "cbt", "mock", or "speed".
CBT_SESSIONS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("user_id", "TEXT NOT NULL"),
    ("exam", "TEXT"),
    ("subject", "TEXT"),
    ("mode", "TEXT"),               # e.g. "cbt", "mock", "speed"
    ("source_year", "INTEGER"),
    ("total_questions", "INTEGER NOT NULL DEFAULT 0"),
    ("answered_count", "INTEGER NOT NULL DEFAULT 0"),
    ("correct_count", "INTEGER NOT NULL DEFAULT 0"),
    ("wrong_count", "INTEGER NOT NULL DEFAULT 0"),
    ("unanswered_count", "INTEGER NOT NULL DEFAULT 0"),
    ("score_percent", "REAL"),
    ("duration_seconds", "INTEGER"),
    ("started_at", "TEXT"),
    ("submitted_at", "TEXT"),
    ("metadata_json", "TEXT"),
]

CBT_SESSIONS_SQLITE_COLUMNS = [
    *CBT_SESSIONS_COLUMNS[:-3],
    ("started_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("submitted_at", "TEXT"),
    ("metadata_json", "TEXT"),
]

CBT_SESSIONS_POSTGRES_COLUMNS = [
    *CBT_SESSIONS_COLUMNS[:-3],
    ("started_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("submitted_at", "TIMESTAMPTZ"),
    ("metadata_json", "TEXT"),
]

# ----------------------------
# cbt_answers — per-question answer within a session
# ----------------------------
CBT_ANSWERS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("session_id", "TEXT NOT NULL"),
    ("user_id", "TEXT NOT NULL"),
    ("question_id", "TEXT NOT NULL"),
    ("question_exam", "TEXT"),
    ("question_subject", "TEXT"),
    ("question_year", "INTEGER"),
    ("selected_answer", "TEXT"),
    ("correct_answer", "TEXT"),
    ("is_correct", "TEXT"),         # "1"/"0" / TRUE/FALSE
    ("time_spent_seconds", "INTEGER"),
    ("created_at", "TEXT"),
    ("metadata_json", "TEXT"),
]

CBT_ANSWERS_SQLITE_COLUMNS = [
    *CBT_ANSWERS_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("metadata_json", "TEXT"),
]

CBT_ANSWERS_POSTGRES_COLUMNS = [
    *CBT_ANSWERS_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("metadata_json", "TEXT"),
]

# ----------------------------
# user_progress — cross-mode activity tracking
# ----------------------------
# Official activity types should align to the product: "study", "cbt", "game".
USER_PROGRESS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("user_id", "TEXT NOT NULL"),
    ("activity_type", "TEXT NOT NULL"),   # official modes: "study", "cbt", "game"
    ("exam", "TEXT"),
    ("subject", "TEXT"),
    ("topic", "TEXT"),
    ("subtopic_id", "TEXT"),
    ("lesson_note_id", "TEXT"),
    ("question_id", "TEXT"),
    ("session_id", "TEXT"),
    ("selected_answer", "TEXT"),
    ("is_correct", "TEXT"),               # "1"/"0" / TRUE/FALSE
    ("score", "REAL"),
    ("time_spent_seconds", "INTEGER"),
    ("metadata_json", "TEXT"),
    ("created_at", "TEXT"),
]

USER_PROGRESS_SQLITE_COLUMNS = [
    *USER_PROGRESS_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

USER_PROGRESS_POSTGRES_COLUMNS = [
    *USER_PROGRESS_COLUMNS[:-1],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

# ----------------------------
# game_sessions — optional/future; schema defined now, dormant until needed
# ----------------------------
GAME_SESSIONS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("user_id", "TEXT NOT NULL"),
    ("exam", "TEXT"),
    ("subject", "TEXT"),
    ("topic", "TEXT"),
    ("subtopic_id", "TEXT"),
    ("total_questions", "INTEGER NOT NULL DEFAULT 0"),
    ("correct_count", "INTEGER NOT NULL DEFAULT 0"),
    ("wrong_count", "INTEGER NOT NULL DEFAULT 0"),
    ("best_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("lives_used", "INTEGER NOT NULL DEFAULT 0"),
    ("duration_seconds", "INTEGER"),
    ("started_at", "TEXT"),
    ("ended_at", "TEXT"),
    ("metadata_json", "TEXT"),
]

GAME_SESSIONS_SQLITE_COLUMNS = [
    *GAME_SESSIONS_COLUMNS[:-3],
    ("started_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("ended_at", "TEXT"),
    ("metadata_json", "TEXT"),
]

GAME_SESSIONS_POSTGRES_COLUMNS = [
    *GAME_SESSIONS_COLUMNS[:-3],
    ("started_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("ended_at", "TIMESTAMPTZ"),
    ("metadata_json", "TEXT"),
]

# ----------------------------
# user_sessions — active login sessions (anti-sharing / session limiting)
# ----------------------------
USER_SESSIONS_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),         # session token (random hex)
    ("user_id", "TEXT NOT NULL"),
    ("identifier", "TEXT NOT NULL"),
    ("device_hint", "TEXT"),            # optional: user-agent snippet
    ("created_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("expires_at", "TEXT"),
    ("is_active", "TEXT"),              # "1"/"0" / TRUE/FALSE
]

USER_SESSIONS_SQLITE_COLUMNS = [
    *USER_SESSIONS_COLUMNS[:-4],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("last_seen_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("expires_at", "TEXT"),
    ("is_active", "TEXT NOT NULL DEFAULT '1'"),
]

USER_SESSIONS_POSTGRES_COLUMNS = [
    *USER_SESSIONS_COLUMNS[:-4],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("last_seen_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("expires_at", "TIMESTAMPTZ"),
    ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
]

# ----------------------------
# user_devices — registered devices per user (device policy enforcement)
# ----------------------------
USER_DEVICES_COLUMNS = [
    ("id",             "TEXT PRIMARY KEY"),       # random hex
    ("user_id",        "TEXT NOT NULL"),
    ("device_id",      "TEXT NOT NULL"),          # provided by client (Android ID, UUID, etc.)
    ("device_name",    "TEXT"),                   # e.g. "Samsung A15"
    ("platform",       "TEXT"),                   # android | ios | web
    ("created_at",     "TEXT"),
    ("last_seen_at",   "TEXT"),
    ("revoked_at",     "TEXT"),                   # null = active; set = revoked
    ("revoke_reason",  "TEXT"),                   # manual | reinstall_heuristic | stale
]

USER_DEVICES_SQLITE_COLUMNS = [
    *USER_DEVICES_COLUMNS[:-4],
    ("created_at",    "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("last_seen_at",  "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("revoked_at",    "TEXT"),
    ("revoke_reason", "TEXT"),
]

USER_DEVICES_POSTGRES_COLUMNS = [
    *USER_DEVICES_COLUMNS[:-4],
    ("created_at",    "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("last_seen_at",  "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("revoked_at",    "TIMESTAMPTZ"),
    ("revoke_reason", "TEXT"),
]

# ----------------------------
# theory_attempts — one row per AI grading attempt
# ----------------------------
THEORY_ATTEMPTS_COLUMNS = [
    ("id",               "TEXT PRIMARY KEY"),
    ("user_id",          "TEXT NOT NULL"),      # identifier (not DB id)
    ("question_id",      "TEXT NOT NULL"),
    ("student_answer",   "TEXT NOT NULL"),
    ("score",            "REAL"),
    ("max_score",        "REAL"),
    ("feedback_json",    "TEXT"),               # full Claude response JSON
    ("model_used",       "TEXT"),               # haiku | sonnet
    ("input_tokens",     "INTEGER"),
    ("output_tokens",    "INTEGER"),
    ("estimated_cost_usd", "REAL"),
    ("created_at",       "TEXT"),
]

THEORY_ATTEMPTS_SQLITE_COLUMNS = [
    *THEORY_ATTEMPTS_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

THEORY_ATTEMPTS_POSTGRES_COLUMNS = [
    *THEORY_ATTEMPTS_COLUMNS[:-1],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]

# ----------------------------
# ai_grading_usage — usage counter per user per period
# ----------------------------
# period_key values:
#   "lifetime"  — free users (single lifetime bucket)
#   "YYYY-MM"   — paid users (monthly bucket, e.g. "2026-05")
#   "admin"     — admin users (single high-limit bucket)
AI_GRADING_USAGE_COLUMNS = [
    ("id",          "TEXT PRIMARY KEY"),
    ("user_id",     "TEXT NOT NULL"),       # identifier (not DB id)
    ("period_key",  "TEXT NOT NULL"),       # "lifetime" | "YYYY-MM" | "admin"
    ("used_count",  "INTEGER NOT NULL DEFAULT 0"),
    ("plan_limit",  "INTEGER NOT NULL"),    # snapshot of limit at time of first use
    ("created_at",  "TEXT"),
    ("updated_at",  "TEXT"),
]

AI_GRADING_USAGE_SQLITE_COLUMNS = [
    *AI_GRADING_USAGE_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

AI_GRADING_USAGE_POSTGRES_COLUMNS = [
    *AI_GRADING_USAGE_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
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
    Initialize DB schema with retry logic for transient connection failures.
    - If DATABASE_URL is set => Postgres (3 attempts, 2s/4s backoff)
    - Else => SQLite using DB_PATH (no retry needed)
    Safe to call multiple times (all CREATE TABLE IF NOT EXISTS).
    """
    import time

    if not _using_postgres():
        _init_db_sqlite(db_path=db_path)
        return

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            _init_db_postgres()
            return
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "init_db attempt %d/3 failed: %s — %s",
                attempt, type(exc).__name__, exc,
            )
            if attempt < 3:
                time.sleep(attempt * 2)  # 2s then 4s

    logger.error("init_db failed after 3 attempts — raising last exception")
    raise last_exc  # type: ignore[misc]


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
# SQL builders
# ----------------------------
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


def _topics_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("topics", columns)


def _subtopics_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("subtopics", columns)


def _lesson_notes_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("lesson_notes", columns)


def _cbt_sessions_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("cbt_sessions", columns)


def _cbt_answers_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("cbt_answers", columns)


def _user_progress_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("user_progress", columns)


def _game_sessions_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("game_sessions", columns)


def _user_sessions_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("user_sessions", columns)


def _user_devices_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("user_devices", columns)


def _theory_attempts_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("theory_attempts", columns)


def _ai_grading_usage_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("ai_grading_usage", columns)


# ----------------------------
# Migration helpers
# ----------------------------
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
# SQLite implementation
# ----------------------------
def _init_db_sqlite(db_path: Optional[str] = None) -> None:
    db_path = db_path or os.getenv("DB_PATH", "exam_partner.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")

        # ---- existing tables ----
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

        # users migrations
        _sqlite_add_missing_columns(cur, "users", [
            ("email", "TEXT"),
            ("paid_until", "TEXT"),
            ("plan", "TEXT NOT NULL DEFAULT 'free'"),
            ("is_founding", "INTEGER NOT NULL DEFAULT 0"),
            ("full_name", "TEXT"),
            ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
        ])

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

        # payments migration: channel column
        _sqlite_add_missing_columns(cur, "payments", [
            ("channel", "TEXT"),
        ])

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

        # ---- new tables ----
        cur.execute(_topics_table_sql(TOPICS_SQLITE_COLUMNS))
        cur.execute(_subtopics_table_sql(SUBTOPICS_SQLITE_COLUMNS))
        cur.execute(_lesson_notes_table_sql(LESSON_NOTES_SQLITE_COLUMNS))
        cur.execute(_cbt_sessions_table_sql(CBT_SESSIONS_SQLITE_COLUMNS))
        cur.execute(_cbt_answers_table_sql(CBT_ANSWERS_SQLITE_COLUMNS))
        cur.execute(_user_progress_table_sql(USER_PROGRESS_SQLITE_COLUMNS))
        cur.execute(_game_sessions_table_sql(GAME_SESSIONS_SQLITE_COLUMNS))
        cur.execute(_user_sessions_table_sql(USER_SESSIONS_SQLITE_COLUMNS))
        cur.execute(_user_devices_table_sql(USER_DEVICES_SQLITE_COLUMNS))
        cur.execute(_theory_attempts_table_sql(THEORY_ATTEMPTS_SQLITE_COLUMNS))
        cur.execute(_ai_grading_usage_table_sql(AI_GRADING_USAGE_SQLITE_COLUMNS))

        # ---- lightweight column migrations ----
        _sqlite_add_missing_question_columns(cur)
        _sqlite_add_missing_columns(cur, "passages", PASSAGES_COLUMNS)
        _sqlite_add_missing_columns(cur, "feedback", FEEDBACK_COLUMNS)
        _sqlite_add_missing_columns(cur, "topics", TOPICS_COLUMNS)
        _sqlite_add_missing_columns(cur, "subtopics", SUBTOPICS_COLUMNS)
        _sqlite_add_missing_columns(cur, "lesson_notes", LESSON_NOTES_COLUMNS)
        _sqlite_add_missing_columns(cur, "theory_attempts", THEORY_ATTEMPTS_COLUMNS)
        _sqlite_add_missing_columns(cur, "ai_grading_usage", AI_GRADING_USAGE_COLUMNS)

        # *** COMMIT PHASE 1 — tables are now durable regardless of index errors ***
        conn.commit()

        # ---- indexes — each wrapped individually so one failure never blocks others ----
        _sqlite_indexes = [
            # questions
            "CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);",
            "CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);",
            "CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);",
            "CREATE INDEX IF NOT EXISTS idx_questions_passage_id ON questions(passage_id);",
            # passages / feedback / audit
            "CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_question_id ON feedback(question_id);",
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_created_at ON admin_audit_log(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);",
            # topics / subtopics / lesson_notes
            "CREATE INDEX IF NOT EXISTS idx_topics_exam_subject ON topics(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_topics_subject_topic ON topics(subject, topic);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_exam_subject ON subtopics(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_topic_id ON subtopics(topic_id);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_subject ON subtopics(subject);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_topic ON subtopics(subject, topic);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_subtopic ON lesson_notes(subtopic_id);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_exam_subject ON lesson_notes(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_subject ON lesson_notes(subject);",
            # cbt_sessions / cbt_answers
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_user ON cbt_sessions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_user_subject ON cbt_sessions(user_id, subject);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_started ON cbt_sessions(started_at);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_session ON cbt_answers(session_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_user ON cbt_answers(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_question ON cbt_answers(question_id);",
            # user_progress
            "CREATE INDEX IF NOT EXISTS idx_user_progress_user ON user_progress(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_user_subject ON user_progress(user_id, subject);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_activity ON user_progress(activity_type);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_created ON user_progress(created_at);",
            # game_sessions
            "CREATE INDEX IF NOT EXISTS idx_game_sessions_user ON game_sessions(user_id);",
            # user_sessions
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_identifier ON user_sessions(identifier);",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_active ON user_sessions(identifier, is_active);",
            # user_devices
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_devices_active_user_device ON user_devices(user_id, device_id) WHERE revoked_at IS NULL;",
            "CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_devices_active ON user_devices(user_id, revoked_at);",
            "ALTER TABLE user_devices ADD COLUMN revoke_reason TEXT;",
            # theory_attempts
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_user ON theory_attempts(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_question ON theory_attempts(question_id);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_created ON theory_attempts(created_at);",
            # ai_grading_usage
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_grading_usage_user_period ON ai_grading_usage(user_id, period_key);",
            "CREATE INDEX IF NOT EXISTS idx_ai_grading_usage_user ON ai_grading_usage(user_id);",
        ]

        for sql in _sqlite_indexes:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.warning("SQLite index DDL skipped (%s): %s", type(exc).__name__, exc)

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
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    return _PGConn(conn)


def _pg_exec_index(db, cur, sql: str) -> None:
    """
    Execute a single DDL index statement in its own savepoint so that a
    failure (e.g. conflicting index definition) never aborts the surrounding
    transaction.  Errors are logged as warnings and skipped.
    """
    try:
        cur.execute("SAVEPOINT _idx;")
        cur.execute(sql)
        cur.execute("RELEASE SAVEPOINT _idx;")
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT _idx;")
        logger.warning("Index DDL skipped (%s): %s", type(exc).__name__, exc)


def _init_db_postgres() -> None:
    db = _get_pg()
    try:
        cur = db.cursor()

        # ------------------------------------------------------------------ #
        # PHASE 1 — Tables + column migrations                                #
        # Committed as one unit.  If this succeeds, tables exist on disk.     #
        # ------------------------------------------------------------------ #

        # ---- existing tables ----
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

        # users migrations
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMPTZ;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_founding BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;")
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
              raw_json TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # payments migration
        cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS channel TEXT;")

        cur.execute(_questions_table_sql())
        cur.execute(_passages_table_sql(PASSAGES_POSTGRES_COLUMNS))
        cur.execute(_feedback_table_sql(FEEDBACK_POSTGRES_COLUMNS))
        _postgres_add_missing_question_columns(cur)
        _postgres_add_missing_columns(cur, "passages", PASSAGES_COLUMNS)
        _postgres_add_missing_columns(cur, "feedback", FEEDBACK_COLUMNS)

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

        # ---- new tables ----
        cur.execute(_topics_table_sql(TOPICS_POSTGRES_COLUMNS))
        cur.execute(_subtopics_table_sql(SUBTOPICS_POSTGRES_COLUMNS))
        cur.execute(_lesson_notes_table_sql(LESSON_NOTES_POSTGRES_COLUMNS))
        cur.execute(_cbt_sessions_table_sql(CBT_SESSIONS_POSTGRES_COLUMNS))
        cur.execute(_cbt_answers_table_sql(CBT_ANSWERS_POSTGRES_COLUMNS))
        cur.execute(_user_progress_table_sql(USER_PROGRESS_POSTGRES_COLUMNS))
        cur.execute(_game_sessions_table_sql(GAME_SESSIONS_POSTGRES_COLUMNS))
        cur.execute(_user_sessions_table_sql(USER_SESSIONS_POSTGRES_COLUMNS))
        cur.execute(_user_devices_table_sql(USER_DEVICES_POSTGRES_COLUMNS))
        cur.execute(_theory_attempts_table_sql(THEORY_ATTEMPTS_POSTGRES_COLUMNS))
        cur.execute(_ai_grading_usage_table_sql(AI_GRADING_USAGE_POSTGRES_COLUMNS))

        # column migrations for new tables (safe to run repeatedly)
        _postgres_add_missing_columns(cur, "topics", TOPICS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "subtopics", SUBTOPICS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "lesson_notes", LESSON_NOTES_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "cbt_sessions", CBT_SESSIONS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "cbt_answers", CBT_ANSWERS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "user_progress", USER_PROGRESS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "game_sessions", GAME_SESSIONS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "user_sessions", USER_SESSIONS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "user_devices", USER_DEVICES_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "theory_attempts", THEORY_ATTEMPTS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "ai_grading_usage", AI_GRADING_USAGE_POSTGRES_COLUMNS)

        # *** COMMIT PHASE 1 — tables are now durable regardless of index errors ***
        db.commit()

        # ------------------------------------------------------------------ #
        # PHASE 2 — Indexes                                                   #
        # Each index runs in its own savepoint so one bad index never rolls   #
        # back the others or (critically) the table commit above.             #
        # ------------------------------------------------------------------ #
        _indexes = [
            # questions
            "CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);",
            "CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);",
            "CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);",
            "CREATE INDEX IF NOT EXISTS idx_questions_passage_id ON questions(passage_id);",
            # passages / feedback / audit
            "CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_question_id ON feedback(question_id);",
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_created_at ON admin_audit_log(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);",
            # topics / subtopics / lesson_notes
            "CREATE INDEX IF NOT EXISTS idx_topics_exam_subject ON topics(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_topics_subject_topic ON topics(subject, topic);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_exam_subject ON subtopics(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_topic_id ON subtopics(topic_id);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_subject ON subtopics(subject);",
            "CREATE INDEX IF NOT EXISTS idx_subtopics_topic ON subtopics(subject, topic);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_subtopic ON lesson_notes(subtopic_id);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_exam_subject ON lesson_notes(exam, subject);",
            "CREATE INDEX IF NOT EXISTS idx_lesson_notes_subject ON lesson_notes(subject);",
            # cbt_sessions / cbt_answers
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_user ON cbt_sessions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_user_subject ON cbt_sessions(user_id, subject);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_sessions_started ON cbt_sessions(started_at);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_session ON cbt_answers(session_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_user ON cbt_answers(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_cbt_answers_question ON cbt_answers(question_id);",
            # user_progress
            "CREATE INDEX IF NOT EXISTS idx_user_progress_user ON user_progress(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_user_subject ON user_progress(user_id, subject);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_activity ON user_progress(activity_type);",
            "CREATE INDEX IF NOT EXISTS idx_user_progress_created ON user_progress(created_at);",
            # game_sessions
            "CREATE INDEX IF NOT EXISTS idx_game_sessions_user ON game_sessions(user_id);",
            # user_sessions
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_identifier ON user_sessions(identifier);",
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_active ON user_sessions(identifier, is_active);",
            # user_devices
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_devices_active_user_device ON user_devices(user_id, device_id) WHERE revoked_at IS NULL;",
            "CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_devices_active ON user_devices(user_id, revoked_at);",
            "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS revoke_reason TEXT;",
            # theory_attempts
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_user ON theory_attempts(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_question ON theory_attempts(question_id);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attempts_created ON theory_attempts(created_at);",
            # ai_grading_usage
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_grading_usage_user_period ON ai_grading_usage(user_id, period_key);",
            "CREATE INDEX IF NOT EXISTS idx_ai_grading_usage_user ON ai_grading_usage(user_id);",
        ]

        for sql in _indexes:
            _pg_exec_index(db, cur, sql)

        # *** COMMIT PHASE 2 — all indexes that succeeded are now durable ***
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


if __name__ == "__main__":
    import sys
    print("Running ExamPartner database initialisation...")
    init_db()
    print("Done. All tables and indexes are up to date.")
    sys.exit(0)
