# Cloud session brief — ExamPartner Windows licensing

**For:** the implementation session working on `nitoni-jim/ExamPartner`
**Owner:** Nitoni (solo developer; works on this in evenings and weekends)
**Date:** 5 October 2026 · **Revised:** 6 October 2026 (§3 environment, §6 pyflakes) · 7 October 2026 (§4.D rulings on the pre-code report, §8) · 8 October 2026 (§4.E licensing never stops the backend starting, §8; §2 signing key per the API contract) · 10 October 2026 (Unit 1 merged; Unit 2 — §4.F, §4.G, §5 — reviewed and approved) · 11 October 2026 (§4.H rulings on the Unit 2 pre-code report; §4.F, §4.G, §5 edited to match) · 12 October 2026 (Unit 2 merged as PR #38; §9 carried into Unit 3) · 19 October 2026 (Unit 3 split into 3a and 3b for review, both before 4 November; §4.I rulings for Unit 3a; §5 is now Unit 3a; §3 branch and baseline)

Read this first, then `backend/CLAUDE.md`, then the Pilot V1 implementation spec. This brief answers the setup questions and settles the open spec gaps. Where it disagrees with the spec, this brief wins and the spec gets updated to match.

---

## 1. What you own

You own the **FastAPI backend** for seat-pool licensing, in `backend/`, on a branch.

- Schema in `db.py` (both the SQLite and Postgres branches)
- `services/licensing_time.py`, `services/licensing_service.py`, `services/fingerprint_service.py`, and any new `services/licence_*.py` modules for lease signing and activation tokens
- `routes/licensing.py` and its registration in `app.py`, and the licensing field on `/health`
- The licensing settings in `config.py` and the `cryptography` pin in `requirements.txt`
- `require_account_owner()` in `services/access_control.py`
- The acceptance tests in `backend/tests/`

## 2. What you do not own

- **The WinUI 3 client.** It is built locally on Windows. You cannot compile or run it here, so do not create it, scaffold it, or write C# for it.
- **The `windows/` folder.** Leave it alone if it exists.
- **The fingerprint survey collector.** Already written and tested — `Collect-Fingerprint.ps1`, `Run-Collector.bat`, `README.txt`. Do not rewrite it. If you think it has a bug, say so rather than replacing it.
- **Android, the PWA in `frontend/`, the content pipeline, `paper_rules`.** Out of scope entirely.
- **`main`.** Never push to it, never merge your own PR.
- **Secrets.** Do not create, print, or commit a signing key. Read `LICENCE_SIGNING_KEY` from the environment. When it is absent, malformed or equal to the published TEST seed, licensing is unavailable (503) and an ERROR is logged — the backend still starts (§4.E, `docs/LICENCE_API_CONTRACT.md` §6.5). Tests use the contract's TEST key or a key generated in-process.

## 3. Setup answers

**Repository.** `nitoni-jim/ExamPartner`. The backend is in `backend/`, the PWA in `frontend/`. Android is **not** in this repo. Read `backend/db.py`, `backend/config.py`, `backend/services/paper_rules_service.py`, `backend/services/access_control.py`, `backend/services/auth_utils.py`, `backend/app.py` and `backend/tests/conftest.py` before proposing anything.

Yes — check §2 of the spec against the live code and report any drift. The conventions in it were derived from the repository at commit `c7422c8`, so they should hold, but verify rather than trust.

**Branch and deploy.** Unit 1's branch `licensing/pilot-v1` is merged and deleted. Work on a new branch per unit, cut from current `main`: **`licensing/unit-3a`** for this one (Unit 2's `licensing/unit-2` is merged). Open a PR. Never push to `main`, never merge. Nitoni reviews and merges in the GitHub web UI.

While your branch is open, Nitoni does not hand-upload files under `backend/` through the GitHub website. If you see an unexpected commit touching your files, stop and say so rather than merging around it.

**Postgres.** The environment's setup script provisions a throwaway Postgres 16 at `127.0.0.1:5433/ep_test` and a Python 3.11 venv at `/opt/ep-venv`. Do not install or initialise Postgres yourself. Run `ep-pg-start` at the start of every session — it is idempotent, and it is needed because a running server does not survive the cached environment snapshot. **Do not touch Neon**, not even a dev branch. Your tests create and drop tables; production holds live paying users' data.

`DATABASE_URL` is **never set** in this environment. If you find it set, stop and report it rather than working around it. `db.py` has exactly one switch to Postgres — `_using_postgres()` and `_get_pg()` read `DATABASE_URL` at call time — and no separate test-database setting, so a Postgres test can only reach Postgres by setting `DATABASE_URL` itself. Postgres tests therefore set it in-process with `monkeypatch.setenv("DATABASE_URL", ...)` from `TEST_DATABASE_URL`, **after asserting the host is `127.0.0.1` or `localhost`**. If `TEST_DATABASE_URL` is missing, those tests **fail, not skip** — a skipped test 4 reads as a passing one, which is the failure `test_app_imports.py` was written to prevent. The existing suite needs no database URL: the `client` fixture in `test_attachments.py` deletes `DATABASE_URL` and runs on a temporary SQLite file.

**Python.** Run everything with `/opt/ep-venv/bin/python`, which is 3.11 to match Render's 3.11.9:

```
cd backend && /opt/ep-venv/bin/python -m pytest -q
```

The system `python3` is 3.13, and the pinned `psycopg2-binary==2.9.9` has no wheel for it, so `pip install` there tries to build from source and fails. **Never change a pin in `requirements.txt` to make something install** — that file ships to production. **Starting baseline for Unit 3a:** Unit 2 merged at `bdaff67` with **372 passed, 0 skipped** on Python 3.11 with local PostgreSQL 16, verified by planning on 12 October. `main` has moved since then (documentation uploads, and possibly Sprint B work). **Run the suite on current `main` before changing anything, and report the count in your pre-code report.** A count below 372, or any skip, means the environment or `main` is wrong, not your work — stop and say which.

## 4. Rulings

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

### F. Rulings for Unit 2 (10 October 2026)

Unit 2 is the first unit that implements `docs/LICENCE_API_CONTRACT.md`. The contract fixes every wire shape; this section settles what it leaves to the server, and two places where the spec contradicts itself. Where this section and the spec disagree, this section wins.

**F1. Identity quality and match strength are different things.** Spec §8.2 uses "confidence" for both. Keep them apart in code:

- **Identity quality of one reading** — what `fingerprint_confidence` records at claim time. Computed from the normalised reading (§4.G) *and* the pool's active bindings:
  - a **usable UUID** is present, 32 hex digits, not all `0` or all `F`, and not carried by two or more active bindings in the pool;
  - a **usable serial** is a distinct normalised value among `system_serial`, `baseboard_serial`, `bios_serial` — `bios_serial` equal to `system_serial` counts once, because on most PCs it is the same number — and not carried by two or more active bindings in the pool;
  - **strong** = usable UUID and at least one usable serial; **weak** = exactly one of those two; **degenerate** = neither.
