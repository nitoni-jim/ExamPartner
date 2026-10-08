# Cloud session brief — ExamPartner Windows licensing

**For:** the implementation session working on `nitoni-jim/ExamPartner`
**Owner:** Nitoni (solo developer; works on this in evenings and weekends)
**Date:** 5 October 2026 · **Revised:** 6 October 2026 (§3 environment, §6 pyflakes) · 7 October 2026 (§4.D rulings on the pre-code report, §8) · 8 October 2026 (§4.E licensing never stops the backend starting, §8)

Read this first, then `backend/CLAUDE.md`, then the Pilot V1 implementation spec. This brief answers the setup questions and settles the open spec gaps. Where it disagrees with the spec, this brief wins and the spec gets updated to match.

---

## 1. What you own

You own the **FastAPI backend** for seat-pool licensing, in `backend/`, on a branch.

- Schema in `db.py` (both the SQLite and Postgres branches)
- `services/licensing_time.py`, `services/licensing_service.py`, `services/fingerprint_service.py`
- `routes/licensing.py` and its registration in `app.py`
- `require_account_owner()` in `services/access_control.py`
- The acceptance tests in `backend/tests/`

## 2. What you do not own

- **The WinUI 3 client.** It is built locally on Windows. You cannot compile or run it here, so do not create it, scaffold it, or write C# for it.
- **The `windows/` folder.** Leave it alone if it exists.
- **The fingerprint survey collector.** Already written and tested — `Collect-Fingerprint.ps1`, `Run-Collector.bat`, `README.txt`. Do not rewrite it. If you think it has a bug, say so rather than replacing it.
- **Android, the PWA in `frontend/`, the content pipeline, `paper_rules`.** Out of scope entirely.
- **`main`.** Never push to it, never merge your own PR.
- **Secrets.** Do not create, print, or commit a signing key. Read `LICENCE_SIGNING_KEY` from the environment and fail clearly when it is absent.

## 3. Setup answers

**Repository.** `nitoni-jim/ExamPartner`. The backend is in `backend/`, the PWA in `frontend/`. Android is **not** in this repo. Read `backend/db.py`, `backend/config.py`, `backend/services/paper_rules_service.py`, `backend/services/access_control.py`, `backend/services/auth_utils.py`, `backend/app.py` and `backend/tests/conftest.py` before proposing anything.

Yes — check §2 of the spec against the live code and report any drift. The conventions in it were derived from the repository at commit `c7422c8`, so they should hold, but verify rather than trust.

**Branch and deploy.** Work on `licensing/pilot-v1`. Open a PR. Never push to `main`, never merge. Nitoni reviews and merges in the GitHub web UI.

While your branch is open, Nitoni does not hand-upload files under `backend/` through the GitHub website. If you see an unexpected commit touching your files, stop and say so rather than merging around it.

**Postgres.** The environment's setup script provisions a throwaway Postgres 16 at `127.0.0.1:5433/ep_test` and a Python 3.11 venv at `/opt/ep-venv`. Do not install or initialise Postgres yourself. Run `ep-pg-start` at the start of every session — it is idempotent, and it is needed because a running server does not survive the cached environment snapshot. **Do not touch Neon**, not even a dev branch. Your tests create and drop tables; production holds live paying users' data.

`DATABASE_URL` is **never set** in this environment. If you find it set, stop and report it rather than working around it. `db.py` has exactly one switch to Postgres — `_using_postgres()` and `_get_pg()` read `DATABASE_URL` at call time — and no separate test-database setting, so a Postgres test can only reach Postgres by setting `DATABASE_URL` itself. Postgres tests therefore set it in-process with `monkeypatch.setenv("DATABASE_URL", ...)` from `TEST_DATABASE_URL`, **after asserting the host is `127.0.0.1` or `localhost`**. If `TEST_DATABASE_URL` is missing, those tests **fail, not skip** — a skipped test 4 reads as a passing one, which is the failure `test_app_imports.py` was written to prevent. The existing suite needs no database URL: the `client` fixture in `test_attachments.py` deletes `DATABASE_URL` and runs on a temporary SQLite file.

**Python.** Run everything with `/opt/ep-venv/bin/python`, which is 3.11 to match Render's 3.11.9:

