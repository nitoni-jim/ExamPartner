"""
tests/test_theory_cbt_presented.py — the Theory CBT presented-question pool.

Covers the brief's T1–T15: Rule 6 in validate_rules_json(), the shared
section plan in cbt_service (_build_theory_section_plan), the
GET /cbt/theory-questions response contract, and the /cbt/papers Theory
count and availability, which must always equal what the session serves.

Rows are seeded straight into a fresh SQLite database (the sqlite_database
fixture). Sampling is checked through the helper with an injected
random.Random(seed); the route is exercised through the real app with
auth_headers, paid by a users row with is_paid = 1 and free by having none.
"""
import json
import logging
import random
import secrets

import pytest
from fastapi import HTTPException

EXAM = "NECO"
SUBJECT = "Financial Accounting"
PAPER = "Theory"


# ---------------------------------------------------------------------------
# Fixtures and seeding helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(sqlite_database, monkeypatch):
    """Fresh database, and fresh once-per-process warning sets."""
    from services import cbt_service
    monkeypatch.setattr(cbt_service, "_ORPHAN_SECTION_WARNED", set())
    monkeypatch.setattr(cbt_service, "_LEGACY_UNCAPPED_WARNED", set())
    monkeypatch.setattr(cbt_service, "_MARKS_MISMATCH_WARNED", set())
    return sqlite_database


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


@pytest.fixture
def paid(db, auth_identifier):
    """Make the auth_headers user a paying user (legacy is_paid flag)."""
    _execute(
        "INSERT INTO users (identifier, salt, pw_hash, is_paid) VALUES (?, ?, ?, ?)",
        (auth_identifier, "s", "h", 1),
    )


def _execute(sql, params=()):
    from config import db_conn
    conn = db_conn()
    try:
        conn.cursor().execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def add_q(section, *, qid=None, year=2020, marks=None, gradeable=True, cbt_eligible=None,
          subject=SUBJECT, paper=PAPER, exam=EXAM, qtype="theory", sub_questions=None,
          diagrams=None, passage_id=None, question_text="Answer all parts of this question."):
    qid = qid or f"Q_{secrets.token_hex(4)}"
    examiner_points = json.dumps(["point one", "point two"]) if gradeable and not sub_questions else None
    _execute(
        "INSERT INTO questions (id, exam, year, subject, paper, section, qtype, marks, "
        "question_text, examiner_points_json, sub_questions_json, diagrams_json, passage_id, "
        "cbt_eligible) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (qid, exam, year, subject, paper, section, qtype, marks, question_text,
         examiner_points,
         json.dumps(sub_questions) if sub_questions is not None else None,
         json.dumps(diagrams) if diagrams is not None else None,
         passage_id, cbt_eligible),
    )
    return qid


def add_many(section, n, **kw):
    return [add_q(section, **kw) for _ in range(n)]


def sec(label, required, presented=None, mpq=None, compulsory=False, **extra):
    s = {"section": label, "instruction": f"Answer {required} from {label}.",
         "required_count": required, "compulsory": compulsory}
    if mpq is not None:
        s["marks_per_question"] = mpq
        s["total_marks"] = required * mpq
    if presented is not None:
        s["presented_count"] = presented
    s.update(extra)
    return s


def add_rule(sections, *, raw=None, subject=SUBJECT, paper=PAPER, exam=EXAM,
             rule_source="actual_paper", country=None, total_marks=None):
    _execute(
        "INSERT INTO paper_rules (id, exam, subject, paper, year, country, duration_minutes, "
        "question_count, total_marks, rule_source, rules_json) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?)",
        (secrets.token_hex(16), exam, subject, paper, country, 150, total_marks, rule_source,
         raw if raw is not None else json.dumps(sections)),
    )


def plan(seed=0, year_filter=None, subject=SUBJECT, **kw):
    from services.cbt_service import _build_theory_section_plan
    return _build_theory_section_plan(EXAM, subject, PAPER, year_filter, rng=random.Random(seed), **kw)