- **Match strength between a reading and one binding:**
  - **strong** = both have the same usable UUID *and* share at least one usable serial;
  - **weak** = exactly one of those holds;
  - **mismatch** = both have usable UUIDs that differ, and they share no usable serial;
  - **none** = no usable evidence on one side or the other.

All of these are **provisional constants in one module**, marked as such, until the school survey freezes them (spec §13 step 12).

**F2. A degenerate new PC versus a degenerate reinstall — the spec contradicts itself.** Spec §12 test 12 says two PCs with identical all-zeros UUIDs both activate. Spec §8.4 case E sends "degenerate against an existing degenerate binding" to ambiguity, which would refuse the second. Ruling: a **degenerate or weak reading** goes to ambiguity only when its **continuity signals** (§4.G) — the system disk's serial, or the permanent address of an onboard network card — equal those stored on **any active binding** in the same pool, whatever that binding's recorded quality. Otherwise it is a new machine and claims capacity like any other. *Any*, not only weak or degenerate bindings: a PC first activated with a strong reading whose firmware later stops reporting usable identifiers still has the same disk, and must go to review rather than take a second seat. Test 12 then holds (two different PCs have different disks), and test 13 holds (a reinstalled PC keeps its disk and its network card). The cost is deliberate and bounded: a degenerate PC that is reinstalled *and* has both its disk and its network card replaced looks new and takes a seat until the school releases the old activation — the ordinary dead-PC path, never a way to gain capacity.

**F3. Activation decision order.** After the gates in contract §5 (session, version, validation, `fingerprint_unreadable`, licensing available, pool access, pool active, subscription):

1. **Replay** (F4). A replay returns the original result.
2. **Clone check.** If the request carries an `installation_id` that belongs to an active binding whose match strength against this reading is not strong, log `clone_flagged` and ignore the ID. A presented installation ID is evidence, never identity.
3. **Exactly one active binding in the pool with a strong match** → restore (case D). More than one → ambiguity.
4. Otherwise, **any active binding with a weak match** → ambiguity.
5. Otherwise, a **weak or degenerate reading** whose continuity signals match **any** active binding in the pool (F2) → ambiguity.
6. Otherwise → `claim_capacity()` (case G), or `pool_full` (case H).

Revoked bindings are never candidates, which is spec rule F: release always beats recognition. An ambiguity creates a `licence_ambiguities` row (`origin = 'activate'`, the request ID, the candidate binding IDs), logs `ambiguous_flagged`, and returns `409 ambiguity_pending` with its ID. **Resolving and collecting ambiguities is Unit 3.** In this unit, a request carrying an `ambiguity_id` simply gets that row's current state back — `ambiguity_pending` or `ambiguity_expired` (7 days) — because nothing can resolve it yet.

**F4. `request_id` — storage, replay and the race.** Add to `institution_seats`: `activation_request_id`, `activation_request_at`, `activation_request_fp` (SHA-256 over the pool ID and the normalised reading, so a reused ID with a different body is detectable), `activation_result` (`activated` | `restored`) and `current_lease` (the lease string last issued). Add to `licence_ambiguities`: `origin` (`activate` | `refresh`), `request_id`, `consumed_at`, and allow `status = 'consumed'` (brief §4.C).

- A request whose `request_id` matches a binding or ambiguity in this pool within the last **24 hours**, with the same fingerprint, is a **replay**: return the stored outcome — `activation_result` and `current_lease`, byte-identical — and consume nothing. The same ID with a different pool or fingerprint → `409 request_id_reused`. Older than 24 hours → treated as a new request.
- **`claim_capacity()` takes the request ID and its fingerprint digest and stores them in the same insert.** After taking the pool lock, it first looks for an active binding in this pool already carrying that request ID and returns it instead of inserting. That closes the race between two concurrent retries of one activation; the existing lock already serialises them, so this costs one indexed query.
- If the binding was created but the lease was not yet issued (a crash between the two steps, F5), a replay finds the binding with no `current_lease` and issues it then. Recovery is the replay path; nothing else is needed.
- **Ambiguity creation must be race-safe too.** Invariant: two concurrent activation calls with the same `request_id` and the same body that both decide "ambiguity" create **exactly one** `licence_ambiguities` row and both return the **same** `ambiguity_id`. The pool lock covers new bindings only, so ambiguity creation needs its own atomic guard. **The mechanism is decided in H2:** `create_or_get_ambiguity()` under a `last_ambiguity_at` pool-row lock, with no unique index.

**F5. Leases.** Implement signing and verification as **pure functions** that take the key as an argument and read no configuration: `sign_lease(payload, private_key) -> str` and `verify_lease(lease, public_keys_by_kid) -> payload`, serialising exactly per contract §6.3. `verify_lease` is not used by an endpoint until Unit 3, but it is written now so the contract's §6.4 test vectors cover both directions in this unit.

Issue a lease by computing `issued_at = now` and `expires_at = min(now + 30 days, subscription_expires_at)`, both truncated to whole seconds, incrementing `lease_serial`, signing, and storing the string in `current_lease` along with `lease_serial`, `lease_issued_at` and `lease_expires_at`. For a restore, this happens in the same transaction as the binding update. For a new activation, `claim_capacity()` commits the binding first and the lease is issued in a second transaction (F4 covers the gap).

**F6. The signing key.** `config.py` reads `LICENCE_SIGNING_KEY` (standard base64 of the 32-byte Ed25519 seed) and `LICENCE_SIGNING_KID` (integer). Licensing is **unavailable** when the key is absent or malformed, when it equals the TEST seed published in contract §6.4, or when the kid is below 1. In that case every `/licence/*` endpoint returns `503 licensing_unavailable`, one ERROR is logged at startup, and `/health` reports `"licensing": "unavailable"`. Extend Unit 1's `/health` value to *schema ready **and** key ready*, still with no database query. The backend still starts (§4.E).

Tests never use a real key. Endpoint tests generate a key in-process and patch it in. The §6.4 vectors are tested against `sign_lease` and `verify_lease` directly with the TEST key. One test proves that configuring the TEST seed as `LICENCE_SIGNING_KEY` makes licensing unavailable.

Add `cryptography` to `requirements.txt`, pinned exactly, and confirm it installs from a wheel on Python 3.11. Nothing in production installs it today, so this is a new production dependency.

