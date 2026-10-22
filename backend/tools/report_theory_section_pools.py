#!/usr/bin/env python3
"""
tools/report_theory_section_pools.py — what Theory CBT can serve, per section.

    python tools/report_theory_section_pools.py                       every Theory paper
    python tools/report_theory_section_pools.py --exam NECO
    python tools/report_theory_section_pools.py --only Biology "Financial Acc"
    python tools/report_theory_section_pools.py --launch-check        exit 1 if not ready

Run from the backend folder with DATABASE_URL pointing at Neon.

READ-ONLY. It never writes to any table, and it reads cbt_eligible only to
count. It is the evidence for authoring presented_count
(tools/add_theory_presented_counts.py) and the gate before turning it on.

--- What it reports ---

For each exam / subject / paper holding theory records:

  - whether a year-NULL, country-NULL paper_rules row exists (the only rows
    Theory CBT reads), its rule_source, and its mode: "presented" when every
    section declares presented_count, "legacy_uncapped" when none does;
  - per section label — the rule's labels and the records' labels both, so an
    orphaned label (records, no rule) and an empty rule (rule, no records)
    both show:
      records         theory records carrying the label
      eligible        of those, CBT-eligible (cbt_eligible NULL or 1)
      gradeable       of those, gradeable by the AI grader
      D10 mismatch    of those, marks differ from the rule's marks_per_question
                      (excluded from the pool; ids and marks listed)
      available       eligible, gradeable and not mismatched — the pool —
                      split by year, so the free-year impact of decisions D5
                      and D2 can be read off directly
      passages        records in the pool sharing a passage_id
      presented / required
      status          what the backend gives this section for a paid user
                      (all years) and for a free user (the free year only)

The status and the counts behind it come from cbt_service's own
_build_theory_section_plan(), the function the session and /cbt/papers both
use, so the report cannot drift from what the backend actually does.

--- --launch-check ---

Exit 1, listing them, if any Theory paper with CBT-eligible records has no
usable rules row, or has one without presented_count. Literature in English
is listed separately as exempt (decision D7: no presented_count yet) and does
not fail the check.

Exit 0 otherwise. Without --launch-check the exit code is always 0 unless the
database cannot be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Decision D7. Matched on subject alone, whatever the exam.
EXEMPT_SUBJECTS = {"Literature in English"}

# Same order as the ORDER BY in cbt_service._get_theory_section_rules().
_RULE_SOURCE_RANK = {"actual_paper": 0, "syllabus_default": 1, "legacy_placeholder": 2}


def with_retry(fn, *args, attempts: int = 3, wait_seconds: float = 3.0, **kwargs):
    """
    Runs one database step, retrying when the connection itself fails.

    Neon's pooler occasionally drops a connection ("SSL SYSCALL error: EOF",
    "server closed the connection unexpectedly"), and a home connection can
    blink out for a moment. Those are worth a retry; anything else is raised
    at once.
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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def theory_records(db_conn, row_get) -> List[Dict[str, Any]]:
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, exam, subject, paper, year, section, marks, passage_id, cbt_eligible, "
            "sub_questions_json, examiner_points_json, metadata_json "
            "FROM questions WHERE qtype = ? ORDER BY exam, subject, paper, id",
            ("theory",),
        )
        cols = ("id", "exam", "subject", "paper", "year", "section", "marks", "passage_id",
                "cbt_eligible", "sub_questions_json", "examiner_points_json", "metadata_json")
        return [{c: row_get(r, c) for c in cols} for r in cur.fetchall() or []]
    finally:
        db.close()


