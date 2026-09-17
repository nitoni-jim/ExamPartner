"""
tests/test_call_claude_errors.py — exception-clause ordering in _call_claude.

One test, for a bug with no runtime symptom.

anthropic.BadRequestError subclasses APIStatusError. A BadRequestError clause
placed after an APIStatusError clause is unreachable: Python matches except
clauses in written order, the parent catches first, and the child never runs.
Nothing raises, nothing fails, no log line says anything is wrong. The only
visible effect is that an image problem reports as a generic API error, which
sends you checking the API key and the model name — the wrong half of the
system.

This reads theory_service.py as TEXT rather than importing it. That module
pulls in config and the database at import time, which is why the route-level
tests elsewhere skip when the app is not importable. A guard against a
source-ordering mistake should never be one of the tests that skips.
"""
import os
import re

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# theory_service.py lives under services/. Both locations are checked rather
# than one hard-coded path, so moving the module renames the failure into
# "file not found" instead of a confusing assertion about clause ordering.
CANDIDATE_PATHS = [
    os.path.join(BACKEND_DIR, "services", "theory_service.py"),
    os.path.join(BACKEND_DIR, "theory_service.py"),
]


def _call_claude_source() -> str:
    path = next((p for p in CANDIDATE_PATHS if os.path.exists(p)), None)
    assert path, f"theory_service.py not found in any of: {CANDIDATE_PATHS}"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    rest = src[src.index("def _call_claude("):]
    return rest[:rest.index("\ndef ", 1)]


def test_bad_request_clause_precedes_its_parent_class():
    import anthropic

    assert issubclass(anthropic.BadRequestError, anthropic.APIStatusError), (
        "The anthropic exception hierarchy changed. If BadRequestError is no "
        "longer a subclass of APIStatusError this constraint is obsolete, and "
        "this test should be deleted rather than worked around."
    )

    clauses = re.findall(r"except anthropic\.(\w+)", _call_claude_source())

    assert "BadRequestError" in clauses, "the image-specific error branch is gone"
    assert "APIStatusError" in clauses, "the generic API status branch is gone"
    assert clauses.index("BadRequestError") < clauses.index("APIStatusError"), (
        f"BadRequestError must be caught before APIStatusError, its parent "
        f"class, or the branch is dead code. Current order: {clauses}"
    )
