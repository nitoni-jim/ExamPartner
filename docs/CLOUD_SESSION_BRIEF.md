# Cloud session brief — ExamPartner Windows licensing

**For:** the implementation session working on `nitoni-jim/ExamPartner`
**Owner:** Nitoni (solo developer; works on this in evenings and weekends)
**Date:** 5 October 2026 · **Revised:** 6 October 2026 (§3 environment, §6 pyflakes) · 7 October 2026 (§4.D rulings on the pre-code report, §8) · 8 October 2026 (§4.E licensing never stops the backend starting, §8; §2 signing key per the API contract) · 10 October 2026 (Unit 1 merged; Unit 2 — §4.F, §4.G, §5 — reviewed and approved)

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

**Branch and deploy.** Unit 1's branch `licensing/pilot-v1` is merged and deleted. Work on a new branch per unit, cut from current `main`: **`licensing/unit-2`** for this one. Open a PR. Never push to `main`, never merge. Nitoni reviews and merges in the GitHub web UI.

While your branch is open, Nitoni does not hand-upload files under `backend/` through the GitHub website. If you see an unexpected commit touching your files, stop and say so rather than merging around it.

**Postgres.** The environment's setup script provisions a throwaway Postgres 16 at `127.0.0.1:5433/ep_test` and a Python 3.11 venv at `/opt/ep-venv`. Do not install or initialise Postgres yourself. Run `ep-pg-start` at the start of every session — it is idempotent, and it is needed because a running server does not survive the cached environment snapshot. **Do not touch Neon**, not even a dev branch. Your tests create and drop tables; production holds live paying users' data.

`DATABASE_URL` is **never set** in this environment. If you find it set, stop and report it rather than working around it. `db.py` has exactly one switch to Postgres — `_using_postgres()` and `_get_pg()` read `DATABASE_URL` at call time — and no separate test-database setting, so a Postgres test can only reach Postgres by setting `DATABASE_URL` itself. Postgres tests therefore set it in-process with `monkeypatch.setenv("DATABASE_URL", ...)` from `TEST_DATABASE_URL`, **after asserting the host is `127.0.0.1` or `localhost`**. If `TEST_DATABASE_URL` is missing, those tests **fail, not skip** — a skipped test 4 reads as a passing one, which is the failure `test_app_imports.py` was written to prevent. The existing suite needs no database URL: the `client` fixture in `test_attachments.py` deletes `DATABASE_URL` and runs on a temporary SQLite file.

**Python.** Run everything with `/opt/ep-venv/bin/python`, which is 3.11 to match Render's 3.11.9:

```
cd backend && /opt/ep-venv/bin/python -m pytest -q
```