def session(client, headers, subject=SUBJECT):
    resp = client.get("/cbt/theory-questions",
                      params={"exam": EXAM, "subject": subject, "paper": PAPER}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def theory_entry(client, headers, subject=SUBJECT):
    resp = client.get("/cbt/papers", params={"exam": EXAM, "subject": subject}, headers=headers)
    assert resp.status_code == 200, resp.text
    entries = [p for p in resp.json()["papers"] if p["qtype"] == "theory"]
    assert len(entries) == 1, resp.json()
    return entries[0]


def served(body):
    return sum(len(s["questions"]) for s in body["sections"])


def assert_picker_agrees(client, headers, body, subject=SUBJECT):
    """T15: the picker's count and availability are what the session serves."""
    entry = theory_entry(client, headers, subject)
    assert entry["count"] == served(body)
    assert entry["available"] == body["selection"]["complete"]
    assert entry["unavailable_reason"] == body["selection"]["unavailable_reason"]
    return entry


def validate(sections, total_marks=None):
    from services.paper_rules_service import validate_rules_json
    validate_rules_json(json.dumps(sections), EXAM, SUBJECT, PAPER, total_marks=total_marks)


def refused(sections, total_marks=None):
    with pytest.raises(HTTPException) as exc:
        validate(sections, total_marks)
    assert exc.value.status_code == 400
    return exc.value.detail


def pooled_crs(presented):
    """WAEC CRS shape: three sections, a floor of 1 each, all in_pool, T = 4."""
    return [
        sec(label, 1, presented=p, mpq=20, in_pool=True, pattern_type="pooled",
            total_required_questions=4)
        for label, p in zip(("Section A", "Section B", "Section C"), presented)
    ]


# ---------------------------------------------------------------------------
# T1 / T1b — the normal case, and the compulsory consistency check
# ---------------------------------------------------------------------------

def test_t1_presented_counts_are_served_and_counted(client, paid, auth_headers):
    add_rule([sec("Section A", 2, 4), sec("Section B", 2, 4), sec("Section C", 1, 3)])
    ids = {lbl: set(add_many(lbl, 10)) for lbl in ("Section A", "Section B", "Section C")}
    add_q(None, qtype="objective", question_text="An objective item")

    body = session(client, auth_headers)
    assert body["selection"] == {
        "mode": "presented", "complete": True, "unavailable_reason": None,
        "year_scope": "all_years", "free_year": None,
    }
    assert [len(s["questions"]) for s in body["sections"]] == [4, 4, 3]
    for s in body["sections"]:
        assert s["status"] == "ok" and s["shortfall"] == 0 and s["available_count"] == 10
        assert {q["id"] for q in s["questions"]} <= ids[s["section"]]

    entry = assert_picker_agrees(client, auth_headers, body)
    assert entry["count"] == 11 and entry["available"] is True

    objective = client.get("/cbt/papers", params={"exam": EXAM, "subject": SUBJECT},
                           headers=auth_headers).json()["papers"]
    objective = [p for p in objective if p["qtype"] == "objective"][0]
    assert objective["available"] is True and objective["unavailable_reason"] is None


def test_t1b_compulsory_presented_must_equal_required(db):
    detail = refused([sec("Section A", 1, 3, compulsory=True)])
    assert "Section A" in detail and "required_count 1" in detail and "presented_count 3" in detail
    assert "consistency check between two authored values, not an inference" in detail


# ---------------------------------------------------------------------------
# T2 / T12 — sampling
# ---------------------------------------------------------------------------

def test_t2_samples_exactly_presented_distinct_from_the_section(db):
    add_rule([sec("Section A", 2, 4)])
    section_ids = set(add_many("Section A", 30))
    p = plan(seed=7)
    picked = p["sections"][0]["selected_ids"]
    assert len(picked) == 4 and len(set(picked)) == 4
    assert set(picked) <= section_ids
    assert p["served_count"] == 4


def test_t12_two_seeds_give_valid_but_different_selections(db):
    add_rule([sec("Section A", 2, 4)])
    section_ids = set(add_many("Section A", 30))
    first = plan(seed=1)["sections"][0]["selected_ids"]
    again = plan(seed=1)["sections"][0]["selected_ids"]
    second = plan(seed=2)["sections"][0]["selected_ids"]
    assert first == again
    for picked in (first, second):
        assert len(set(picked)) == 4 and set(picked) <= section_ids
    assert set(first) != set(second)


# ---------------------------------------------------------------------------
# T3 / T4 / T5 — short pools, insufficient pools, compulsory sections
# ---------------------------------------------------------------------------

def test_t3_short_pool_serves_what_exists(client, paid, auth_headers):
    add_rule([sec("Section A", 2, 4)])
    add_many("Section A", 3)
    body = session(client, auth_headers)
    s = body["sections"][0]
    assert len(s["questions"]) == 3
    assert (s["status"], s["shortfall"], s["available_count"], s["presented_count"]) == ("short", 1, 3, 4)
    assert body["selection"]["complete"] is True
    assert assert_picker_agrees(client, auth_headers, body)["available"] is True


def test_t4_insufficient_pool_makes_the_paper_unavailable(client, paid, auth_headers):
    add_rule([sec("Section A", 1, 2), sec("Section B", 2, 4)])
    add_many("Section A", 2)
    add_many("Section B", 1)
    body = session(client, auth_headers)
    a, b = body["sections"]
    assert a["status"] == "ok"
    assert (b["status"], b["available_count"], b["shortfall"], len(b["questions"])) == ("insufficient", 1, 3, 1)
    assert body["selection"]["complete"] is False
    assert body["selection"]["unavailable_reason"] == "Section B has 1 question available; the paper requires 2."
    entry = assert_picker_agrees(client, auth_headers, body)
    assert entry["available"] is False and "Section B" in entry["unavailable_reason"]


def test_t5_compulsory_section_serves_all_of_them(client, paid, auth_headers):
    add_rule([sec("Part I", 5, 5, compulsory=True)])
    add_many("Part I", 10)
    validate([sec("Part I", 5, 5, compulsory=True)])
    body = session(client, auth_headers)
    assert len(body["sections"][0]["questions"]) == 5
    assert body["sections"][0]["status"] == "ok"
    assert_picker_agrees(client, auth_headers, body)


# ---------------------------------------------------------------------------
# T6 — pooled rows
# ---------------------------------------------------------------------------

def test_t6_pooled_row_samples_per_section_and_validates(client, paid, auth_headers):
    rules = pooled_crs([3, 3, 2])
    validate(rules, total_marks=80)
    add_rule(rules, total_marks=80)
    for lbl in ("Section A", "Section B", "Section C"):
        add_many(lbl, 5)
    body = session(client, auth_headers)
    assert [len(s["questions"]) for s in body["sections"]] == [3, 3, 2]
    assert all(s["in_pool"] and s["total_required_questions"] == 4 for s in body["sections"])
    assert_picker_agrees(client, auth_headers, body)

    detail = refused(pooled_crs([1, 1, 1]), total_marks=80)
    assert "add up to 3" in detail and "total_required_questions is 4" in detail


# ---------------------------------------------------------------------------
# T7 — legacy rows are served exactly as before
# ---------------------------------------------------------------------------

def test_t7_rows_without_presented_count_are_legacy_uncapped(client, paid, auth_headers, caplog):
    from config import db_conn
    from services.cbt_service import _section_header
    from services.question_utils import QUESTION_SELECT_COLS, build_passage_lookup, row_to_question

    rules = [sec("Section A", 2, mpq=12.5), sec("Section B", 3, mpq=15)]
    add_rule(rules)
    gradeable = add_many("Section A", 6, marks=12.5) + add_many("Section B", 7, marks=15)
    add_q("Section A", gradeable=False)

    caplog.set_level(logging.WARNING, logger="exampartner")
    body = session(client, auth_headers)
    session(client, auth_headers)
    legacy_warnings = [r for r in caplog.records if "legacy_uncapped" in r.getMessage()]
    assert len(legacy_warnings) == 1

    assert body["selection"]["mode"] == "legacy_uncapped"
    assert body["selection"]["complete"] is True
    assert served(body) == len(gradeable)

    # What the pre-change function returned for each section: its header
    # plus every gradeable record, serialized by row_to_question().
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {QUESTION_SELECT_COLS} FROM questions WHERE id IN "
                    f"({','.join('?' * len(gradeable))})", tuple(gradeable))
        rows = cur.fetchall()
        lookup = build_passage_lookup(conn, rows)
    finally:
        conn.close()
    expected_q = {r["id"]: row_to_question(r, lookup) for r in rows}

    for rule, s in zip(rules, body["sections"]):
        header = _section_header(rule)
        assert {k: s[k] for k in header} == json.loads(json.dumps(header))
        expected = sorted((q for q in expected_q.values() if q["section"] == rule["section"]),
                          key=lambda q: q["id"])
        assert sorted(s["questions"], key=lambda q: q["id"]) == json.loads(json.dumps(expected))
        assert (s["presented_count"], s["shortfall"], s["status"]) == (None, 0, "ok")
        assert s["available_count"] == len(expected)

    assert_picker_agrees(client, auth_headers, body)


