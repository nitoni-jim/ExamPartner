#!/usr/bin/env python3
"""
tools/check_cbt_structure.py — can this batch be served in CBT?

    python tools/check_cbt_structure.py WAEC_2010_BIOLOGY_THEORY.jsonl
    python tools/check_cbt_structure.py content/*.jsonl --set-eligibility

Run it after the other three validators and before import. Exit 0 when every
theory record's section is one paper_rules declares; 1 otherwise.

--- The gap this closes ---

The three existing validators check a record against itself and against the
spec. None of them consults paper_rules, so none can tell whether a record can
actually be ALLOCATED. Two batches have now reached Neon with fields nothing
was checking:

  paper = "Biology 2 (Essay)"   the paper's printed title rather than the value
                                the bank uses. paper_rules resolves on
                                (exam, subject, paper), so duration and
                                question-count silently fell back to defaults,
                                and the selection screen grew a test type named
                                after the field.

  section = "Part I" / "Part III"   the 2010 paper's own labels. paper_rules
                                for this paper declares "Section A" and
                                "Section C" only, so fetch_cbt_theory_paper()
                                dropped every one of those records — never
                                served, no error raised.

That second one is the dangerous shape, because it FAILS CLOSED. Nothing is
visibly broken; the questions simply never appear. Correcting the labels to
match paper_rules — which looks like tidying up — would have started serving
six records from a paper whose real structure nobody has confirmed.

--- What a section mismatch means, and what it does not ---

A mismatch is NOT a claim that the records are wrong. The 2010 paper really
does print PART I and PART III; paper_rules really does describe the 2023
paper, where the equivalent sections are named differently and gated
differently (2023's Section C is answer-all at 30 marks; 2010's Part III is a
free choice of one at 20). Both are accurate about their own year.

What it means is that this paper's structure is not confirmed against the rules
CBT would allocate with. So the batch is Study-eligible and CBT-ineligible
until either the rules gain a row for it or the structure is confirmed to
match. That is what --set-eligibility writes: cbt_eligible = false, leaving the
content fully available in Study mode where no allocation happens.

--- Why this is not a per-question decision ---

Structure is a property of a PAPER. Sixteen subjects times a handful of years
is a tractable list, and most of it passes untouched: structure is stable
across years within a board and subject, which the audit brief documents
repeatedly (Literature identical 2015-2023, Mathematics 2020-2024, CRS
2020-2024). The exceptions are rare and already named. A batch matching its
paper's confirmed structure passes here automatically and needs no decision at
all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THEORY_TYPES = {"theory", "essay", "practical"}


def load_paper_rules() -> Dict[Tuple[str, str, str], List[Dict[str, Any]]]:
    """
    Reads every paper_rules row into {(exam, subject, paper): [section rules]}.

    Uses the application's own db_conn, so it reads whatever DATABASE_URL
    points at — the same database the import will write to. Checking a batch
    against a different database than it lands in would be worse than not
    checking it.
    """
    from config import db_conn
    from services.question_utils import row_get

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute("SELECT exam, subject, paper, rules_json FROM paper_rules")
        rows = cur.fetchall() or []
    finally:
        db.close()

    out: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        raw = row_get(row, "rules_json")
        try:
            rules = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            rules = []
        if not isinstance(rules, list):
            rules = []
        key = (row_get(row, "exam"), row_get(row, "subject"), row_get(row, "paper"))
        out[key] = [r for r in rules if isinstance(r, dict)]
    return out


def check_file(path: str, rules: Dict[Tuple[str, str, str], List[Dict[str, Any]]]):
    """Returns (checked, findings, eligible) for one file."""
    findings: List[str] = []
    seen: Counter = Counter()
    checked = 0
    key: Optional[Tuple[str, str, str]] = None

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                findings.append(f"  line {line_no}: unparseable JSON — {exc}")
                continue

            if str(record.get("qtype") or "").lower() not in THEORY_TYPES:
                continue        # objective papers are answer-all, no allocation
            checked += 1

            k = (record.get("exam"), record.get("subject"), record.get("paper"))
            if key is None:
                key = k
            elif k != key:
                findings.append(
                    f"  line {line_no}: mixed (exam, subject, paper) in one file — "
                    f"{k} after {key}. paper_rules resolves per paper, so a mixed "
                    f"batch cannot be checked as a unit."
                )
            seen[record.get("section")] += 1

    if checked == 0 or key is None:
        return 0, findings, True

    declared_rules = rules.get(key)
    if declared_rules is None:
        # Not a failure on its own. An objective-only paper has no row, and a
        # paper never yet served in CBT has none either. It does mean nothing
        # can allocate from this batch.
        findings.append(
            f"  no paper_rules row for {key[0]}/{key[1]}/{key[2]} — CBT cannot "
            f"allocate from this paper until one exists"
        )
        return checked, findings, False

    declared: Set[str] = {r.get("section") for r in declared_rules if r.get("section")}
    present: Set[str] = {s for s in seen if s}

    unknown = sorted(present - declared)
    if unknown:
        for section in unknown:
            findings.append(
                f"  section {section!r} ({seen[section]} record(s)) is not declared in "
                f"paper_rules for {key[0]}/{key[1]}/{key[2]}. Declared: "
                f"{sorted(declared)}. These records would be silently dropped by "
                f"fetch_cbt_theory_paper() — never served, no error raised."
            )

    # A declared section with no records is NOT reported as a failure. A batch
    # is often one year of a paper whose rules cover sections that year did not
    # use, or whose other sections are ingested separately. Reporting it would
    # train people to ignore this tool.
    return checked, findings, not unknown


def set_eligibility(path: str, eligible: bool) -> int:
    """Writes cbt_eligible onto every record in the file. Returns count."""
    out, n = [], 0
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            record = json.loads(raw)
            record["cbt_eligible"] = eligible
            out.append(json.dumps(record, ensure_ascii=False))
            n += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Check a batch's sections against paper_rules")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--set-eligibility", action="store_true",
                    help="write cbt_eligible into each record from this check's result")
    args = ap.parse_args()

    try:
        rules = load_paper_rules()
    except Exception as exc:
        print(f"Could not read paper_rules: {exc}", file=sys.stderr)
        print("Is DATABASE_URL set to the database this batch will be imported into?",
              file=sys.stderr)
        return 1
    print(f"paper_rules: {len(rules)} paper(s) with declared sections\n")

    failed = 0
    for path in args.paths:
        if not os.path.exists(path):
            print(f"MISSING  {path}")
            failed += 1
            continue

        checked, findings, eligible = check_file(path, rules)
        if checked == 0:
            print(f"skip     {path}  (no theory records)")
            continue

        status = "ok      " if eligible else "CBT-INELIGIBLE"
        print(f"{status} {path}  ({checked} theory record(s))")
        for f in findings:
            print(f)
        if not eligible:
            failed += 1

        if args.set_eligibility:
            n = set_eligibility(path, eligible)
            print(f"  -> wrote cbt_eligible = {str(eligible).lower()} on {n} record(s)")
        print()

    if failed:
        print(f"{failed} file(s) are not CBT-eligible.")
        print("They remain fully available in Study mode — no allocation happens there,")
        print("so a paper whose structure is unconfirmed is still sound to study from.")
        return 1

    print("All batches match their paper's declared sections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