def rules_rows(db_conn, row_get) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Best year-NULL, country-NULL row per paper, as Theory CBT chooses it."""
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT exam, subject, paper, rule_source, rules_json FROM paper_rules "
            "WHERE year IS NULL AND country IS NULL"
        )
        found = cur.fetchall() or []
    finally:
        db.close()

    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in found:
        key = (row_get(r, "exam"), row_get(r, "subject"), row_get(r, "paper"))
        rank = _RULE_SOURCE_RANK.get(row_get(r, "rule_source"), 3)
        if key in best and best[key]["rank"] <= rank:
            continue
        raw = row_get(r, "rules_json")
        try:
            rules = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            rules = None
        best[key] = {
            "rank": rank,
            "rule_source": row_get(r, "rule_source"),
            "rules": rules if isinstance(rules, list) and rules else None,
            "raw_present": raw is not None and str(raw).strip() != "",
        }
    return best


def free_year(db_conn, exam: str, subject: str) -> Optional[int]:
    from services.access_control import get_free_year_for_subject
    db = db_conn()
    try:
        return get_free_year_for_subject(db, exam, subject)
    finally:
        db.close()


# ---------------------------------------------------------------------------

def _eligible(value: Any) -> bool:
    from services.cbt_service import _is_cbt_eligible
    return _is_cbt_eligible(value)


def _num(x: Any) -> str:
    if x is None:
        return "—"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return repr(x)
    return f"{f:g}"


def report_paper(key, records, rule_row, db_conn) -> Dict[str, Any]:
    from services.cbt_service import (
        _build_theory_section_plan,
        _is_gradeable_theory_row,
        _marks_mismatch,
    )

    exam, subject, paper = key
    eligible_records = [r for r in records if _eligible(r["cbt_eligible"])]
    rules = rule_row["rules"] if rule_row else None

    print(f"{exam} / {subject} / {paper}")
    if rule_row is None:
        print("  rules row: NONE (year NULL, country NULL) — CBT falls back to the flat list")
        mode = None
    elif rules is None:
        what = "malformed or not a list" if rule_row["raw_present"] else "no rules_json"
        print(f"  rules row: {rule_row['rule_source']}, {what} — CBT falls back to the flat list")
        mode = None
    else:
        presented = [s.get("presented_count") for s in rules if isinstance(s, dict)]
        if all(p is not None for p in presented):
            mode = "presented"
        elif any(p is not None for p in presented):
            mode = "legacy_uncapped (presented_count on SOME sections only)"
        else:
            mode = "legacy_uncapped"
        print(f"  rules row: {rule_row['rule_source']}, mode {mode}")

    fy = with_retry(free_year, db_conn, exam, subject)
    paid_plan = free_plan = None
    if rules is not None:
        paid_plan = with_retry(_build_theory_section_plan, exam, subject, paper, None)
        free_plan = with_retry(_build_theory_section_plan, exam, subject, paper, fy,
                               no_free_year=fy is None)
    print(f"  free year: {fy if fy is not None else 'none'}")

    rule_by_label = {s.get("section"): s for s in (rules or []) if isinstance(s, dict)}
    record_labels = sorted({r["section"] for r in records if r["section"]}, key=str)
    labels = list(rule_by_label) + [lbl for lbl in record_labels if lbl not in rule_by_label]

    def plan_section(p, label):
        if p is None:
            return None
        for s in p["sections"]:
            if s["rule"].get("section") == label:
                return s
        return None

    for label in labels:
        rule = rule_by_label.get(label)
        in_label = [r for r in records if r["section"] == label]
        elig = [r for r in in_label if _eligible(r["cbt_eligible"])]
        grade = [r for r in elig if _is_gradeable_theory_row(r)]
        mpq = rule.get("marks_per_question") if rule else None
        mismatch = [r for r in grade if rule and _marks_mismatch(r["marks"], mpq)]
        pool = [r for r in grade if r not in mismatch]
        by_year: Dict[Any, int] = defaultdict(int)
        for r in pool:
            by_year[r["year"]] += 1
        passages: Dict[str, List[str]] = defaultdict(list)
        for r in pool:
            if r["passage_id"]:
                passages[r["passage_id"]].append(r["id"])
        shared = {pid: ids for pid, ids in passages.items() if len(ids) > 1}

        tag = "" if rule or rules is None else "   ORPHAN — no rule declares this label; never served"
        print(f"  {label!r}{tag}")
        print(f"    records {len(in_label)}, eligible {len(elig)}, gradeable {len(grade)}, "
              f"D10 mismatch {len(mismatch)}, available {len(pool)}")
        if mismatch:
            print(f"    D10 (rule marks_per_question {_num(mpq)}): "
                  + ", ".join(f"{r['id']} marks {_num(r['marks'])}" for r in mismatch))
        years = ", ".join(f"{y if y is not None else 'no year'}: {n}"
                          for y, n in sorted(by_year.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))
        print(f"    available by year: {years or 'none'}")
        if shared:
            for pid, ids in sorted(shared.items()):
                print(f"    passage {pid}: {', '.join(ids)}")
        if rule:
            print(f"    presented {rule.get('presented_count', '—')} / required "
                  f"{rule.get('required_count', 0)}"
                  + ("   compulsory" if rule.get("compulsory") else ""))
            ps, fs = plan_section(paid_plan, label), plan_section(free_plan, label)
            if ps and fs:
                print(f"    status: paid {ps['status']} (serves {len(ps['selected_ids'])} of "
                      f"{ps['available_count']}), free {fs['status']} (serves "
                      f"{len(fs['selected_ids'])} of {fs['available_count']})")

    if paid_plan is not None:
        for who, p in (("paid", paid_plan), ("free", free_plan)):
            verdict = "available" if p["complete"] else f"UNAVAILABLE — {p['unavailable_reason']}"
            print(f"  {who}: serves {p['served_count']} question(s), {verdict}")
    print()

    return {"key": key, "eligible": len(eligible_records), "mode": mode}


def main() -> int:
    ap = argparse.ArgumentParser(description="Report Theory CBT section pools (read-only)")
    ap.add_argument("--exam", help="only this exam (e.g. NECO)")
    ap.add_argument("--only", nargs="+", metavar="SUBJECT",
                    help="limit to these subjects (case-insensitive, part of the name is enough)")
    ap.add_argument("--launch-check", action="store_true",
                    help="exit 1 if a Theory paper with CBT-eligible records lacks presented_count")
    args = ap.parse_args()

    url = (os.getenv("DATABASE_URL") or "").strip()
    engine = "Postgres" if url.lower().startswith("postgres") else "SQLite (DATABASE_URL not set)"
    print(f"Reading {engine}. Read-only: nothing is written.\n")

    from config import db_conn
    from services.question_utils import row_get

    records = with_retry(theory_records, db_conn, row_get)
    rules = with_retry(rules_rows, db_conn, row_get)

    papers: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        papers[(r["exam"], r["subject"], r["paper"])].append(r)

    keys = sorted(papers, key=lambda k: tuple(str(x) for x in k))
    if args.exam:
        keys = [k for k in keys if k[0] == args.exam]
    if args.only:
        words = [w.lower() for w in args.only]
        keys = [k for k in keys if any(w in str(k[1]).lower() for w in words)]
    if not keys:
        print("No theory records match.")
        return 0

    results = [report_paper(k, papers[k], rules.get(k), db_conn) for k in keys]

    if not args.launch_check:
        return 0

    print("=" * 72)
    print("Launch check: every Theory paper with CBT-eligible records needs a rules row")
    print("with presented_count on every section.\n")
    failing, exempt, ready = [], [], []
    for res in results:
        exam, subject, paper = res["key"]
        name = f"{exam} / {subject} / {paper}"
        if res["eligible"] == 0:
            continue
        if res["mode"] == "presented":
            ready.append(name)
            continue
        why = "no usable rules row" if res["mode"] is None else "rules row without presented_count"
        if subject in EXEMPT_SUBJECTS:
            exempt.append(f"{name} — {why}")
        else:
            failing.append(f"{name} — {why}")

    print(f"Ready ({len(ready)}):")
    for n in ready:
        print(f"  {n}")
    print(f"\nExempt, decision D7 ({len(exempt)}):")
    for n in exempt:
        print(f"  {n}")
    print(f"\nNot ready ({len(failing)}):")
    for n in failing:
        print(f"  {n}")
    print()
    if failing:
        print(f"FAIL: {len(failing)} Theory paper(s) not ready.")
        return 1
    print("PASS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