# ---------------------------------------------------------------------------
# T8 — Rule 6 refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sections, fragments", [
    ([sec("Section A", 2, 4), sec("Section B", 2)],
     ["missing: ['Section B']", "every section or none"]),
    ([sec("Section A", 2, 1)],
     ["Section A", "presented_count 1", "required_count 2"]),
    ([sec("Section A", 2, 0)],
     ["Section A", "presented_count 0", "at least 1"]),
    ([sec("Section A", 2, 2.5)],
     ["Section A", "presented_count 2.5", "whole number"]),
    ([sec("Section A", 1, True)],
     ["Section A", "presented_count True", "whole number"]),
    ([sec("Section A", 2, "4")],
     ["Section A", "presented_count '4'", "whole number"]),
], ids=["partial", "below_required", "zero", "fraction", "bool", "string"])
def test_t8_rule6_refusals(db, sections, fragments):
    detail = refused(sections)
    for fragment in fragments:
        assert fragment in detail, detail


def test_t8_pooled_sum_below_total_required(db):
    detail = refused(pooled_crs([1, 1, 1]), total_marks=80)
    assert detail == (
        "Pooled row: the sections' presented_count add up to 3 but "
        "total_required_questions is 4. The paper cannot require more answers "
        "than the questions it presents."
    )


