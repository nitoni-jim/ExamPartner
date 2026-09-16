"""
tests/test_rubric_engine.py — Rule 16b scoring engine.

Worked cases come from the spec's own examples where it gives them: WAEC
2020 Chemistry Q3(a)(i) for depends_on, Q3(c) for all_or_nothing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.rubric_engine import (  # noqa: E402
    RubricError,
    expected_judgement_ids,
    gradeable_criteria,
    parse_scope,
    render_criteria_for_prompt,
    score_scope,
)


def crit(cid, group, marks, **kw):
    return {"id": cid, "group": group, "criterion": f"criterion {cid}", "marks": marks, **kw}


def run(points, groups, judgements, scope="(a)(i)"):
    cs, gs = parse_scope(points, groups, scope)
    return score_scope(cs, gs, judgements)


# ---------------------------------------------------------------------------
# sum
# ---------------------------------------------------------------------------

def test_sum_awards_each_satisfied_criterion():
    r = run(
        [crit("c1", "g", 1), crit("c2", "g", 1), crit("c3", "g", 1)],
        [{"id": "g", "rule": "sum", "max_marks": 3}],
        {"c1": True, "c2": True, "c3": False},
    )
    assert r.awarded == 2
    assert r.effective_max == 3


def test_sum_cap_is_enforced():
    """The failure that motivated Rule 16b: a prose 3-mark subsection cap was
    overrun by one criterion during diagram-grading trials. Structurally, it
    cannot be."""
    r = run(
        [crit("c1", "g", 2), crit("c2", "g", 2), crit("c3", "g", 2)],
        [{"id": "g", "rule": "sum", "max_marks": 3}],
        {"c1": True, "c2": True, "c3": True},
    )
    assert r.awarded == 3


def test_model_reported_total_is_never_consulted():
    """Rule 16b: any total the model reports is diagnostic only. The engine
    takes judgements, not totals — there is no parameter to pass one."""
    r = run(
        [crit("c1", "g", 1), crit("c2", "g", 1)],
        [{"id": "g", "rule": "sum", "max_marks": 2}],
        {"c1": {"satisfied": True, "evidence": "labelled correctly"},
         "c2": {"satisfied": False, "evidence": ""},
         "total_marks_awarded": 2},
    )
    assert r.awarded == 1


def test_unjudged_criterion_counts_as_unsatisfied():
    """A truncated response must not be able to inflate a score."""
    r = run(
        [crit("c1", "g", 1), crit("c2", "g", 1)],
        [{"id": "g", "rule": "sum", "max_marks": 2}],
        {"c1": True},
    )
    assert r.awarded == 1
    missing = next(c for c in r.criteria if c.id == "c2")
    assert missing.evidence == "not judged"


# ---------------------------------------------------------------------------
# any_n
# ---------------------------------------------------------------------------

def test_any_n_caps_at_n():
    r = run(
        [crit(f"c{i}", "g", 1) for i in range(1, 6)],
        [{"id": "g", "rule": "any_n", "max_marks": 3, "any_n": 3}],
        {f"c{i}": True for i in range(1, 6)},
    )
    assert r.awarded == 3


def test_any_n_takes_the_highest_value_satisfied():
    """A candidate satisfying more than n is credited with their best n."""
    r = run(
        [crit("c1", "g", 1), crit("c2", "g", 3), crit("c3", "g", 2)],
        [{"id": "g", "rule": "any_n", "max_marks": 5, "any_n": 2}],
        {"c1": True, "c2": True, "c3": True},
    )
    assert r.awarded == 5   # 3 + 2, not 1 + 3


def test_any_n_below_n_scores_what_was_met():
    r = run(
        [crit(f"c{i}", "g", 1) for i in range(1, 6)],
        [{"id": "g", "rule": "any_n", "max_marks": 3, "any_n": 3}],
        {"c1": True, "c2": True},
    )
    assert r.awarded == 2


# ---------------------------------------------------------------------------
# all_or_nothing — WAEC 2020 Chemistry Q3(c), the "[2 or 0]" form
# ---------------------------------------------------------------------------

def test_all_or_nothing_awards_full_only_when_complete():
    points = [crit("c1", "g", 1), crit("c2", "g", 1)]
    groups = [{"id": "g", "rule": "all_or_nothing", "max_marks": 2}]

    assert run(points, groups, {"c1": True, "c2": True}).awarded == 2
    assert run(points, groups, {"c1": True, "c2": False}).awarded == 0


# ---------------------------------------------------------------------------
# depends_on — WAEC 2020 Chemistry Q3(a)(i)
# ---------------------------------------------------------------------------

def test_depends_on_suppresses_a_met_criterion():
    """The scheme prints: candidates must indicate energy content of products
    and reactants on the energy profile to score for axes. So axes depends on
    energy contents, and a correct pair of axes scores nothing without it."""
    points = [
        crit("energy_contents", "g", 1),
        crit("axes", "g", 1, depends_on=["energy_contents"]),
    ]
    groups = [{"id": "g", "rule": "sum", "max_marks": 2}]

    both = run(points, groups, {"energy_contents": True, "axes": True})
    assert both.awarded == 2

    axes_only = run(points, groups, {"energy_contents": False, "axes": True})
    assert axes_only.awarded == 0
    outcome = next(c for c in axes_only.criteria if c.id == "axes")
    assert "suppressed" in outcome.reason
    assert not outcome.satisfied


def test_dependency_suppression_is_transitive():
    """A dependency that was itself suppressed does not satisfy a dependent."""
    r = run(
        [crit("a", "g", 1),
         crit("b", "g", 1, depends_on=["a"]),
         crit("c", "g", 1, depends_on=["b"])],
        [{"id": "g", "rule": "sum", "max_marks": 3}],
        {"a": False, "b": True, "c": True},
    )
    assert r.awarded == 0


def test_dependency_cycle_is_rejected():
    with pytest.raises(RubricError, match="cycle"):
        parse_scope(
            [crit("a", "g", 1, depends_on=["b"]), crit("b", "g", 1, depends_on=["a"])],
            [{"id": "g", "rule": "sum", "max_marks": 2}],
        )


def test_self_dependency_is_rejected():
    with pytest.raises(RubricError, match="depends on itself"):
        parse_scope(
            [crit("a", "g", 1, depends_on=["a"])],
            [{"id": "g", "rule": "sum", "max_marks": 1}],
        )


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------

def test_positional_leaves_numerator_and_denominator():
    """A candidate is never penalised for a criterion the system declined to
    judge — the marks leave the denominator rather than counting as zero."""
    r = run(
        [crit("naming", "g", 1),
         crit("structure", "g", 1),
         crit("placement", "g", 1, capability="positional")],
        [{"id": "g", "rule": "sum", "max_marks": 3}],
        {"naming": True, "structure": True},
    )
    assert r.awarded == 2
    assert r.effective_max == 2
    assert r.declared_max == 3
    assert r.excluded_marks == 1


def test_positional_is_never_scored_as_zero():
    """Even when nothing else is met, the excluded criterion is not a zero
    against the candidate — it is simply absent from the denominator."""
    r = run(
        [crit("a", "g", 1), crit("p", "g", 1, capability="positional")],
        [{"id": "g", "rule": "sum", "max_marks": 2}],
        {"a": False},
    )
    assert r.awarded == 0
    assert r.effective_max == 1


def test_positional_is_absent_from_the_prompt():
    cs, _ = parse_scope(
        [crit("a", "g", 1), crit("p", "g", 1, capability="positional")],
        [{"id": "g", "rule": "sum", "max_marks": 2}],
    )
    rendered = render_criteria_for_prompt(cs)
    assert "[a]" in rendered
    assert "[p]" not in rendered
    assert expected_judgement_ids(cs) == ["a"]
    assert len(gradeable_criteria(cs)) == 1


def test_prompt_does_not_expose_mark_values():
    """Showing marks invites the model to total them; arithmetic is the
    engine's job."""
    cs, _ = parse_scope(
        [crit("a", "g", 7)],
        [{"id": "g", "rule": "sum", "max_marks": 7}],
    )
    assert "7" not in render_criteria_for_prompt(cs)


