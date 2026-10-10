"""
services/licensing_time.py — the one place licensing reads the clock and
compares timestamps.

Timestamps come back from the database as `str` on SQLite and `datetime` on
Postgres (see backend/CLAUDE.md). Comparing the raw values works on one engine
and raises TypeError on the other — access_control.is_paid_user() had exactly
that shape. Licensing is almost entirely expiry comparison, so every licensing
comparison goes through to_datetime() here, in Python. Never compare
timestamps in SQL: lexicographic ISO comparison works until a format varies,
then fails quietly.

The test clock (spec §6.3) is an offset added to now(). It is honoured only
when config.ALLOW_TEST_CLOCK is true, and is read at call time rather than
import time so a test can toggle it. When the flag is false the offset is
ignored entirely — it is not an error to have set one, it simply has no
effect — so a stray offset can never move production time.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import config

_clock_offset: timedelta = timedelta(0)


def now() -> datetime:
    """Aware UTC datetime, honouring the test clock when it is allowed."""
    real = datetime.now(timezone.utc)
    if config.ALLOW_TEST_CLOCK:
        return real + _clock_offset
    return real


def now_iso() -> str:
    return now().isoformat()


def set_clock_offset(offset: timedelta) -> None:
    """Shift now() by `offset`. Has no effect unless ALLOW_TEST_CLOCK is true."""
    global _clock_offset
    _clock_offset = offset


def reset_clock() -> None:
    set_clock_offset(timedelta(0))


def to_datetime(value: Union[str, datetime, None]) -> Optional[datetime]:
    """
    Aware UTC datetime from a DB value, or None for None.

    Naive values are taken as UTC: SQLite's datetime('now') column default
    writes "YYYY-MM-DD HH:MM:SS" with no offset, and that is UTC.

    An unparseable string raises ValueError rather than being guessed at — a
    licensing decision made on a misread expiry is worse than a loud failure.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        raise TypeError(f"Cannot interpret {type(value).__name__} as a timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_expired(value: Union[str, datetime, None], at: Optional[datetime] = None) -> bool:
    """
    True when `value` is at or before `at` (default: now()).

    None counts as expired. For subscription_expires_at that is the spec's
    meaning — NULL is "no entitlement yet" (§3.2) — and for any other expiry a
    missing value must never read as unlimited.
    """
    expires = to_datetime(value)
    if expires is None:
        return True
    reference = to_datetime(at) if at is not None else now()
    return expires <= reference