```
cd backend && /opt/ep-venv/bin/python -m pytest -q
```

The system `python3` is 3.13, and the pinned `psycopg2-binary==2.9.9` has no wheel for it, so `pip install` there tries to build from source and fails. **Never change a pin in `requirements.txt` to make something install** — that file ships to production. Baseline at `0d1d121`, verified by running: **103 passed** with `pyflakes` installed; 102 passed and 1 skipped without it (see §6).

## 4. Rulings on the three gaps

### A. Degenerate machines must not hit the ambiguity queue every 30 days — accepted

Your reading is correct and this was the most serious defect in the spec. Implement the fix as you proposed, with one addition.

On **refresh** (not on activation), a binding whose stored `fingerprint_confidence` is `weak` or `degenerate` may refresh normally when **installation continuity** holds:

1. The presented `installation_id` equals the binding's current `installation_id`, and
2. The supporting signals — Windows `MachineGuid`, disk serial, MAC — match the snapshot stored on the binding.

Continuity broken → ambiguity path, as §8.6.1 says. Continuity intact → refresh normally.

This is sound because the two cases need different evidence. Recognising a machine *after a reinstall* requires firmware identity, which a degenerate machine does not have. Confirming *the same installation is still running* only requires that nothing about it changed — and `MachineGuid`, disk serial and MAC are good at exactly that, while being useless for the first job. It is also what the architecture handoff's corrected fingerprint paragraph already calls for: "installation/server history and, where necessary, administrator confirmation."

**The addition: the ratchet must cover the supporting snapshot too.** §8.6.1 ratchets `machine_fingerprint_hash` forward on a strong match. Do the same for the supporting signals on a successful continuity refresh, or the first disk swap on a degenerate machine breaks continuity and locks it out — reintroducing the bug one maintenance event later.

Note what remains true: a degenerate machine that is genuinely reinstalled, or has its disk replaced, *does* go to the admin. That is correct. For a machine with no firmware identity, a hardware change is genuinely indistinguishable from a different machine, and a human should decide.

### B. Per-signal matching, hash for audit — accepted

Correct; §8.4 and §8.2 contradict each other and your resolution is the right one. Match signal by signal against the pool's active bindings. Keep `machine_fingerprint_hash` for the audit log and as an exact-match fast path.

**One requirement:** define the normalisation explicitly and in one place — trim, upper-case, strip the placeholder values (`To Be Filled By O.E.M.`, `Default string`, `System Serial Number` and the rest), then hash the signals in a fixed documented order. Otherwise the fast path misses for cosmetic reasons and you get an exact-match lookup that silently never matches. The survey collector already implements this filter in `Clean-Value`; mirror its list rather than inventing a second one.

### C. Client collects its lease by resending with `ambiguity_id` — accepted

As proposed. Two requirements:

- **Single use.** Once consumed, the ambiguity row cannot be replayed. `open → resolved → consumed`, and a consumed row is refused.
- **It expires.** An unresolved ambiguity older than some window (7 days is fine) becomes `expired` and is refused. The `status` column already allows this.

Keep the auth rules as the spec has them: an activation resend still runs under owner authentication; a refresh resend needs only the lease.

### D. Rulings on the pre-code report (7 October 2026)

The plan in your pre-code report is approved, with the changes and conditions below. Where this section and your report differ, this section wins.

**Timestamps — approved as you proposed.** Every licensing timestamp is logically a timestamp, stored as ISO-8601 `TEXT` on SQLite and `TIMESTAMPTZ` on Postgres — the existing pattern in `db.py` and the type table in `backend/CLAUDE.md`. The bare `TEXT` in spec §3's tables was documentation drift; spec §3.1 now states the per-engine types. Comparisons still happen in Python through `licensing_time`, never in SQL.

**Change 1 — `claim_capacity()` always generates the new binding's `installation_id`.** It takes no `installation_id` parameter. A client-presented installation ID is evidence for the clone check upstream in activation (spec §8.4 step 4), never the identity of a new binding. Accepting one here would let the activation unit pass a cloned machine's ID into a new binding — colliding with `ux_seat_bindings_active_installation` in the clone case, or adopting the clone's identity.