def test_absent_capability_is_gradeable():
    cs, _ = parse_scope(
        [crit("a", "g", 1)],
        [{"id": "g", "rule": "sum", "max_marks": 1}],
    )
    assert cs[0].is_gradeable


def test_unknown_capability_is_rejected_loudly():
    """A typo must fail rather than silently fall through to gradeable."""
    with pytest.raises(RubricError, match="capability"):
        parse_scope(
            [crit("a", "g", 1, capability="postional")],
            [{"id": "g", "rule": "sum", "max_marks": 1}],
        )


def test_any_n_pool_shrinks_when_criteria_are_excluded():
    r = run(
        [crit("c1", "g", 1), crit("c2", "g", 1),
         crit("c3", "g", 1, capability="positional"),
         crit("c4", "g", 1, capability="positional")],
        [{"id": "g", "rule": "any_n", "max_marks": 3, "any_n": 3}],
        {"c1": True, "c2": True},
    )
    assert r.effective_max == 2      # n drops from 3 to 2 survivors
    assert r.awarded == 2


def test_all_or_nothing_group_is_dropped_when_a_member_is_excluded():
    """all_or_nothing is indivisible, so a partially judgeable group leaves
    the denominator entirely rather than being awarded on a subset."""
    r = run(
        [crit("a", "g", 1), crit("p", "g", 1, capability="positional")],
        [{"id": "g", "rule": "all_or_nothing", "max_marks": 2}],
        {"a": True},
    )
    assert r.awarded == 0
    assert r.effective_max == 0
    assert r.excluded_marks == 2
    assert "indivisible" in r.groups[0].note


