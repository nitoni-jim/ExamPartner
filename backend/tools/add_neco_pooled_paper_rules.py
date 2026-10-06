#!/usr/bin/env python3
"""
tools/add_neco_pooled_paper_rules.py — write the first pooled (Pattern B)
paper_rules rows: NECO Christian Religious Studies and NECO History, Theory.

    python tools/add_neco_pooled_paper_rules.py                      # dry run, writes nothing
    python tools/add_neco_pooled_paper_rules.py --commit
    python tools/add_neco_pooled_paper_rules.py --commit --only History
    python tools/add_neco_pooled_paper_rules.py --commit --replace   # overwrite an existing row

Run from the backend folder with DATABASE_URL pointing at Neon; the script
refuses to run otherwise. The 14 fixed NECO rows stay with
add_neco_theory_paper_rules.py, whose sum-of-sections check a pooled row
fails by design (45 in sections against a 60 maximum).

Every write goes through upsert_paper_rule(), so
validate_rules_json() runs on each row, including the Sprint B pooled checks
(design §3.6) and the label check against questions.section. The dry run
runs the same validator without writing, then prints exactly what --commit
would send. After --commit, each row is read back and compared.

Both rows: year NULL, country NULL (D3), rule_source actual_paper (D4),
question_count NULL, the same as the 14 NECO rows written on 3 October 2026.

--- Shape (Sprint B design §3.1, decisions B5 and B6) ---

Sections A, B and C. Answer four in all, at least one from each section:
every section has required_count 1 (its floor) and in_pool true, and every
section carries pattern_type "pooled" and total_required_questions 4. That
leaves 4 - 3 = 1 extra place, filled by the best surplus answer from any
section. Each section's total_marks is its floor (1 x 15). The row's
total_marks is the paper maximum: 3 x 15 + 1 x 15 = 60.

--- Where every value comes from ---

NECO Christian Religious Studies, Paper II
    duration 90       "1 hour 30 minutes", Paper II cover, 2025 (S4012),
                      the latest paper available. 2019 (E4012) agrees.
    structure         "This paper consists of three sections: A, B and C.
                      You are to answer four questions, choosing at least one
                      question from each Section." (2025 and 2019 covers)
    15 marks          "Each question carries 15 marks." (2025 and 2019 covers)

NECO History, Paper II
    duration 120      "2 hours", Paper II cover, 2021 (S4052), the latest
                      cover available. The 2024 copy is a typed compilation
                      with no cover, so it is not used for timing.
    structure         "This paper consists of three sections: A, B and C.
                      Answer four questions in all, choosing at least one
                      question from each section." (2021 cover; 2024 agrees)
    15 marks          NOT printed. Both papers say only "All questions carry
                      equal marks." 15 is the mark carried by all nine NECO
                      2021 History Theory records in Neon (checked 6 October
                      2026), which is also what the grader scores each answer
                      out of, so the row and the grades agree. Revisit if a
                      NECO History marking scheme ever shows otherwise.

Section labels "Section A" / "Section B" / "Section C" match questions.section
verbatim for both subjects (checked 6 October 2026); the validator re-checks
this on every run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXAM = "NECO"
PAPER = "Theory"
RULE_SOURCE = "actual_paper"
INSTRUCTION = "Answer at least one question from this section."


def _pooled_sections(marks: float, total_required: int) -> list:
    return [
        {
            "section": label,
            "instruction": INSTRUCTION,
            "required_count": 1,
            "in_pool": True,
            "pattern_type": "pooled",
            "total_required_questions": total_required,
            "compulsory": False,
            "marks_per_question": marks,
            "total_marks": marks,
        }
        for label in ("Section A", "Section B", "Section C")
    ]


ROWS = [
    {
        "subject": "Christian Religious Studies",
        "duration_minutes": 90,
        "total_marks": 60,
        "rules": _pooled_sections(15, 4),
    },
    {
        "subject": "History",
        "duration_minutes": 120,
        "total_marks": 60,
        "rules": _pooled_sections(15, 4),
    },
]


def _retrying(fn, what: str, attempts: int = 3):
    """Retries dropped Neon connections. Never retries a validation refusal."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — filtered by type name below
            transient = type(exc).__name__ in ("OperationalError", "InterfaceError")
            if not transient or attempt == attempts:
                raise
            print(f"  {what}: connection dropped ({type(exc).__name__}), retrying in {attempt * 2}s")
            time.sleep(attempt * 2)


def _load_rows(row_get, db_conn):
    """Every paper_rules row, read with no parameters (works on SQLite and Postgres)."""
    def go():
        db = db_conn()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id, exam, subject, paper, year, country, rule_source, "
                "duration_minutes, total_marks, rules_json FROM paper_rules"
            )
            return cur.fetchall() or []
        finally:
            db.close()
    return _retrying(go, "read paper_rules")


def _find(rows, row_get, subject):
    for r in rows:
        if (
            row_get(r, "exam") == EXAM
            and row_get(r, "subject") == subject
            and row_get(r, "paper") == PAPER
            and row_get(r, "year") is None
            and row_get(r, "country") is None
            and row_get(r, "rule_source") == RULE_SOURCE
        ):
            return r
    return None


def _as_list(value):
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _same(existing, row, row_get, normalize_marks) -> bool:
    return (
        _as_list(row_get(existing, "rules_json")) == row["rules"]
        and row_get(existing, "duration_minutes") == row["duration_minutes"]
        and normalize_marks(row_get(existing, "total_marks")) == row["total_marks"]
    )


