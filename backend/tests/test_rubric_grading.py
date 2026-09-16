"""
tests/test_rubric_grading.py — the Rule 16b grading path end to end.

Uses the real WAEC 2010 Biology Q1(c) record. No API call: the model's
response is stubbed, which is the point — everything the candidate sees is
computed from judgements by code, so the whole scoring path is testable
without a network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.rubric_engine import RubricError  # noqa: E402
from services.theory_rubric_grading import (  # noqa: E402
    build_content_blocks,
    build_rubric_prompt,
    is_structured_rubric,
    parse_question_scopes,
    question_uses_rubric_engine,
    score_from_response,
)

BIOLOGY_Q1C = {
    "label": "(c)",
    "marks": 6,
    "expects_diagram": True,
    "question_text": "Make a diagram 8\u201310 cm long of a flame cell and label fully.",
    "examiner_points": [
        {"id": "title",   "group": "drawing", "criterion": "Title given for the drawing.", "marks": 1, "capability": "supported"},
        {"id": "size",    "group": "drawing", "criterion": "Drawing is 8\u201310 cm long as specified.", "marks": 1, "capability": "absolute_scale"},
        {"id": "clarity", "group": "drawing", "criterion": "Clean continuous lines, neat ruled label lines that touch the structures named.", "marks": 1, "capability": "positional"},
    ] + [
        {"id": f"l_{n}", "group": "labels", "criterion": f"Label: {n}.", "marks": 0.5, "capability": "supported"}
        for n in ("lumen", "nucleus", "basal", "cilium", "duct", "tubule", "cyto", "vacuole", "intra")
    ],
    "rubric_groups": [
        {"id": "drawing", "rule": "sum", "max_marks": 3},
        {"id": "labels", "rule": "any_n", "max_marks": 3, "any_n": 6},
    ],
}

QUESTION = {
    "id": "WAEC_2010_BIOLOGY_THEORY_Q1",
    "exam": "WAEC", "subject": "Biology", "year": 2010, "topic": "Excretion",
    "question_text": "Study the specimens and answer the questions.",
    "marks": 20,
    "sub_questions": [BIOLOGY_Q1C],
}


def response(satisfied_ids, **kw):
    ids = set(satisfied_ids)
    scopes = []
    for sq in QUESTION["sub_questions"]:
        judgements = [
            {"id": p["id"], "satisfied": p["id"] in ids, "evidence": "seen in the drawing"}
            for p in sq["examiner_points"]
            if p.get("capability", "supported") == "supported"
        ]
        scopes.append({"label": sq["label"], "judgements": judgements})
    return {
        "question_id": QUESTION["id"], "confidence": 0.9, "needs_review": False,
        "scopes": scopes, "overall_feedback": "Good.", "improvement_tip": "Label more.",
        **kw,
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_structured_rubric_is_detected():
    assert question_uses_rubric_engine(QUESTION)


def test_string_rubric_is_not_routed_to_the_engine():
    legacy = {**QUESTION, "sub_questions": [
        {"label": "(a)", "marks": 2, "examiner_points": ["Euglena is not a true animal."]}
    ]}
    assert not question_uses_rubric_engine(legacy)


def test_essay_rubric_dict_is_not_a_structured_rubric():
    """The English essay rubric is a dict, and a different grading path."""
    assert not is_structured_rubric({"content": {"max_marks": 10}})


def test_half_converted_record_fails_loudly():
    """Routing is on ANY scope, so a mixed record reaches parse and raises a
    named error rather than grading down the string path, where its criterion
    objects would render into the prompt as Python dict reprs."""
    mixed = {**QUESTION, "sub_questions": [
        BIOLOGY_Q1C,
        {"label": "(d)", "marks": 4, "examiner_points": ["A string point."]},
    ]}
    assert question_uses_rubric_engine(mixed)
    with pytest.raises(RubricError, match="half-converted"):
        parse_question_scopes(mixed)


def test_labelled_stem_with_no_rubric_is_skipped_not_failed():
    """Rule 16a: a sub-question whose marks all sit in its children carries
    no examiner_points and nothing to grade."""
    withstem = {**QUESTION, "sub_questions": [
        {"label": "(a)", "marks": 0, "question_text": "Study the diagram."},
        BIOLOGY_Q1C,
    ]}
    scopes = parse_question_scopes(withstem)
    assert [s.label for s in scopes] == ["(c)"]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt_omits_excluded_criteria_entirely():
    scopes = parse_question_scopes(QUESTION)
    prompt = build_rubric_prompt(QUESTION, scopes, "my answer")
    assert "[title]" in prompt
    assert "[size]" not in prompt and "8\u201310 cm long as specified" not in prompt
    assert "[clarity]" not in prompt and "label lines that touch" not in prompt


def test_prompt_never_reveals_mark_values():
    """A model that knows the values will total them, and totals are code's
    job. It was told before, and prose caps were overrun."""
    scopes = parse_question_scopes(QUESTION)
    prompt = build_rubric_prompt(QUESTION, scopes, "my answer")
    assert "0.5" not in prompt
    assert "max_marks" not in prompt


def test_schema_asks_for_no_total():
    scopes = parse_question_scopes(QUESTION)
    prompt = build_rubric_prompt(QUESTION, scopes, "answer")
    # Only the schema block. The words appear earlier in the grading rule that
    # forbids calculating them, which is the opposite of asking for them.
    schema = prompt.split("RESPONSE FORMAT")[-1]
    for forbidden in ("total_marks_awarded", "marks_awarded", "percentage", "max_marks"):
        assert forbidden not in schema, f"schema still asks for {forbidden}"


def test_prompt_notes_the_attachment_when_one_is_present():
    scopes = parse_question_scopes(QUESTION)
    plain = build_rubric_prompt(QUESTION, scopes, "answer")
    with_img = build_rubric_prompt(QUESTION, scopes, "answer", attachment_labels=["(c)"])
    assert "already" in with_img and "rotation" in with_img
    assert "rotation" not in plain


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------

def test_no_attachments_yields_the_bare_string():
    """The text-only path must produce a request identical to the existing
    one, not an equivalent-but-different shape."""
    assert build_content_blocks("hello", []) == "hello"


def test_image_blocks_precede_the_text():
    blocks = build_content_blocks("PROMPT", [("(c)", b"\xff\xd8\xffdata", "image/jpeg")])
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[-1]["text"] == "PROMPT"


def test_pdf_becomes_a_document_block():
    blocks = build_content_blocks("P", [("(c)", b"%PDF-1.7", "application/pdf")])
    assert blocks[0]["type"] == "document"


def test_unsupported_attachment_type_is_skipped_not_fatal():
    blocks = build_content_blocks("P", [("(c)", b"xx", "application/zip")])
    assert blocks == "P" or all(b.get("type") != "image" for b in blocks)


# ---------------------------------------------------------------------------
# Scoring — the numbers the candidate sees
# ---------------------------------------------------------------------------

def test_full_marks_on_the_assessable_criteria():
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, response(
        ["title"] + [f"l_{n}" for n in ("lumen", "nucleus", "basal", "cilium", "duct", "tubule")]
    ))
    assert out["total_marks_awarded"] == 4
    assert out["max_marks"] == 4
    assert out["declared_max_marks"] == 6
    assert out["excluded_marks"] == 2
    assert out["percentage"] == 100.0


def test_the_model_total_is_ignored_even_when_supplied():
    """Rule 16b: any total the model reports is diagnostic only."""
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, response(["title"], total_marks_awarded=6))
    assert out["total_marks_awarded"] == 1


def test_any_n_cap_holds_when_all_nine_labels_are_satisfied():
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, response(
        ["title"] + [f"l_{n}" for n in ("lumen", "nucleus", "basal", "cilium", "duct",
                                        "tubule", "cyto", "vacuole", "intra")]
    ))
    assert out["total_marks_awarded"] == 4     # 1 title + 3 capped labels, not 5.5


def test_excluded_criteria_never_appear_in_the_breakdown():
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, response(["title"]))
    shown = [p["point"] for p in out["point_breakdown"]]
    assert not any("8\u201310 cm" in p for p in shown)
    assert not any("touch the structures" in p for p in shown)
    assert not any("8\u201310 cm" in p for p in out["missed_points"])


def test_exclusion_note_is_written_for_the_candidate():
    scopes = parse_question_scopes(QUESTION)
    note = score_from_response(QUESTION, scopes, response(["title"]))["exclusion_note"]
    assert "6" in note and "4" in note
    assert "not lost marks" in note


def test_no_exclusion_fields_when_nothing_is_excluded():
    """A text question must not carry diagram scaffolding in its result."""
    clean = {**QUESTION, "sub_questions": [{
        **BIOLOGY_Q1C,
        "examiner_points": [{"id": "a", "group": "g", "criterion": "x", "marks": 1}],
        "rubric_groups": [{"id": "g", "rule": "sum", "max_marks": 1}],
        "marks": 1,
    }]}
    out = score_from_response(clean, parse_question_scopes(clean), {
        "question_id": "x", "confidence": 1.0, "needs_review": False,
        "scopes": [{"label": "(c)", "judgements": [{"id": "a", "satisfied": True}]}],
        "overall_feedback": "", "improvement_tip": "",
    })
    assert "excluded_marks" not in out and "exclusion_note" not in out


def test_a_scope_the_model_omitted_scores_zero_not_an_error():
    """A partial response must never inflate a score."""
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, {
        "question_id": QUESTION["id"], "confidence": 0.5, "needs_review": True,
        "scopes": [], "overall_feedback": "", "improvement_tip": "",
    })
    assert out["total_marks_awarded"] == 0
    assert out["max_marks"] == 4


def test_a_hallucinated_criterion_id_is_ignored():
    """The rubric decides what counts, never the model's output."""
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, {
        "question_id": QUESTION["id"], "confidence": 0.9, "needs_review": False,
        "scopes": [{"label": "(c)", "judgements": [
            {"id": "title", "satisfied": True},
            {"id": "l_invented", "satisfied": True},
        ]}],
        "overall_feedback": "", "improvement_tip": "",
    })
    assert out["total_marks_awarded"] == 1


def test_evidence_reaches_the_candidate_breakdown():
    scopes = parse_question_scopes(QUESTION)
    out = score_from_response(QUESTION, scopes, {
        "question_id": QUESTION["id"], "confidence": 0.9, "needs_review": False,
        "scopes": [{"label": "(c)", "judgements": [
            {"id": "title", "satisfied": True, "evidence": "titled 'Flame cell'"},
        ]}],
        "overall_feedback": "", "improvement_tip": "",
    })
    title = next(p for p in out["point_breakdown"] if "Title" in p["point"])
    assert "Flame cell" in title["comment"]
