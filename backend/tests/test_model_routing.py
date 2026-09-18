"""
tests/test_model_routing.py — which model grades a diagram.

A source-level test, like test_call_claude_errors.py. theory_service imports
config and the database, so a behavioural test would need the whole app stood
up to assert on a branch that is four lines long.

The rule this guards: a scope the candidate answered with a drawing goes
straight to Sonnet. It exists because of a measurement, not a preference —
ten Haiku gradings of one flame-cell diagram scored 3.0 to 4.0 out of 4 on
identical input, all reporting confidence around 0.85. The correct score was
4.0 every time. Since 0.85 is well above ESCALATION_THRESHOLD (0.6), the
existing escalation path could never have caught it: the model was
confidently wrong, not uncertain.
"""
import os
import re

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = [
    os.path.join(BACKEND_DIR, "services", "theory_service.py"),
    os.path.join(BACKEND_DIR, "theory_service.py"),
]


def _grade_theory_source() -> str:
    path = next((p for p in CANDIDATES if os.path.exists(p)), None)
    assert path, f"theory_service.py not found in any of: {CANDIDATES}"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    rest = src[src.index("def grade_theory("):]
    end = rest.find("\ndef ", 1)
    return rest if end == -1 else rest[:end]


def test_a_drawing_selects_sonnet_as_the_first_model():
    src = _grade_theory_source()
    assert "first_model = MODEL_SONNET if has_drawing else MODEL_HAIKU" in src, (
        "diagram scopes must be graded with Sonnet from the first call"
    )


def test_drawing_detection_covers_both_signals():
    """An attachment is the direct signal; expects_diagram catches the case
    where the question asks for a drawing and the candidate typed instead —
    still a diagram question, still worth the stronger model."""
    src = _grade_theory_source()
    assert "bool(attachment_refs)" in src
    assert "expects_diagram" in src


def test_sonnet_is_not_escalated_to_itself():
    """Re-running the same model would bill the student twice for the same
    judgement and change nothing."""
    src = _grade_theory_source()
    assert "first_model != MODEL_SONNET and (confidence < ESCALATION_THRESHOLD" in src, (
        "the escalation branch must be skipped when Sonnet already ran"
    )


def test_text_only_grading_still_starts_on_haiku():
    """The change must not quietly move every theory question to Sonnet. Text
    grading keeps Haiku plus the confidence-based escalation path, where that
    path works as designed."""
    src = _grade_theory_source()
    assert "else MODEL_HAIKU" in src
    assert re.search(r"confidence\s*<\s*ESCALATION_THRESHOLD", src)