def _eligible_labels(db_conn, row_get, subject: str) -> dict:
    """Section label -> count of CBT-eligible theory records, selected the way CBT selects them."""
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT section, COUNT(*) AS n FROM questions "
            "WHERE exam = ? AND subject = ? AND paper = ? AND qtype = ? "
            "AND (cbt_eligible IS NULL OR cbt_eligible = 1) GROUP BY section",
            (EXAM, subject, PAPER, "theory"),
        )
        return {row_get(r, "section"): int(row_get(r, "n")) for r in cur.fetchall() or []}
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Write the NECO CRS and History pooled paper_rules rows")
    ap.add_argument("--commit", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--only", help='one subject, e.g. "History"')
    ap.add_argument("--replace", action="store_true", help="overwrite a row that already exists")
    ap.add_argument("--allow-local-db", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # Without DATABASE_URL, config.db_conn() quietly opens a local SQLite file:
    # the rows would be "written" and "read back" there while Neon is untouched.
    # Same guard as add_neco_theory_paper_rules.py.
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url.lower().startswith("postgres") and not args.allow_local_db:
        print("DATABASE_URL is not set to a Postgres URL. Set it to Neon and re-run. "
              "Nothing was written.")
        return 1

    from fastapi import HTTPException

    from config import db_conn
    import services.paper_rules_service as prs
    from services.paper_rules_service import upsert_paper_rule, validate_rules_json
    from services.question_utils import normalize_marks, row_get

    # An older paper_rules_service.py in this backend folder would refuse every
    # pooled row on the plain marks sum (45 in sections vs a 60 maximum).
    if not hasattr(prs, "_check_pooling"):
        print("services/paper_rules_service.py in this backend folder predates Sprint B "
              "(no pooled-row checks). Copy the current file from the services folder of "
              "the git backend (commit 3eab3e1) into this backend folder, then re-run. "
              "Nothing was written.")
        return 1

    targets = [r for r in ROWS if not args.only or r["subject"] == args.only]
    if not targets:
        print(f"No row for --only {args.only!r}. Choose from: {[r['subject'] for r in ROWS]}")
        return 2

    existing_rows = _load_rows(row_get, db_conn)
    failures = 0

    for row in targets:
        subject = row["subject"]
        payload = json.dumps(row["rules"], ensure_ascii=False)
        print(f"\n{EXAM}/{subject}/{PAPER}")
        print(f"  duration {row['duration_minutes']} min, total_marks {row['total_marks']}, rule_source {RULE_SOURCE}")
        print(f"  rules_json {payload}")

        existing = _find(existing_rows, row_get, subject)
        if existing is not None and _same(existing, row, row_get, normalize_marks):
            print("  already present and identical: nothing to do")
            continue
        if existing is not None and not args.replace:
            print("  SKIP: a different row already exists. Re-run with --replace to overwrite it.")
            print(f"  existing: duration {row_get(existing, 'duration_minutes')}, total "
                  f"{normalize_marks(row_get(existing, 'total_marks'))}, rules_json "
                  f"{json.dumps(_as_list(row_get(existing, 'rules_json')), ensure_ascii=False)}")
            continue

        # Labels, both directions, against the records CBT actually serves.
        # validate_rules_json() catches a declared label with no record at all;
        # this also catches an eligible record whose label the row does not
        # declare, which CBT would drop silently.
        try:
            in_neon = _retrying(lambda: _eligible_labels(db_conn, row_get, subject), "read questions")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: could not read questions: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            failures += 1
            continue
        declared = [s["section"] for s in row["rules"]]
        label_problems = [
            f"{n} CBT-eligible record(s) carry section {k!r}, which this row does not declare"
            for k, n in sorted(in_neon.items(), key=lambda kv: str(kv[0])) if k not in declared
        ] + [
            f"section {lbl!r} has no CBT-eligible record, so it would be served empty"
            for lbl in declared if lbl not in in_neon
        ]
        if label_problems:
            print("  FAIL: " + "; ".join(label_problems))
            failures += 1
            continue
        print("  labels: " + ", ".join(f"{k} ({in_neon[k]})" for k in declared))

        try:
            _retrying(
                lambda: validate_rules_json(
                    rules_json=payload, exam=EXAM, subject=subject, paper=PAPER,
                    total_marks=row["total_marks"],
                ),
                "validate",
            )
        except HTTPException as exc:
            print(f"  REFUSED by validator: {exc.detail}")
            failures += 1
            continue
        print("  validator: ok")

        if not args.commit:
            print(f"  DRY RUN: would {'UPDATE' if existing is not None else 'INSERT'}. Re-run with --commit to write.")
            continue

        try:
            result = _retrying(
                lambda: upsert_paper_rule(
                    exam=EXAM, subject=subject, paper=PAPER, rule_source=RULE_SOURCE,
                    year=None, country=None,
                    duration_minutes=row["duration_minutes"], question_count=None,
                    total_marks=row["total_marks"], rules_json=payload,
                ),
                "write",
            )
        except HTTPException as exc:
            print(f"  REFUSED on write: {exc.detail}")
            failures += 1
            continue
        print(f"  written, id {result['id']}")

        # Read back rather than trusting the write.
        back = _find(_load_rows(row_get, db_conn), row_get, subject)
        ok = back is not None and _same(back, row, row_get, normalize_marks)
        print(f"  read-back {'matches' if ok else 'DOES NOT MATCH'}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"{failures} row(s) failed.")
        return 1
    print("Done." if args.commit else "Dry run complete. Nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
