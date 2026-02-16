#!/usr/bin/env python3
"""Import exam JSONL records into the questions table."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Reuse existing DB connection logic.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import get_db, init_db  # noqa: E402

QUESTION_COLUMNS = [
    "id",
    "exam",
    "year",
    "subject",
    "paper",
    "section",
    "qtype",
    "sort_key",
    "page",
    "marks",
    "question_text",
    "options_json",
    "answer",
    "explanation",
    "sub_questions_json",
    "solution_steps_json",
    "diagrams_json",
]

JSON_TO_JSON_COL = {
    "options": "options_json",
    "sub_questions": "sub_questions_json",
    "solution_steps": "solution_steps_json",
    "diagrams": "diagrams_json",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import exam JSONL into questions table.")
    parser.add_argument("--file", required=True, help="Path to JSONL file.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and parse, but do not write to DB.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Abort when encountering invalid JSON or malformed records.",
    )
    parser.add_argument(
        "--upsert",
        action="store_true",
        help="Upsert records by id. Without this flag, duplicate IDs are skipped.",
    )
    return parser.parse_args()


def _encode_jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = {col: None for col in QUESTION_COLUMNS}

    for col in QUESTION_COLUMNS:
        if col in record:
            normalized[col] = _encode_jsonish(record[col])

    for src_key, dest_col in JSON_TO_JSON_COL.items():
        if src_key in record:
            normalized[dest_col] = json.dumps(record[src_key], ensure_ascii=False)

    return normalized


def _record_is_valid(record: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(record, dict):
        return False, "record must be a JSON object"

    required = ["id", "qtype", "question_text"]
    missing = [key for key in required if key not in record]
    if missing:
        return False, f"missing required fields for import: {', '.join(missing)}"

    if not isinstance(record["id"], str):
        return False, "id must be a string"

    return True, None


def _insert_sql() -> str:
    placeholders = ",".join(["?"] * len(QUESTION_COLUMNS))
    columns = ",".join(QUESTION_COLUMNS)
    return f"INSERT INTO questions ({columns}) VALUES ({placeholders})"


def _upsert_sql() -> str:
    updates = ",".join(f"{col}=excluded.{col}" for col in QUESTION_COLUMNS if col != "id")
    columns = ",".join(QUESTION_COLUMNS)
    placeholders = ",".join(["?"] * len(QUESTION_COLUMNS))
    return (
        f"INSERT INTO questions ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )


def _rollback(db: Any) -> None:
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        rollback()


def main() -> int:
    args = _parse_args()
    input_path = Path(args.file)

    inserted = 0
    skipped = 0
    errors = 0

    init_db()
    db = get_db()

    try:
        cur = db.cursor()
        if args.upsert:
            sql = _upsert_sql()
        else:
            sql = _insert_sql()

        with input_path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                row = raw.strip()
                if not row:
                    continue

                try:
                    record = json.loads(row)
                except json.JSONDecodeError as exc:
                    errors += 1
                    print(f"line {line_no}: invalid JSON ({exc.msg})")
                    if args.strict:
                        _rollback(db)
                        return 2
                    continue

                valid, reason = _record_is_valid(record)
                if not valid:
                    errors += 1
                    print(f"line {line_no}: {reason}")
                    if args.strict:
                        _rollback(db)
                        return 2
                    continue

                normalized = _normalize_record(record)
                values = [normalized[col] for col in QUESTION_COLUMNS]

                if args.dry_run:
                    inserted += 1
                    continue

                try:
                    cur.execute(sql, values)
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    # Duplicate IDs can happen in non-upsert mode.
                    msg = str(exc)
                    if not args.upsert and "UNIQUE" in msg.upper():
                        skipped += 1
                        print(f"line {line_no}: duplicate id {normalized['id']} (skipped)")
                        continue

                    errors += 1
                    print(f"line {line_no}: failed to import ({exc})")
                    if args.strict:
                        _rollback(db)
                        return 2

        if args.dry_run:
            print(f"dry-run complete: would import {inserted} records")
        else:
            db.commit()
            print(f"import complete: inserted_or_updated={inserted}, skipped={skipped}, errors={errors}")

        return 0 if errors == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