**Change 2 — `claim_capacity()` revalidates entitlement inside the capacity-grant transaction.** Immediately before inserting the binding, in the same transaction, it re-checks that the pool's `status` is `active` and that the account's subscription is `active` and unexpired (via `licensing_time`), refusing with 402 for a subscription that is not active or has expired (spec §8.4 step 2) and 409 for a pool that is not active (spec §8.4 step 1 names no code; the final error body for both comes with the API contract). Activation may also check both earlier for a fast, clear rejection. The purpose is that **every current and future capacity-grant path enforces the same entitlement boundary**, because `claim_capacity()` is the single chokepoint (spec §5.2). Do **not** describe this as race-free against a concurrent subscription change: the subscription lives on the `accounts` row, and the pool-row lock does not serialise updates to it.

**Condition — the concurrency harness must prove independent connections.** Patching `licensing_service.db_conn` is approved, but the patch must hand each thread its own pre-opened connection (thread-local lookup, not one shared patched value). Test 4 asserts that the 24 workers ran on 24 distinct `pg_backend_pid()` values. A harness that silently shares connections serialises the workers and passes for the wrong reason.

**Condition — the audit row.** Writing the `activate` row to `seat_activation_log` inside `claim_capacity()`'s transaction is approved. `performed_by` and `actor_role` are required arguments with no defaults, and the row carries its own snapshots of fingerprint hash, signals and label (spec §3.5).

**Also approved as you proposed:** `is_paid_user()` on the real clock, not `licensing_time.now()`; `require_account_owner()` returning 403 for an unknown account; `assert_licensing_constraints()` run once after the `init_db()` retry loop, against the same `db_path`; the plain 409 `pool_full` message until `docs/LICENCE_API_CONTRACT.md` exists; running spec test 6 by hand and reporting it in the PR; leaving the inline `paid_until` normalisations in `theory_service.py` and `paystack_routes.py` alone.

**Two notes for `backend/CLAUDE.md`, in your PR.** `CLAUDE.md` lives in `backend/`, so it changes on your branch, not by upload. Add, briefly and in its existing style:
- `paystack_routes.py` predates these conventions — it calls `get_db()` directly and switches placeholders by engine. It works; do not copy it.
- `_POSTGRES_COLUMN_TYPE_CHANGES` in `db.py` is the mechanism for changing an existing Postgres column's type. `ADD COLUMN IF NOT EXISTS` never changes a type.

### E. Licensing must never stop the backend starting (8 October 2026)

**Decision (Nitoni).** This backend also serves the live Android app. A licensing schema problem **closes licensing, not the platform**: `/auth`, CBT, Study, theory grading and every other existing route keep working. This replaces the "raises at boot" requirement in spec §4.2 and the old test 31. Apply it to PR #37 before merge.

**1. Isolate licensing DDL.** The five licensing tables, their ordinary indexes and `ux_seat_bindings_active_installation` move into their own step, run **after the core schema has committed**, in both `_init_db_sqlite()` and `_init_db_postgres()` paths — not inside the existing phase 1 / phase 2 transactions, where a licensing failure would roll back or abort core schema work. `init_db()` catches any failure in that step, logs it at **ERROR** with the exception (not `warning` — this is not a performance index), and records licensing as unavailable with the reason. Core schema failures still fail startup exactly as they do today. The Postgres retry loop keeps covering the core schema only; the licensing step runs once.

**2. Enforce at the chokepoint, live.** `claim_capacity()` checks, inside its own transaction and before granting, that `ux_seat_bindings_active_installation` exists — the same catalog query `assert_licensing_constraints()` uses — and refuses with **503** ("Licensing is unavailable") if it does not, writing nothing. This is checked on every claim, not read from a startup flag, so it cannot go stale and does not depend on `init_db()` having run in this process. Claims are rare; one catalog query each is negligible. **Standing rule for later units:** every licensing path that creates a binding or changes a binding's `installation_id` (restore, in the activation unit) performs the same check.

**3. Report it on `/health`, without touching the database.** Add `"licensing": "ready" | "unavailable"` to the `/health` response, from the result `init_db()` recorded at startup. **Do not query the database from `/health`**: if Render or anything else polls it, a per-request query would keep Neon's compute awake around the clock. `/health` keeps returning 200 either way — licensing must not make the service look unhealthy.

