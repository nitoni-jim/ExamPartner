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
    country: Optional[str] = Query(default=None),
    supports_country: bool = Query(
        default=False,
        description=(
            "Client capability flag. Set true only if the caller can "
            "distinguish country-specific paper_rules rows from "
            "country-agnostic ones. Defaults false, which withholds "
            "country-specific rows — the safe answer for clients built "
            "before country support existed."
        ),
    ),
):
    """
    Returns paper_rules rows matching the given filters (all optional).

    country returns that country's rows PLUS country-agnostic ones, since an
    agnostic row still applies to that candidate. Omitting it returns every
    row, including country-specific ones.

    ROLLOUT SAFETY: PaperRulesSyncService calls this with no filters and does
    a full-table replace into its local cache, and clients built before
    country support select on (exam, subject, paper, year) alone — so they
    would match BOTH variants of a country-varying paper and pick
    arbitrarily, on a device that is offline by design.

    supports_country is the guard. Absent (the default), country-specific
    rows are withheld entirely and the caller sees exactly what it saw before
    country existed. Country-varying rows can therefore be authored at any
    time without waiting on client adoption; old installs keep resolving as
    they always have, and pick up variants only once updated.

    No authentication required — paper_rules is structural exam metadata
    (timing, question counts, marks), not paid content, unlike /cbt/questions
    and /cbt/papers. Free and paid users see identical data.

    Primary consumer: Android's PaperRulesSyncService, which fetches the
    full table (no filters) on app launch/login and caches it locally in
    paper_rules_cache for fully offline CBT runtime use.
    """
    return list_paper_rules(
        exam=exam, subject=subject, paper=paper, year=year,
        country=country, supports_country=supports_country,
    )


@router.get("/paper-rules/resolve")
def paper_rules_resolve(
    exam: str = Query(...),
    subject: str = Query(...),
    paper: str = Query(...),
    year: Optional[int] = Query(default=None),
    qtype: str = Query(default="objective"),
    country: Optional[str] = Query(default=None),
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
    return resolve_paper_rule(exam=exam, subject=subject, paper=paper, year=year, qtype=qtype, country=country)


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

    # NULL = applies to every candidate country, which is the state of every
    # pre-existing row. A value marks a rule that applies only to that
    # country — needed because WAEC Geography's section STRUCTURE differs by
    # country, not just its content.
    country:          Optional[str] = None

    # Opt-in acknowledgement that omitted fields should be nulled on an
    # existing row. Defaults False: the service rejects an update that would
    # silently clear a populated field, because the UPDATE sets every column
    # unconditionally and an omitted field is indistinguishable from an
    # explicit null at this layer. Send the complete row, or set this true
    # when the clearing is genuinely intended. Ignored on insert.
    allow_clearing:   bool = False


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

    Note for whoever builds that bulk variant: rules_json validation and the
    partial-update guard both live in upsert_paper_rule() rather than here,
    specifically so a second entry point cannot bypass them. Call the service
    function; do not reimplement the write.
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
        country=body.country,
        allow_clearing=body.allow_clearing,
    )