# ---------------------------------------------------------------------------
# Rule 21 structural conditions (spec lines 641-653)
# ---------------------------------------------------------------------------

def test_string_array_is_rejected():
    with pytest.raises(RubricError, match="not an object"):
        parse_scope(["Euglena is not a true animal."],
                    [{"id": "g", "rule": "sum", "max_marks": 1}])


@pytest.mark.parametrize("missing", ["id", "group", "criterion", "marks"])
def test_missing_required_field_is_rejected(missing):
    point = crit("a", "g", 1)
    del point[missing]
    with pytest.raises(RubricError, match=missing):
        parse_scope([point], [{"id": "g", "rule": "sum", "max_marks": 1}])


def test_criterion_naming_an_undeclared_group_is_rejected():
    with pytest.raises(RubricError, match="not declared"):
        parse_scope([crit("a", "ghost", 1)],
                    [{"id": "g", "rule": "sum", "max_marks": 1}])


def test_group_referenced_by_no_criterion_is_rejected():
    with pytest.raises(RubricError, match="no criterion references it"):
        parse_scope(
            [crit("a", "g1", 1)],
            [{"id": "g1", "rule": "sum", "max_marks": 1},
             {"id": "g2", "rule": "sum", "max_marks": 1}],
        )


def test_duplicate_criterion_id_is_rejected():
    with pytest.raises(RubricError, match="duplicate criterion id"):
        parse_scope([crit("a", "g", 1), crit("a", "g", 1)],
                    [{"id": "g", "rule": "sum", "max_marks": 2}])


def test_unreachable_maximum_is_rejected():
    with pytest.raises(RubricError, match="unreachable"):
        parse_scope([crit("a", "g", 1), crit("b", "g", 1)],
                    [{"id": "g", "rule": "sum", "max_marks": 3}])


def test_any_n_arithmetic_mismatch_is_rejected_when_marks_are_uniform():
    with pytest.raises(RubricError, match="does not equal max_marks"):
        parse_scope([crit(f"c{i}", "g", 1) for i in range(1, 6)],
                    [{"id": "g", "rule": "any_n", "max_marks": 4, "any_n": 3}])


def test_non_uniform_any_n_skips_the_arithmetic_check():
    """The spec scopes reconciliation to uniform marks, because non-uniform
    any_n has no single defensible product to compare against."""
    cs, gs = parse_scope(
        [crit("a", "g", 3), crit("b", "g", 2), crit("c", "g", 1)],
        [{"id": "g", "rule": "any_n", "max_marks": 5, "any_n": 2}],
    )
    assert len(cs) == 3


def test_invalid_rule_is_rejected():
    with pytest.raises(RubricError, match="expected one of"):
        parse_scope([crit("a", "g", 1)],
                    [{"id": "g", "rule": "average", "max_marks": 1}])


def test_any_n_on_a_sum_group_is_rejected():
    """Carrying any_n where the rule is not any_n means one of the two is
    wrong, and guessing which would silently pick a scoring behaviour."""
    with pytest.raises(RubricError, match="any_n applies only"):
        parse_scope([crit("a", "g", 1)],
                    [{"id": "g", "rule": "sum", "max_marks": 1, "any_n": 1}])


def test_any_n_greater_than_member_count_is_rejected():
    # Marks of 2 each so the criteria sum (4) clears max_marks (3) and the
    # unreachable-maximum check — which runs first — does not mask this one.
    with pytest.raises(RubricError, match="only 2 criteria"):
        parse_scope([crit("a", "g", 2), crit("b", "g", 2)],
                    [{"id": "g", "rule": "any_n", "max_marks": 3, "any_n": 3}])


def test_scope_label_appears_in_the_error():
    """A multi-part record must fail with the part identified."""
    with pytest.raises(RubricError, match=r"\(b\)\(ii\)"):
        parse_scope([], [{"id": "g", "rule": "sum", "max_marks": 1}], scope_label="(b)(ii)")


# ---------------------------------------------------------------------------
# Multi-group
# ---------------------------------------------------------------------------

def test_groups_are_capped_independently():
    """Ids need be unique only within a scope, and each group totals on its
    own — a surplus in one never fills a shortfall in another."""
    r = run(
        [crit("a1", "labels", 1), crit("a2", "labels", 1), crit("a3", "labels", 1),
         crit("b1", "details", 1), crit("b2", "details", 1)],
        [{"id": "labels", "rule": "sum", "max_marks": 2},
         {"id": "details", "rule": "sum", "max_marks": 2}],
        {"a1": True, "a2": True, "a3": True, "b1": True, "b2": False},
    )
    assert r.awarded == 3          # labels capped at 2, details 1
    assert r.effective_max == 4
