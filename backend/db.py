import logging
import os
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

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
    ("marks", "DOUBLE PRECISION"),  # half marks — see _POSTGRES_COLUMN_TYPE_CHANGES
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
    # Rule 16b siblings to examiner_points, for FLAT theory records only.
    #
    # Where a record has sub_questions, Rule 16a puts examiner_points,
    # rubric_groups and expects_diagram on the parts, and they ride inside
    # sub_questions_json as ordinary JSON — no column is involved and none is
    # needed. These two exist for the other branch: a theory question with no
    # sub-parts, which carries all three at top level.
    #
    # Without them the fields are silently dropped at ingestion and the
    # question becomes permanently ungradeable: parse_scope() raises
    # "rubric_groups must be a non-empty array", the candidate gets a 503 and
    # is not charged, and nothing in the log points at a missing column.
    ("rubric_groups_json", "TEXT"),
    # Whether this question may be served in CBT. NULL or 1 = yes, 0 = no.
    #
    # NULL rather than a DEFAULT 1 so every existing row keeps its current
    # behaviour exactly, with no backfill: the filters read
    # (cbt_eligible IS NULL OR cbt_eligible = 1).
    #
    # Exists because CBT needs a paper's structure confirmed against a
    # complete source before it can allocate from it, and Study mode does
    # not. WAEC 2010 Biology is the case that forced it: the per-question
    # content is audited and sound, but the source copy is missing Part II
    # entirely, so the paper's real section structure cannot be derived from
    # it. Without this column the only lever is ingest-or-don't, which keeps
    # sound content out of Study as well.
    #
    # Distinct from the `country` column planned for paper_rules (see the
    # orphaned-section guard in cbt_service). That one declares "this section
    # is out of scope for our candidates"; this one declares "this paper's
    # structure is not confirmed yet". A record can need either, or both.
    ("cbt_eligible", "INTEGER"),
    # 0 or 1. Kept as INTEGER rather than BOOLEAN so the SQLite and Postgres
    # column lists stay identical, as every other flag in this table does.
    ("expects_diagram", "INTEGER"),
    ("concepts_json", "TEXT"),
    ("common_traps_json", "TEXT"),
    ("references_json", "TEXT"),
    # Official marking guide where available (v16.1 theory pipeline).
    # Written by import_exam_jsonl_full.py from the `marking_scheme` field.
    # Present in the deployed Neon database but previously absent from this
    # list, so freshly provisioned databases lacked it and question imports
    # failed against them.
    ("marking_scheme_json", "TEXT"),
    ("metadata_json", "TEXT"),
    ("passage_id", "TEXT"),
    ("passage_snapshot", "TEXT"),
    # Prescribed set text this question is bound to (e.g. "Hamlet").
    # Absent/NULL means the question is not tied to any set text and is
    # always eligible. See set_text filtering note below.
    ("set_text", "TEXT"),
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
    # Mirrors questions.set_text so a set-text query returns the extracts
    # alongside the questions that reference them.
    ("set_text", "TEXT"),
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
# theory_attachments — candidate diagram uploads (Sprint A)
# ----------------------------
# One row per stored image or PDF that a candidate uploads as part of a
# theory answer.
#
# Separate from theory_attempts rather than a column on it, because an
# attachment exists BEFORE grading. The upload has to complete before the
# grading request is sent — otherwise a slow connection burns a grading
# credit on a file that never arrived — and theory_attempts rows are only
# written after grading returns. The attachment cannot hang off a row that
# does not exist yet.
#
# Column notes:
#   user_id            The identifier (email/phone), matching the convention
#                      already used by theory_attempts, NOT users.id.
#   attempt_key        Client-generated, stable across retakes within one
#                      submission. This is what makes a retake REPLACE the
#                      previous photo instead of appending a second one —
#                      see attachment_service._supersede_pending(). Without
#                      it, grading could find two candidate images for one
#                      sub-question and have no basis to choose.
#   sub_question_label NULL means a whole-question diagram. Nullable rather
#                      than defaulted to "" because the supersede predicate
#                      has to distinguish "no label" from a label, and SQL's
#                      NULL comparison rules make that explicit rather than
#                      accidental.
#   storage_key        The object key in R2. UNIQUE because it is the handle
#                      the grading path passes around; a duplicate would make
#                      ownership ambiguous.
#   width / height     NULL for PDFs — there is no single raster to measure.
#   status             pending | consumed | deleted. Rows are tombstoned
#                      rather than removed, so "my diagram vanished" is
#                      answerable from support.
#   theory_attempt_id  Set when a grading attempt reads the attachment,
#                      linking it back to the attempt it belongs to.
THEORY_ATTACHMENTS_COLUMNS = [
    ("id",                  "TEXT PRIMARY KEY"),
    ("user_id",             "TEXT NOT NULL"),
    ("question_id",         "TEXT NOT NULL"),
    ("sub_question_label",  "TEXT"),
    ("attempt_key",         "TEXT NOT NULL"),
    ("storage_key",         "TEXT NOT NULL UNIQUE"),
    ("content_type",        "TEXT NOT NULL"),
    ("byte_size",           "INTEGER NOT NULL"),
    ("width",               "INTEGER"),
    ("height",              "INTEGER"),
    ("sha256",              "TEXT"),
    ("status",              "TEXT NOT NULL DEFAULT 'pending'"),
    ("theory_attempt_id",   "TEXT"),
    ("consumed_at",         "TEXT"),
    ("expires_at",          "TEXT"),
    ("created_at",          "TEXT"),
]

