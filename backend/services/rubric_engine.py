"""
services/rubric_engine.py — Rule 16b machine-readable rubric scoring.

Pure functions over plain data. No database, no HTTP, no model client — so
every rule below is testable in isolation and the same code path that scores
production also runs in the validator.

--- What this exists for ---

Spec v16.3 Rule 16b: "Marks are determined by code, not by the model. The AI
grader's output is a per-criterion satisfied/unsatisfied judgement with its
evidence. Group rules, caps, any-N selection, conditional suppression and
arithmetic are applied by ExamPartner in code. Any total the model reports
is diagnostic only and is never the production score."

The rule exists because prose caps were demonstrably not honoured: a rubric
stating a 3-mark subsection maximum in plain language was overrun by one
criterion during diagram-grading trials, while the structurally identical
labels cap was applied correctly in the same response. Compliance that
holds sometimes is not enforcement.

--- Shape ---

    examiner_points: [
      {id, group, criterion, marks, capability?, depends_on?}
    ]
    rubric_groups: [
      {id, rule, max_marks, any_n?}
    ]

Both sit at the same level: on each marked sub-question where the record has
sub_questions, or at the top level where it does not (Rule 16a). Ids need be
unique only within that scope, which is why everything here operates on one
scope at a time.

--- capability ---

    absent | "supported"   -> gradeable
    "positional"           -> excluded from the prompt, the numerator AND
                              the denominator
    "absolute_scale"       -> likewise excluded
    anything else          -> RubricError

Rejecting unknown values is deliberate. A typo like "postional" silently
falling through to gradeable would put a criterion the grader cannot judge
in front of the model, which is the exact failure the field was added to
prevent. A capability the engine does not recognise fails loudly.

Positional marks leave the denominator rather than counting as zero. A
candidate is never penalised for a criterion the system declined to judge —
the question is scored out of what was actually assessed, and the caller is
told the difference so the student can be shown it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

VALID_RULES = frozenset({"sum", "any_n", "all_or_nothing"})

CAPABILITY_SUPPORTED = "supported"
CAPABILITY_POSITIONAL = "positional"
CAPABILITY_ABSOLUTE_SCALE = "absolute_scale"

# Values that remove a criterion from the prompt, the numerator and the
# denominator. Separate values rather than one "unsupported", because each
# has its own recovery path and they must be re-enabled independently:
#
#   positional      relative placement, connection, whether a label line
#                   reaches the structure it names. Measured at 77% on a
#                   planted error in the R&D track. Clears when positional
#                   grading passes R&D.
#   absolute_scale  physical size of the drawing — "8-10 cm long". A photo
#                   of a notebook page carries no scale reference: unknown
#                   camera distance, unknown zoom, no ruler in frame. No
#                   vision improvement recovers this. It clears only if a
#                   scale reference enters the frame, which is a product
#                   change and not an R&D outcome.
#
# Collapsing these into one value would mean the day positional grading
# ships, scale judgement silently ships with it.
EXCLUDED_CAPABILITIES = frozenset({CAPABILITY_POSITIONAL, CAPABILITY_ABSOLUTE_SCALE})
VALID_CAPABILITIES = frozenset({CAPABILITY_SUPPORTED}) | EXCLUDED_CAPABILITIES


class RubricError(ValueError):
    """Raised for a malformed or unscoreable rubric.

    Always a content defect, never a candidate's fault. Callers must map it
    to a refusal that does NOT charge a grading credit.
    """


# ---------------------------------------------------------------------------
# Parsed shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Criterion:
    id: str
    group: str
    criterion: str
    marks: float
    capability: str = CAPABILITY_SUPPORTED
    depends_on: Sequence[str] = field(default_factory=tuple)

    # Whether the source record omitted capability entirely, as opposed to
    # stating "supported". Both grade the same way — the Rule 21 gate needs
    # to tell them apart, because on an expects_diagram scope an omission is an
    # author who has not made the judgement, while an explicit "supported"
    # is an author who has.
    capability_was_absent: bool = False

    @property
    def is_gradeable(self) -> bool:
        return self.capability not in EXCLUDED_CAPABILITIES


@dataclass(frozen=True)
class Group:
    id: str
    rule: str
    max_marks: float
    any_n: Optional[int] = None


@dataclass
class CriterionOutcome:
    id: str
    criterion: str
    declared_marks: float
    awarded: float
    satisfied: bool
    counted: bool               # False when excluded or suppressed
    reason: str                 # why it scored what it scored
    evidence: str = ""


@dataclass
class GroupOutcome:
    id: str
    rule: str
    declared_max: float
    effective_max: float        # after capability exclusion
    awarded: float
    excluded_marks: float
    note: str = ""


@dataclass
class ScopeResult:
    awarded: float
    effective_max: float        # the denominator the candidate is scored out of
    declared_max: float         # what the scheme declares, before exclusions
    excluded_marks: float       # declared_max - effective_max
    criteria: List[CriterionOutcome]
    groups: List[GroupOutcome]

    @property
    def has_exclusions(self) -> bool:
        return self.excluded_marks > 0


# ---------------------------------------------------------------------------
# Parsing and structural validation
# ---------------------------------------------------------------------------

def _as_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RubricError(f"{what} must be a number, got {type(value).__name__}")
    return float(value)


def parse_scope(
    examiner_points: Any,
    rubric_groups: Any,
    scope_label: str = "top-level",
    strict: bool = False,
) -> tuple[List[Criterion], List[Group]]:
    """
    Parses and structurally validates one scope.

    strict=True adds the authoring-time conditions: defects that must never
    enter the content set, but that production should degrade around rather
    than refuse over if a record predating the rule is already ingested. The
    validator passes strict=True; grading uses the default.

    Keeping both behind one function means the validator and production can
    never disagree about what a valid rubric IS — only about where the line
    is enforced.

    Enforces the Rule 21 conditions that are checkable from structure alone
    (spec lines 641-653). Raises RubricError on the first defect found, with
    the scope named — a multi-part record fails with the part identified
    rather than as a whole.
    """
    if not isinstance(examiner_points, list) or not examiner_points:
        raise RubricError(f"{scope_label}: examiner_points must be a non-empty array")
    if not isinstance(rubric_groups, list) or not rubric_groups:
        raise RubricError(f"{scope_label}: rubric_groups must be a non-empty array")

    # --- groups ---
    groups: List[Group] = []
    seen_group_ids: Set[str] = set()
    for raw in rubric_groups:
        if not isinstance(raw, dict):
            raise RubricError(f"{scope_label}: rubric_groups entry is not an object")
        gid = raw.get("id")
        if not isinstance(gid, str) or not gid:
            raise RubricError(f"{scope_label}: rubric_groups entry missing id")
        if gid in seen_group_ids:
            raise RubricError(f"{scope_label}: duplicate group id {gid!r}")
        seen_group_ids.add(gid)

        rule = raw.get("rule")
        if rule not in VALID_RULES:
            raise RubricError(
                f"{scope_label}: group {gid!r} has rule {rule!r}; "
                f"expected one of {sorted(VALID_RULES)}"
            )

        max_marks = _as_number(raw.get("max_marks"), f"{scope_label}: group {gid!r} max_marks")
        if max_marks <= 0:
            raise RubricError(f"{scope_label}: group {gid!r} max_marks must be positive")

        any_n = raw.get("any_n")
        if rule == "any_n":
            if not isinstance(any_n, int) or isinstance(any_n, bool) or any_n < 1:
                raise RubricError(
                    f"{scope_label}: group {gid!r} has rule any_n but no valid any_n value"
                )
        elif any_n is not None:
            raise RubricError(
                f"{scope_label}: group {gid!r} has rule {rule!r} but carries any_n; "
                f"any_n applies only to rule any_n"
            )

        groups.append(Group(id=gid, rule=rule, max_marks=max_marks, any_n=any_n))

    # --- criteria ---
    criteria: List[Criterion] = []
    seen_ids: Set[str] = set()
    for raw in examiner_points:
        if not isinstance(raw, dict):
            raise RubricError(
                f"{scope_label}: examiner_points entry is a "
                f"{type(raw).__name__}, not an object (Rule 16b)"
            )
        for required in ("id", "group", "criterion", "marks"):
            if required not in raw:
                raise RubricError(f"{scope_label}: examiner_points entry missing {required!r}")

        cid = raw["id"]
        if not isinstance(cid, str) or not cid:
            raise RubricError(f"{scope_label}: criterion id must be a non-empty string")
        if cid in seen_ids:
            raise RubricError(f"{scope_label}: duplicate criterion id {cid!r}")
        seen_ids.add(cid)

        gid = raw["group"]
        if gid not in seen_group_ids:
            raise RubricError(
                f"{scope_label}: criterion {cid!r} names group {gid!r}, "
                f"which is not declared in rubric_groups"
            )

        text = raw["criterion"]
        if not isinstance(text, str) or not text.strip():
            raise RubricError(f"{scope_label}: criterion {cid!r} has empty criterion text")

        marks = _as_number(raw["marks"], f"{scope_label}: criterion {cid!r} marks")
        if marks < 0:
            raise RubricError(f"{scope_label}: criterion {cid!r} marks must not be negative")

        capability_absent = "capability" not in raw
        capability = raw.get("capability", CAPABILITY_SUPPORTED)
        if capability not in VALID_CAPABILITIES:
            raise RubricError(
                f"{scope_label}: criterion {cid!r} has capability {capability!r}; "
                f"expected one of {sorted(VALID_CAPABILITIES)}"
            )

        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise RubricError(f"{scope_label}: criterion {cid!r} depends_on must be an array of ids")

        criteria.append(
            Criterion(
                id=cid,
                group=gid,
                criterion=text,
                marks=marks,
                capability=capability,
                capability_was_absent=capability_absent,
                depends_on=tuple(depends_on),
            )
        )

    _validate_cross_references(criteria, groups, scope_label)
    if strict:
        _validate_authoring_only(criteria, groups, scope_label)
    return criteria, groups


def _validate_authoring_only(
    criteria: Sequence[Criterion],
    groups: Sequence[Group],
    scope_label: str,
) -> None:
    """
    Conditions rejected at authoring but tolerated at runtime.

    An all_or_nothing group holding an excluded criterion is mis-modelled
    content, not a scoring question. all_or_nothing means the scheme awards
    nothing for partial work — the "[2 or 0]" form, as in WAEC 2020
    Chemistry Q3(c). If part of such a group cannot be judged, then either
    the criteria are not really indivisible, in which case the rule should be
    sum, or the whole group is ungradeable, in which case every criterion in
    it should be excluded. That is an author's judgement and it is cheap to
    make at authoring time.

    It is not rejected at runtime because refusing to grade a question at all
    is worse for the candidate than grading it out of a reduced total, and a
    record ingested before this rule existed should still be gradeable.
    _effective_group_max() is the runtime backstop: it drops the group, which
    fails safe rather than generous.
    """
    for g in groups:
        if g.rule != "all_or_nothing":
            continue
        excluded = [c.id for c in criteria if c.group == g.id and not c.is_gradeable]
        if excluded and len(excluded) < len([c for c in criteria if c.group == g.id]):
            raise RubricError(
                f"{scope_label}: group {g.id!r} has rule all_or_nothing but "
                f"{', '.join(excluded)} is excluded by capability. The rule awards "
                f"nothing for partial work, so a partly unjudgeable group is "
                f"mis-modelled: use rule sum if the criteria are divisible, or "
                f"exclude every criterion in the group if none can be judged"
            )


def _validate_cross_references(
    criteria: Sequence[Criterion],
    groups: Sequence[Group],
    scope_label: str,
) -> None:
    by_id = {c.id: c for c in criteria}

    # depends_on targets exist, and no self-reference
    for c in criteria:
        for dep in c.depends_on:
            if dep == c.id:
                raise RubricError(f"{scope_label}: criterion {c.id!r} depends on itself")
            if dep not in by_id:
                raise RubricError(
                    f"{scope_label}: criterion {c.id!r} depends_on {dep!r}, which does not exist"
                )

    # no dependency cycles
    _assert_acyclic(criteria, scope_label)

    # every declared group is referenced
    referenced = {c.group for c in criteria}
    for g in groups:
        if g.id not in referenced:
            raise RubricError(
                f"{scope_label}: group {g.id!r} is declared but no criterion references it"
            )

    for g in groups:
        members = [c for c in criteria if c.group == g.id]
        total = sum(c.marks for c in members)

        # An unreachable maximum means the scheme was transcribed wrong: a
        # candidate satisfying every criterion still could not reach the
        # stated total.
        if total < g.max_marks - 1e-9:
            raise RubricError(
                f"{scope_label}: group {g.id!r} criteria sum to {total:g} "
                f"but max_marks is {g.max_marks:g}; the maximum is unreachable"
            )

        if g.rule == "any_n":
            if g.any_n > len(members):
                raise RubricError(
                    f"{scope_label}: group {g.id!r} has any_n={g.any_n} "
                    f"but only {len(members)} criteria"
                )
            # Reconciled only where criterion marks are uniform — the spec
            # scopes the check that way because non-uniform any_n has no
            # single defensible product to compare against.
            uniform = {c.marks for c in members}
            if len(uniform) == 1:
                unit = next(iter(uniform))
                if abs(g.any_n * unit - g.max_marks) > 1e-9:
                    raise RubricError(
                        f"{scope_label}: group {g.id!r} any_n={g.any_n} x {unit:g} marks "
                        f"= {g.any_n * unit:g}, which does not equal max_marks {g.max_marks:g}"
                    )


def _assert_acyclic(criteria: Sequence[Criterion], scope_label: str) -> None:
    colour: Dict[str, int] = {}          # 0 = visiting, 1 = done
    by_id = {c.id: c for c in criteria}

    def visit(cid: str, trail: List[str]) -> None:
        state = colour.get(cid)
        if state == 1:
            return
        if state == 0:
            cycle = " -> ".join(trail[trail.index(cid):] + [cid])
            raise RubricError(f"{scope_label}: depends_on cycle: {cycle}")
        colour[cid] = 0
        for dep in by_id[cid].depends_on:
            visit(dep, trail + [cid])
        colour[cid] = 1

    for c in criteria:
        visit(c.id, [])


# ---------------------------------------------------------------------------
# Capability filtering
# ---------------------------------------------------------------------------

def gradeable_criteria(criteria: Iterable[Criterion]) -> List[Criterion]:
    """The criteria the grader is actually asked to judge."""
    return [c for c in criteria if c.is_gradeable]


def _effective_group_max(group: Group, members: Sequence[Criterion]) -> tuple[float, float, str]:
    """
    Returns (effective_max, excluded_marks, note) for one group after
    positional criteria are removed.

    The three rules need different treatment, because "remove a criterion"
    means something different in each:

    sum
        Each criterion carries its own marks, so the excluded ones come
        straight out of the maximum. Unambiguous.

    any_n
        The candidate picks n from a pool. Shrinking the pool is fine while
        n still fits; when it does not, n drops to what remains and the
        maximum drops with it. Where marks are non-uniform the maximum is
        reduced by the excluded marks instead, since there is no single unit
        to multiply by.

    all_or_nothing
        Atomic by construction — the "[2 or 0]" form in real schemes. If any
        member cannot be judged, the group cannot be awarded and cannot
        fairly be refused either, so the WHOLE group leaves the denominator.

        This is the one place where a second reading is defensible: judge on
        the surviving members and keep the group. It is rejected here
        because all_or_nothing means the scheme treats those criteria as one
        indivisible mark, and scoring a subset would award full marks for
        partial work. Dropping the group is also what the reduced-denominator
        policy says to do with marks the system declines to assess.
        Changing this decision means changing this function only.
    """
    excluded = [c for c in members if not c.is_gradeable]
    if not excluded:
        return group.max_marks, 0.0, ""

    excluded_marks = sum(c.marks for c in excluded)
    survivors = [c for c in members if c.is_gradeable]

    if group.rule == "all_or_nothing":
        return 0.0, group.max_marks, (
            f"all_or_nothing group dropped: {len(excluded)} of {len(members)} "
            f"criteria are not machine-gradeable, and the group is indivisible"
        )

    if not survivors:
        return 0.0, group.max_marks, "every criterion in this group is not machine-gradeable"

    if group.rule == "any_n":
        marks_set = {c.marks for c in members}
        if len(marks_set) == 1:
            unit = next(iter(marks_set))
            effective_n = min(group.any_n, len(survivors))
            effective_max = effective_n * unit
            note = (
                f"any_n reduced from {group.any_n} to {effective_n} "
                f"({len(excluded)} criteria not machine-gradeable)"
                if effective_n != group.any_n
                else f"{len(excluded)} criteria not machine-gradeable; pool reduced"
            )
            return effective_max, group.max_marks - effective_max, note
        effective_max = max(0.0, group.max_marks - excluded_marks)
        return effective_max, group.max_marks - effective_max, (
            "non-uniform any_n: maximum reduced by the excluded criterion marks"
        )

    # sum
    effective_max = max(0.0, group.max_marks - excluded_marks)
    return effective_max, group.max_marks - effective_max, (
        f"{len(excluded)} criterion(s) not machine-gradeable"
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_scope(
    criteria: Sequence[Criterion],
    groups: Sequence[Group],
    judgements: Dict[str, Any],
) -> ScopeResult:
    """
    Applies group rules, caps, any-N selection, conditional suppression and
    arithmetic to a set of per-criterion judgements.

    judgements maps criterion id -> {"satisfied": bool, "evidence": str}.
    A bare bool is also accepted.

    A criterion the model did not judge is treated as unsatisfied rather
    than as an error: a truncated or partial response must not be able to
    inflate a score, and the evidence field records that it was missing so
    the gap is visible in the breakdown.

    Judgements for excluded or unknown ids are ignored — the rubric decides
    what counts, never the model's output.
    """
    by_id = {c.id: c for c in criteria}

    def satisfied_of(cid: str) -> tuple[bool, str]:
        raw = judgements.get(cid)
        if raw is None:
            return False, "not judged"
        if isinstance(raw, bool):
            return raw, ""
        if isinstance(raw, dict):
            return bool(raw.get("satisfied")), str(raw.get("evidence") or "")
        return False, "unparseable judgement"

    # Pass 1 — raw satisfaction, before dependencies.
    raw_satisfied: Dict[str, bool] = {}
    evidence: Dict[str, str] = {}
    for c in criteria:
        ok, ev = satisfied_of(c.id)
        raw_satisfied[c.id] = ok
        evidence[c.id] = ev

    # Pass 2 — conditional suppression. "A criterion with depends_on scores 0
    # whenever any id it names is unsatisfied, regardless of whether the
    # criterion itself is met." Evaluated after all independent criteria,
    # and transitively: a dependency that was itself suppressed does not
    # satisfy a dependent. parse_scope has already proved the graph acyclic,
    # so this terminates.
    effective: Dict[str, bool] = {}

    def resolve(cid: str) -> bool:
        if cid in effective:
            return effective[cid]
        c = by_id[cid]
        ok = raw_satisfied[cid]
        if ok:
            for dep in c.depends_on:
                if not resolve(dep):
                    ok = False
                    break
        effective[cid] = ok
        return ok

    for c in criteria:
        resolve(c.id)

    # Pass 3 — per group.
    group_outcomes: List[GroupOutcome] = []
    criterion_outcomes: List[CriterionOutcome] = []
    total_awarded = 0.0
    total_effective_max = 0.0
    total_declared_max = 0.0

    for g in groups:
        members = [c for c in criteria if c.group == g.id]
        effective_max, excluded_marks, note = _effective_group_max(g, members)
        total_declared_max += g.max_marks
        total_effective_max += effective_max

        scorers = [c for c in members if c.is_gradeable] if effective_max > 0 else []

        if g.rule == "all_or_nothing":
            all_met = bool(scorers) and all(effective[c.id] for c in scorers)
            awarded = effective_max if all_met else 0.0
            for c in members:
                counted = c in scorers
                criterion_outcomes.append(_outcome(
                    c, effective, evidence,
                    awarded=(c.marks if (counted and all_met) else 0.0),
                    counted=counted,
                    reason=_reason(c, effective, raw_satisfied,
                                   group_note="all_or_nothing: group met" if all_met
                                   else "all_or_nothing: group not met"),
                ))

        elif g.rule == "any_n":
            met = [c for c in scorers if effective[c.id]]
            # Highest-value first, so a candidate who satisfies more than n
            # is credited with their best n rather than their first n.
            met.sort(key=lambda c: c.marks, reverse=True)
            limit = min(g.any_n, len(scorers)) if g.any_n else len(scorers)
            chosen = met[:limit]
            chosen_ids = {c.id for c in chosen}
            awarded = min(sum(c.marks for c in chosen), effective_max)
            for c in members:
                counted = c in scorers
                took = c.id in chosen_ids
                criterion_outcomes.append(_outcome(
                    c, effective, evidence,
                    awarded=(c.marks if took else 0.0),
                    counted=counted,
                    reason=_reason(c, effective, raw_satisfied,
                                   group_note=None if took
                                   else ("beyond the any_n limit" if effective.get(c.id) and counted
                                         else None)),
                ))

        else:  # sum
            awarded = 0.0
            for c in members:
                counted = c in scorers
                gets = c.marks if (counted and effective[c.id]) else 0.0
                awarded += gets
                criterion_outcomes.append(_outcome(
                    c, effective, evidence,
                    awarded=gets, counted=counted,
                    reason=_reason(c, effective, raw_satisfied),
                ))
            # The cap the model was demonstrably unable to honour in prose.
            awarded = min(awarded, effective_max)

        total_awarded += awarded
        group_outcomes.append(GroupOutcome(
            id=g.id, rule=g.rule,
            declared_max=g.max_marks, effective_max=effective_max,
            awarded=awarded, excluded_marks=excluded_marks, note=note,
        ))

    return ScopeResult(
        awarded=round(total_awarded, 4),
        effective_max=round(total_effective_max, 4),
        declared_max=round(total_declared_max, 4),
        excluded_marks=round(total_declared_max - total_effective_max, 4),
        criteria=criterion_outcomes,
        groups=group_outcomes,
    )


def _outcome(
    c: Criterion,
    effective: Dict[str, bool],
    evidence: Dict[str, str],
    awarded: float,
    counted: bool,
    reason: str,
) -> CriterionOutcome:
    return CriterionOutcome(
        id=c.id,
        criterion=c.criterion,
        declared_marks=c.marks,
        awarded=awarded,
        satisfied=bool(effective.get(c.id)),
        counted=counted,
        reason=reason,
        evidence=evidence.get(c.id, ""),
    )


def _reason(
    c: Criterion,
    effective: Dict[str, bool],
    raw: Dict[str, bool],
    group_note: Optional[str] = None,
) -> str:
    if not c.is_gradeable:
        return "excluded: not machine-gradeable, removed from the total"
    if raw.get(c.id) and not effective.get(c.id):
        unmet = [d for d in c.depends_on if not effective.get(d)]
        return f"suppressed: depends on {', '.join(unmet)}, which was not met"
    if group_note:
        return group_note
    return "met" if effective.get(c.id) else "not met"


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def render_criteria_for_prompt(criteria: Sequence[Criterion], indent: str = "    ") -> str:
    """
    Renders the gradeable criteria for the grading prompt.

    Positional criteria are omitted entirely rather than shown and marked
    unscoreable. Mentioning them would invite the model to reason about them
    and report a total that includes them, and Rule 16b makes the model's
    total diagnostic anyway — but a breakdown the student sees should not
    contain judgements the system has decided not to trust.

    Marks are deliberately NOT printed. The model returns satisfied or
    unsatisfied per criterion; arithmetic is this module's job, and showing
    mark values invites the model to total them.
    """
    lines = []
    for c in gradeable_criteria(criteria):
        lines.append(f"{indent}[{c.id}] {c.criterion}")
        if c.depends_on:
            lines.append(
                f"{indent}    (only credit this if {', '.join(c.depends_on)} is also met)"
            )
    return "\n".join(lines)


def expected_judgement_ids(criteria: Sequence[Criterion]) -> List[str]:
    """The criterion ids the model must return a judgement for."""
    return [c.id for c in gradeable_criteria(criteria)]