def test_t8_absent_everywhere_and_valid_counts_pass(db):
    validate([sec("Section A", 2), sec("Section B", 3)])
    validate([sec("Section A", 2, 4), sec("Section B", 3, 3)])
    validate(pooled_crs([2, 1, 1]), total_marks=80)


def test_t8_upsert_runs_rule6(db):
    from services.paper_rules_service import upsert_paper_rule
    with pytest.raises(HTTPException) as exc:
        upsert_paper_rule(exam=EXAM, subject=SUBJECT, paper=PAPER, rule_source="actual_paper",
                          rules_json=json.dumps([sec("Section A", 2, 1)]))
    assert "presented_count 1" in exc.value.detail


# ---------------------------------------------------------------------------
# T9 — no rules row, malformed rules_json
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "{not json", json.dumps({"section": "Section A"})],
                         ids=["no_row", "malformed", "object_not_list"])
def test_t9_no_usable_rules_keeps_sections_empty(client, paid, auth_headers, raw):
    if raw is not None:
        add_rule(None, raw=raw)
    add_many("Section A", 3)
    body = session(client, auth_headers)
    assert body["sections"] == []
    assert body["selection"] == {
        "mode": "legacy_uncapped", "complete": True, "unavailable_reason": None,
        "year_scope": "all_years", "free_year": None,
    }
    # No rules row: the picker keeps counting records, unchanged.
    entry = theory_entry(client, auth_headers)
    assert entry["count"] == 3 and entry["available"] is True


# ---------------------------------------------------------------------------
# T10 — what never enters a pool
# ---------------------------------------------------------------------------

def test_t10_orphans_ungradeable_and_mark_mismatches_are_excluded(client, paid, auth_headers, caplog):
    add_rule([sec("Section A", 2, 4, mpq=12.5)])
    good = set(add_many("Section A", 3, marks=12.5)) | {add_q("Section A", marks=None)}
    add_q("Section A ", marks=12.5)                 # orphan: trailing space
    add_q("Section A", marks=12.5, gradeable=False)  # ungradeable
    mismatch = add_q("Section A", marks=15)          # D10
    add_q("Section A", marks=12.5, cbt_eligible=0)   # authored out of CBT

    caplog.set_level(logging.WARNING, logger="exampartner")
    body = session(client, auth_headers)
    session(client, auth_headers)
    s = body["sections"][0]
    assert s["available_count"] == 4
    assert {q["id"] for q in s["questions"]} == good
    assert_picker_agrees(client, auth_headers, body)

    d10 = [r.getMessage() for r in caplog.records if "marks_per_question" in r.getMessage()]
    assert len(d10) == 1 and mismatch in d10[0]
    assert len([r for r in caplog.records if "Orphaned section" in r.getMessage()]) == 1


def test_t10_marks_compare_as_floats(db):
    from services.cbt_service import _marks_mismatch
    assert not _marks_mismatch(12.5, 12.5)
    assert not _marks_mismatch(15.0, 15)
    assert not _marks_mismatch(None, 15)
    assert not _marks_mismatch(15, None)
    assert _marks_mismatch(15, 12.5)


