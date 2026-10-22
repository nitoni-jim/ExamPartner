#!/usr/bin/env python3
"""
tools/add_theory_presented_counts.py — author presented_count into the live
Theory paper_rules rows.

    python tools/add_theory_presented_counts.py                      dry run, writes nothing
    python tools/add_theory_presented_counts.py --only Biology       dry run, one subject
    python tools/add_theory_presented_counts.py --commit             write, then read back

Run from the backend folder with DATABASE_URL pointing at Neon.

--- What this writes ---

presented_count is how many questions the printed paper presents in a
section ("answer two of these four" presents 4). Once every section of a row
declares it, Theory CBT serves that many questions per section, sampled from
the section's pool, instead of every gradeable question it holds.

For each entry in PRESENTED_COUNTS below, the script reads the live
year-NULL, country-NULL row for that paper — the one Theory CBT uses, best
rule_source first — and adds presented_count to each section object. No
other key or value in rules_json changes, and the row's other columns
(rule_source, duration_minutes, question_count, total_marks) are written
back exactly as read. It writes through upsert_paper_rule(), so
validate_rules_json() — Rule 6 included — runs on every write.

It writes ONLY to paper_rules. It never touches the questions table and
never reads or writes cbt_eligible.

--- Evidence pointers are required ---

PRESENTED_COUNTS ships EMPTY on purpose. Every value comes from Nitoni's
Phase 3 evidence table — the printed paper — never from the number of
records in Neon, never from required_count, and never from a guess. A wrong
count does not fail loudly: the session simply offers the wrong amount of
choice, on every device, until someone notices.

So each value is a pair, (presented_count, "evidence pointer"), and the
pointer must say where the number was read, specifically enough that someone
else could open the same page and check it: e.g.
"2021 Paper II booklet p.3: 'Answer two questions from this section', Q1-Q4".
An entry with an empty pointer is refused.

--- Checks before anything is written ---

All of these run in the dry run too. With --commit, every selected entry must
pass every check before the first row is written; if one fails, nothing is.

  1. The live row exists (year NULL, country NULL) and its rules_json is a
     list of section objects.
  2. The row's section labels are exactly the entry's labels — no section
     missing from the entry, none extra.
  3. Every evidence pointer is a non-empty string.
  4. validate_rules_json() accepts the merged rules_json.

The before/after rules_json is printed as a diff. After --commit every row
is read back and compared field by field with what was meant to be written.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (exam, subject, paper) -> {section label: (presented_count, "evidence pointer")}
#
# Labels exactly as in the live rules_json (and so in questions.section).
# Example shape only — NOT data:
#   ("NECO", "Financial Accounting", "Theory"): {
#       "Section A": (4, "2021 Paper II p.2: 'Answer two questions', Q1-Q4 printed"),
#       "Section B": (5, "2021 Paper II p.5: 'Answer three questions', Q5-Q9 printed"),
#   },
PRESENTED_COUNTS: Dict[Tuple[str, str, str], Dict[str, Tuple[int, str]]] = {
}

# Same order as the ORDER BY in cbt_service._get_theory_section_rules().
_RULE_SOURCE_RANK = {"actual_paper": 0, "syllabus_default": 1, "legacy_placeholder": 2}


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


def as_number(x: Any):
    if x is None:
        return None
    f = float(x)
    return int(f) if f.is_integer() else f


def live_rows(db_conn, row_get, key: Tuple[str, str, str]) -> List[Dict[str, Any]]:
    """Every year-NULL, country-NULL row for this paper, best rule_source first."""
    exam, subject, paper = key
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT rule_source, duration_minutes, question_count, total_marks, rules_json "
            "FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? "
            "AND year IS NULL AND country IS NULL",
            (exam, subject, paper),
        )
        out = []
        for r in cur.fetchall() or []:
            raw = row_get(r, "rules_json")
            try:
                rules = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                rules = None
            out.append({
                "rule_source": row_get(r, "rule_source"),
                "duration_minutes": row_get(r, "duration_minutes"),
                "question_count": row_get(r, "question_count"),
                "total_marks": as_number(row_get(r, "total_marks")),
                "rules": rules,
            })
    finally:
        db.close()
    out.sort(key=lambda e: _RULE_SOURCE_RANK.get(e["rule_source"], 3))
    return out


def merge(rules: List[Dict[str, Any]], counts: Dict[str, Tuple[int, str]]) -> List[Dict[str, Any]]:
    """Each section object with presented_count set; nothing else touched."""
    merged = []
    for s in rules:
        new = dict(s)
        new["presented_count"] = counts[s["section"]][0]
        merged.append(new)
    return merged


def show_diff(before: Any, after: Any) -> None:
    a = json.dumps(before, indent=2, ensure_ascii=False).splitlines()
    b = json.dumps(after, indent=2, ensure_ascii=False).splitlines()
    for line in difflib.unified_diff(a, b, "rules_json (live)", "rules_json (after)", lineterm="", n=1):
        print(f"    {line}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Author presented_count into Theory paper_rules rows")
    ap.add_argument("--commit", action="store_true", help="write the rows (default: dry run)")
    ap.add_argument("--only", nargs="+", metavar="SUBJECT",
                    help="limit to these subjects (case-insensitive, part of the name is enough)")
    ap.add_argument("--allow-local-db", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url.lower().startswith("postgres") and not args.allow_local_db:
        print("DATABASE_URL is not set to a Postgres URL. Set it to Neon and re-run. "
              "Nothing was written.")
        return 1

    if not PRESENTED_COUNTS:
        print("PRESENTED_COUNTS is empty. Fill it from the Phase 3 evidence table, one "
              "(presented_count, evidence pointer) per section, then re-run. Nothing was written.")
        return 0

    from config import db_conn
    from services.question_utils import row_get
    from services.paper_rules_service import upsert_paper_rule, validate_rules_json

    keys = list(PRESENTED_COUNTS)
    if args.only:
        words = [w.lower() for w in args.only]
        unmatched = [w for w in words if not any(w in k[1].lower() for k in keys)]
        if unmatched:
            print("--only matched no subject for: " + ", ".join(unmatched))
            print("Subjects: " + ", ".join(sorted({k[1] for k in keys})))
            return 1
        keys = [k for k in keys if any(w in k[1].lower() for w in words)]

    mode = "COMMIT" if args.commit else "DRY RUN — nothing will be written"
    print(f"{mode}\n{len(keys)} paper(s)\n")

    # ---- Phase 1: check every entry; write nothing --------------------------
    failed, to_write, unchanged = [], [], []
    planned: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for key in keys:
        exam, subject, paper = key
        counts = PRESENTED_COUNTS[key]
        print(f"{exam} / {subject} / {paper}")
        for label, (n, evidence) in counts.items():
            print(f"  {label!r:26} presented {n!r:4} — {evidence}")

        problems: List[str] = []
        for label, (_, evidence) in counts.items():
            if not isinstance(evidence, str) or not evidence.strip():
                problems.append(f"{label!r} has no evidence pointer")

        try:
            rows = with_retry(live_rows, db_conn, row_get, key)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not read paper_rules: {type(exc).__name__}: "
                            f"{str(exc).splitlines()[0]}")
            rows = []

        live = rows[0] if rows else None
        if live is None:
            problems.append("no year-NULL, country-NULL paper_rules row exists for this paper")
        elif not isinstance(live["rules"], list) or not live["rules"] or not all(
                isinstance(s, dict) and s.get("section") for s in live["rules"]):
            problems.append(f"the live {live['rule_source']} row has no usable rules_json")
        else:
            live_labels = [s["section"] for s in live["rules"]]
            if set(live_labels) != set(counts) or len(live_labels) != len(set(live_labels)):
                problems.append(f"section labels differ — live row {live_labels}, "
                                f"entry {list(counts)}")
            for other in rows[1:]:
                print(f"  note: a {other['rule_source']} row also exists; Theory CBT reads the "
                      f"{live['rule_source']} row, so only that one is changed")

        if not problems:
            merged = merge(live["rules"], counts)
            print(f"  live row: {live['rule_source']}")
            show_diff(live["rules"], merged)
            try:
                with_retry(validate_rules_json, json.dumps(merged, ensure_ascii=False),
                           exam, subject, paper, live["total_marks"])
            except Exception as exc:  # noqa: BLE001
                if hasattr(exc, "detail"):
                    problems.append(f"validate_rules_json refused it: {exc.detail}")
                else:
                    problems.append(f"could not run validate_rules_json: {type(exc).__name__}: "
                                    f"{str(exc).splitlines()[0]}")
            planned[key] = {**live, "rules": merged}

        if problems:
            failed.append(key)
            print("  FAIL")
            for p in problems:
                print(f"    - {p}")
        elif planned[key]["rules"] == live["rules"]:
            unchanged.append(key)
            print("  ok — already present and identical, nothing to do")
        else:
            to_write.append(key)
            print("  ok")
        print()

    if failed:
        print(f"{len(failed)} paper(s) failed a check: "
              + ", ".join(f"{k[0]}/{k[1]}/{k[2]}" for k in failed))
        print("Nothing was written.")
        return 1

    if not args.commit:
        print(f"All {len(keys)} paper(s) pass. {len(to_write)} would be written, "
              f"{len(unchanged)} already present.")
        print("Re-run with --commit to write them.")
        return 0

    # ---- Phase 2: write ------------------------------------------------------
    written = []
    for key in to_write:
        exam, subject, paper = key
        plan = planned[key]
        try:
            # Safe to retry: upsert_paper_rule() finds the existing row by
            # (exam, subject, paper, year NULL, rule_source, country NULL) and
            # updates it. Every column is passed as read, so its
            # partial-update guard has nothing to refuse.
            with_retry(
                upsert_paper_rule,
                exam=exam,
                subject=subject,
                paper=paper,
                rule_source=plan["rule_source"],
                year=None,
                duration_minutes=plan["duration_minutes"],
                question_count=plan["question_count"],
                total_marks=plan["total_marks"],
                rules_json=json.dumps(plan["rules"], ensure_ascii=False),
                country=None,
            )
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or str(exc).splitlines()[0]
            print(f"WRITE FAILED for {exam}/{subject}/{paper}: {detail}")
            print("Re-running with --commit is safe: rows already written are skipped.")
            print("Written before the failure: "
                  + (", ".join(f"{k[1]}" for k in written) or "none"))
            return 1
        written.append(key)
        print(f"written  {exam}/{subject}/{paper}")

    # ---- Phase 3: read back --------------------------------------------------
    print("\nRead-back:")
    bad = []
    for key in keys:
        plan = planned[key]
        try:
            rows = with_retry(live_rows, db_conn, row_get, key)
        except Exception as exc:  # noqa: BLE001
            bad.append(key)
            print(f"  COULD NOT READ BACK {key[1]}: {type(exc).__name__}: "
                  f"{str(exc).splitlines()[0]} — re-run with --commit to check it")
            continue
        found = [r for r in rows if r["rule_source"] == plan["rule_source"]]
        diffs = []
        if len(found) != 1:
            diffs.append(f"found {len(found)} {plan['rule_source']} row(s)")
        else:
            got = found[0]
            for field in ("rule_source", "duration_minutes", "question_count", "total_marks", "rules"):
                if got[field] != plan[field]:
                    diffs.append(f"{field}: wrote {plan[field]!r}, read {got[field]!r}")
        if diffs:
            bad.append(key)
            print(f"  MISMATCH {key[0]}/{key[1]}/{key[2]}: " + "; ".join(diffs))
        else:
            print(f"  matches  {key[0]}/{key[1]}/{key[2]}")

    if bad:
        print(f"\n{len(bad)} row(s) did not read back as written.")
        return 1
    print(f"\nDone. {len(written)} written, {len(unchanged)} already present, "
          f"all {len(keys)} read back correctly.")
    print("Theory CBT reads paper_rules live, so the next session uses these counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