The system `python3` is 3.13, and the pinned `psycopg2-binary==2.9.9` has no wheel for it, so `pip install` there tries to build from source and fails. **Never change a pin in `requirements.txt` to make something install** — that file ships to production. **Starting baseline for Unit 2:** current `main` at `3a8e313` (Unit 1 merged), verified by running on Python 3.11 with local PostgreSQL 16: **160 passed, 4 warnings, 0 skipped** — the 103 pre-licensing tests plus Unit 1's 57. Any count below 160 before you change anything means the environment, not your work, is wrong.

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
- **Ambiguity creation must be race-safe too.** Invariant: two concurrent activation calls with the same `request_id` and the same body that both decide "ambiguity" create **exactly one** `licence_ambiguities` row and both return the **same** `ambiguity_id`. The pool lock covers new bindings only, so ambiguity creation needs its own atomic guard. **Propose the mechanism in your pre-code report**; do not pick one silently. If it involves a unique index, say how it relates to the §4.E rule on correctness-critical indexes.

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
2. Update `installation_id` to a fresh `token_hex(16)`, `last_seen_at`, the label if one was supplied, and the request columns (F4).
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
- **Continuity signals** (brief §4.A and F2):
  - `windows_machine_guid`;
  - the serial of the **one** disk marked `is_system_disk` — none if no disk is marked; never guess, and never use a USB disk;
  - the network adapters whose `pnp_device_id` starts with `PCI\`. For each, use `permanent_address` when present, else `mac`, normalised to 12 hex digits. Ignore all-zero addresses and **locally administered** ones (bit `0x02` of the first byte set) — that is how Windows' random Wi-Fi addresses are marked.

  Store the raw reading in `fingerprint_signals_json`, as spec §3.4 says, and derive these on demand.
- **The survey collector does not yet record the continuity fields.** `tools/fingerprint-survey/Collect-Fingerprint.ps1` records each disk's model, serial, interface and size and each adapter's name and MAC, but not `is_system_disk`, `pnp_device_id`, `permanent_address`, `media_type`, `index` or `collection_errors`. Survey files therefore exercise the **identity** rules but not the continuity rules. Write the continuity tests from constructed readings, not survey files. **Do not modify the collector** (§2): planning revises it separately, before the school survey and the client phase.
- **`fingerprint_unreadable`** when `collection_errors` names `Win32_ComputerSystemProduct`, `Win32_BaseBoard` or `Win32_BIOS`.

## 5. The current unit of work — Unit 2: a PC can be activated

**Unit 1 is done.** It was spec §13 steps 2–5: time helpers, schema, owner check and `claim_capacity()`. It merged as PR #37 (`3a8e313`) on 10 October 2026, is deployed, and `/health` reports `"licensing": "ready"` on production.

**Unit 2 builds everything the Windows client needs in order to activate**, to `docs/LICENCE_API_CONTRACT.md`, under the rulings in §4.F and §4.G:

1. **Configuration** — `LICENCE_SIGNING_KEY`, `LICENCE_SIGNING_KID` and `MIN_WINDOWS_APP_VERSION` in `config.py`; key loading and validation (F6, F9).
2. **`services/fingerprint_service.py`** — normalisation, identity quality, match strength, continuity signals and the identity hash (F1, G).
3. **Lease signing** — the pure `sign_lease` and `verify_lease` functions and the serialisation (F5, contract §6), plus `cryptography` in `requirements.txt`.
4. **The activation session token** — issue and verify (F7, contract §11).
5. **The error helper and wire timestamps** (F8).
6. **Schema additions** — the F4 columns, both engines, idempotent as always, plus a best-effort index on `institution_seats(seat_pool_id, activation_request_id)`.
7. **`claim_capacity()` changes** — the request ID and fingerprint digest (F4), the structured errors (F8), and a `fingerprint_confidence` passed in from the identity-quality calculation.
8. **`routes/licensing.py`** — `POST /licence/activation-session` and `POST /licence/activate` exactly as contract §4–§5, implementing F3 and F10. Register the router in `app.py`.
9. **The three pilot setup endpoints** (F11).
10. **`/health`** — licensing is ready only when the schema is ready *and* the key is loaded (F6).

**Not in this unit** (Unit 3):
- `POST /licence/refresh` and machine verification on refresh;
- the refresh ratchet, stale-serial signalling and refresh request IDs;
- resolving and collecting ambiguities, and the support endpoints;
- release and the owner read endpoints;
- account editing and pool resizing;
- the test-clock endpoint.

Do not build any of these, even where the code would be convenient to write now. One exception: `verify_lease` is written here, for the vectors (F5).

**Write these tests first, where they pin behaviour:**
- **The contract §6.4 vectors**, in both directions, against `sign_lease` and `verify_lease`.
- **Spec §12 tests**, through the real routes:
  - **1** — activation into a pool with room returns a lease;
  - **2** — a repeated activation, both for a strong PC and, via `request_id`, for a **degenerate** PC: same binding, no second seat, no ambiguity;
  - **3** — pool full → `pool_full`, nothing evicted;
  - **7** — a reformatted PC with a strong match is restored with a new `installation_id` and no new seat;
  - **9** — a strong match to a **revoked** binding is not restored (set `revoked_at` directly; release is Unit 3);
  - **10** — a cloned image on different hardware gets its own seat;
  - **11** — a reused `installation_id` from a conflicting machine logs `clone_flagged` and is not merged;
  - **12** — two all-zeros-UUID PCs with different disks both activate, both `degenerate`;
  - **13** — a reinstalled degenerate PC with the same disk → `409 ambiguity_pending` and an ambiguity row;
  - **26** — activate with no activation token → 401, no capacity consumed.
- **Contract behaviour:**
  - an activation token is **rejected** by `get_current_user()`, and an ordinary user token is **rejected** by `/licence/activate`;
  - the session registers **no** device — the `user_devices` count is unchanged;
  - an owner of no account → `no_licensing_access`; an admin sees every pool;
  - another owner's pool and a non-existent pool both return the same `pool_access_denied`;
  - precedence: a suspended full pool → `pool_inactive`, and a lapsed full pool → `subscription_inactive`;
  - `request_id_reused`;
  - **two concurrent activations with the same `request_id`** on Postgres, barrier-synchronised as in test 4 → exactly one binding;
  - **two concurrent activations with the same `request_id` that lead to ambiguity**, the same way → exactly one ambiguity row, and both responses carry the same `ambiguity_id` (F4);
  - key absent, key equal to the TEST seed, or kid 0 → 503 on both endpoints and `/health` `"unavailable"`;
  - `426` below the minimum version;
  - every error body carries `code`, `message`, `retryable` and `server_time`;
  - every wire timestamp matches the §2 format;
  - the returned lease verifies with the public key and carries the right payload, with `expires_at = min(30 days, subscription end)`;
  - the restore path refuses with 503 when the index is missing (the standing rule).
- **Fingerprint unit tests** — every collector placeholder maps to absent; repeated-character values; UUID degenerate forms; locally administered and all-zero MACs ignored; a USB disk never used as the system disk; `bios_serial == system_serial` counted once.
- **Test 4 still passes, and spec test 6 by hand still fails without the lock.**

**Deployment note for Nitoni, not for you:** after the Unit 2 PR is merged and before it is deployed, generate the production key locally (contract §6.5). Set only the private seed as `LICENCE_SIGNING_KEY`, and set `LICENCE_SIGNING_KID=1`, on Render; the public key goes into the WinUI client. Without them, the deploy is safe but licensing reports unavailable.

**First deliverable, before any code:** a short pre-code report, as for Unit 1:
- confirm the environment checks;
- list any drift between this brief, the contract and the code on current `main`;
- give your plan, and name any gap that blocks code (§7).

Then wait for go-ahead. **Stop at the end of this unit** and open the PR.

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

Items listed here are not yours to act on; they may change the spec under you. **None is open at the moment.** Closed since the last revision: the boot behaviour (decided — §4.E), Render deployment (manual), and the architecture handoff: `docs/ExamPartner_Windows_SeatPool_Architecture_Handoff.md` (updated 27 September) already carries the fingerprint correction, and spec §0 now cites that file.
