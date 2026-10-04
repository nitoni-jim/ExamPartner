#!/usr/bin/env python3
"""
tools/check_cbt_structure.py — report how CBT will serve a theory batch.

    python tools/check_cbt_structure.py NECO_2015_BIOLOGY_THEORY_V16.jsonl
    python tools/check_cbt_structure.py content/*.jsonl

REPORT ONLY. This tool reads JSONL files and the paper_rules table. It writes
nothing: not to the files, not to the database.

--- cbt_eligible is authored, never written by tooling ---

cbt_eligible is decided when the JSONL is authored (spec Rule 3d) and is
imported exactly as written. Absent or true means the record may be served in
CBT; false means Study mode only. This tool reads it and never changes it.

The --set-eligibility flag that used to write cbt_eligible from this check's
result has been removed. Passing it now stops the run with an explanation.

--- Which paper_rules row it checks against ---

The row CBT sectioning will actually use, chosen exactly the way
cbt_service._get_theory_section_rules() chooses it:

  * only rows with year IS NULL. A row with a year is never used for
    sectioning, so it is ignored here too;
  * best rule_source first: actual_paper > syllabus_default > legacy_placeholder;
  * country is not considered, because sectioning has no country filter;
  * if that row's rules_json is empty, malformed or not a list, the paper is
    served flat, exactly as sectioning would serve it.

--- What it reports ---

Only records authored CBT-eligible are checked against the row. Records
authored cbt_eligible: false are never served in CBT, so their labels do not
matter there; they are counted and noted, never failed.

  FAIL  An eligible record's section label is not declared in the row.
        fetch_cbt_theory_paper() would drop it silently: never served in CBT,
        no error raised. Either the row or the authored value needs a
        decision. This tool makes neither.
  FAIL  A year-NULL row for this paper carries a country. Sectioning ignores
        country, so that row could be served to every candidate in every
        country. Policy: no country-specific theory rows until Sprint B.
  FAIL  Unparseable JSON, or more than one (exam, subject, paper) in a file.
  flat  No usable row. CBT serves the paper as a flat list: every question
        offered, no section choice counts. NOT a failure, and never a reason
        to change cbt_eligible.
  ok    Every eligible record's label is declared in the row.

Exit code 1 if any file FAILs or is missing, 0 otherwise.

--- Contract with bulk_import_theory.py ---

The bulk runner imports this file and calls load_paper_rules() and
check_file() directly. Their names and return shapes are kept:

  load_paper_rules() -> {(exam, subject, paper): [section rule dicts]}
      Holds only papers whose sectioning row has usable rules, so
      `key in result` means "CBT serves this paper by section".
  check_file(path, rules) -> (checked, findings, ok)
      checked   theory records in the file, eligible or not
      findings  report lines; "mixed (exam, subject, paper)" and
                "unparseable JSON" keep their wording, because the runner
                matches on them
      ok        False only for the FAIL cases above. A paper with no row
                returns True.

Change either signature only together with the runner, or the next import
breaks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THEORY_TYPES = {"theory", "essay", "practical"}

Key = Tuple[Any, Any, Any]

# Same order as the ORDER BY in cbt_service._get_theory_section_rules().
_RULE_SOURCE_ORDER = {"actual_paper": 0, "syllabus_default": 1, "legacy_placeholder": 2}

# Filled by load_paper_rules(): papers that have at least one year-NULL row
# carrying a country, with the countries found. Module-level so that
# check_file() keeps the signature the bulk runner depends on.
COUNTRY_ROWS: Dict[Key, List[str]] = {}


def _parse_rules(raw: Any) -> List[Dict[str, Any]]:
    """Mirrors _get_theory_section_rules(): anything but a non-empty list is no rules."""
    if raw is None:
        return []
    if isinstance(raw, list):
        parsed: Any = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(parsed, list):
        return []
    return [r for r in parsed if isinstance(r, dict)]


def load_paper_rules() -> Dict[Key, List[Dict[str, Any]]]:
    """
    Returns {(exam, subject, paper): [section rules]} for every paper that CBT
    sectioning would serve by section, using the row sectioning would pick.

    Uses the application's own db_conn, so it reads whatever DATABASE_URL
    points at: the database the batch will be imported into.
    """
    from config import db_conn
    from services.question_utils import row_get

    db = db_conn()
    cur = db.cursor()
    try:
        # No parameters, so the same text runs on SQLite and Postgres.
        cur.execute(
            "SELECT exam, subject, paper, year, country, rule_source, rules_json "
            "FROM paper_rules"
        )
        rows = cur.fetchall() or []
    finally:
        db.close()

    COUNTRY_ROWS.clear()
    candidates: Dict[Key, List[Any]] = {}
    for row in rows:
        if row_get(row, "year") is not None:
            continue  # never used for sectioning
        key = (row_get(row, "exam"), row_get(row, "subject"), row_get(row, "paper"))
        candidates.setdefault(key, []).append(row)
        country = row_get(row, "country")
        if country:
            COUNTRY_ROWS.setdefault(key, []).append(str(country))

    out: Dict[Key, List[Dict[str, Any]]] = {}
    for key, rows_for_key in candidates.items():
        rows_for_key.sort(key=lambda r: _RULE_SOURCE_ORDER.get(row_get(r, "rule_source"), 3))
        rules = _parse_rules(row_get(rows_for_key[0], "rules_json"))
        if rules:
            out[key] = rules
    return out


def check_file(path: str, rules: Dict[Key, List[Dict[str, Any]]]):
    """Returns (checked, findings, ok) for one file. See the module docstring."""
    findings: List[str] = []
    seen: Counter = Counter()      # section label -> eligible record count
    checked = 0
    study_only = 0
    ok = True
    key: Optional[Key] = None

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                findings.append(f"  line {line_no}: unparseable JSON — {exc}")
                ok = False
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
                ok = False

            # Same test the bulk runner uses. Read, never written.
            if record.get("cbt_eligible") is False:
                study_only += 1
                continue
            seen[record.get("section")] += 1

    if checked == 0 or key is None:
        return 0, findings, ok

    label = f"{key[0]}/{key[1]}/{key[2]}"

    countries = COUNTRY_ROWS.get(key)
    if countries:
        findings.append(
            f"  paper_rules has year-NULL row(s) for {label} with country "
            f"{sorted(set(countries))}. Sectioning has no country filter, so such a "
            f"row can be served to every candidate. No country-specific theory rows "
            f"until Sprint B."
        )
        ok = False

    if study_only:
        findings.append(
            f"  {study_only} record(s) authored cbt_eligible: false — Study mode only, "
            f"never served in CBT, so their section labels are not checked"
        )

    if not seen:
        return checked, findings, ok

    declared_rules = rules.get(key)
    if declared_rules is None:
        findings.append(
            f"  no paper_rules row for {label} — served flat in CBT (every question "
            f"offered, no section choice counts). Not a failure."
        )
        return checked, findings, ok

    declared = {r.get("section") for r in declared_rules if r.get("section")}
    # A missing label counts as undeclared too: sectioning skips a record
    # with no section, exactly as it skips one with an unknown section.
    unknown = sorted((s for s in seen if s not in declared), key=lambda s: str(s))
    for section in unknown:
        findings.append(
            f"  section {section!r} ({seen[section]} CBT-eligible record(s)) is not "
            f"declared in the paper_rules row for {label}. Declared: "
            f"{sorted(declared)}. fetch_cbt_theory_paper() would drop these records "
            f"silently — never served in CBT, no error raised. The row or the "
            f"authored value needs a decision; this tool changes neither."
        )
    if unknown:
        ok = False

    # A declared section with no records is not reported. A batch is often one
    # year of a paper whose rules cover sections that year did not use.
    return checked, findings, ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report how CBT will serve each theory batch. Writes nothing."
    )
    ap.add_argument("paths", nargs="+")
    # Kept only to refuse it with an explanation rather than a bare argparse error.
    ap.add_argument("--set-eligibility", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.set_eligibility:
        print(
            "--set-eligibility has been removed. cbt_eligible is authored in the "
            "JSONL (spec Rule 3d) and no tool writes it. Nothing was run.",
            file=sys.stderr,
        )
        return 2

    try:
        rules = load_paper_rules()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read paper_rules: {exc}", file=sys.stderr)
        print("Is DATABASE_URL set to the database this batch will be imported into?",
              file=sys.stderr)
        return 1

    print(f"paper_rules: {len(rules)} paper(s) that CBT serves by section")
    for key, countries in sorted(COUNTRY_ROWS.items(), key=lambda kv: str(kv[0])):
        print(f"  WARNING year-NULL row(s) with country {sorted(set(countries))} "
              f"for {key[0]}/{key[1]}/{key[2]}")
    print()

    failed = 0
    for path in args.paths:
        if not os.path.exists(path):
            print(f"MISSING  {path}")
            failed += 1
            continue

        checked, findings, ok = check_file(path, rules)
        if checked == 0:
            print(f"skip     {path}  (no theory records)")
        else:
            if not ok:
                status = "FAIL    "
            elif any("served flat" in f for f in findings):
                status = "flat    "
            else:
                status = "ok      "
            print(f"{status} {path}  ({checked} theory record(s))")
        for f in findings:
            print(f)
        if not ok:
            failed += 1
        print()

    if failed:
        print(f"{failed} file(s) need a decision before import. Nothing was changed.")
        return 1

    print("No failures. Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