**F7. The activation session token.** Follow contract §11. Payload `{"typ": "ep.activation", "sub", "role", "iat", "exp"}`, where `role` is `institution_owner` or `exampartner_admin` as determined at session time (contract §11: the token carries the caller's identifier and role). It is encoded as base64url, then `.`, then an HMAC-SHA256 over the ASCII bytes of the payload segment, keyed with `HMAC(JWT_SECRET, "exampartner-activation-v1")`. Compare with `hmac.compare_digest`. **The `role` in the token is informational, not authoritative** — see the next point.

- **Ownership and admin status are re-checked at activate time**, with `require_account_owner()` against the pool's own account. A changed owner or a removed admin therefore takes effect immediately, not after 15 minutes.
- Verify the password by importing `_verify_password` from `routes/auth.py`. Do not move it or change it. Never call `register_device()`.
- The session endpoint lowercases and trims the identifier, as `/auth/login` does.

**F8. One error helper, used everywhere.** `licence_error(status, code, message, retryable, **extra)` raises `HTTPException` with the contract §9.3 body, including `server_time`. Every `/licence/*` route and `claim_capacity()` use it. Unit 1's plain-string refusals in `claim_capacity()` move to codes:

- `licensing_unavailable` (503)
- `pool_inactive` (409)
- `subscription_inactive` (402)
- `pool_full` (409), with `pool_size` and `active`
- `internal_error` (500) for the invariant

A missing pool becomes `pool_access_denied` (403) at the route — the contract returns the same code for a pool that does not exist. Update Unit 1's tests that assert on the old strings. Every success body also carries `server_time`. One helper formats wire timestamps (RFC 3339, UTC, whole seconds, `Z`); never send `now_iso()`'s format.

**F9. Client version.** `MIN_WINDOWS_APP_VERSION` in `config.py`, default `0.0.0`, so nothing is blocked until Nitoni sets it. Compare `MAJOR.MINOR.PATCH` numerically. A malformed `app_version` → `invalid_request`; one below the minimum → `426 client_upgrade_required` with `min_app_version`.

**F10. The restore path is a binding write.** In one transaction:
1. The live index check (the §4.E standing rule — restore changes `installation_id`).
2. Update `installation_id` to a fresh `token_hex(16)`, `last_seen_at` and the request columns (F4). **Keep the existing `machine_label`**; use the request's only if the binding has none (H6).
3. Ratchet the stored hash and signals snapshot forward on the strong match, as spec §8.6.1 does for refresh.
4. Issue the lease.
5. Log `restore`.

No capacity is claimed.

**F11. Pilot setup endpoints are in this unit.** Without them, the pilot school's account and pool could only be created by editing Neon by hand, which spec §10.4 forbids. Add three ExamPartner-admin endpoints from spec §10.1, authorised with the ordinary bearer token and `require_admin()`:

- `POST /institutions/accounts` — `name`, `owner_identifier`, optional `contact_email`, `account_type` `institution`. The owner must already be a registered user; the identifier is lowercased.
- `POST /institutions/accounts/{id}/seat_pools` — `pool_size`, optional `label`. Status `active`.
- `PATCH /institutions/accounts/{id}/subscription` — `subscription_status`, `subscription_expires_at`. Logs `renewal`.

They are not part of the client contract. Return plain `{"ok": true, …}` bodies and use ordinary `HTTPException` errors. Account editing and pool resizing wait for Unit 3.

### G. Fingerprint rules for Unit 2 (provisional)

Everything here lives in `services/fingerprint_service.py`, in one place, marked provisional. The client sends raw values (contract §7); none of this is the client's concern.

- **Normalise** every string: trim, upper-case. Map to absent:
  - the collector's `Clean-Value` placeholder list (`tools/fingerprint-survey/Collect-Fingerprint.ps1`), compared case-insensitively;
  - any value made of one repeated character (`0000000`, `FFFFFFFF`, `XXXXXXXX`);
  - `0123456789`, `123456789` and `1234567890`.

  Expect this list to grow from survey data. Growing it is a server change only.
- **UUID:** strip braces and hyphens and upper-case before the checks in F1.
- **Identity hash** (`machine_fingerprint_hash`): SHA-256, lowercase hex, over `key=value` pairs joined with `|`, in this fixed order, absent values as empty: `smbios_uuid`, `system_manufacturer`, `system_model`, `system_serial`, `baseboard_manufacturer`, `baseboard_product`, `baseboard_serial`, `bios_serial`. It is for audit and an exact-match fast path only. **Matching is signal by signal (F1), never by hash.**
- **Continuity signals — two sets, for two jobs (H1):**
  - **At activation** (F2, F3 step 5 — "is this a reinstall of a PC we know?"): only
    - the serial of the **one** disk marked `is_system_disk` — none if no disk is marked; never guess, and never use a USB disk; and
    - the network adapters whose `pnp_device_id` starts with `PCI\`. For each, use `permanent_address` when present, else `mac`, normalised to 12 hex digits. Ignore all-zero addresses and **locally administered** ones (bit `0x02` of the first byte set) — that is how Windows' random Wi-Fi addresses are marked.
  - **At refresh** (§4.A, Unit 3 — "is the same installation still running?"): those two **plus** `windows_machine_guid`.
  - `MachineGuid` is never an activation signal: unsysprepped images share it across a lab, and a reinstall changes it.

  Store the raw reading in `fingerprint_signals_json`, as spec §3.4 says, and derive these on demand.
- **The survey collector does not yet record the continuity fields.** `tools/fingerprint-survey/Collect-Fingerprint.ps1` records each disk's model, serial, interface and size and each adapter's name and MAC, but not `is_system_disk`, `pnp_device_id`, `permanent_address`, `media_type`, `index` or `collection_errors`. Survey files therefore exercise the **identity** rules but not the continuity rules. Write the continuity tests from constructed readings, not survey files. **Do not modify the collector** (§2): planning revises it separately, before the school survey and the client phase.
- **`fingerprint_unreadable`** when `collection_errors` names `Win32_ComputerSystemProduct`, `Win32_BaseBoard` or `Win32_BIOS`.

### H. Rulings on the Unit 2 pre-code report (11 October 2026)

The plan in your pre-code report is approved, with the rulings below. Where this section and your report differ, this section wins. §4.F, §4.G and §5 have been edited to match, so they no longer contradict it.

**H1. `MachineGuid` is not an activation continuity signal.** Your proposal is right, and for a stronger reason than the report gives. PCs imaged from one master copy without sysprep share the same `MachineGuid`, so counting it would send every such lab PC after the first to ambiguity. And a Windows reinstall changes it, so it cannot recognise a reinstall in the first place. **At activation (F2, F3 step 5), continuity is the system disk's serial and the onboard network cards' permanent addresses only.** `MachineGuid` stays in the **refresh** installation-continuity rule (§4.A, Unit 3), where the binding's own installation history accompanies it. §4.G now lists the two sets separately.

**H2. Race-safe ambiguity creation — your mechanism is approved.** All ambiguity creation goes through one function, `create_or_get_ambiguity()`:
1. `UPDATE seat_pools SET last_ambiguity_at = ? WHERE id = ?` — a meaningful write that takes the pool-row lock, held to commit. Comment it the way the `last_claim_at` lock is commented.
2. Under the lock, look up the request: the same `request_id` and `request_fp` within 24 hours → return that row's `ambiguity_id`; the same `request_id` with a different `request_fp` → `request_id_reused`.
3. Otherwise insert exactly one row and its `ambiguous_flagged` audit row, in the same transaction, and commit.

No unique index, for the reasons you gave. Unit 3's refresh-raised ambiguities must use the same function. Write that rule into `backend/CLAUDE.md` beside the `claim_capacity()` one.

**H3. Lock order — a new standing rule.** No transaction may hold a lock on a binding row and then take a lock on its pool row, or the reverse. Today they never meet: restore locks only the binding, while claims and ambiguity creation lock only the pool. Unit 3's refresh is the first code that could want both — a refresh that turns into an ambiguity — and acquiring them in opposite orders in two transactions can deadlock. If a path ever needs both, it takes the **pool first**. Record this in `backend/CLAUDE.md` too.

**H4. Cross-pool reuse — boundary.** A `request_id` reused **sequentially** in a different pool must give `request_id_reused`. Same-pool concurrent retries must be fully race-safe (H2 and F4). A broken client sending one `request_id` to two pools **at the same instant** is not guarded, and no global lock is to be added for it: it cannot push any pool past its entitlement, because `claim_capacity()` protects every pool independently.

**H5. Indexes and the request digest.** Approved:
- a best-effort index on `institution_seats(activation_request_id)` alone, replacing the two-column index the brief first named, because reuse must be detectable across pools;
- a best-effort index on `licence_ambiguities(request_id)`;
- a `request_fp` column on `licence_ambiguities`, storing the digest at creation. Never recompute it from stored signals, whose normalisation will change as the placeholder list grows.

None of these indexes is correctness-critical; they go in the ordinary best-effort list.

**H6. Restore keeps the existing machine label.** This changes F10's "the label if one was supplied". On a recognised restore, the binding's existing `machine_label` stands, because the owner may have renamed it, and a technician's typing at a reinstalled PC should not silently undo that. Use the request's `machine_label` only if the binding has none. Return the binding's actual label in the response, as contract §5 already provides, so the technician sees the PC's real name. A fresh activation uses the request's label as before. Renaming stays an explicit owner operation (Unit 3).

**H7. `clone_flagged` is logged against the activating account, not the matched one.** The presented `installation_id` may belong to a binding of a **different school**. Logging the event — with its signals and label snapshot — into that school's history would show one school's PC details in another school's owner-facing history (spec §10.2). Log it against the account of the pool being activated into, and put the matched `binding_id` in `detail_json`, where ExamPartner support can see it.

**H8. Confirmed as you proposed:**
- `subscription_inactive` carries `subscription_expires_at`, since the contract governs wire details;
- spec test 26 becomes 401 `activation_session_invalid` (the spec has been updated);
- the activation-session error order — version, then validation, then `licensing_unavailable`, then credentials, then `no_licensing_access`;
- reading the bearer token inside the route, so the 422-before-401 order holds;
- `cryptography==50.0.2` as the only new direct pin, with `cffi` and `pycparser` arriving as its dependencies;
- one guard-removed-by-hand check per race guard, as for spec test 6;
- the test showing one ambiguity per pool for a batch of boards sharing a real serial.

**H9. The secrets item is closed.** Nitoni has confirmed that `JWT_SECRET` and `ADMIN_IDENTIFIERS` are set on Render. The activation-token key derived from `JWT_SECRET` (F7) therefore rests on a real secret.

### I. Rulings for Unit 3a (19 October 2026)

Unit 3a makes a licensed PC **keep working**: it renews its lease, a school can free a seat, and a PC stuck in an ambiguity can be unstuck. It implements contract §9 (refresh) and §10 (collecting a resolved ambiguity), spec §8.5, §8.6, §8.6.1 and §10.4's ambiguity endpoints, and brief §9 R1 and R2. Where this section and the spec disagree, this section wins. The contract was amended on 19 October to match (contract §13); where the contract governs a wire detail, it wins.

**I1. Refresh — the decision order.** `POST /licence/refresh` takes **no `Authorization` header**; the lease is the authorisation. Never call `get_current_user()` on this route. After FastAPI's `422`, check in this order:

1. `client_upgrade_required` (426) → `invalid_request` (400: shape, `request_id`, `lease` a string, `fingerprint_signals` per `validate_signals()`) → `fingerprint_unreadable` (400) → `licensing_unavailable` (503).
2. **Verify the lease** with `verify_lease()` and the public key of the **configured** kid only. Malformed, wrong signature, or any other kid — including kid 0 — → `403 lease_invalid`, log `refresh_denied`. Key rotation is deferred (spec §8.7).
3. **Resolve the binding** by the payload's `binding_id`. If the payload's `account_id` or `seat_pool_id` differs from the binding's → `lease_invalid` (a correctly signed lease that disagrees with its binding is a server defect; log it at ERROR). Not found or **revoked** → `403 lease_revoked`, log `refresh_denied`. Release beats everything after this point.
4. **Superseded installation.** The payload's `installation_id` differs from the binding's **current** `installation_id` → `403 machine_mismatch`, log `refresh_denied` with `detail.reason = "installation_superseded"`. The binding was restored onto a newer installation (a reinstall, or a cloned image that activated later); this lease belongs to an installation that no longer holds the activation. The client's remedy for `machine_mismatch` — discard the lease and activate — is the right one: the same PC is restored without a new seat, a different one claims its own.
5. **Replay** (I4). The same logical refresh within 24 hours → return the binding's **current** lease byte-identical, issue nothing, flag nothing.
6. **Pool and subscription.** Pool not `active` → `409 pool_inactive`. Subscription not active or expired → `402 subscription_inactive` with `subscription_expires_at`. Log `refresh_denied` for both. The client keeps its current lease until its own expiry. This comes before collection, so no path issues a lease beyond the paid term.
7. **Collection** (I6) if the body carries `ambiguity_id`.
8. **Machine verification** (I2) → refresh, `machine_mismatch`, or an ambiguity.
9. **Stale serial.** The presented `lease_serial` is below the binding's `lease_serial` → log `stale_serial_flagged` and **carry on**. Evidence for a human, never a denial (spec §8.6.1). A replay (step 5) is never counted.
10. **Issue** (I3).

An **expired** presented lease is never a bar at any step (spec §8.6 step 5).

**I2. Machine verification.** Compare the presented reading with the **binding's stored snapshot**, never with the lease (spec §8.6.1), using `match_strength()` and a `PoolIndex` of the pool's active bindings, exactly as activation does.

- **strong** → refresh.
- **mismatch** (both have usable UUIDs that differ, and no usable serial is shared) → `403 machine_mismatch`, log `clone_flagged` against this binding's account with the presented signals, issue nothing, **leave the binding untouched**. This holds whether or not installation continuity also holds: a motherboard replacement is resolved by the school releasing the old activation (spec §8.6.1, contract §9.3).
- **weak** or **none** → the **installation-continuity rule** (brief §4.A). It holds when **all** of:
  - the lease's `installation_id` is the binding's current one (already guaranteed by I1 step 4);
  - the presented `windows_machine_guid`, normalised, equals the snapshot's and is not absent;
  - **if** the snapshot carries a system-disk serial or an onboard network address (`Reading.activation_disk`, `activation_nics`), at least one of them is shared with the presented reading. A snapshot with neither relies on the first two alone.

  Continuity holds → refresh. Continuity broken → an ambiguity, `origin = 'refresh'`, the binding as its one candidate, through `create_or_get_ambiguity()` (H2) → `409 ambiguity_pending`. The client keeps its current lease until expiry.

**This extends brief §4.A on purpose**, the way F2 extended spec §8.4 case E: the continuity rule applies to **any** binding whose match is weak or none, not only bindings recorded `weak` or `degenerate` at claim. A PC activated with strong identity whose firmware later stops reporting a serial — a BIOS update that resets it to a placeholder is the realistic case — is still the same running installation, and must not join the review queue every month for it. The rule needs the same installation, the same `MachineGuid` and the same disk or network card, so a copied lease on a cloned-image PC still fails it: the disk and network card differ.

"At least one of disk or NIC", rather than both, is what makes the §4.A ratchet survive maintenance: a disk swapped by cloning the old disk keeps the installation and the `MachineGuid`, so it passes on the network card and then ratchets the new disk in.

**I3. Issuing a refreshed lease, and the ratchet.** One transaction, locking the **binding row only** (H3):

1. Take the binding-row lock with a meaningful write, as `_restore()` does.
2. Under the lock, re-read the binding and re-check revoked (→ `lease_revoked`), the installation (→ `machine_mismatch`) and the replay (→ return current lease). A release or restore that committed while this request was deciding wins.
3. **Ratchet.** If the recomputed identity hash or the stored snapshot differs from the presented reading, write the presented reading's hash and raw signals onto the binding — on a strong match (spec §8.6.1) **and** on a continuity refresh (§4.A). `fingerprint_confidence` is **not** changed: it records quality at claim time (spec §3.4).
4. `lease_serial + 1`, `expires_at = min(now + 30 days, subscription_expires_at)`, sign with `_sign_for_binding()`, store `current_lease` and the lease columns, set `last_seen_at`, store the refresh request (I4).
5. **Log** — spec §3.5 says do not log every refresh. Log `refresh` only when the presented lease had expired, when `last_seen_at` was null or more than 7 days old, or when step 3 ratcheted (put the previous hash in `detail_json`). Otherwise update `last_seen_at` silently.

No capacity is touched, no binding is created, and `installation_id` does not change, so the §4.E live index check is not required on this path.

**I4. One authoritative request history (brief §9 R1).** R1 is mandatory and its five tests stand. In addition:

- The history covers **every** state-changing licensing request: activation, restore, refresh, ambiguity collection on either endpoint, **release (I7) and ambiguity resolution (I5)**. Record each with its `request_id`, its operation, the pool, the binding or ambiguity it concerns, its request digest, the outcome a replay must return, and when it arrived.
- **A refresh replay** is the same `request_id`, the same binding and the same digest within 24 hours. Return the binding's **current** lease unchanged (contract §9.2) — even when a later refresh under a different `request_id` has issued a newer one, since issuing again is the thing a replay must never do. Not a stale-serial signal.
- **The refresh digest** covers the binding ID, the normalised reading and the `ambiguity_id`, and is computed once and stored (H5). Not the lease bytes: a retry presents the same old lease, but a client that lost its response may hold either.
- **Cross-operation reuse** — a `request_id` seen in the last 24 hours under a different operation, pool, binding or digest → `409 request_id_reused`. This holds across endpoints: an activation `request_id` reused for a refresh is refused.
- **Propose the storage in your pre-code report** (brief §9 R1). A request-log table keyed by `request_id` is the expected shape. Its index is best-effort, like H5's: correctness rests on the lookup, and H4 still applies — no global lock.
- Do **not** retire Unit 2's request columns on `institution_seats` or `licence_ambiguities`. Write the history alongside them, and read it first.
- **The history fails closed** (contract §11). It is correctness-critical: without it a retry can consume a second seat or a reused ID can be mistaken for a replay. So:
  - its table is created in the **licensing DDL step** (§4.E), and a failure there records licensing unavailable, like any other licensing schema failure;
  - if it cannot be read or written at request time — the table missing after startup, or any database error from it — the operation returns **`503 licensing_unavailable`**, rolls back, and writes nothing: no binding, no lease, no log row, no ambiguity change, no release;
  - it **never** falls back to the per-binding request columns, and never surfaces a raw database `500`;
  - the rest of the backend is untouched: `/auth`, CBT, Study and every non-licensing route keep working, and `/health` still makes no database query (§4.E). It reports what startup recorded.

  On Postgres, a failed statement aborts the transaction, so roll back before raising the `503`.

**I5. Ambiguity resolution — `POST /support/licensing/ambiguities/{id}/resolve`.** ExamPartner admin only (`require_admin()`, ordinary bearer token). Body `{"request_id": "<UUID v4>", "decision": "recognize_existing" | "treat_as_new", "binding_id": "…"}`. `request_id` is required. `binding_id` is required for `recognize_existing` and refused otherwise. Logs `ambiguity_resolved`, with the decision and binding in `detail_json`, `actor_role = 'exampartner_admin'`. Errors use `licence_error()` and the contract §9.3 body; success is `{"ok": true, "ambiguity_id", "status", "resolution", "resolved_binding_id", "resolved_at", "server_time"}`.

- **Order:** `401` without a valid bearer token → `403` for a non-admin → `400 invalid_request` → `503 licensing_unavailable` → an unknown `ambiguity_id` → `404 ambiguity_not_found` → the request history (I4) → the state checks below.
- **The request history.** The same `request_id` with the same ambiguity, decision and binding within 24 hours **replays the stored outcome** — success or the refusal it got — and changes nothing. The same `request_id` with any other body, or one used by any other operation → `409 request_id_reused`.
- **Only an `open` ambiguity younger than 7 days can be resolved.** A new request against any other state gets exactly one of:

  | State | Response |
  |---|---|
  | `open`, 7 days or older, or `expired` | `409 ambiguity_expired`. An expired-but-still-`open` row may be marked `expired` at this point. |
  | `resolved` — **including** by an identical decision under a different `request_id` | `409 ambiguity_already_resolved`, with `resolution`, `resolved_binding_id` and `resolved_at` in the body, so the admin sees what was decided |
  | `consumed` | `409 ambiguity_consumed`, with the same three fields |

  A resolution is never changed once made. To undo one, the school releases the binding.
- **`recognize_existing`.** The `binding_id` must be one of the ambiguity's candidates **and still active**. Otherwise → `409 ambiguity_candidate_invalid`, with the ambiguity's candidate IDs in the body (a released candidate can never be recognised: rule F). This check follows the state checks above, so a resolved, consumed or expired ambiguity reports its state first. Records the decision only; the restore or ratchet happens when the PC collects (I6), so a PC that never comes back changes nothing.
- **`treat_as_new`, activation-raised.** Capacity is claimed **at resolution**, through `claim_capacity()` — the single chokepoint — with the ambiguity's presented signals, hash and label, `performed_by` the admin, `actor_role = 'exampartner_admin'`, and `fingerprint_confidence` computed from the presented reading against the pool now. The new binding becomes `resolved_binding_id`; its lease is issued when the PC collects. Claiming now means a full pool is reported to the admin, who can tell the school to release a PC, rather than discovered later by the technician. `pool_full`, `pool_inactive` and `subscription_inactive` come back to the admin, and the ambiguity stays `open`.
- **`treat_as_new`, refresh-raised.** The admin has decided the PC presenting the lease is **not** the binding's machine. Nothing is claimed. The decision is recorded, and collection returns `403 machine_mismatch` (I6), so the PC goes through ordinary activation with the owner's sign-in and claims its own seat there. The refresh response has no `installation_id` field (contract §9.1), so a refresh can never hand a PC a new binding.
- **Invariants — propose the mechanism in your pre-code report:** exactly one resolution per ambiguity, even with two concurrent resolve calls — the loser gets `ambiguity_already_resolved`, or the stored outcome if it is a replay of the winner's request; a `treat_as_new` claim never leaves an orphan binding behind a failed status update, or a resolved status behind a failed claim; and `claim_capacity()` stays the only path that creates a binding. Where a path needs the pool lock and a row lock, take the **pool first** (H3).

**I6. Collecting a resolved ambiguity (contract §10).** Keep Unit 2's identity checks exactly, in their order — wrong pool or origin → `ambiguity_mismatch`; the raising `request_id` → `request_id_reused`; a different digest → `ambiguity_mismatch` — and add the I4 history check. Then, by state:

| State | Activation-raised (`activate` + `ambiguity_id`) | Refresh-raised (`refresh` + `ambiguity_id`) |
|---|---|---|
| `open`, under 7 days | `409 ambiguity_pending` | `409 ambiguity_pending` |
| `open`, 7 days or older, or `expired` | `409 ambiguity_expired` | `409 ambiguity_expired` |
| `consumed` | `409 ambiguity_consumed` — unless this is a replay (I4), which returns the original result | same |
| resolved, its binding now revoked | `403 lease_revoked` | `403 lease_revoked` |
| resolved `recognize_existing` | **Restore** onto the resolved binding as `_restore()` does — new `installation_id`, ratchet, lease, `restore` log — and return `200`, `result: "restored"` | **Refresh** the lease's binding through I3, ratcheting the presented reading in, and return `200`, `result: "refreshed"` |
| resolved `treat_as_new` | Issue the new binding's lease with `_ensure_lease()`, return `200`, `result: "activated"` | `403 machine_mismatch` |

- **Refresh-raised collection** also requires the lease to verify (I1 steps 2–4) and its `binding_id` to be the ambiguity's candidate. Otherwise → `ambiguity_mismatch`.
- **Single use.** The transaction that issues the lease marks the ambiguity `consumed` with `consumed_at`, conditionally on its still being `resolved`. Two concurrent collections of one resolved ambiguity issue **one** lease: the loser replays it if it is the same logical request, and gets `ambiguity_consumed` otherwise. `recognize_existing` restores lock the binding row; nothing here takes the pool lock.
- The collected restore changes `installation_id`, so it performs the §4.E live index check.
- A resolved ambiguity that is never collected does not expire. A `treat_as_new` binding with no lease is an ordinary active binding: the school releases it if the PC never returns.

**I7. Release — `POST /institutions/seats/{binding_id}/release`** (spec §8.5).

- **Authorisation:** an ordinary bearer token, then `require_account_owner()` against the **binding's own** account (spec §9: resolve the object, then check the caller owns it). An unknown binding and another school's binding return the **same** `403 binding_access_denied` — no existence leak. No token → `401 not_authenticated`. A lease is not a bearer token; spec test 28 is the proof.
- **Body:** `{"request_id": "<UUID v4>", "revoke_reason": "manual" | "replacement" | "hardware_failure" | "retired" | "renewal_retire" | "support" | "other", "reason": "free text, at most 500 characters"}`. `request_id` is **required**; the other two are optional, `revoke_reason` defaulting to `manual`. A missing or malformed `request_id`, or an unknown `revoke_reason` → `400 invalid_request`. **Neither reason field ever gates the release** (spec §8.5).
- **Order:** `401 not_authenticated` → `400 invalid_request` → `503 licensing_unavailable` → `403 binding_access_denied` → the request history (I4) → the effect.
- **The request history.** Release is the first owner operation the WinUI client will call (Unit 3b), so it follows contract §9.2 like every client operation. The same `request_id`, binding, `revoke_reason` and `reason` within 24 hours **replays the stored response** byte-for-byte, including the original `result` and pool counts. The same `request_id` with a different body, or one used by any other operation → `409 request_id_reused`. Authorisation runs **before** the replay check, so a replay never shows one owner another school's response.
- **Effect**, in one transaction locking the **binding row only**: set `revoked_at`, `revoke_reason` and `revoked_by`, and log `release` with the binding's snapshots and `reason`. Capacity is free the moment it commits. Pending ambiguities naming the binding as a candidate are left alone; they fail at resolution or collection because the binding is revoked.
- **Idempotent by state, too:** a **new** `request_id` releasing an already released binding returns `200` with `result: "already_released"` and the original `released_at`, records the request, and writes nothing else.
- **Response `200`:** `{"result": "released" | "already_released", "binding_id", "released_at", "seat_pool_id", "pool_size", "active", "available", "over_capacity", "server_time"}`, with the pool counts read **after** the commit, and wire timestamps (F8).
- **Errors use `licence_error()` and the contract §9.3 body** (F8), not plain `HTTPException` strings. Unit 3b writes this endpoint into contract v1.1 exactly as built here, so build it to that standard now.
- **Not in 3a:** accepting the licence session token on this route. That is Unit 3b (I9).

**I8. Admin visibility — `GET /support/licensing/ambiguities`.** ExamPartner admin only. Query `status`, default `open`. For each ambiguity: `ambiguity_id`, `account_id`, `account_name`, `seat_pool_id`, `pool_label`, `origin`, `machine_label`, `created_at`, `expires_at` (`created_at` + 7 days), `status`, the presented reading **normalised** (UUID, serials, system disk, onboard addresses, `MachineGuid`), and each candidate's `binding_id`, `machine_label`, `last_seen_at`, `fingerprint_confidence`, whether it is still active, and its match strength against the presented reading. That is everything Nitoni needs to decide by PowerShell. Plain `{"ok": true, …}` bodies, like F11. The clone and stale-serial review list is Unit 3b.

**I9. What Unit 3b will add, so you do not build it now.** Recorded so the 3a code does not close it off:
- **The licence session token** (Nitoni's decision, 19 October). The 15-minute token from `/licence/activation-session` is extended from activation-only to **licensing-only management**. It will authorise activate, the owner's machine list, owner-visible history, rename and **release**, and nothing else: not ambiguity resolution, account editing, subscription changes, pool resizing, the support endpoints, or any non-licensing route. It stays short-lived, in memory on the client, bound to the signed-in identity and the accounts it may manage, and useless on every Android route. The owner endpoints **also keep** accepting the ordinary bearer token, for PowerShell and support use. The path `/licence/activation-session` stays.
  - **For 3a:** keep authorisation in `release` in one small function, so 3b can add the second token type without restructuring the route. Do not change `F7`'s token or its verification.
- Contract v1.1: the owner endpoints, written into the contract, with I7's release shape, `request_id` rule and error codes unchanged. The codes 3a introduces — `not_authenticated`, `binding_access_denied` on release, and `ambiguity_not_found`, `ambiguity_already_resolved` and `ambiguity_candidate_invalid` on the admin resolve route — go into v1.1's §9.3 as built.
- The owner reads, rename, account editing, pool resizing, the signals review list, and the test-clock endpoint.

**I10. Tests use the in-process clock.** Spec tests 15–19 shift time with `licensing_time.set_clock_offset()` under `ALLOW_TEST_CLOCK` patched true, as Unit 1 made possible. The test-clock **endpoint** is Unit 3b, and `ALLOW_TEST_CLOCK` stays absent in production.

## 5. The current unit of work — Unit 3a: a licensed PC keeps working

**Units 1 and 2 are done.** Unit 1 (time helpers, schema, owner check, `claim_capacity()`) merged as PR #37 (`3a8e313`). Unit 2 (fingerprint rules, activation session, activate and restore, lease signing, error envelope, pilot setup endpoints) merged as PR #38 (`bdaff67`) on 12 October 2026, and is **deployed to production**: the kid 1 signing key is set, and `/health` reports `"licensing": "ready"`.

**Unit 3 is split in two for review, not for scope.** Nitoni's decision, 19 October 2026: **the whole of Unit 3 is finished before 4 November 2026**, the last day this Cloud Session is available, and before the first school trial on 10 November. Unit 3a comes first; Unit 3b is briefed as soon as 3a's PR is open. Nothing in Unit 3 is deferred past the trial. Plan your work so 3a's PR can be opened within about a week.

**Unit 3a builds**, under §4.I and contract §9–§10:

1. **`POST /licence/refresh`** — the I1 decision order, I2 machine verification, I3 issuing and the ratchet, and stale-serial flagging. Register it in `routes/licensing.py`.
2. **One request history** for activation, restore, refresh, ambiguity collection, release and ambiguity resolution — brief §9 R1 and I4. Schema, both engines, idempotent as always.
3. **Ambiguity resolution** — `POST /support/licensing/ambiguities/{id}/resolve` (I5).
4. **Collecting a resolved ambiguity** on both `activate` and `refresh` (I6), replacing Unit 2's report-the-state-only behaviour.
5. **Release** — `POST /institutions/seats/{binding_id}/release` (I7).
6. **`GET /support/licensing/ambiguities`** (I8).
7. **Brief §9 R2** — tighten the ambiguity race test so removing the `last_ambiguity_at` lock fails it every time.
8. **`backend/CLAUDE.md`** — add the refresh path to the H3 lock-order note (it takes the binding lock, and only `create_or_get_ambiguity()` takes the pool lock, never both in one transaction), and a line that every state-changing licensing request records itself in the request history, which fails closed with `503` and is never bypassed (I4).

**Not in this unit** (Unit 3b, briefed next, still before 4 November):
- the licence session token extension (I9);
- the owner endpoints `GET /institutions/accounts/{id}/seat_pools`, `…/machines` and `…/history`, and rename (`PATCH /institutions/seats/{binding_id}`);
- account editing (`PATCH /institutions/accounts/{id}`) and pool resizing (`PATCH /institutions/seat_pools/{id}`);
- `GET /support/licensing/signals` (the clone and stale-serial review list);
- `POST /support/licensing/test-clock`;
- contract v1.1.

Do not build any of these, even where the code would be convenient to write now.

**Tests — write these first, where they pin behaviour:**

- **Spec §12, through the real routes:**
  - **8** — dead PC, old installation unreachable, owner releases → revoked, capacity free immediately, a replacement activates with no extra seat;
  - **9** — now with a real release instead of setting `revoked_at` by hand;
  - **14** — admin resolves `recognize_existing` → the PC collects, is restored onto the right binding, no new seat, `ambiguity_resolved` logged;
  - **15–19** — the lease and expiry tests, on the in-process clock (I10). **16** is the one that matters most: a 40-day-old expired lease self-recovers with no administrator;
  - **20** — stale serial: logged, not denied;
  - **21–25** — machine verification. In **24**, test both halves: continuity broken → ambiguity; continuity intact → refreshed, with the snapshot ratcheted;
  - **27** — owner A cannot release owner B's binding: `binding_access_denied`, identical to an unknown binding;
  - **28** — a lease presented as a bearer token cannot release anything.
- **Brief §9 R1's five tests**, plus:
  - an activation `request_id` reused for a refresh → `request_id_reused`;
  - a refresh retried after a lost response → the same lease, byte-identical, serial unchanged, no `stale_serial_flagged`;
  - a refresh retried after a later refresh has issued a newer lease → the current lease, nothing issued.
- **Refresh decision order:**
  - kid 0 (the TEST key) and an unknown kid → `lease_invalid`;
  - a revoked binding → `lease_revoked`, ahead of every later check;
  - a superseded installation → `machine_mismatch` with `installation_superseded` logged;
  - a suspended pool → `pool_inactive`; a lapsed subscription → `subscription_inactive`;
  - **the I2 extension:** a binding recorded `strong` whose firmware now reports a placeholder serial, with continuity intact → refreshed, not an ambiguity;
  - a copied lease on a cloned-image PC — same `installation_id` and `MachineGuid`, different disk and network card → not refreshed;
  - a disk cloned onto a new disk — same installation and `MachineGuid` and network card, new disk serial → refreshed, the new disk ratcheted in, and the next refresh still passes.
- **Resolution and collection:**
  - `treat_as_new` on a full pool → `pool_full` to the admin, the ambiguity still `open`;
  - `treat_as_new` activation-raised → the PC collects `activated` and the pool has one more active binding;
  - `treat_as_new` refresh-raised → collection returns `machine_mismatch`, nothing claimed;
  - `recognize_existing` on a candidate released before resolution → `ambiguity_candidate_invalid`; released after resolution → collection returns `lease_revoked`;
  - collecting twice → one lease, then `ambiguity_consumed`;
  - an ambiguity older than 7 days → cannot be resolved, and collects as `ambiguity_expired`.
- **Concurrency, on Postgres, barrier-synchronised as in test 4**, each with its guard removed by hand to show it fails:
  - two concurrent refreshes with the same `request_id` → one lease issued, both responses identical;
  - two concurrent refreshes with different `request_id`s → two leases, serials N+1 and N+2, never two with the same serial;
  - two concurrent collections of one resolved ambiguity → one lease;
  - two concurrent `resolve` calls → one resolution, and at most one binding claimed;
  - a release concurrent with a refresh → the refresh either completes before the release or returns `lease_revoked`, never a lease issued after `revoked_at`.
- **Release:** the same `request_id` retried → the original response byte-for-byte; a new `request_id` on a released binding → `already_released`; the same `request_id` with a different `revoke_reason` → `request_id_reused`; an activation's `request_id` reused for a release → `request_id_reused`; a missing `request_id` → `invalid_request`; the pool counts in the response are post-commit; capacity freed by release is immediately claimable (spec test 5 still passes).
- **Resolution:** `recognize_existing` naming a binding that is not a candidate, or a candidate released before resolution → `ambiguity_candidate_invalid`, the ambiguity still `open`; the same `request_id` retried → the stored outcome, nothing changed; a second resolution under a new `request_id`, whether the same decision or a different one → `ambiguity_already_resolved` with the first resolution's fields; resolving a consumed one → `ambiguity_consumed`; a refresh's `request_id` reused for a resolution → `request_id_reused`.
- **The history fails closed.** After startup has recorded licensing ready, drop or rename the request-history table, then call activate, refresh, collection on both endpoints, release and resolve. Each returns `503 licensing_unavailable` with the §9.3 body, and **nothing is written**: binding count, lease serials, log rows, ambiguity rows and `revoked_at` are all unchanged. In the same state, `/health` returns `200` and a non-licensing route (for example `/auth/login` or the content-version route) still works. Restore the table and every operation works again. On Postgres, and on SQLite if practical, as for test 31.
- **Every response carries `server_time`**, and every new error body the §9.3 shape.
- **Test 4 still passes, spec test 6 by hand still fails without the lock**, and the R2-tightened ambiguity race test fails every time without its lock.

**The pre-code report** comes first, as for every unit (§6, §7). It must include:
- the baseline test count on current `main` (§3);
- the storage for the request history (I4, brief §9 R1);
- the mechanism for the I5 resolution invariants;
- how each new path follows H3, path by path;
- any drift between this brief, the contract and the code.

Then wait for go-ahead. Implement to §4.F–§4.I and the contract. **Stop at the end of this unit** and open the PR on `licensing/unit-3a`.

**Deployment note for Nitoni, not for you:** Unit 3a adds tables or columns, so the next manual deploy runs its schema step on Neon. Nothing needs setting on Render. Keep `ALLOW_TEST_CLOCK` absent.

## 6. How to work

- Follow `backend/CLAUDE.md` exactly. The conventions in it are not style preferences; most of them are failure modes that are silent when you get them wrong.
- Do not refactor adjacent code, reorder imports, or reformat files. Change what the task needs.
- Do not condense or delete existing comments. They record production failures and are the only record of several hard-won decisions.
- Add `cryptography` to `requirements.txt` in Unit 2, for lease signing, pinned exactly — it ships to production, and nothing installs it today. Add nothing else to `requirements.txt` without saying so in the pre-code report.
- Run the test suite before opening the PR. `test_app_imports.py` and `test_no_undefined_names.py` must stay green. **A skip does not count as green.** `pyflakes` is in `requirements-dev.txt` since Unit 1, so `test_no_undefined_names.py` runs rather than skips.
- In the PR description, state what you verified by running versus what you believe by reading. Keep those separate.

## 7. If you find another gap

Do what you just did: stop, state it, propose a fix, wait. That is the behaviour this project wants, and it has already caught three real defects plus one that would have broken every degenerate lab PC.

Two qualifications. Be specific about why it blocks code rather than listing everything you noticed — a gap that only changes a docstring is not a blocker. And when the fix is yours to judge and reversible, propose it, implement it behind a clear note in the PR, and let review catch it; reserve stopping for decisions that are expensive to undo or that change product behaviour.

## 8. Not yours to decide — raised with Nitoni separately

Items listed here are not yours to act on; they may change the spec under you. **None is open at the moment.** The licence session token (I9) is decided and is Unit 3b's; do not start it in 3a. Closed since the last revision: the boot behaviour (decided — §4.E), Render deployment (manual), and the architecture handoff: `docs/ExamPartner_Windows_SeatPool_Architecture_Handoff.md` (updated 27 September) already carries the fingerprint correction, and spec §0 now cites that file.

## 9. Carried into Unit 3 — mandatory

**Now part of Unit 3a** (§5, §4.I I4). R1's five tests and R2 stand unchanged; I4 extends R1 to every Unit 3a state-changing licensing operation — activation, restore, refresh, ambiguity collection, release and ambiguity resolution — and makes the history fail closed.

Recorded on 12 October 2026, when PR #38 (Unit 2) was approved for merge with this limitation known. **These are correctness requirements for Unit 3, not optional clean-up.**

**R1. One authoritative 24-hour request-ID history.** Unit 2 keeps only the **latest** activation or restore `request_id` on the binding (`institution_seats.activation_request_id`), and ambiguity collection checks only the target ambiguity's own raising ID. So:
- after a restore overwrites a binding's request ID, the earlier activation's ID drops out of view and a reuse of it is treated as new;
- ambiguity collection can accept a `request_id` that was used within 24 hours by another binding or another ambiguity, including in another pool.

Neither can consume a seat, issue a lease, alter an ambiguity, cross a school boundary or reveal another PC's state in Unit 2 — collection there only reports pending or expired. In Unit 3 it becomes correctness-critical, because collecting a **resolved** ambiguity restores or creates a binding and issues a lease.

**Rule:** before any activation, restore, refresh, ambiguity collection, release or ambiguity resolution performs or returns an idempotent outcome, the server must be able to tell whether that `request_id` has been used **anywhere** in the preceding 24 hours — including earlier activation and restore IDs that are no longer a binding's latest, and IDs used by ambiguities. The same logical request → replay its stored outcome. A different pool, body or operation → `409 request_id_reused`. Do **not** rely solely on `institution_seats.activation_request_id`. Propose the storage — for example a request log keyed by `request_id` — in the Unit 3 pre-code report. It is a sequential-misuse check: H4 still applies, and no global lock is to be added.

**Required tests:**
1. Ambiguity collection reusing a recent successful activation `request_id` from the **same** pool → `request_id_reused`.
2. Ambiguity collection reusing a recent `request_id` from **another** pool → `request_id_reused`.
3. Ambiguity collection reusing **another ambiguity's** `request_id` → `request_id_reused`.
4. A genuinely new collection `request_id` from the raising PC → the same ambiguity outcome.
5. Activate with R1, then restore with R2, then reuse R1 within 24 hours → replayed or refused according to R1's **original** operation, never treated as a new request.

**R2. The ambiguity race test should fail every time when its guard is removed.** With the `last_ambiguity_at` lock removed, the Unit 2 race test failed 2 of 3 iterations in planning's re-run, because its barrier sits before `create_or_get_ambiguity()` rather than between that function's lookup and its insert. Tighten it so removing the lock fails deterministically, as test 4 does for the claim lock.