THEORY_ATTACHMENTS_SQLITE_COLUMNS = [
    *THEORY_ATTACHMENTS_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

THEORY_ATTACHMENTS_POSTGRES_COLUMNS = [
    *THEORY_ATTACHMENTS_COLUMNS[:-1],
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
# password_reset_tokens — single-use OTP codes for password recovery
# ----------------------------
# token_hash:  SHA-256 of the raw 6-digit code sent to the user.
#              The raw code is NEVER stored.
# used_at:     NULL = unused; set to now() when the code is consumed.
#              Enforces single-use on the database level.
# expires_at:  30 minutes from creation. Checked at redemption time.
# identifier:  Matches users.identifier (email or phone string used at login).
# ----------------------------
PASSWORD_RESET_TOKENS_COLUMNS = [
    ("id",          "TEXT PRIMARY KEY"),          # random UUID hex
    ("identifier",  "TEXT NOT NULL"),             # users.identifier
    ("token_hash",  "TEXT NOT NULL UNIQUE"),      # SHA-256(raw_code)
    ("expires_at",  "TEXT NOT NULL"),             # ISO timestamp
    ("used_at",     "TEXT"),                      # NULL = unused
    ("created_at",  "TEXT"),
]

PASSWORD_RESET_TOKENS_SQLITE_COLUMNS = [
    *PASSWORD_RESET_TOKENS_COLUMNS[:-1],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

PASSWORD_RESET_TOKENS_POSTGRES_COLUMNS = [
    *PASSWORD_RESET_TOKENS_COLUMNS[:-1],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]


# ----------------------------
# ai_grading_credit_purchases — top-up credit packs bought via Paystack
# ----------------------------
AI_GRADING_CREDIT_PURCHASES_COLUMNS = [
    ("id",                "TEXT PRIMARY KEY"),
    ("user_identifier",   "TEXT NOT NULL"),
    ("credits_total",     "INTEGER NOT NULL"),
    ("credits_used",      "INTEGER NOT NULL DEFAULT 0"),
    ("amount_paid",       "INTEGER NOT NULL"),
    ("currency",          "TEXT NOT NULL DEFAULT 'NGN'"),
    ("payment_reference", "TEXT UNIQUE"),
    ("status",            "TEXT NOT NULL DEFAULT 'active'"),
    ("created_at",        "TEXT"),
    ("expires_at",        "TEXT"),
]

AI_GRADING_CREDIT_PURCHASES_SQLITE_COLUMNS = [
    *AI_GRADING_CREDIT_PURCHASES_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("expires_at", "TEXT"),
]

AI_GRADING_CREDIT_PURCHASES_POSTGRES_COLUMNS = [
    *AI_GRADING_CREDIT_PURCHASES_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("expires_at", "TIMESTAMPTZ"),
]


# ----------------------------
# paper_rules — per-paper timing/count/marks metadata (Sprint 3)
# ----------------------------
# One row = one paper (Objective / Theory / Oral English / etc.) for a given
# exam+subject, optionally pinned to a specific year. year IS NULL means a
# year-agnostic rule (syllabus-derived or a temporary fallback) — see
# rule_source below for which kind.
#
# country IS NULL means the rule applies regardless of candidate country —
# the same "applies regardless" semantics year IS NULL already carries, and
# the state every pre-existing row is in. A non-NULL country marks a rule
# that applies ONLY to that country. Needed because WAEC Geography runs a
# different section/count STRUCTURE per country (Nigeria: fixed allocation;
# Gambia/Liberia/Sierra Leone: distributed minimum), which rules_json alone
# cannot express — duration_minutes, question_count and total_marks are
# columns, outside that JSON entirely.
#
# year and country are INDEPENDENTLY nullable, so row identity is a 2x2
# matrix, not a single toggle. upsert_paper_rule() branches on all four
# combinations; see the comment there before changing either predicate.
#
# Resolution order (implemented in services/paper_rules_service.py):
#   1. exact match: exam + subject + paper + year + country
#   2. best year-NULL match for exam + subject + paper + country, preferring
#      rule_source = 'actual_paper' over 'syllabus_default' over
#      'legacy_placeholder' if more than one year-NULL row exists
#      (country-specific rows are preferred over country-agnostic ones at
#      each tier — a NULL-country row is the fallback, never a competitor)
#   3. if nothing in the table at all, the route falls back to the existing
#      hardcoded CBT_PAPER_DURATION_MINUTES / get_cbt_cap() in cbt_service.py
#      — paper_rules never needs every row populated to be useful.
#
# rule_source values:
#   actual_paper        — confirmed from a real booklet/cover page for that
#                          specific year (highest confidence)
#   syllabus_default     — derived from the current official syllabus because
#                          the specific year's paper didn't state it
#   legacy_placeholder    — the old universal hardcoded guess (e.g. 60/120),
#                          kept only until replaced by one of the above; a
#                          to-do marker, not a real source
PAPER_RULES_COLUMNS = [
    ("id",                "TEXT PRIMARY KEY"),
    ("exam",              "TEXT NOT NULL"),
    ("subject",           "TEXT NOT NULL"),
    ("paper",              "TEXT NOT NULL"),
    ("year",               "INTEGER"),                 # NULL = year-agnostic rule
    ("country",            "TEXT"),                    # NULL = applies to every candidate country
    ("duration_minutes",   "INTEGER"),                  # this paper's own duration only
    ("question_count",     "INTEGER"),                  # nullable — not always known
    ("total_marks",        "DOUBLE PRECISION"),                  # nullable — not always known
    ("rule_source",        "TEXT NOT NULL"),             # actual_paper | syllabus_default | legacy_placeholder
    ("rules_json",         "TEXT"),                      # reserved for future structured rules; unused for now
    ("created_at",         "TEXT"),
    ("updated_at",         "TEXT"),
]

PAPER_RULES_SQLITE_COLUMNS = [
    *PAPER_RULES_COLUMNS[:-2],
    ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
]

PAPER_RULES_POSTGRES_COLUMNS = [
    *PAPER_RULES_COLUMNS[:-2],
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
]


# ----------------------------
# Windows seat-pool licensing (Pilot V1)
# ----------------------------
# See docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md §3–§4.
#
# Every `*_at` column is a timestamp: ISO TEXT on SQLite, TIMESTAMPTZ on
# Postgres, like every other table here. The lists below are written once with
# TEXT and _licensing_postgres_columns() swaps the type for Postgres, rather
# than restating each list — institution_seats has timestamps scattered
# through it, and the [:-2] slicing the older tables use would silently drop
# or duplicate one. No column DEFAULTs: licensing writes every timestamp
# from services/licensing_time.py, so the test clock governs all of them.
#
# Timestamps come back as str on SQLite and datetime on Postgres. Compare them
# only through licensing_time.to_datetime(), never in SQL.

def _licensing_postgres_columns(columns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (name, ddl.replace("TEXT", "TIMESTAMPTZ", 1) if name.endswith("_at") else ddl)
        for name, ddl in columns
    ]


# accounts — an institution (V1) or reseller (not built). owner_identifier is
# the primary administrator and matches users.identifier; no FK, as with the
# other newer tables. subscription_expires_at NULL = no entitlement yet.
ACCOUNTS_COLUMNS = [
    ("id",                      "TEXT PRIMARY KEY"),
    ("name",                    "TEXT NOT NULL"),
    ("account_type",            "TEXT NOT NULL"),       # institution | reseller
    ("contact_email",           "TEXT"),
    ("owner_identifier",        "TEXT"),
    ("subscription_status",     "TEXT"),                # active | expired | suspended
    ("subscription_expires_at", "TEXT"),
    ("created_at",              "TEXT"),
    ("updated_at",              "TEXT"),
]

# seat_pools — purchased concurrent capacity. pool_size is mutable; that is
# the whole resizing story. There is no CHECK on capacity and there cannot be
# one: CHECK (active <= pool_size) rejects the shrink itself, and
# over-capacity after a shrink is a supported state (spec §3.3, §5.4).
#
# last_claim_at is load-bearing. claim_capacity() writes it as its first
# statement, and that write is the row lock that serialises concurrent
# claims. See services/licensing_service.py before touching it.
SEAT_POOLS_COLUMNS = [
    ("id",            "TEXT PRIMARY KEY"),
    ("account_id",    "TEXT NOT NULL"),
    ("pool_size",     "INTEGER NOT NULL"),
    ("label",         "TEXT"),
    ("status",        "TEXT"),                          # active | suspended
    ("last_claim_at", "TEXT"),
    ("created_at",    "TEXT"),
    ("updated_at",    "TEXT"),
]

# institution_seats — the name is kept for continuity, but a row is NOT a
# seat. It is an activation binding: one machine's claim on capacity. A pool
# of 20 creates no rows. Active = revoked_at IS NULL, the user_devices idiom.
# Available capacity = pool_size - COUNT(active), computed, never stored.
# Revoked rows are kept forever — the row id IS the activation identity, and
# a revoked one must never be silently restored by hardware recognition.
INSTITUTION_SEATS_COLUMNS = [
    ("id",                       "TEXT PRIMARY KEY"),
    ("seat_pool_id",             "TEXT NOT NULL"),
    ("account_id",               "TEXT NOT NULL"),      # denormalised: scope checks need no join
    ("machine_fingerprint_hash", "TEXT NOT NULL"),
    ("fingerprint_signals_json", "TEXT"),
    ("fingerprint_confidence",   "TEXT"),               # strong | weak | degenerate, at claim time
    ("installation_id",          "TEXT"),               # server-issued; the CURRENT installation
    ("machine_label",            "TEXT"),
    ("activated_at",             "TEXT"),
    ("last_seen_at",             "TEXT"),
    ("lease_issued_at",          "TEXT"),
    ("lease_expires_at",         "TEXT"),
    ("lease_serial",             "INTEGER NOT NULL DEFAULT 0"),
    ("revoked_at",               "TEXT"),               # NULL = active
    ("revoke_reason",            "TEXT"),
    ("revoked_by",               "TEXT"),
    ("created_at",               "TEXT"),
    ("updated_at",               "TEXT"),
]

# seat_activation_log — immutable: never updated, never deleted. Carries its
# OWN copies of fingerprint hash, signals and label, so history still
# reconstructs after the capacity is reused by a different machine.
SEAT_ACTIVATION_LOG_COLUMNS = [
    ("id",                       "TEXT PRIMARY KEY"),
    ("account_id",               "TEXT NOT NULL"),
    ("seat_pool_id",             "TEXT"),
    ("binding_id",               "TEXT"),               # NULL for pool-level events
    ("action",                   "TEXT NOT NULL"),
    ("installation_id",          "TEXT"),
    ("machine_fingerprint_hash", "TEXT"),
    ("machine_label",            "TEXT"),
    ("fingerprint_signals_json", "TEXT"),
    ("performed_by",             "TEXT"),
    ("actor_role",               "TEXT"),               # institution_owner | exampartner_admin | client
    ("reason",                   "TEXT"),
    ("detail_json",              "TEXT"),
    ("created_at",               "TEXT"),
]

# licence_ambiguities — separate from the log because the log is immutable
# and an ambiguity has mutable state: open -> resolved -> consumed, or
# expired (brief §4.C).
LICENCE_AMBIGUITIES_COLUMNS = [
    ("id",                         "TEXT PRIMARY KEY"),
    ("account_id",                 "TEXT"),
    ("seat_pool_id",               "TEXT"),
    ("presented_signals_json",     "TEXT"),
    ("presented_fingerprint_hash", "TEXT"),
    ("presented_installation_id",  "TEXT"),
    ("machine_label",              "TEXT"),
    ("candidate_binding_ids_json", "TEXT"),
    ("status",                     "TEXT"),
    ("resolution",                 "TEXT"),             # recognize_existing | treat_as_new
    ("resolved_binding_id",        "TEXT"),
    ("resolved_by",                "TEXT"),
    ("resolved_at",                "TEXT"),
    ("created_at",                 "TEXT"),
]

# (table name, column list) — the SQLite branch uses these lists as written,
# the Postgres branch through _licensing_postgres_columns().
LICENSING_TABLES = [
    ("accounts",            ACCOUNTS_COLUMNS),
    ("seat_pools",          SEAT_POOLS_COLUMNS),
    ("institution_seats",   INSTITUTION_SEATS_COLUMNS),
    ("seat_activation_log", SEAT_ACTIVATION_LOG_COLUMNS),
    ("licence_ambiguities", LICENCE_AMBIGUITIES_COLUMNS),
]

# Ordinary licensing indexes — best-effort: a failure is logged and skipped,
# like every other performance index. They run inside the licensing step
# (_init_licensing_schema), not in the core index loops.
LICENSING_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_identifier);",
    "CREATE INDEX IF NOT EXISTS idx_seat_pools_account ON seat_pools(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_seat_bindings_pool_active ON institution_seats(seat_pool_id, revoked_at);",
    "CREATE INDEX IF NOT EXISTS idx_seat_bindings_account ON institution_seats(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_seat_bindings_fp ON institution_seats(machine_fingerprint_hash);",
    "CREATE INDEX IF NOT EXISTS idx_seat_log_account_created ON seat_activation_log(account_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_seat_log_binding ON seat_activation_log(binding_id);",
    "CREATE INDEX IF NOT EXISTS idx_ambiguities_open ON licence_ambiguities(status, created_at);",
]

# The licensing-critical constraint: one active binding per installation. It
# is also the clone detector.
#
# This one is deliberately NOT best-effort. The index loops swallow a failure
# as a logger.warning, which for this index would mean clone detection
# silently disappears with the only trace in a Render boot log. It is created
# where an exception is raised, and assert_licensing_constraints() then
# verifies it exists.
#
# Raised, but not fatal to the backend (brief §4.E, 8 October 2026). This
# backend also serves the live Android app, so a licensing schema problem
# closes LICENSING, never the platform: init_db() catches the failure in the
# licensing step, logs it at ERROR and records licensing as unavailable, and
# every other route keeps working. The guarantee is then enforced where
# bindings are written — claim_capacity() checks for this index in its own
# transaction on every claim and refuses with 503 if it is missing. Any later
# path that creates a binding or changes a binding's installation_id must do
# the same; a startup flag alone would let licensing code run on the
# assumption the index holds.
#
# The realistic way it goes missing is not a manual DROP — the next init_db()
# simply recreates a dropped index — but failing to CREATE because existing
# data violates it: two active bindings sharing an installation_id.
#
# To change its definition, do not edit this statement in place:
# CREATE UNIQUE INDEX IF NOT EXISTS is a no-op when the name already exists,
# so existing databases would keep the old definition forever. DROP the old
# name and create a versioned one (_v2), as ux_paper_rules_unique_row_v2 does,
# and update LICENSING_REQUIRED_INDEX to match.
#
# NULLs are distinct in unique indexes, so this guards nothing for a binding
# with installation_id NULL. claim_capacity() always generates one, which is
# what makes the index meaningful.
LICENSING_REQUIRED_INDEX = "ux_seat_bindings_active_installation"
LICENSING_REQUIRED_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_seat_bindings_active_installation "
    "ON institution_seats(installation_id) WHERE revoked_at IS NULL;"
)


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

    Core schema failures raise and stop startup, exactly as before. The
    licensing schema runs afterwards as its own step and NEVER raises out of
    here — see _init_licensing_schema().
    """
    import time

    if not _using_postgres():
        _init_db_sqlite(db_path=db_path)
        _init_licensing_schema(db_path=db_path)
        return

    # The retry loop covers the core schema only.
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            _init_db_postgres()
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "init_db attempt %d/3 failed: %s — %s",
                attempt, type(exc).__name__, exc,
            )
            if attempt < 3:
                time.sleep(attempt * 2)  # 2s then 4s

    if last_exc is not None:
        logger.error("init_db failed after 3 attempts — raising last exception")
        raise last_exc

    # Run once, after the retry loop: a licensing schema problem (duplicate
    # active installation_ids, a missing index) is not a transient connection
    # failure, and retrying it would only delay the same result by six seconds.
    _init_licensing_schema()


# ----------------------------
# Licensing schema step and status
# ----------------------------
# The outcome of the last licensing step in THIS process, read by /health.
# Deliberately not re-read from the database on each /health request: if
# Render or anything else polls /health, a per-request query would keep
# Neon's compute awake around the clock.
#
# It is a report, not a gate. claim_capacity() never trusts it — it checks
# the index itself, live, so a stale value here can never grant capacity.
_licensing_status: dict = {"ready": False, "reason": "init_db() has not run in this process"}


def licensing_status() -> dict:
    """{"ready": bool, "reason": str | None} from the last init_db() here."""
    return dict(_licensing_status)


def _init_licensing_schema(db_path: Optional[str] = None) -> None:
    """
    Create the licensing tables and indexes, then assert the licensing-
    critical index exists. Runs AFTER the core schema has committed, on its
    own connection, so nothing here can roll back or abort core schema work.

    Any failure — table DDL, the required index refusing to build over
    duplicate data, assert_licensing_constraints() — is caught, logged at
    ERROR with the exception, and recorded as licensing unavailable. It is
    never re-raised: licensing closes, the rest of the backend starts
    (brief §4.E).
    """
    try:
        if _using_postgres():
            _init_licensing_postgres()
        else:
            _init_licensing_sqlite(db_path=db_path)
        assert_licensing_constraints(db_path=db_path)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.error(
            "LICENSING UNAVAILABLE — licensing schema step failed; seat-pool "
            "licensing is closed, every other route is unaffected. %s",
            reason,
            exc_info=exc,
        )
        _licensing_status.update(ready=False, reason=reason)
        return
    _licensing_status.update(ready=True, reason=None)


def _init_licensing_sqlite(db_path: Optional[str] = None) -> None:
    db_path = db_path or os.getenv("DB_PATH", "exam_partner.db")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        for table_name, columns in LICENSING_TABLES:
            cur.execute(_table_sql(table_name, columns))
            _sqlite_add_missing_columns(cur, table_name, columns)
        conn.commit()

        for sql in LICENSING_INDEXES:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.warning("SQLite licensing index DDL skipped (%s): %s", type(exc).__name__, exc)

        # NOT in the loop above: a failure here is raised to
        # _init_licensing_schema(). See LICENSING_REQUIRED_INDEX_SQL.
        cur.execute(LICENSING_REQUIRED_INDEX_SQL)
        conn.commit()
    finally:
        conn.close()


def _init_licensing_postgres() -> None:
    db = _get_pg()
    try:
        cur = db.cursor()
        for table_name, columns in LICENSING_TABLES:
            pg_columns = _licensing_postgres_columns(columns)
            cur.execute(_table_sql(table_name, pg_columns))
            _postgres_add_missing_columns(cur, table_name, pg_columns)
        db.commit()

        for sql in LICENSING_INDEXES:
            _pg_exec_index(db, cur, sql)
        db.commit()

        # Deliberately NOT through _pg_exec_index: a failure here is raised to
        # _init_licensing_schema(). See LICENSING_REQUIRED_INDEX_SQL.
        cur.execute(LICENSING_REQUIRED_INDEX_SQL)
        db.commit()
    finally:
        db.close()


class LicensingConstraintError(RuntimeError):
    """A correctness-critical licensing constraint is missing from the schema."""


def licensing_index_present(cur) -> bool:
    """
    True if ux_seat_bindings_active_installation exists. Takes the CALLER's
    cursor so claim_capacity() can run it inside its own transaction; the
    one catalog query shared by every check, so they cannot disagree.
    """
    if _using_postgres():
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = ?",
            (LICENSING_REQUIRED_INDEX,),
        )
    else:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (LICENSING_REQUIRED_INDEX,),
        )
    return cur.fetchone() is not None


def assert_licensing_constraints(db_path: Optional[str] = None) -> None:
    """
    Raise LicensingConstraintError unless ux_seat_bindings_active_installation
    exists.

    init_db() calls this at the end of the licensing step and CATCHES the
    error: licensing is recorded unavailable and the backend still starts
    (brief §4.E). The function itself still raises, so any other caller gets
    a hard failure.

    db_path must be the same one init_db() was given. config.DB_PATH and the
    DB_PATH env var can differ under SQLite (see the client fixture in
    tests/test_attachments.py); checking a different file than the one just
    initialised would pass or fail for the wrong reason.
    """
    db = get_db(db_path)
    try:
        present = licensing_index_present(db.cursor())
    finally:
        db.close()

    if not present:
        raise LicensingConstraintError(
            f"Required licensing index {LICENSING_REQUIRED_INDEX} is missing. "
            "Without it one installation can hold two active seat bindings and "
            "clone detection is disabled."
        )


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


def _theory_attachments_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("theory_attachments", columns)


def _ai_grading_usage_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("ai_grading_usage", columns)


def _password_reset_tokens_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("password_reset_tokens", columns)


def _ai_grading_credit_purchases_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("ai_grading_credit_purchases", columns)


def _paper_rules_table_sql(columns: list[tuple[str, str]]) -> str:
    return _table_sql("paper_rules", columns)


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


# Columns whose TYPE changed after the table was first created. ADD COLUMN IF
# NOT EXISTS never touches an existing column, so a type change needs its own
# step, run on every init_db() and a no-op once applied.
#
# Half marks (spec v16.4): NECO 2021 Financial Accounting Q1-Q4 are worth 12.5.
# Postgres does not reject 12.5 going into an INTEGER column; it rounds it to
# 13 with no error, and the grading prompt and percentage then use 13.
# DOUBLE PRECISION rather than NUMERIC: halves are exact in binary floating
# point, and psycopg2 returns NUMERIC as Decimal, which json.dumps cannot
# encode — _store_attempt() would swallow that error and silently not save.
_POSTGRES_COLUMN_TYPE_CHANGES = [
    ("questions", "marks", "DOUBLE PRECISION"),
    ("paper_rules", "total_marks", "DOUBLE PRECISION"),
]
_POSTGRES_INTEGER_TYPES = {"smallint", "integer", "bigint"}


def _postgres_apply_column_type_changes(cur) -> None:
    for table, column, target in _POSTGRES_COLUMN_TYPE_CHANGES:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
            (table, column),
        )
        row = cur.fetchone()
        if not row:
            continue
        data_type = str(row.get("data_type") if hasattr(row, "get") else row[0]).lower()
        if data_type in _POSTGRES_INTEGER_TYPES:
            cur.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {target} "
                f"USING {column}::{target}"
            )
            logger.info("Widened %s.%s from %s to %s", table, column, data_type, target)


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
            # Candidate country, ISO 3166-1 alpha-2, validated against
            # config.SUPPORTED_COUNTRIES. NULL = not yet stated, which is the
            # state of every account created before this column existed.
            #
            # Needed because WAEC Geography runs a different section/count
            # STRUCTURE per country, so resolving the right paper_rules row
            # requires knowing the candidate's country — content curation
            # alone cannot express it. See paper_rules.country in this file.
            ("country", "TEXT"),
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
        cur.execute(_theory_attachments_table_sql(THEORY_ATTACHMENTS_SQLITE_COLUMNS))
        cur.execute(_ai_grading_usage_table_sql(AI_GRADING_USAGE_SQLITE_COLUMNS))
        cur.execute(_password_reset_tokens_table_sql(PASSWORD_RESET_TOKENS_SQLITE_COLUMNS))
        cur.execute(_ai_grading_credit_purchases_table_sql(AI_GRADING_CREDIT_PURCHASES_SQLITE_COLUMNS))
        cur.execute(_paper_rules_table_sql(PAPER_RULES_SQLITE_COLUMNS))

        # ---- lightweight column migrations ----
        _sqlite_add_missing_question_columns(cur)
        _sqlite_add_missing_columns(cur, "passages", PASSAGES_COLUMNS)
        _sqlite_add_missing_columns(cur, "feedback", FEEDBACK_COLUMNS)
        _sqlite_add_missing_columns(cur, "topics", TOPICS_COLUMNS)
        _sqlite_add_missing_columns(cur, "subtopics", SUBTOPICS_COLUMNS)
        _sqlite_add_missing_columns(cur, "lesson_notes", LESSON_NOTES_COLUMNS)
        _sqlite_add_missing_columns(cur, "theory_attempts", THEORY_ATTEMPTS_COLUMNS)
        _sqlite_add_missing_columns(cur, "theory_attachments", THEORY_ATTACHMENTS_COLUMNS)
        _sqlite_add_missing_columns(cur, "ai_grading_usage", AI_GRADING_USAGE_COLUMNS)
        _sqlite_add_missing_columns(cur, "password_reset_tokens", PASSWORD_RESET_TOKENS_COLUMNS)
        _sqlite_add_missing_columns(cur, "ai_grading_credit_purchases", AI_GRADING_CREDIT_PURCHASES_COLUMNS)
        _sqlite_add_missing_columns(cur, "paper_rules", PAPER_RULES_COLUMNS)

        # *** COMMIT PHASE 1 — tables are now durable regardless of index errors ***
        conn.commit()

        # ---- indexes — each wrapped individually so one failure never blocks others ----
        _sqlite_indexes = [
            # questions
            "CREATE INDEX IF NOT EXISTS idx_questions_exam_year_subject ON questions(exam, year, subject);",
            "CREATE INDEX IF NOT EXISTS idx_questions_qtype ON questions(qtype);",
            "CREATE INDEX IF NOT EXISTS idx_questions_sort_key ON questions(sort_key);",
            "CREATE INDEX IF NOT EXISTS idx_questions_passage_id ON questions(passage_id);",
            # Partial: only set-text-bound rows are indexed. The vast majority of
            # questions have set_text NULL and are filtered by the IS NULL branch,
            # which this index deliberately does not cover.
            "CREATE INDEX IF NOT EXISTS idx_questions_set_text ON questions(set_text) WHERE set_text IS NOT NULL;",
            # passages / feedback / audit
            "CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);",
            "CREATE INDEX IF NOT EXISTS idx_passages_set_text ON passages(set_text) WHERE set_text IS NOT NULL;",
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
            # theory_attachments
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_lookup ON theory_attachments(user_id, question_id, attempt_key);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_key ON theory_attachments(storage_key);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_sweep ON theory_attachments(status, expires_at);",
            # ai_grading_usage
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_grading_usage_user_period ON ai_grading_usage(user_id, period_key);",
            "CREATE INDEX IF NOT EXISTS idx_ai_grading_usage_user ON ai_grading_usage(user_id);",
            # password_reset_tokens
            "CREATE INDEX IF NOT EXISTS idx_prt_identifier ON password_reset_tokens(identifier);",
            "CREATE INDEX IF NOT EXISTS idx_prt_expires_at ON password_reset_tokens(expires_at);",
            # ai_grading_credit_purchases
            "CREATE INDEX IF NOT EXISTS idx_agcp_user ON ai_grading_credit_purchases(user_identifier);",
            "CREATE INDEX IF NOT EXISTS idx_agcp_expires ON ai_grading_credit_purchases(expires_at);",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_agcp_reference ON ai_grading_credit_purchases(payment_reference);",
            # paper_rules
            "CREATE INDEX IF NOT EXISTS idx_paper_rules_lookup ON paper_rules(exam, subject, paper, year);",
            # Unique row identity widened to include country. The old index is
            # dropped by name rather than recreated in place, because
            # CREATE UNIQUE INDEX IF NOT EXISTS is a no-op when the name
            # already exists — it would silently keep the narrow definition on
            # any existing database. Versioning the name makes the migration
            # idempotent and greppable; the DROP is safe to run forever.
            #
            # Note this index does NOT deduplicate country-agnostic rows:
            # both engines treat NULLs as distinct in unique indexes (same
            # caveat as `year` above). The real guard against overwriting the
            # wrong row is the four-branch predicate in upsert_paper_rule();
            # this index catches a different, louder failure.
            "DROP INDEX IF EXISTS ux_paper_rules_unique_row;",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_rules_unique_row_v2 ON paper_rules(exam, subject, paper, year, country);",
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
        # Candidate country — see the SQLite branch above for why this exists.
        # Kept in step with that list: users migrations are declared in two
        # places in this file, unlike paper_rules' single column list.
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT;")

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
        cur.execute(_theory_attachments_table_sql(THEORY_ATTACHMENTS_POSTGRES_COLUMNS))
        cur.execute(_ai_grading_usage_table_sql(AI_GRADING_USAGE_POSTGRES_COLUMNS))
        cur.execute(_password_reset_tokens_table_sql(PASSWORD_RESET_TOKENS_POSTGRES_COLUMNS))
        cur.execute(_ai_grading_credit_purchases_table_sql(AI_GRADING_CREDIT_PURCHASES_POSTGRES_COLUMNS))
        cur.execute(_paper_rules_table_sql(PAPER_RULES_POSTGRES_COLUMNS))

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
        _postgres_add_missing_columns(cur, "theory_attachments", THEORY_ATTACHMENTS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "ai_grading_usage", AI_GRADING_USAGE_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "password_reset_tokens", PASSWORD_RESET_TOKENS_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "ai_grading_credit_purchases", AI_GRADING_CREDIT_PURCHASES_POSTGRES_COLUMNS)
        _postgres_add_missing_columns(cur, "paper_rules", PAPER_RULES_POSTGRES_COLUMNS)
        _postgres_apply_column_type_changes(cur)

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
            # Partial: only set-text-bound rows are indexed. The vast majority of
            # questions have set_text NULL and are filtered by the IS NULL branch,
            # which this index deliberately does not cover.
            "CREATE INDEX IF NOT EXISTS idx_questions_set_text ON questions(set_text) WHERE set_text IS NOT NULL;",
            # passages / feedback / audit
            "CREATE INDEX IF NOT EXISTS idx_passages_lookup ON passages(exam, year, subject, paper, section);",
            "CREATE INDEX IF NOT EXISTS idx_passages_set_text ON passages(set_text) WHERE set_text IS NOT NULL;",
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
            # theory_attachments
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_lookup ON theory_attachments(user_id, question_id, attempt_key);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_key ON theory_attachments(storage_key);",
            "CREATE INDEX IF NOT EXISTS idx_theory_attachments_sweep ON theory_attachments(status, expires_at);",
            # ai_grading_usage
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_grading_usage_user_period ON ai_grading_usage(user_id, period_key);",
            "CREATE INDEX IF NOT EXISTS idx_ai_grading_usage_user ON ai_grading_usage(user_id);",
            # password_reset_tokens
            "CREATE INDEX IF NOT EXISTS idx_prt_identifier ON password_reset_tokens(identifier);",
            "CREATE INDEX IF NOT EXISTS idx_prt_expires_at ON password_reset_tokens(expires_at);",
            # ai_grading_credit_purchases
            "CREATE INDEX IF NOT EXISTS idx_agcp_user ON ai_grading_credit_purchases(user_identifier);",
            "CREATE INDEX IF NOT EXISTS idx_agcp_expires ON ai_grading_credit_purchases(expires_at);",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_agcp_reference ON ai_grading_credit_purchases(payment_reference);",
            # paper_rules
            "CREATE INDEX IF NOT EXISTS idx_paper_rules_lookup ON paper_rules(exam, subject, paper, year);",
            # Unique row identity widened to include country. The old index is
            # dropped by name rather than recreated in place, because
            # CREATE UNIQUE INDEX IF NOT EXISTS is a no-op when the name
            # already exists — it would silently keep the narrow definition on
            # any existing database. Versioning the name makes the migration
            # idempotent and greppable; the DROP is safe to run forever.
            #
            # Note this index does NOT deduplicate country-agnostic rows:
            # both engines treat NULLs as distinct in unique indexes (same
            # caveat as `year` above). The real guard against overwriting the
            # wrong row is the four-branch predicate in upsert_paper_rule();
            # this index catches a different, louder failure.
            "DROP INDEX IF EXISTS ux_paper_rules_unique_row;",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_rules_unique_row_v2 ON paper_rules(exam, subject, paper, year, country);",
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
