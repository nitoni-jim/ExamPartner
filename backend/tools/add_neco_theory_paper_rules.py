#!/usr/bin/env python3
"""
tools/add_neco_theory_paper_rules.py — author the NECO Theory paper_rules rows.

    python backend\\tools\\add_neco_theory_paper_rules.py                  dry run, writes nothing
    python backend\\tools\\add_neco_theory_paper_rules.py --only Biology   dry run, one subject
    python backend\\tools\\add_neco_theory_paper_rules.py --commit         write, then read back

Run from the neco_tutor folder with DATABASE_URL pointing at Neon, the same
way check_cbt_structure.py is run.

--- What this writes ---

One paper_rules row per NECO Theory paper below: 14 rows, Paper II of each
subject. Every row is

    year = NULL, country = NULL     CBT sectioning reads only year-NULL rows
                                    and has no country filter, so a theory
                                    row covers every year and every country.
    rule_source = "actual_paper"    sections, counts, marks and duration are
                                    all read from genuine NECO booklets.
    question_count = NULL           not read by Theory CBT; the WAEC Biology
                                    row leaves it empty too.

It writes ONLY to paper_rules, and only through upsert_paper_rule(), which
runs the service's own validate_rules_json(). It never touches the questions
table and never reads or writes cbt_eligible: that field is authored in the
JSONL and is not this script's business.

--- Not included, on purpose ---

  NECO Christian Religious Studies, NECO History   Pattern B (a floor in each
                                                   section plus a shared extra).
                                                   Needs Sprint B.
  NECO Physics 2014                                every record is authored
                                                   cbt_eligible false, so CBT
                                                   serves nothing from it.

--- Checks before anything is written ---

All of these run in the dry run too. With --commit, every selected row must
pass every check before the first row is written; if one fails, nothing is.

  1. The service is current: upsert_paper_rule() takes a country argument.
     An older copy in neco_tutor\\backend would use a country-blind row
     lookup and the old whole-number marks check.
  2. Arithmetic: each section's total_marks = required_count x
     marks_per_question, marks are multiples of 0.5, and the sections add up
     to the row's total_marks.
  3. Labels against Neon: every section label held by a CBT-eligible theory
     record is declared in the row (an undeclared label would be dropped from
     CBT silently), and every declared label has at least one CBT-eligible
     record (otherwise that section would be served empty).
  4. No existing row is overwritten. If a year-NULL, country-NULL row already
     exists for the paper and differs from this one, the script stops and
     shows both. Re-run with --replace only if replacing it is intended.
  5. validate_rules_json(), the same gate upsert_paper_rule() applies.

After --commit, every row is read back from the database and compared field
by field with what was meant to be written.

--- Where each value comes from ---

Section labels: verbatim from Neon (inventory query, 3 Oct 2026).
Counts: the printed Paper II instruction, as recorded in the audit brief.
Marks per question: the printed paper, matching every record in Neon.
compulsory: true only where every question in the section must be answered
(decision D6, 3 Oct 2026). It does not change scoring; the app scores the
top required_count graded answers in each section either way.
duration_minutes: the Paper II time printed on the paper (decision D4).
Geography and Mathematics use the 2025 covers, the latest papers supplied
for the purpose; the 2018 Mathematics copy has no Paper II cover.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXAM = "NECO"
PAPER = "Theory"
RULE_SOURCE = "actual_paper"


def section(label: str, instruction: str, required: int, marks_each: float,
            compulsory: bool = False) -> Dict[str, Any]:
    total = required * marks_each
    return {
        "section": label,
        "instruction": instruction,
        "required_count": required,
        "compulsory": compulsory,
        "marks_per_question": int(marks_each) if float(marks_each).is_integer() else marks_each,
        "total_marks": int(total) if float(total).is_integer() else total,
    }


# subject (exact Neon value) -> (duration_minutes, source of the duration, sections)
ROWS: Dict[str, Dict[str, Any]] = {
    "Agricultural Science": {
        "duration": 90, "duration_source": "2018 cover: Paper II 1 hour 30 minutes",
        "sections": [
            section("Section A", "Answer one question from this section.", 1, 16),
            section("Section B", "Answer one question from this section.", 1, 16),
            section("Section C", "Answer one question from this section.", 1, 16),
            section("Section D", "Answer one question from this section.", 1, 16),
            section("Section E", "Answer one question from this section.", 1, 16),
        ],
    },
    "Biology": {
        "duration": 90, "duration_source": "2015 cover: Paper II 1 hour, 30 minutes",
        "sections": [
            section("Essay", "Answer three questions only.", 3, 20),
        ],
    },
    "Chemistry": {
        "duration": 90, "duration_source": "2015 cover: Paper II 1 hour, 30 minutes",
        "sections": [
            section("Essay", "Answer four questions only.", 4, 25),
        ],
    },
    "Civic Education": {
        "duration": 100, "duration_source": "2015 cover: Paper II 1 hour, 40 minutes",
        "sections": [
            section("Section A", "Answer two questions from this section.", 2, 15),
            section("Section B", "Answer two questions from this section.", 2, 15),
        ],
    },
    "Commerce": {
        "duration": 100, "duration_source": "2019 and 2025 covers: Paper II 1 hour 40 minutes",
        "sections": [
            section("Theory", "Answer five questions only.", 5, 20),
        ],
    },
    "Computer Studies": {
        # The paper prints six questions; Q6 is held back from Neon because
        # the source copy is incomplete. "Answer four" still works with five.
        "duration": 100, "duration_source": "2020 Paper II cover: 1 hour 40 minutes",
        "sections": [
            section("Theory", "Answer four questions only.", 4, 10),
        ],
    },
    "Economics": {
        "duration": 120, "duration_source": "2018 cover: Paper II 2 hours",
        "sections": [
            section("Section A", "Answer one question from this section.", 1, 20),
            section("Section B", "Answer four questions from this section.", 4, 20),
        ],
    },
    "English Language": {
        "duration": 105, "duration_source": "2018 Paper II cover: 1 hour 45 minutes",
        "sections": [
            section("Section A", "Answer one question only from this section.", 1, 50),
            section("Section B", "Answer all the questions in this section.", 1, 20, compulsory=True),
            section("Section C", "Answer all the questions in this section.", 1, 30, compulsory=True),
        ],
    },
    "Financial Accounting": {
        "duration": 150, "duration_source": "2021 cover: Paper II 2 hours 30 minutes",
        "sections": [
            section("Section A", "Answer two questions from this section.", 2, 12.5),
            section("Section B", "Answer three questions from this section.", 3, 15),
        ],
    },
    "Geography": {
        "duration": 120, "duration_source": "2025 Paper II cover: 2 hours",
        "sections": [
            section("Section A", "Answer two questions from this section.", 2, 20),
            section("Section B", "Answer two questions from this section.", 2, 20),
        ],
    },
    "Government": {
        "duration": 100, "duration_source": "2007 cover: Paper II 1 hour 40 minutes",
        "sections": [
            section("Section A", "Answer two questions from this section.", 2, 20),
            section("Section B", "Answer three questions from this section.", 3, 20),
        ],
    },
    "Islamic Religious Studies": {
        "duration": 90, "duration_source": "2020 cover: Paper II 1 hour 30 minutes",
        "sections": [
            section("Part I", "Answer one question from this part.", 1, 15),
            section("Part II", "Answer one question from this part.", 1, 15),
            section("Part III", "Answer one question from this part.", 1, 15),
            section("Part IV", "Answer one question from this part.", 1, 15),
        ],
    },
    "Literature in English": {
        # Paper IV (prose), the paper Neon holds under paper "Theory".
        "duration": 75, "duration_source": "2015 cover: Paper IV 1 hour, 15 minutes",
        "sections": [
            section("I - African Prose", "Answer one question from this section.", 1, 30),
            section("II - Non-African Prose", "Answer one question from this section.", 1, 30),
        ],
    },
    "Mathematics": {
        "duration": 150, "duration_source": "2025 Paper II cover: 2 hours 30 minutes",
        "sections": [
            section("Part I", "Attempt all questions in this part.", 5, 8, compulsory=True),
            section("Part II", "Answer five questions only in this part.", 5, 12),
        ],
    },
}


def with_retry(fn, *args, attempts: int = 3, wait_seconds: float = 3.0, **kwargs):
    """
    Runs one database step, retrying when the connection itself fails.

    Neon's pooler occasionally drops a connection ("SSL SYSCALL error: EOF",
    "server closed the connection unexpectedly"), and a home connection can
    blink out for a moment. Those are worth a retry. A refusal from
    validate_rules_json is an HTTPException, not a connection error, so it is
    raised at once and never retried.
    """
    import time
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if name not in ("OperationalError", "InterfaceError") or attempt == attempts:
                raise
            last = exc
            print(f"  (connection problem, retrying in {wait_seconds:g}s: {str(exc).splitlines()[0]})")
            time.sleep(wait_seconds)
    raise last  # pragma: no cover


def row_total(sections: List[Dict[str, Any]]) -> float:
    return sum(float(s["total_marks"]) for s in sections)


def as_number(x: float):
    return int(x) if float(x).is_integer() else x


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_service(upsert_paper_rule) -> Optional[str]:
    if "country" not in inspect.signature(upsert_paper_rule).parameters:
        return ("services/paper_rules_service.py in this backend folder is older than the "
                "deployed one (upsert_paper_rule has no country argument). Copy the current "
                "file from the services folder of the git backend into this backend folder, "
                "then re-run. Nothing was written.")
    return None


def check_arithmetic(subject: str, spec: Dict[str, Any]) -> List[str]:
    problems = []
    for s in spec["sections"]:
        for key in ("marks_per_question", "total_marks"):
            if (float(s[key]) * 2) % 1:
                problems.append(f"{s['section']!r}: {key} {s[key]} is not a multiple of 0.5")
        if abs(float(s["total_marks"]) - s["required_count"] * float(s["marks_per_question"])) > 1e-9:
            problems.append(f"{s['section']!r}: total_marks {s['total_marks']} is not "
                            f"{s['required_count']} x {s['marks_per_question']}")
        if s["required_count"] < 1:
            problems.append(f"{s['section']!r}: required_count must be at least 1")
    if not isinstance(spec["duration"], int) or spec["duration"] <= 0:
        problems.append(f"duration_minutes {spec['duration']!r} must be a positive whole number")
    return problems


def eligible_labels(db_conn, row_get, subject: str) -> Dict[Any, int]:
    """Section label -> number of CBT-eligible theory records, as CBT selects them."""
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT section, COUNT(*) AS n FROM questions "
            "WHERE exam = ? AND subject = ? AND paper = ? AND qtype = ? "
            "AND (cbt_eligible IS NULL OR cbt_eligible = 1) "
            "GROUP BY section",
            (EXAM, subject, PAPER, "theory"),
        )
        return {row_get(r, "section"): int(row_get(r, "n")) for r in cur.fetchall() or []}
    finally:
        db.close()


def existing_rows(db_conn, row_get, subject: str) -> List[Dict[str, Any]]:
    """Every year-NULL, country-NULL row for this paper, whatever its rule_source."""
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT rule_source, duration_minutes, question_count, total_marks, rules_json "
            "FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? "
            "AND year IS NULL AND country IS NULL",
            (EXAM, subject, PAPER),
        )
        out = []
        for r in cur.fetchall() or []:
            raw = row_get(r, "rules_json")
            try:
                rules = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                rules = raw
            tm = row_get(r, "total_marks")
            out.append({
                "rule_source": row_get(r, "rule_source"),
                "duration_minutes": row_get(r, "duration_minutes"),
                "question_count": row_get(r, "question_count"),
                "total_marks": None if tm is None else float(tm),
                "rules": rules,
            })
        return out
    finally:
        db.close()


def planned(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rule_source": RULE_SOURCE,
        "duration_minutes": spec["duration"],
        "question_count": None,
        "total_marks": row_total(spec["sections"]),
        "rules": spec["sections"],
    }


def same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (a["rule_source"] == b["rule_source"]
            and a["duration_minutes"] == b["duration_minutes"]
            and a["question_count"] == b["question_count"]
            and a["total_marks"] is not None and b["total_marks"] is not None
            and abs(a["total_marks"] - b["total_marks"]) < 1e-9
            and a["rules"] == b["rules"])


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Author the NECO Theory paper_rules rows")
    ap.add_argument("--commit", action="store_true", help="write the rows (default: dry run)")
    ap.add_argument("--only", nargs="+", metavar="SUBJECT",
                    help="limit to these subjects (case-insensitive, part of the name is enough)")
    ap.add_argument("--replace", action="store_true",
                    help="allow replacing an existing, different actual_paper row")
    ap.add_argument("--allow-local-db", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url.lower().startswith("postgres") and not args.allow_local_db:
        print("DATABASE_URL is not set to a Postgres URL. Set it to Neon and re-run. "
              "Nothing was written.")
        return 1

    from config import db_conn
    from services.question_utils import row_get
    from services.paper_rules_service import upsert_paper_rule, validate_rules_json

    problem = check_service(upsert_paper_rule)
    if problem:
        print(problem)
        return 1

    subjects = list(ROWS)
    if args.only:
        words = [w.lower() for w in args.only]
        unmatched = [w for w in words if not any(w in s.lower() for s in subjects)]
        if unmatched:
            print("--only matched no subject for: " + ", ".join(unmatched))
            print("Subjects: " + ", ".join(subjects))
            return 1
        subjects = [s for s in subjects if any(w in s.lower() for w in words)]

    mode = "COMMIT" if args.commit else "DRY RUN — nothing will be written"
    print(f"{mode}\n{len(subjects)} row(s): {EXAM} / <subject> / {PAPER}, year NULL, "
          f"country NULL, rule_source {RULE_SOURCE}\n")

    # ---- Phase 1: check every row; write nothing ---------------------------
    failed, to_write, unchanged = [], [], []
    for subject in subjects:
        spec = ROWS[subject]
        plan = planned(spec)
        labels = [s["section"] for s in spec["sections"]]
        print(f"{subject}")
        print(f"  duration {spec['duration']} min   ({spec['duration_source']})")
        for s in spec["sections"]:
            flag = "  compulsory" if s["compulsory"] else ""
            print(f"  {s['section']!r:26} answer {s['required_count']} × "
                  f"{s['marks_per_question']} = {s['total_marks']}{flag}")
        print(f"  total_marks {as_number(plan['total_marks'])}")

        problems = check_arithmetic(subject, spec)

        # A connection failure is reported once, as itself. The label check
        # is skipped when the questions could not be read, so a dropped
        # connection never shows up as a false "served empty" finding.
        try:
            in_neon = with_retry(eligible_labels, db_conn, row_get, subject)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not read questions: {type(exc).__name__}: "
                            f"{str(exc).splitlines()[0]}")
            in_neon = None
        if in_neon is not None:
            undeclared = sorted((k for k in in_neon if k not in labels), key=str)
            for k in undeclared:
                problems.append(f"{in_neon[k]} CBT-eligible record(s) carry section {k!r}, which "
                                f"this row does not declare — CBT would drop them silently")
            for lbl in labels:
                if lbl not in in_neon:
                    problems.append(f"section {lbl!r} has no CBT-eligible record in Neon — it "
                                    f"would be served empty")

            try:
                with_retry(validate_rules_json, json.dumps(spec["sections"], ensure_ascii=False),
                           EXAM, subject, PAPER, plan["total_marks"])
            except Exception as exc:  # noqa: BLE001
                if hasattr(exc, "detail"):
                    problems.append(f"validate_rules_json refused it: {exc.detail}")
                else:
                    problems.append(f"could not run validate_rules_json: {type(exc).__name__}: "
                                    f"{str(exc).splitlines()[0]}")

        try:
            existing = with_retry(existing_rows, db_conn, row_get, subject)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not read paper_rules: {type(exc).__name__}: "
                            f"{str(exc).splitlines()[0]}")
            existing = []

        status = "new row"
        if any(same(e, plan) for e in existing):
            status = "already present and identical — nothing to do"
            unchanged.append(subject)
        else:
            for e in existing:
                note = (f"existing {e['rule_source']} row: duration {e['duration_minutes']}, "
                        f"total {e['total_marks']}, rules {json.dumps(e['rules'], ensure_ascii=False)}")
                if e["rule_source"] == RULE_SOURCE and not args.replace:
                    problems.append(note + " — differs; re-run with --replace to overwrite it")
                else:
                    print(f"  note: {note}")
                    if e["rule_source"] == RULE_SOURCE:
                        status = "replaces the existing actual_paper row (--replace)"
                    else:
                        status = (f"new row; it outranks the existing {e['rule_source']} row, "
                                  f"which stays in the table")

        if problems:
            failed.append(subject)
            print("  FAIL")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"  ok — {status}")
            if subject not in unchanged:
                to_write.append(subject)
        print()

    if failed:
        print(f"{len(failed)} row(s) failed a check: {', '.join(failed)}")
        print("Nothing was written.")
        return 1

    if not args.commit:
        print(f"All {len(subjects)} row(s) pass. {len(to_write)} would be written, "
              f"{len(unchanged)} already present.")
        print("Re-run with --commit to write them.")
        return 0

    # ---- Phase 2: write ------------------------------------------------------
    written = []
    for subject in to_write:
        spec = ROWS[subject]
        try:
            # Safe to retry: upsert_paper_rule() finds an existing row and
            # updates it rather than inserting a second one.
            with_retry(
                upsert_paper_rule,
                exam=EXAM,
                subject=subject,
                paper=PAPER,
                rule_source=RULE_SOURCE,
                year=None,
                duration_minutes=spec["duration"],
                question_count=None,
                total_marks=as_number(row_total(spec["sections"])),
                rules_json=json.dumps(spec["sections"], ensure_ascii=False),
                country=None,
            )
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or str(exc).splitlines()[0]
            print(f"WRITE FAILED for {subject}: {detail}")
            print("Re-running with --commit is safe: rows already written are skipped.")
            print(f"Written before the failure: {', '.join(written) or 'none'}")
            print(f"Not attempted: {', '.join(to_write[len(written) + 1:]) or 'none'}")
            return 1
        written.append(subject)
        print(f"written  {subject}")

    # ---- Phase 3: read back --------------------------------------------------
    print("\nRead-back:")
    bad = []
    for subject in subjects:
        plan = planned(ROWS[subject])
        try:
            found = with_retry(existing_rows, db_conn, row_get, subject)
        except Exception as exc:  # noqa: BLE001
            bad.append(subject)
            print(f"  COULD NOT READ BACK {subject}: {type(exc).__name__}: "
                  f"{str(exc).splitlines()[0]} — re-run with --commit to check it")
            continue
        rows = [e for e in found if e["rule_source"] == RULE_SOURCE]
        if len(rows) == 1 and same(rows[0], plan):
            print(f"  matches  {subject}")
        else:
            bad.append(subject)
            print(f"  MISMATCH {subject}: found {len(rows)} {RULE_SOURCE} row(s): "
                  f"{json.dumps(rows, ensure_ascii=False, default=str)}")

    if bad:
        print(f"\n{len(bad)} row(s) did not read back as written: {', '.join(bad)}")
        return 1
    print(f"\nDone. {len(written)} written, {len(unchanged)} already present, "
          f"all {len(subjects)} read back correctly.")
    print("The app picks these up at its next paper-rules sync (app launch).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
