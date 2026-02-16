#!/usr/bin/env python3
"""Validate exam JSONL records and emit machine-readable reports."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_KEYS = {"id", "qtype", "question_text", "exam", "year", "subject"}
ALLOWED_QTYPES = {"objective", "theory"}
OBJECTIVE_ID_PATTERN = re.compile(r"^(NECO|WAEC|JAMB)_\d{4}_[A-Z0-9_]+_OBJECTIVE_Q([1-9]\d*)$")
THEORY_ID_PATTERN = re.compile(r"^(NECO|WAEC|JAMB)_\d{4}_[A-Z0-9_]+_THEORY_T([1-9]\d*)$")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate exam JSONL records.")
    parser.add_argument("--file", required=True, help="Path to the input JSONL file.")
    parser.add_argument("--reports", default="reports", help="Directory where reports are written.")
    return parser.parse_args()


def _validate_line(obj: dict[str, Any], seen_ids: set[str]) -> list[str]:
    errors: list[str] = []

    missing_keys = sorted(k for k in REQUIRED_KEYS if k not in obj)
    if missing_keys:
        errors.append(f"Missing required keys: {', '.join(missing_keys)}")
        return errors

    qtype = obj.get("qtype")
    if qtype not in ALLOWED_QTYPES:
        errors.append("qtype must be one of: objective, theory")

    record_id = obj.get("id")
    if not isinstance(record_id, str):
        errors.append("id must be a string")
    else:
        if qtype == "objective":
            if not OBJECTIVE_ID_PATTERN.match(record_id):
                errors.append("id does not match objective pattern")
        elif qtype == "theory":
            if not THEORY_ID_PATTERN.match(record_id):
                errors.append("id does not match theory pattern")

        if record_id in seen_ids:
            errors.append("duplicate id")
        else:
            seen_ids.add(record_id)

    return errors


def main() -> int:
    args = _parse_args()
    input_path = Path(args.file)
    reports_dir = Path(args.reports)
    reports_dir.mkdir(parents=True, exist_ok=True)

    errors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    lines_processed = 0

    with input_path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue

            lines_processed += 1
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(
                    {
                        "line": line_no,
                        "id": None,
                        "errors": [f"invalid JSON: {exc.msg}"],
                    }
                )
                continue

            if not isinstance(obj, dict):
                errors.append(
                    {
                        "line": line_no,
                        "id": None,
                        "errors": ["record must be a JSON object"],
                    }
                )
                continue

            line_errors = _validate_line(obj, seen_ids)
            if line_errors:
                errors.append(
                    {
                        "line": line_no,
                        "id": obj.get("id"),
                        "errors": line_errors,
                    }
                )

    base_name = input_path.stem
    errors_path = reports_dir / f"{base_name}.errors.json"
    summary_path = reports_dir / f"{base_name}.summary.json"

    summary = {
        "file": str(input_path),
        "lines_processed": lines_processed,
        "error_count": len(errors),
        "is_clean": len(errors) == 0,
    }

    errors_path.write_text(json.dumps(errors, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
