"""
tests/conftest.py — shared test setup and fixtures.

Puts the backend root on sys.path once, so individual test files do not each
repeat a sys.path.insert. pytest inserts the rootdir only under some
invocations, and `pytest` versus `python -m pytest` differ on whether the
working directory lands on the path — this removes the difference.
"""
import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# Tokens are minted with the application's own make_token() rather than
# hand-assembled here. auth_utils signs with JWT_SECRET and read_token()
# rejects anything whose signature does not match, so a fixture that built its
# own token would have to duplicate the signing scheme — and would then keep
# passing if that scheme ever changed, which is the opposite of what an auth
# test is for.
#
# The identifier is an email because that is what get_current_user() returns
# as "sub" and what every service in the codebase uses as user_id.

TEST_USER = "test.student@example.com"
OTHER_USER = "other.student@example.com"


def _bearer(identifier: str) -> dict:
    from services.auth_utils import make_token
    return {"Authorization": f"Bearer {make_token(identifier)}"}


@pytest.fixture
def auth_identifier() -> str:
    """The identifier auth_headers authenticates as.

    Exposed separately so a test can assert on stored rows — attachment
    ownership is recorded against this exact string.
    """
    return TEST_USER


@pytest.fixture
def auth_headers() -> dict:
    """Authorization header for a signed-in user."""
    return _bearer(TEST_USER)


@pytest.fixture
def other_auth_headers() -> dict:
    """A DIFFERENT signed-in user.

    Required for the ownership tests: proving one user cannot read another's
    attachment needs two real identities, not one identity and an absent
    header, which only proves that unauthenticated access is refused.
    """
    return _bearer(OTHER_USER)