# ---------------------------------------------------------------------------
# T11 — a whole record travels intact
# ---------------------------------------------------------------------------

def test_t11_multi_part_record_with_diagrams_and_passage_served_whole(client, paid, auth_headers):
    _execute(
        "INSERT INTO passages (id, exam, year, subject, paper, section, title, passage_type, "
        "passage_text, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("P1", EXAM, 2020, SUBJECT, PAPER, "Section A", "Ledger", "case",
         "Trial balance of Ade Ltd", json.dumps({"instruction": "Use the data"})),
    )
    parts = [
        {"label": "a", "question_text": "Prepare the ledger", "marks": 6,
         "examiner_points": ["opening balance"]},
        {"label": "b", "question_text": "Balance it", "marks": 6.5,
         "examiner_points": ["closing balance"]},
    ]
    diagrams = [{"filename": "ledger.png", "caption": "Ledger"}]
    qid = add_q("Section A", marks=12.5, sub_questions=parts, diagrams=diagrams, passage_id="P1")
    add_rule([sec("Section A", 1, 1, mpq=12.5)])

    body = session(client, auth_headers)
    (q,) = body["sections"][0]["questions"]
    assert q["id"] == qid
    assert [p["label"] for p in q["sub_questions"]] == ["a", "b"]
    assert all("examiner_points" not in p for p in q["sub_questions"])
    assert q["diagrams"] == diagrams
    assert q["passage_snapshot"]["passage_text"] == "Trial balance of Ade Ltd"
    assert_picker_agrees(client, auth_headers, body)


# ---------------------------------------------------------------------------
# T13 — free users draw from the free year only
# ---------------------------------------------------------------------------

def test_t13_free_user_draws_only_the_free_year(client, auth_headers):
    add_rule([sec("Section A", 1, 2)])
    old = set(add_many("Section A", 3, year=2015))
    add_many("Section A", 5, year=2021)

    body = session(client, auth_headers)
    assert body["selection"]["year_scope"] == "free_year"
    assert body["selection"]["free_year"] == 2015
    s = body["sections"][0]
    assert s["available_count"] == 3
    assert {q["id"] for q in s["questions"]} <= old and len(s["questions"]) == 2
    assert_picker_agrees(client, auth_headers, body)


def test_t13_free_year_short_of_required_is_unavailable(client, auth_headers):
    add_rule([sec("Section A", 2, 4)])
    add_many("Section A", 1, year=2015)
    add_many("Section A", 6, year=2021)
    body = session(client, auth_headers)
    assert body["sections"][0]["status"] == "insufficient"
    assert body["selection"]["complete"] is False
    assert_picker_agrees(client, auth_headers, body)["available"] is False


def test_t13_free_user_with_no_free_year_gets_200_incomplete(client, auth_headers):
    add_rule([sec("Section A", 1, 2), sec("Section B", 1, 2)])
    add_many("Section A", 3, year=None)
    add_many("Section B", 3, year=None)
    body = session(client, auth_headers)
    assert body["selection"] == {
        "mode": "presented", "complete": False,
        "unavailable_reason": "No free-year Theory questions for this subject.",
        "year_scope": "free_year", "free_year": None,
    }
    assert [s["available_count"] for s in body["sections"]] == [0, 0]
    assert [s["status"] for s in body["sections"]] == ["insufficient", "insufficient"]
    assert served(body) == 0
    # The picker shows no papers at all for a subject with no free year.
    papers = client.get("/cbt/papers", params={"exam": EXAM, "subject": SUBJECT},
                        headers=auth_headers).json()["papers"]
    assert papers == []


# ---------------------------------------------------------------------------
# T14 — country-specific rows are not read
# ---------------------------------------------------------------------------

def test_t14_country_specific_row_is_ignored(client, paid, auth_headers):
    add_rule([sec("Section A", 1, 2)], rule_source="syllabus_default")
    add_rule([sec("Section A", 3, 3, compulsory=True)], rule_source="actual_paper", country="GM")
    add_many("Section A", 5)
    body = session(client, auth_headers)
    s = body["sections"][0]
    assert (s["required_count"], s["presented_count"], len(s["questions"])) == (1, 2, 2)
    assert_picker_agrees(client, auth_headers, body)
