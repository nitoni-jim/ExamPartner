"""
routes/paper_rules.py — paper_rules HTTP routes for ExamPartner.

Business logic lives in services/paper_rules_service.py.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from services.access_control import require_admin
from services.auth_utils import get_current_user
from services.paper_rules_service import (
    list_paper_rules,
    resolve_paper_rule,
    upsert_paper_rule,
)

router = APIRouter(tags=["paper_rules"])


@router.get("/paper-rules")
def paper_rules(
    exam: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    paper: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
):
    """
    Returns paper_rules rows matching the given filters (all optional).

    No authentication required — paper_rules is structural exam metadata
    (timing, question counts, marks), not paid content, unlike /cbt/questions
    and /cbt/papers. Free and paid users see identical data.

    Primary consumer: Android's PaperRulesSyncService, which fetches the
    full table (no filters) on app launch/login and caches it locally in
    paper_rules_cache for fully offline CBT runtime use.
    """
    return list_paper_rules(exam=exam, subject=subject, paper=paper, year=year)


@router.get("/paper-rules/resolve")
def paper_rules_resolve(
    exam: str = Query(...),
    subject: str = Query(...),
    paper: str = Query(...),
    year: Optional[int] = Query(default=None),
    qtype: str = Query(default="objective"),
):
    """
    Resolves the effective duration/count/marks for ONE paper, applying the
    full three-tier fallback (exact year -> year-NULL -> hardcoded default).

    This is a convenience/debugging endpoint — Android's CBT runtime does
    NOT call this live. It always resolves from the locally cached
    paper_rules_cache table, since CBT must work fully offline. This route
    exists for testing the resolution logic directly, and for any future
    admin tooling that wants to preview what a given (exam, subject, paper,
    year) would currently resolve to without needing the full table dump.
    """
    return resolve_paper_rule(exam=exam, subject=subject, paper=paper, year=year, qtype=qtype)


class UpsertPaperRuleRequest(BaseModel):
    exam:             str
    subject:          str
    paper:            str
    rule_source:      str
    year:             Optional[int] = None
    duration_minutes: Optional[int] = None
    question_count:   Optional[int] = None
    total_marks:      Optional[int] = None
    rules_json:       Optional[str] = None


@router.post("/paper-rules")
def upsert_paper_rules_route(
    body: UpsertPaperRuleRequest,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Admin-only. Inserts or updates one paper_rules row.

    Used to populate rows from audit data (e.g. the booklet audit master
    table) — this is a manual/admin data-entry path, not part of any
    automated pipeline. No bulk-import endpoint exists yet; each row is
    upserted individually. A bulk variant can be added later if the volume
    of confirmed audit rows makes one-at-a-time impractical.
    """
    require_admin(user)
    return upsert_paper_rule(
        exam=body.exam,
        subject=body.subject,
        paper=body.paper,
        rule_source=body.rule_source,
        year=body.year,
        duration_minutes=body.duration_minutes,
        question_count=body.question_count,
        total_marks=body.total_marks,
        rules_json=body.rules_json,
    )