**4. Keep `assert_licensing_constraints()` as it is.** It still raises `LicensingConstraintError`; the change is that `init_db()` catches it inside the licensing step instead of letting it stop startup. Its existing direct tests stay.

**5. Entitlement before capacity.** Move the pool-status and subscription checks in `claim_capacity()` ahead of the capacity count — still after the lock, still in the same transaction, so correctness is unchanged. A suspended or lapsed school is then told its entitlement is the problem (402 / 409 pool not active), not to free a seat.

**6. Replace test 31.** "Drop the index and restart" cannot reproduce a startup failure: `init_db()` runs `CREATE UNIQUE INDEX IF NOT EXISTS` again and simply recreates a dropped index. The realistic failure is the index **failing to create** because existing data violates it. New test 31, on Postgres (and SQLite if practical):
- drop the index, insert two **active** bindings sharing one `installation_id`, run `init_db()`;
- `init_db()` does **not** raise; a core table (`users`) is still queryable; the recorded licensing status is unavailable and `/health` reports `"licensing": "unavailable"` with 200;
- `claim_capacity()` refuses with 503 and writes no binding and no log row;
- revoke one duplicate, run `init_db()` again → index recreated, status ready, `claim_capacity()` succeeds.

**Render deploys manually** (confirmed by Nitoni). Merging PR #37 does not touch production; the next manual deploy of `main` — whoever's work it carries — is what creates the licensing tables in Neon.

## 5. The current unit of work

Spec §13 steps 2–5, and nothing beyond:

1. `services/licensing_time.py` — `now()`, `now_iso()`, `to_datetime()`, `is_expired()`, plus the injectable test clock gated on `ALLOW_TEST_CLOCK`. Route `access_control.is_paid_user()` through `to_datetime()` to fix the SQLite `TypeError`.
2. The licensing schema in `db.py`, both engines, plus the non-swallowed index path and `assert_licensing_constraints()`.
3. `require_account_owner()`.
4. `claim_capacity()` — **write acceptance test 4 before the implementation**, with the barrier-synchronised harness the spec specifies, against local Postgres.

**Stop there.** No endpoints, no activation algorithm, no lease signing, no fingerprint service. Those come in a later unit once this one is reviewed. A narrow first delivery tells Nitoni whether `backend/CLAUDE.md` is doing its job before anything larger is handed over.

## 6. How to work

- Follow `backend/CLAUDE.md` exactly. The conventions in it are not style preferences; most of them are failure modes that are silent when you get them wrong.
- Do not refactor adjacent code, reorder imports, or reformat files. Change what the task needs.
- Do not condense or delete existing comments. They record production failures and are the only record of several hard-won decisions.
- Add `cryptography` to `requirements.txt` **only** when you reach lease signing, which is not this unit.
- Run the test suite before opening the PR. `test_app_imports.py` and `test_no_undefined_names.py` must stay green. **A skip does not count as green.** `test_no_undefined_names.py` currently skips on a plain `pip install -r requirements-dev.txt`, because `pyflakes` is not listed there, though the test's skip message says it is. Add `pyflakes==3.2.0` to `backend/requirements-dev.txt` as part of this unit (dev-only, so nothing reaches Render). The setup script installs it in the meantime.
- In the PR description, state what you verified by running versus what you believe by reading. Keep those separate.

## 7. If you find another gap

Do what you just did: stop, state it, propose a fix, wait. That is the behaviour this project wants, and it has already caught three real defects plus one that would have broken every degenerate lab PC.

Two qualifications. Be specific about why it blocks code rather than listing everything you noticed — a gap that only changes a docstring is not a blocker. And when the fix is yours to judge and reversible, propose it, implement it behind a clear note in the PR, and let review catch it; reserve stopping for decisions that are expensive to undo or that change product behaviour.

## 8. Not yours to decide — raised with Nitoni separately

Items listed here are not yours to act on; they may change the spec under you. **None is open at the moment.** Closed since the last revision: the boot behaviour (decided — §4.E), Render deployment (manual), and the architecture handoff: `docs/ExamPartner_Windows_SeatPool_Architecture_Handoff.md` (updated 27 September) already carries the fingerprint correction, and spec §0 now cites that file.
