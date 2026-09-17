#!/usr/bin/env python3
"""
tools/validate_16b.py — Rule 16a/16b compliance checker for JSONL batches.

Run against a regenerated batch BEFORE it goes anywhere near Neon:

    python tools/validate_16b.py path/to/BIOLOGY_2010_THEORY.jsonl
    python tools/validate_16b.py content/*.jsonl --quiet

Exit code 0 when every record passes, 1 otherwise — so it drops into a
pre-commit hook or CI step without further wiring.

Uses services/rubric_engine.parse_scope() rather than reimplementing the
checks, so the validator and production scoring can never disagree about
what a valid rubric is. Anything parse_scope accepts, the engine can score.

--- What is checked ---

Rule 16a, placement:
  - a record with sub_questions must not carry top-level examiner_points
  - every sub-question with non-zero marks must carry examiner_points
  - a sub-question with no marks (a labelled stem whose marks all sit in its
    children) must not carry examiner_points — there is nothing to grade
  - a theory record with no sub_questions must carry top-level
    examiner_points

Rule 16b, structure (delegated to parse_scope, spec lines 641-653):
  - objects not strings; required fields; group cross-references; duplicate
    ids; unreachable maxima; any_n arithmetic; depends_on targets and cycles
  - capability is "supported" or "positional", nothing else

Scope: gradeable theory records only. Objective records are skipped by
design — their examiner_points is authoring-time metadata that
row_to_question() never serializes and no scoring path reads, and Rule 16b
does not reach them. Run with --include-objective to see them counted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.rubric_engine import RubricError, parse_scope  # noqa: E402

THEORY_TYPES = {"theory", "essay", "practical"}

# Criterion or question text that implies the candidate draws something, or
# that a property of the drawing is being judged. Used only to WARN — a
# heuristic cannot decide whether a criterion is gradeable, but it can stop
# a diagram question slipping through untagged, which is what happened to
# WAEC 2010 Biology Q1(c): "Make a diagram 8-10 cm long of a flame cell",
# with criteria for label lines touching structures and for the drawing's
# physical length, and no expects_diagram and no capability on anything.
DRAWING_TEXT = re.compile(
    r"\b(draw|drawing|sketch|diagram|illustrate|"
    r"label(?:s|led|ling)?\s+(?:line|the|fully)|"
    r"\d+\s*[-\u2013]\s*\d+\s*cm|cm\s+long|"
    r"touch(?:es|ing)?\s+the|adjacent\s+to|points?\s+to|"
    r"correctly\s+(?:placed|positioned|located)|to\s+scale)\b",
    re.I,
)


class Finding:
    __slots__ = ("line", "qid", "rule", "message")

    def __init__(self, line: int, qid: str, rule: str, message: str):
        self.line, self.qid, self.rule, self.message = line, qid, rule, message

    def __str__(self) -> str:
        return f"  line {self.line} [{self.qid}] {self.rule}: {self.message}"


def _marks_of(obj: Dict[str, Any]) -> float:
    raw = obj.get("marks")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return float(raw)


def check_record(record: Dict[str, Any], line: int) -> List[Finding]:
    findings: List[Finding] = []
    qid = str(record.get("id") or "?")

    def add(rule: str, message: str) -> None:
        findings.append(Finding(line, qid, rule, message))

    sub_questions = record.get("sub_questions") or []
    top_points = record.get("examiner_points")
    top_groups = record.get("rubric_groups")

    if sub_questions:
        # --- Rule 16a branch 1 ---
        if top_points:
            # Not a style nit. _build_prompt() resolves the two layouts by
            # preference, not merge: if any sub-question carries
            # examiner_points, that layout wins and the top-level list is
            # discarded in full, silently, with no indication which was used.
            add("16a", "record has sub_questions AND top-level examiner_points; "
                       "the top-level list would be silently discarded")
        if top_groups:
            add("16a", "record has sub_questions AND top-level rubric_groups")

        for sq in sub_questions:
            if not isinstance(sq, dict):
                add("16a", "sub_questions entry is not an object")
                continue
            label = str(sq.get("label") or "?")
            marks = _marks_of(sq)
            points = sq.get("examiner_points")
            groups = sq.get("rubric_groups")

            if marks > 0:
                if not points:
                    # Rule 7 interaction: absence here is not a harmless
                    # omission. Both theory.py and
                    # cbt_service._is_gradeable_theory_row() accept a record
                    # when ANY single sub-question carries a rubric, so a
                    # partially populated record passes the gradeability gate
                    # and grades every part against one part's criteria — at
                    # the cost of a student's credit.
                    add("16a", f"{label} carries {marks:g} marks but has no examiner_points")
                    continue
                if not groups:
                    add("16b", f"{label} has examiner_points but no sibling rubric_groups")
                    continue
                try:
                    criteria, parsed_groups = parse_scope(points, groups, label, strict=True)
                except RubricError as exc:
                    add("16b", str(exc))
                    continue
                _check_marks_reconcile(criteria, parsed_groups, marks, label, add)
                _check_diagram_scope(sq, criteria, label, add)
            else:
                if points:
                    add("16a", f"{label} carries no marks but has examiner_points; "
                               f"a labelled stem has nothing to grade")
    else:
        # --- Rule 16a branch 2 ---
        if not top_points:
            add("16a", "theory record has no sub_questions and no top-level examiner_points")
            return findings
        if not top_groups:
            add("16b", "top-level examiner_points with no sibling rubric_groups")
            return findings
        try:
            criteria, parsed_groups = parse_scope(top_points, top_groups, "top-level", strict=True)
        except RubricError as exc:
            add("16b", str(exc))
            return findings
        _check_marks_reconcile(criteria, parsed_groups, _marks_of(record), "top-level", add)
        _check_diagram_scope(record, criteria, "top-level", add)

    return findings


def _check_diagram_scope(scope_obj: Dict[str, Any], criteria, label: str, add) -> None:
    """
    Rule 21, expects_diagram condition — scoped to the sub-question, not the record.

    The launch plan states the condition at record level, and named the field
    has_diagram. Both were changed in v16.4: the field is expects_diagram,
    because Section 4 already owns `diagrams` and `answer_diagrams` for images
    shipped WITH a question and this one means the candidate must produce a
    drawing — close to the opposite, and the two legitimately co-occur on the
    same record.

    On the record-level scoping: That is too coarse
    for a multi-part question: WAEC 2010 Biology Q1 has four sub-questions and
    only (c) is a drawing, so a record-level flag would demand explicit
    capability on roughly thirty pure-text criteria in (a), (b) and (d). Every
    one would be filled "supported", which trains bulk-filling and defeats a
    gate whose purpose is forcing a real judgement. expects_diagram belongs where
    the marks are, exactly as Rule 16a puts examiner_points there.

    Two checks, one hard and one advisory:

      REJECT   expects_diagram is true and any criterion in that scope omits
               capability. The author must decide for each one.

      WARN     criterion or question text implies a drawing but expects_diagram
               is absent. Heuristic, so it cannot reject — but an untagged
               diagram question is the failure this whole capability field
               exists to prevent, and it is invisible to every structural
               check.
    """
    expects_diagram = scope_obj.get("expects_diagram")

    if expects_diagram is True:
        untagged = [c.id for c in criteria if c.capability_was_absent]
        if untagged:
            add("21", f"{label}: expects_diagram is true but capability is absent on "
                      f"{', '.join(untagged)}; every criterion must state it explicitly")
        if not any(not c.is_gradeable for c in criteria):
            add("21", f"{label}: expects_diagram is true but no criterion is excluded; "
                      f"confirm none of them judge placement or physical size")
        return

    suspicious = []
    if DRAWING_TEXT.search(str(scope_obj.get("question_text") or "")):
        suspicious.append("question text")
    for c in criteria:
        if DRAWING_TEXT.search(c.criterion):
            suspicious.append(c.id)
    if suspicious:
        add("21?", f"{label}: expects_diagram is absent but drawing language appears in "
                   f"{', '.join(suspicious[:6])}"
                   f"{' and others' if len(suspicious) > 6 else ''} — "
                   f"confirm this question does not take a diagram")


def _check_marks_reconcile(criteria, groups, declared_marks: float, scope: str, add) -> None:
    """
    The rubric's group maxima must total the marks the scheme declares for
    that scope.

    This is the check Rule 16a exists to make possible — "splitting the two
    prevents any sub-question's examiner_points from being reconciled
    against its own mark total." A mismatch means the transcription lost or
    invented marks somewhere, and it is invisible once the parts are
    flattened.
    """
    if declared_marks <= 0:
        return
    rubric_total = sum(g.max_marks for g in groups)
    if abs(rubric_total - declared_marks) > 1e-9:
        add("16a", f"{scope}: rubric_groups total {rubric_total:g} marks "
                   f"but the scope declares {declared_marks:g}")

    positional = [c for c in criteria if not c.is_gradeable]
    if positional and len(positional) == len(criteria):
        add("capability", f"{scope}: every criterion is positional, so nothing "
                          f"in this scope can be graded")


def check_file(path: str, include_objective: bool) -> Tuple[int, int, List[Finding]]:
    checked = skipped = 0
    findings: List[Finding] = []

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                findings.append(Finding(line_no, "?", "json", f"unparseable: {exc}"))
                continue

            qtype = str(record.get("qtype") or record.get("type") or "").lower()
            if qtype not in THEORY_TYPES:
                if include_objective:
                    skipped += 1
                else:
                    skipped += 1
                continue

            checked += 1
            findings.extend(check_record(record, line_no))

    return checked, skipped, findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Rule 16a/16b compliance checker")
    ap.add_argument("paths", nargs="+", help="JSONL files to check")
    ap.add_argument("--include-objective", action="store_true",
                    help="count objective records (they are never checked against 16b)")
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    args = ap.parse_args()

    total_checked = total_skipped = 0
    total_findings: List[Finding] = []

    for path in args.paths:
        if not os.path.exists(path):
            print(f"MISSING  {path}", file=sys.stderr)
            total_findings.append(Finding(0, "?", "io", f"file not found: {path}"))
            continue

        checked, skipped, findings = check_file(path, args.include_objective)
        total_checked += checked
        total_skipped += skipped
        total_findings.extend(findings)

        if findings:
            print(f"FAIL     {path}  ({checked} theory record(s), {len(findings)} finding(s))")
            for f in findings:
                print(str(f))
        elif not args.quiet:
            print(f"ok       {path}  ({checked} theory record(s), {skipped} skipped)")

    print()
    if total_findings:
        by_rule: Dict[str, int] = {}
        for f in total_findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        summary = ", ".join(f"{rule}: {n}" for rule, n in sorted(by_rule.items()))
        print(f"{len(total_findings)} finding(s) across {total_checked} theory record(s) — {summary}")
        return 1

    print(f"All {total_checked} theory record(s) pass Rule 16a/16b. "
          f"{total_skipped} objective record(s) skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
