# ExamPartner Windows — Seat-Pool Licensing
## Pilot V1 Implementation Spec

**Date:** 27 September 2026 · **Revision 3**
**Status:** Ready for implementation
**Audience:** the implementation session working on the ExamPartner FastAPI backend

---

## 0. What this document is, and what it is not

This is the **implementation spec** for the first licensing slice. It derives from, and does not replace, the documents above it:

| Document | Role |
|---|---|
| `ExamPartner_Windows_SeatPool_Licensing_FINAL_Architecture_Handoff_2026-09-26.docx` | Decisions of record — product architecture. Still authoritative on **what the product does**. |
| `ExamPartner_Windows_Project_Report_Summary.docx` | Planning context — framework, scope, timeline, validation basis. |
| `ExamPartner_SeatPool_Backend_Task_Spec.docx` | **Superseded.** Its DDL and its `>3 activations in 30 days` rule should not be implemented. |

**One correction to carry back into the architecture handoff:**

> Hardware fingerprints may be missing, degenerate or non-unique across apparently identical PCs. Machine recognition must detect these cases and must not assume SMBIOS identifiers are globally reliable. Ambiguous identity must fall back to additional installation/server history and, where necessary, administrator confirmation, rather than automatically merging machines.

### What changed across revisions

| Area | Revision 1 | Current |
|---|---|---|
| Capacity mechanism | `slot_no` with a partial unique index | **Pool-row lock + count.** `slot_no` removed entirely. (r2) |
| Over-capacity gate | "blocked automatically" — **wrong**, see §5.1 | Explicit count comparison, slot-agnostic (r2) |
| Retry after conflict | Retry on same connection — **broken on Postgres** | Fresh connection per attempt (r2) |
| Activation authority | Undefined for a fresh install — **a security hole** | Authenticated institution owner, §8.4 (r2) |
| Refresh authority | Bare `installation_id` | Presented signed lease (r2), **plus machine verification** (r3), §8.6 |
| Expired lease | Unspecified | Self-recovers, §8.6 (r2) |
| Concurrency test harness | Unsynchronised threads | **Barrier-synchronised**, measured deterministic, §12 (r3) |

Revision 1's §5 was wrong in a way worth recording: it claimed over-capacity blocked new claims automatically because every slot in `1..pool_size` would be occupied. That holds only while active slots run contiguously from 1. Cut a 20-seat pool to 15 with slots 1–18 active, release slot 3, and slot 3 is claimable inside `1..15` while 17 machines are still active. The first proposed fix — reject whenever any active binding sits above `pool_size` — was concurrency-safe but made an internal slot number govern the customer's rights: an administrator who retires the machines in slots 1–3, as is their right, is left at exactly entitlement yet permanently unable to activate anything. **The root error was treating slot occupancy and capacity count as the same thing.** They are not, and the fix was to stop numbering seats at all.

Revision 2's §8.6 had a related omission: it moved refresh authorisation onto the signed lease but never used the `fingerprint_signals` the client sends. A lease copied from PC-A to PC-B would refresh indefinitely on PC-B, because the stale-serial signal only fires when *both* machines refresh — and a retired or permanently offline PC-A never does. §8.6 now verifies the machine on every refresh.

---

## 1. Scope

### In scope for Pilot V1

- Institution accounts with a **primary owner** linked to the existing user identity model
- Seat pools with **count-based capacity** — no seat rows, no slot numbers
- Activation of a new machine, with **serialised capacity enforcement**
- **Recognised reinstall** restoration that consumes no additional capacity
- **Explicit release** that works without the old installation being reachable, and that hardware recognition can never undo
- **Immutable audit history** that survives capacity reuse
- **30-day signed offline licence lease**, renewable, never beyond subscription expiry, **self-recovering after expiry**
- **Lightweight licence refresh** authorised by the presented lease **and verified against the presenting machine**, independent of content sync
- **Degenerate/ambiguous fingerprint handling** with ExamPartner-admin resolution
- **Duplicate installation-ID and stale-serial clone signalling**
- **Shared time abstraction** with an injectable test clock
- **Pre-implementation fingerprint survey spike**

### Deliberately deferred (designed, not built here)

24-hour Deployment Sessions · reseller accounts, scoping and dashboards · institution-admin web dashboard (API only in V1) · renewal-resizing UI · anomaly scoring · key rotation, hardware-backed key storage, anti-tamper · the WinUI client beyond §7's collector.

### Out of scope

Android, existing backend features, content pipeline.

---

## 2. Codebase conventions — derived from the repository, not assumed

Read from `backend/db.py`, `backend/services/paper_rules_service.py`, `backend/services/access_control.py` at `main`. See also `backend/CLAUDE.md`. **Follow these exactly.**

**Dual engine.** Every table must work on **SQLite and Postgres**. `db.py` declares schema as Python column lists applied idempotently (`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`) in two parallel branches, `_init_db_sqlite()` and `_init_db_postgres()`. No Alembic. Add licensing columns the same way, **in both branches**.

Production is Neon Postgres; SQLite is local dev. The SQLite path must not crash; Postgres is the path that must be correct.

**Placeholders are `?`.** `_PGCursor.execute()` rewrites `?` → `%s`. Never write `%s`.

**Connections.** `from config import db_conn`, then:

```python
db = db_conn()
try:
    cur = db.cursor()
    ...
    db.commit()
finally:
    db.close()
```

Statements between the first one and `commit()` are **one transaction on both engines** — psycopg2 opens one implicitly, and Python's `sqlite3` begins one before DML. §5 depends on this.

**`_PGConn` has no `rollback()`** — only `cursor()`, `commit()`, `close()`. Closing without committing is the only rollback available. See §5.3.

**IDs.** `secrets.token_hex(16)` into `TEXT PRIMARY KEY`. Never `SERIAL` — not portable to SQLite.

**Timestamps.** Written from Python as `datetime.now(timezone.utc).isoformat()`; read back as **`str` on SQLite, `datetime` on Postgres**. See §6.

**Errors.** Services raise `HTTPException` directly. Do not introduce the `{"data"|"error"}` envelope here.

**Returns.** `{"ok": True, ...}`.

**Row normalisation.** Each service defines its own `_row_to_dict(row)` handling `sqlite3.Row` and `RealDictRow`. Copy `paper_rules_service.py`.

**Auth** is enforced at the route layer. `require_admin(user)` lives in `services/access_control.py`.

**Validate in the service, not the route** — a future bulk path would bypass a route-level check. `upsert_paper_rule()` shows the pattern.

### New files

```
services/licensing_service.py      capacity, activation, restore, release, refresh
services/licensing_time.py         time normalisation + injectable clock
services/fingerprint_service.py    hashing, matching, confidence
routes/licensing.py                endpoints in §10
```

Register the router in `app.py` beside the existing ones. Add `cryptography` to `requirements.txt` — it is not currently a dependency.

---

## 3. Data model

### 3.1 Naming note — read before the tables

`institution_seats` keeps its name for continuity with the prior documents. **Its semantics have changed.** A row is not a seat. A row is an **activation binding**: one machine's claim on capacity.

- No `'free'` state, no free rows, **no slot numbers.** A pool of 20 creates no binding rows.
- **Active binding** = `revoked_at IS NULL`, the idiom `user_devices` already uses.
- **Available capacity** = `pool_size − COUNT(active bindings)`, computed, never stored.
- A revoked row is **retained forever** — it is the activation identity that must never be silently restored (§8.4 rule F).
- The row's `id` **is** the activation identity.

In the product and the UI these are still "seats" — schools understand "20 seats." Internally they are licensed capacity.

### 3.2 `accounts`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `token_hex(16)` |
| `name` | TEXT NOT NULL | |
| `account_type` | TEXT NOT NULL | `institution` \| `reseller`. V1 creates only `institution`. |
| `contact_email` | TEXT | |
| `owner_identifier` | TEXT | **The primary administrator.** Matches `users.identifier`. |
| `subscription_status` | TEXT | `active` \| `expired` \| `suspended` |
| `subscription_expires_at` | TEXT | ISO. NULL = no entitlement yet. |
| `created_at` / `updated_at` | TEXT | |

No FK to `users` — newer tables here use indexes instead. Index `owner_identifier`.

### 3.3 `seat_pools`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `account_id` | TEXT NOT NULL | |
| `pool_size` | INTEGER NOT NULL | **Purchased concurrent capacity.** Mutable — that is the whole resizing story. |
| `label` | TEXT | |
| `status` | TEXT | `active` \| `suspended` |
| `last_claim_at` | TEXT | **Load-bearing — see §5.2.** Updated inside every capacity claim. It both serialises claims and records when capacity was last consumed. |
| `created_at` / `updated_at` | TEXT | |

**No `CHECK` constraint on capacity is possible.** `CHECK (active_count <= pool_size)` was tested and rejects the shrink itself — you cannot reduce `pool_size` to 15 while 18 machines are active, and over-capacity is a state we deliberately support. Capacity is therefore enforced in code at one chokepoint, not by the schema. §14 records that trade.

### 3.4 `institution_seats` — activation bindings

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | **The activation identity.** |
| `seat_pool_id` | TEXT NOT NULL | |
| `account_id` | TEXT NOT NULL | Denormalised so scope checks need no join. |
| `machine_fingerprint_hash` | TEXT NOT NULL | Server-side stable hash of the composite. |
| `fingerprint_signals_json` | TEXT | Raw snapshot — for support, and for re-matching when the algorithm changes. |
| `fingerprint_confidence` | TEXT | `strong` \| `weak` \| `degenerate`, recorded **at claim time**. Load-bearing; see §8.3. |
| `installation_id` | TEXT | Server-issued. The **current** installation. Updated on recognised reinstall. |
| `machine_label` | TEXT | Human-friendly, admin-editable. |
| `activated_at` | TEXT | |
| `last_seen_at` | TEXT | Last successful licence refresh. |
| `lease_issued_at` | TEXT | |
| `lease_expires_at` | TEXT | |
| `lease_serial` | INTEGER NOT NULL DEFAULT 0 | Increments per issue. Replay guard and clone signal. |
| `revoked_at` | TEXT | **NULL = active.** |
| `revoke_reason` | TEXT | `manual` \| `replacement` \| `hardware_failure` \| `retired` \| `renewal_retire` \| `support` \| `other` |
| `revoked_by` | TEXT | |
| `created_at` / `updated_at` | TEXT | |

No separate lease table. The current lease lives on the binding; issuance and denial go to the log.

### 3.5 `seat_activation_log` — immutable history

Never updated, never deleted. Carries **its own copies** of fingerprint hash and label so history reconstructs after capacity is reused by a different machine.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `account_id` | TEXT NOT NULL | |
| `seat_pool_id` | TEXT | |
| `binding_id` | TEXT | Nullable — pool-level events have none. |
| `action` | TEXT NOT NULL | `activate` \| `restore` \| `release` \| `refresh` \| `refresh_denied` \| `ambiguous_flagged` \| `ambiguity_resolved` \| `clone_flagged` \| `stale_serial_flagged` \| `resize` \| `renewal` |
| `installation_id` | TEXT | |
| `machine_fingerprint_hash` | TEXT | Snapshot. |
| `machine_label` | TEXT | Snapshot. |
| `fingerprint_signals_json` | TEXT | Snapshot. |
| `performed_by` | TEXT | |
| `actor_role` | TEXT | `institution_owner` \| `exampartner_admin` \| `client` |
| `reason` | TEXT | Optional. Must never gate whether a legitimate release is allowed. |
| `detail_json` | TEXT | |
| `created_at` | TEXT | |

**Do not log every refresh.** Twenty PCs refreshing daily would bury what matters. Log `refresh` only when a lease is reissued after a gap, always log `refresh_denied`, and otherwise just update `last_seen_at` — as `user_devices.last_seen_at` already does.

### 3.6 `licence_ambiguities`

Needed because the log is immutable and an ambiguity has mutable state.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `account_id` / `seat_pool_id` | TEXT | |
| `presented_signals_json` | TEXT | |
| `presented_fingerprint_hash` | TEXT | |
| `presented_installation_id` | TEXT | |
| `machine_label` | TEXT | |
| `candidate_binding_ids_json` | TEXT | |
| `status` | TEXT | `open` \| `resolved` \| `expired` |
| `resolution` | TEXT | `recognize_existing` \| `treat_as_new` |
| `resolved_binding_id` | TEXT | |
| `resolved_by` / `resolved_at` | TEXT | |
| `created_at` | TEXT | |

---

## 4. Indexes and the hardening requirement

### 4.1 Licensing-critical constraint

```sql
-- One active binding per installation. Also the clone detector.
CREATE UNIQUE INDEX ux_seat_bindings_active_installation
  ON institution_seats(installation_id) WHERE revoked_at IS NULL;
```

### 4.2 Why this needs special handling

`db.py` wraps **every** index in `try/except` and, on failure, logs a warning and continues. Sensible for content indexes; unacceptable here. If this index silently fails, clone detection disappears and the only trace is a `logger.warning` in a Render boot log.

**Required, both parts:**

1. Create it through a **non-swallowed path** — outside the `_indexes` / `_sqlite_indexes` loops, where an exception propagates.
2. Add `assert_licensing_constraints()`, called at the end of `init_db()`, which verifies it exists and **raises** if not:
   - SQLite: `SELECT name FROM sqlite_master WHERE type='index' AND name = ?`
   - Postgres: `SELECT indexname FROM pg_indexes WHERE indexname = ?`

Note `CREATE UNIQUE INDEX IF NOT EXISTS` is a **no-op when the name already exists**, even with a different definition. To widen it, `DROP INDEX IF EXISTS` the old name and create a versioned one, as `ux_paper_rules_unique_row_v2` does.

### 4.3 Ordinary indexes (best-effort path is fine)

```sql
CREATE INDEX idx_accounts_owner            ON accounts(owner_identifier);
CREATE INDEX idx_seat_pools_account        ON seat_pools(account_id);
CREATE INDEX idx_seat_bindings_pool_active ON institution_seats(seat_pool_id, revoked_at);
CREATE INDEX idx_seat_bindings_account     ON institution_seats(account_id);
CREATE INDEX idx_seat_bindings_fp          ON institution_seats(machine_fingerprint_hash);
CREATE INDEX idx_seat_log_account_created  ON seat_activation_log(account_id, created_at);
CREATE INDEX idx_seat_log_binding          ON seat_activation_log(binding_id);
CREATE INDEX idx_ambiguities_open          ON licence_ambiguities(status, created_at);
```

---

## 5. Capacity enforcement

### 5.1 What does not work

**Naive `COUNT` then `INSERT`.** Measured, not theorised: 24 concurrent claims against a 15-seat pool on Postgres 16 produced **23 active bindings**. Under READ COMMITTED every transaction reads its own snapshot, sees room, and inserts. Wrapping it in a single `INSERT … SELECT` does not help.

**A counter column on the pool.** Concurrency-safe via `UPDATE … WHERE active_count < pool_size`, and it was verified to hold at exactly 15. Rejected anyway for two reasons: it cannot carry a `CHECK` backstop (§3.3), so it buys no schema-level guarantee; and it is a second source of truth for a fact the bindings table already holds. In testing it drifted immediately — `counter=0` while `active=15` — the moment a code path did not maintain it.

**Slot numbers.** Revision 1's approach. Concurrency-safe, but it makes an internal number govern the customer's rights (§0), and keeping it would require a compaction routine to stop arbitrary numbering stranding legitimate capacity.

### 5.2 What does work: lock the pool row, then count

Verified on **Postgres 16 and SQLite**, 24 concurrent claims, three scenarios, all correct.

```
claim_capacity(pool_id):            # the ONLY place capacity is granted
    db = db_conn()
    try:
        cur = db.cursor()

        # (1) Take a row lock on the pool, held until commit. Concurrent
        #     claims block here and re-evaluate afterwards. This statement
        #     is what makes the count below trustworthy.
        cur.execute("UPDATE seat_pools SET last_claim_at = ? WHERE id = ?",
                    (now_iso(), pool_id))

        # (2) Now the count cannot change underneath us.
        active = SELECT COUNT(*) FROM institution_seats
                 WHERE seat_pool_id = ? AND revoked_at IS NULL
        pool_size = SELECT pool_size FROM seat_pools WHERE id = ?

        if active >= pool_size:
            db.commit()
            raise HTTPException(409, "No capacity available in this pool")

        # (3) Insert the binding.
        cur.execute("INSERT INTO institution_seats (...) VALUES (...)")

        # (4) Invariant re-check, same transaction. Cheap insurance against
        #     a logic error above; aborts rather than oversubscribing.
        if recount > pool_size:
            db.close()               # discards — see 5.3
            raise HTTPException(500, "Capacity invariant violated")

        db.commit()
    finally:
        db.close()
```

**`claim_capacity()` is the only path that may create a new active binding.** Activation, ambiguity resolution that results in a new machine, and any future path — Deployment Sessions included — must go through it rather than inserting a binding directly. Because capacity is code-enforced rather than database-enforced (§3.3, §14), a path that bypasses this function silently removes the capacity guarantee for every customer. This is the single most likely future regression in the whole slice.

**Step 1 is not a no-op and must not be removed.** It looks like a pointless write; it is the entire concurrency guarantee. It is deliberately written as a *meaningful* update — `last_claim_at` records when the pool last consumed capacity, useful for support and later abuse analysis — so that deleting it breaks something visible rather than silently removing the lock. Comment it accordingly, in the style this codebase already uses for load-bearing oddities.

On Postgres the `UPDATE` takes a row-exclusive lock; a second transaction blocks and, under READ COMMITTED, its subsequent `SELECT` takes a fresh snapshot that includes the first transaction's committed insert. SQLite serialises writers at the database level, so the sequence cannot interleave at all; 24 concurrent writers completed with no `database is locked` errors under Python's default 5-second busy timeout, which `db.py`'s `sqlite3.connect()` gets implicitly.

**`slot_no` does not exist.** There is nothing to compact, normalise, or strand.

### 5.3 Retrying after an integrity error

Revision 1's pseudocode retried on the same connection after a unique violation. **That is broken on Postgres:** the transaction enters a failed state and every subsequent statement raises `InFailedSqlTransaction` until it is rolled back — and `_PGConn` has no `rollback()`.

Where a retry is needed (the installation-ID unique index, §8.4 step 4), the loop must be: **close the connection without committing, open a fresh one, recompute, retry** — bounded at 5 attempts. Closing is the only rollback this connection model offers.

`db.py` does contain a savepoint pattern (`_pg_exec_index` uses raw `SAVEPOINT` / `ROLLBACK TO SAVEPOINT`), which would also work. It is not used here: a fresh connection is simpler and obviously correct, and the retry path is rare enough that the cost is irrelevant. Adding `rollback()` to `_PGConn` is the better long-term fix, but it is shared infrastructure touched by every service and belongs as its own deliberate change.

Detect the violation portably, using the lazy-import style `_get_pg()` already uses:

```python
def is_unique_violation(exc: Exception) -> bool:
    import sqlite3
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        import psycopg2
        if isinstance(exc, psycopg2.IntegrityError):
            return True
    except ImportError:
        pass
    return False
```

### 5.4 Over-capacity is a state, not an error

After a shrink, active bindings may exceed `pool_size`. **Never force-kill machines to reconcile.**

- `over_capacity = active_count > pool_size`, computed and exposed on pool reads
- New claims are refused by the `active >= pool_size` comparison in §5.2 — **slot-agnostic**, so which machines are active is irrelevant
- Existing machines keep working and keep renewing leases
- The administrator retires **whichever machines they choose**, by ordinary release, with `revoke_reason = 'renewal_retire'`
- Once `active_count <= pool_size` the state clears on its own; once strictly below, new activations resume

Verified: 18 active cut to a 15-seat pool, administrator retires the machines that happened to be added first — 15 active, 24 concurrent claims all correctly refused. Retire one more and exactly one claim is granted. No machine's fate depends on an internal number.

A resize concurrent with a claim can leave the pool briefly over-capacity. Self-correcting, and the resize endpoint should re-read the active count after writing `pool_size` so its response reports the resulting state accurately.

---

## 6. Time handling

### 6.1 The existing trap

`access_control.is_paid_user()` compares `paid_until > now` against the raw DB value — a `datetime` on Postgres, a `str` on SQLite, where it raises `TypeError`. Latent and dev-only today, but licensing is almost entirely expiry comparison. Fix it once, centrally, and route `is_paid_user()` through the same helper as part of this slice.

### 6.2 `services/licensing_time.py`

```python
now()                      -> aware UTC datetime, honouring the test clock
now_iso()                  -> str
to_datetime(value)         -> aware UTC datetime from str | datetime | None
is_expired(value, at=None) -> bool
```

`to_datetime` normalises the SQLite-`str` / Postgres-`datetime` split in one place. **All licensing comparisons happen in Python via these helpers. Never compare timestamps in SQL** — lexicographic ISO comparison works until a format varies, then fails silently.

### 6.3 Injectable test clock

Two mechanisms, testing different boundaries:

1. **Clock offset** — shifts `now()`, for 30-day lease expiry.
2. **Direct expiry set** — a support endpoint writing `accounts.subscription_expires_at`, for the subscription boundary and the expired-then-renewed path.

Both gated on `ALLOW_TEST_CLOCK`, **default false**. When false the offset is ignored and the endpoint returns 403. Without this, tests 13–17 take 30 days and a year.

---

## 7. Fingerprint survey spike — do this first

Confidence thresholds in §8.2 are **provisional** and must not be frozen until survey data exists. Everything else can proceed in parallel.

### 7.1 Deliverable

A standalone, portable Windows utility. No installer, no network, **read-only** — it activates nothing and consumes no capacity. Runs from a USB stick. Prompts only for a machine label. Writes one JSON file per machine plus a CSV summary.

PowerShell is preferred over a compiled .NET tool: no build step, no runtime to install on a school PC, readable by the school's IT admin, and writable without access to Windows. Ship a `.bat` wrapper invoking it with `-ExecutionPolicy Bypass`.

### 7.2 Signals

**Candidate identity:** system/SMBIOS UUID; baseboard manufacturer, product, serial; BIOS vendor, version, release date, serial; system manufacturer, model, serial.

**Supporting, not identifying:** CPU name, family, model, cores. Modern x86 exposes no per-CPU serial, so across a batch of identical PCs these are **identical** and distinguish nothing. Collect them to confirm exactly that.

**Collected to demonstrate unsuitability:** Windows `MachineGuid` and install ID, disk serials, MAC addresses, total RAM.

### 7.3 What the survey must answer

- Are the SMBIOS UUIDs actually unique across the batch?
- Are any blank, all-zeros, or all-FFs?
- Are baseboard and system serials present, or empty strings?
- Are any values **duplicated** across supposedly distinct machines?
- Which combination actually distinguishes them?
- **Second pass, one machine:** which values survive a Windows reinstall or repair?

### 7.4 Scope of the first run

Start with **Nitoni's own Windows laptop plus one office Windows PC.** A first-pass validation of the collector and the candidate signals — **not** proof of uniqueness across a school lab, and not to be treated as such.

Design it so the identical tool runs later across the pilot school's lab, by the school's IT administrator, without Nitoni present. Batch-purchased identical PCs are the case that matters and cannot be simulated on two dissimilar machines.

The full school survey must happen **before the thresholds are frozen**, but it does not block the backend work.

---

## 8. Machine identity and the activation algorithm

### 8.1 The three identities — keep them distinct

| Identity | Where it lives | Behaviour |
|---|---|---|
| **Physical machine** | `machine_fingerprint_hash` | Should survive reinstall and routine component change. |
| **Installation** | `installation_id`, server-issued | Changes on reinstall. Audit and clone detection. |
| **Activation** | `institution_seats.id` | Survives a recognised reinstall. **Permanently revoked** on release. |

Collapsing these into one `hardware_id` — as the original task spec did — makes reformat recovery structurally impossible: there is nowhere to record that installation B is the same machine as installation A.

### 8.2 Confidence — provisional, pending §7

| Level | Provisional rule |
|---|---|
| `strong` | System UUID present, non-degenerate and matching, **plus** at least one of baseboard / BIOS / system serial matching. |
| `weak` | Only one stable signal matches; or the UUID is degenerate but baseboard **and** BIOS serials match. |
| `degenerate` | UUID missing / all-zeros / all-FFs, **and** baseboard and system serials absent, empty, or already duplicated within this pool. |

Mark these constants as provisional in code. They are what the survey exists to replace.

### 8.3 Degenerate first activation — subtle, important

A machine presenting a degenerate fingerprint on **first** activation has nothing to match against, so it claims capacity normally. It must be stored with `fingerprint_confidence = 'degenerate'`.

That recording is the point: a later reinstall of that machine routes to **ambiguity resolution** rather than silently matching a different identical PC in the same lab. Without it, PC 7 reinstalling could restore PC 12's activation.

### 8.4 Activation

`POST /licence/activate` — body: `pool_id`, `fingerprint_signals`, `machine_label`, optional `installation_id`.

**Authorisation — this was missing in revision 1 and was a security hole.** A fresh Windows installation holds no activation and no lease, so it cannot authenticate as itself, and Deployment Sessions are deferred. As written, anyone who learned a `pool_id` could consume a school's capacity.

> A fresh activation, or a reinstall that needs new authorisation, runs **under the authenticated institution owner** (or an ExamPartner admin). The technician signs in on the PC, the backend verifies ownership of the pool, and only then is capacity claimed. The server generates the `installation_id` and issues the signed lease. **Administrator credentials are not retained on the PC** — they authorise this one activation.

That is exactly the friction the 24-hour Deployment Session removes later, which is a sign the sequencing is coherent rather than improvised.

Then:

1. **Resolve** pool and account; verify pool `status = 'active'`.
2. **Subscription gate** (rule C). If not `active`, or `subscription_expires_at` has passed, return `402` and issue **nothing**.
3. **Compute** `machine_fingerprint_hash` and confidence.
4. **Clone check.** If `installation_id` is supplied and maps to an active binding whose fingerprint differs materially: log `clone_flagged`, treat the supplied id as untrusted, **do not merge the machines**, and continue on fingerprint evidence alone. A duplicated installation ID is a *signal*, never an identity.
5. **Match** against bindings in this pool by fingerprint hash:

| Case | Condition | Action |
|---|---|---|
| **D — recognised reinstall** | Strong match to an **active** binding | Restore. Update `installation_id`, `last_seen_at`, label if supplied. Issue a lease, increment `lease_serial`. Log `restore`. **No capacity consumed.** |
| **Duplicate/retry** | Same active machine repeats activation | As D. Idempotent. |
| **F — revoked match** | Matching bindings are all **revoked** | **Do not restore.** Fall through to G/H as a new activation. Explicit revocation always beats hardware recognition. |
| **E — ambiguous** | Weak confidence, several candidates, or degenerate against an existing degenerate binding | **Claim nothing.** Create a `licence_ambiguities` row, log `ambiguous_flagged`, return `409` with its id. Requires admin resolution (§10.4). |
| **G — new machine, capacity free** | No match, capacity available | `claim_capacity()` (§5.2). Create binding, issue lease, log `activate`. |
| **H — no capacity** | No match, pool at or over entitlement | `409 pool_full`. **Never auto-evict.** |

### 8.5 Release

`POST /institutions/seats/{binding_id}/release` — institution owner of that account, or ExamPartner admin.

- Set `revoked_at`, `revoke_reason`, `revoked_by`. Log `release`.
- Capacity is free **immediately**.
- **Must not require the old installation to be reachable.** The dead-PC case is the one schools actually hit, and this is the primary product requirement.
- The activation is permanently revoked; hardware recognition can never restore it (rule F).
- Old machine online → next refresh denied. Offline → runs to `lease_expires_at`, then denied. That bounded window is why the lease is 30 days.
- `reason` is for support. It must never gate whether a legitimate release is allowed.

### 8.6 Licence refresh

`POST /licence/refresh` — body: **the current signed lease**, plus `fingerprint_signals`.

Revision 1 looked the binding up by bare `installation_id` with no proof of possession, which quietly turned `installation_id` into a permanent bearer token — one also written into `seat_activation_log` and visible to support. The client presents its signed lease instead: `installation_id` is the **identifier**, the lease is the **authorisation**.

1. **Verify the lease signature.** Invalid → `403`, log `refresh_denied`.
2. Resolve the binding from the lease. Not found or **revoked** → `403 lease_revoked`, log `refresh_denied`. This is where release is enforced.
3. **Verify the presenting machine** — see §8.6.1. This is the step that stops a copied lease.
4. **Subscription expired** → deny issuance beyond the paid term.
5. **Expiry of the presented lease is not a bar to refreshing.** A validly signed but expired lease still refreshes, provided the signature verifies, the machine is recognised, the binding is active and the subscription is current.
6. Issue a new lease: `lease_expires_at = min(now() + 30 days, subscription_expires_at)`. Increment `lease_serial`. Update `last_seen_at`.

### 8.6.1 Machine verification on refresh

Recompute the fingerprint from the submitted `fingerprint_signals` and compare it, using the same confidence rules as §8.2:

| Outcome | Action |
|---|---|
| **Strong match** | Refresh normally. If the hash drifted (see the ratchet below), update the binding's stored fingerprint. |
| **Clearly a different physical machine** | **Do not refresh.** `403 machine_mismatch`, log `clone_flagged`. The activation is untouched — this denies *this* machine, it does not revoke anyone. |
| **Weak or degenerate** | Do not refresh silently. Create a `licence_ambiguities` row, log `ambiguous_flagged`, return `409` with its id, and route to resolution (§10.4). |

Not every fingerprint difference is a permanent denial. That would contradict the maintenance-friendly philosophy the whole design rests on: an SSD swap, a RAM upgrade or a BIOS update must still be the same machine.

**Compare against the binding, not the lease.** Both carry a `machine_fingerprint_hash`, and an implementer will otherwise pick one arbitrarily. The binding is the **mutable current truth**; the lease is a **point-in-time snapshot** that may predate legitimate maintenance. Comparing against the lease would produce intermittent false denials after any hardware change. The lease's signed hash remains useful as a secondary tamper signal: if it matches neither the presented machine nor the binding's current value, the lease has probably been moved — log it alongside the clone signals.

**Ratchet the stored fingerprint forward.** On every strong match where the recomputed hash differs from the stored one, write the new hash and signals snapshot onto the binding. Without this, a PC that has RAM upgraded, then a disk replaced, then a BIOS update, drifts step by step away from a frozen day-one reading — each change individually a strong match, the accumulation eventually not — and a perfectly healthy lab machine fails verification for no single identifiable reason. Ratcheting makes maintenance accumulate gracefully.

**Keep the denial reasons distinguishable.** `machine_mismatch` and `lease_revoked` need different error codes because the remedies differ: a mismatch (a motherboard replacement, say) means the administrator releases the old activation and the rebuilt PC activates fresh, while a revocation means capacity must be freed or bought. A client that cannot tell them apart will show the wrong instruction to a technician standing at the machine.

**Why step 4 matters.** The 30 days mean "you may operate offline this long without contacting ExamPartner," not "after 30 days this installation is dead and needs reactivating." A PC offline for 40 days must reconnect and recover **by itself**. Get this wrong and a school returning from a long holiday finds its lab locked out — precisely the "we lost our subscription" complaint this whole design exists to answer.

**Stale `lease_serial` is a signal, not proof.** A clone presents the same valid lease; the first to refresh advances to N+1, so the second arrives holding a stale serial. Log `stale_serial_flagged` and surface it for review. **Do not treat it as automatic proof of abuse** — restoring a machine from an old system backup can legitimately present stale state. It belongs alongside the duplicate-installation-ID detector as evidence for a human, not an automatic denial.

**Refresh is not activation.** It must not claim capacity, must not create a binding, and **must not trigger content synchronisation**. Payload is identifiers, lease state, timestamps and a signed response.

### 8.7 Lease signing — minimal but real

Ed25519 via `cryptography`. Private key from env (`LICENCE_SIGNING_KEY`); public key embedded in the client.

Payload: `binding_id`, `installation_id`, `account_id`, `seat_pool_id`, `machine_fingerprint_hash`, `lease_serial`, `issued_at`, `expires_at`, `subscription_expires_at`.

The client stores it under Windows DPAPI, which binds it to the machine and provides integrity protection for the stored blob, and rejects a lease whose `lease_serial` is lower than the one it holds.

**Deferred:** key rotation, hardware-backed storage, anti-tamper, certificate infrastructure. The bounded threat is a school extending a released seat by up to 30 days, not a determined attacker.

---

## 9. Authorisation

The existing model is one `users` table plus `is_admin`, with `require_admin()` the only gate. V1 adds the **minimum**: `accounts.owner_identifier`.

| Role | Determined by | May do |
|---|---|---|
| **Institution owner** | `user.sub == accounts.owner_identifier` | View own pools and machines; release own bindings; rename own labels; **authorise activation on a fresh PC**. Nothing on another account. |
| **ExamPartner admin** | `require_admin()` | Everything, plus ambiguity resolution, account/pool creation, subscription changes, test clock. |
| **Windows client** | Holds a valid signed lease | **Refresh only.** Cannot activate unaided (§8.4), cannot release any machine. |

Add to `services/access_control.py`:

```python
def require_account_owner(user, account_id) -> str:
    """ExamPartner admin passes. Otherwise user.sub must equal
    accounts.owner_identifier. Raises 403. Returns the identifier."""
```

**No client-supplied `account_id`, `pool_id` or `binding_id` may bypass an ownership check.** Resolve the object, then verify the caller owns it.

Why the owner matters in V1: the flow the pilot exists to validate is *"a computer was reformatted, recover the seat."* If only Nitoni can release, the pilot measures his response time rather than the product.

---

## 10. Endpoints

### 10.1 Administration — ExamPartner admin

| Endpoint | Purpose |
|---|---|
| `POST /institutions/accounts` | Create account, set `owner_identifier`. |
| `PATCH /institutions/accounts/{id}` | Update name, contact, owner. |
| `PATCH /institutions/accounts/{id}/subscription` | Set expiry / status. Logs `renewal`. |
| `POST /institutions/accounts/{id}/seat_pools` | Create pool with `pool_size`, optional label. |
| `PATCH /institutions/seat_pools/{id}` | Change `pool_size`. Logs `resize`. Never force-kills (§5.4). |

### 10.2 Institution owner

| Endpoint | Purpose |
|---|---|
| `GET /institutions/accounts/{id}/seat_pools` | `pool_size`, `active`, `available`, `over_capacity`, `subscription_expires_at`. |
| `GET /institutions/accounts/{id}/machines` | Active bindings: label, `activated_at`, `last_seen_at`, `lease_expires_at`, `fingerprint_confidence`. |
| `GET /institutions/accounts/{id}/history` | Paginated `seat_activation_log`. |
| `POST /institutions/seats/{binding_id}/release` | §8.5. |
| `PATCH /institutions/seats/{binding_id}` | Rename `machine_label` **only**. |

### 10.3 Windows client

| Endpoint | Purpose |
|---|---|
| `POST /licence/activate` | §8.4 — authorised by the institution owner. |
| `POST /licence/refresh` | §8.6 — authorised by the presented lease. |

### 10.4 Support — ExamPartner admin

| Endpoint | Purpose |
|---|---|
| `GET /support/licensing/ambiguities` | Open ambiguities with presented signals and candidates. |
| `POST /support/licensing/ambiguities/{id}/resolve` | `{decision: "recognize_existing" \| "treat_as_new", binding_id?}`. Logs `ambiguity_resolved`. |
| `GET /support/licensing/signals` | `clone_flagged` and `stale_serial_flagged` events for review. |
| `POST /support/licensing/test-clock` | Gated on `ALLOW_TEST_CLOCK`; 403 otherwise. |

Resolution goes through the application, never a manual Neon edit, so the business rule runs and the decision is audited.

---

## 11. Renewal and resizing

Workflow and UI are **deferred**; the data model must not make resizing impossible, and with count-based capacity it does not. There are no seat rows and no slot numbers to reorganise, so every case below is a single `UPDATE`.

- **Renewal, unchanged size:** extend `subscription_expires_at`. Bindings, fingerprints, activation and installation identities untouched. Existing PCs pick up new leases through ordinary refresh. **Renewal is not reinstallation and not reactivation.**
- **Increase, 20 → 30:** `UPDATE seat_pools SET pool_size = 30`. Existing machines untouched; 10 more claimable.
- **Decrease, 20 → 15:** one `UPDATE`. If active ≤ 15, nothing else happens. If active > 15, the pool is over-capacity (§5.4) — no machine is killed, no new claims succeed, and **the administrator chooses which machines to retire**, whichever they are.
- **Expiry without renewal:** must **not** uninstall the app, delete content, erase progress, or discard activation history. The backend stops issuing leases beyond the paid term. A PC runs until the earlier of its lease expiry and the subscription expiry.
- **Expired then renewed:** retained machines resume through ordinary refresh with their existing identities, presenting their expired lease (§8.6 step 4). Never forced through reinstallation or fresh activation.

---

## 12. Acceptance tests

All runnable without waiting real time (§6.3).

**Capacity and activation**
1. 20-seat pool, 18 active, new PC activates → 19 active, 1 available, lease issued.
2. Same active PC repeats activation after a timeout → existing activation returned, **no second seat consumed**.
3. Pool full → clean `pool_full`, **no eviction**.
4. **Concurrency:** 24 simultaneous activations against a 15-seat pool → **exactly 15 granted**, 9 refused, `active_count` never exceeds `pool_size`. Must run against **Postgres** — SQLite serialises writers and would pass regardless.

   **Harness matters, and the shape below was measured rather than assumed.** Each worker must open its database connection **before** waiting on a `threading.Barrier(24)`, so all 24 are released together without connection-setup jitter staggering them. Measured over 20 iterations on Postgres 16:

   | Harness | Oversubscribed | Resulting active count |
   |---|---|---|
   | No lock, threads started in a loop | 20/20 | 16–24, **varies per run** |
   | No lock, barrier-synchronised | 20/20 | **24 every run** |
   | With lock, barrier-synchronised | 0/20 | **15 every run** |

   Unsynchronised threads do expose the bug, but with a variable count, so an assertion on an exact number would be flaky. With the barrier both directions are deterministic. Run 5 iterations rather than 1 as cheap insurance on slower CI, and assert `granted == pool_size` exactly.

5. **Regression for the revision-1 bug:** 20-seat pool with 18 active, shrink to 15, release any three machines → 15 active, and new activation is **refused**. Release a fourth → exactly one activation granted. Must hold whichever machines were retired.
6. Remove the `UPDATE seat_pools SET last_claim_at` lock statement from `claim_capacity()` → test 4 **fails deterministically**, granting all 24. Confirms the lock is load-bearing and guards against it being optimised away.

**Reinstall and recovery**
7. Reformat, strong hardware match, activation active → restored, `installation_id` updated, **no new seat**.
8. Dead PC, old installation unreachable, admin releases → revoked, capacity free immediately, replacement activates at no extra cost.
9. Former machine returns after explicit release → hardware match does **not** restore the revoked activation.
10. Cloned image on a second physical PC → new machine, needs its own capacity.
11. Same `installation_id` from a conflicting machine context → `clone_flagged`, machines **not** merged.

**Degenerate identity**
12. Two PCs with identical all-zeros UUIDs both activate → both succeed, both recorded `degenerate`.
13. One reinstalls → routed to **ambiguity resolution**, does not restore the other's activation.
14. Admin resolves `recognize_existing` → correct activation restored, no new seat, `ambiguity_resolved` logged.

**Lease and expiry** *(test clock)*
15. Lease issued, clock +29 days → refresh succeeds.
16. **Expired-lease self-recovery:** clock +40 days, no refresh in between, PC presents its validly signed **expired** lease → new lease issued, **no administrator involvement**, no new seat.
17. Same, but the activation was released → refresh **denied**.
18. Same, but the subscription expired → **denied until renewal**, then succeeds through ordinary refresh.
19. Admin releases PC-A while offline; PC-B takes the capacity → PC-A runs to its existing `lease_expires_at`, then refresh is denied.
20. **Stale serial:** lease copied to a second machine, first refreshes to serial N+1, second presents N → `stale_serial_flagged` logged and surfaced. **Refresh is not automatically denied on the serial alone** — it is evidence for review, since a restore from backup can present stale state legitimately. (The machine check in test 22 is what actually stops the clone.)

**Machine verification on refresh** *(§8.6.1)*

21. **Copied lease, different machine:** PC-A's validly signed lease presented from a materially different physical machine → `403 machine_mismatch`, `clone_flagged` logged, **no new lease issued**, and PC-A's activation left **untouched and active**. Must hold even when PC-A never reconnects, so no stale-serial signal ever fires.
22. **Maintenance is not a mismatch:** same machine refreshes after a simulated disk replacement and RAM change, still a strong match → refresh succeeds, **and the binding's stored `machine_fingerprint_hash` is updated** to the new reading.
23. **Ratchet accumulation:** three successive refreshes, each a strong match with a small drift → all three succeed. Assert the third succeeds *because* the binding ratcheted forward, by confirming the third reading would **not** be a strong match against the original day-one hash.
24. **Weak identity on refresh:** presented signals give weak or degenerate confidence → `409`, `ambiguous_flagged` logged with an ambiguity id, no lease issued, activation untouched.
25. **Distinguishable denials:** a `machine_mismatch` response and a `lease_revoked` response carry different error codes, so a client can show the right remedy.

**Authorisation**
26. `POST /licence/activate` with no authenticated institution owner → `403`. **No capacity consumed.**
27. Institution owner A attempts to release a binding on account B → `403`.
28. Client holding a valid lease attempts to release any binding → `403`.

**Resizing**
29. 20 → 30 → existing machines untouched, 10 more claimable.
30. 20 seats / 18 active → 15 → over-capacity, no machine killed, new claims blocked, admin retires 3, state clears.

**Hardening**
31. Drop `ux_seat_bindings_active_installation`, restart → backend **fails to start** with a clear error.

---

## 13. Implementation order

1. Fingerprint survey collector (§7) — parallel to everything else.
2. `licensing_time.py` (§6), including the `is_paid_user()` fix.
3. Schema in `db.py`, both engines; non-swallowed index path; `assert_licensing_constraints()` (§3, §4).
4. `require_account_owner()` (§9).
5. `claim_capacity()` (§5.2) — **write test 4 first**, against Postgres.
6. `fingerprint_service.py`, thresholds marked provisional (§8.2).
7. Activate / restore / release, including the owner-authorised bootstrap (§8.4, §8.5).
8. Lease issue, signing and refresh — including machine verification and the fingerprint ratchet (§8.6.1), and expired-lease recovery (§8.6, §8.7).
9. Owner read endpoints and history (§10.2).
10. Support ambiguity and signals endpoints (§10.4).
11. Full acceptance suite (§12).
12. Freeze fingerprint thresholds once school survey data is in — **before the licensing work is called complete.**

---

## 14. Open risks

**Capacity is code-enforced, not schema-enforced.** A `CHECK` constraint was tested and is impossible alongside the shrink (§3.3), and dropping `slot_no` removed the unique-index cap. Correctness now rests on `claim_capacity()` being the only path that grants capacity, and on the lock statement inside it surviving future edits. Mitigated by the single chokepoint, the meaningful-write framing, the in-transaction invariant re-check, and tests 4–6. This is a deliberate trade: the product invariant that capacity is a count, with the administrator free to retire any machine, was judged worth more than a database-level ceiling.

**Fingerprint thresholds are provisional.** Set from reasoning, not measurement, until §7 returns data. Two dissimilar machines cannot validate behaviour across a batch of identical ones, which is the case that matters.

**The pilot is a real customer.** Identity gets validated on a school's live lab. The survey exists to move that risk earlier, where it is cheap.

**SQLite parity is dev-only.** Neon is production. The SQLite path must not crash, but concurrency correctness is only genuinely exercised on Postgres.

**No reseller scoping.** `account_type` accepts `reseller`, but nothing enforces isolation. Do not sell through a reseller until that slice lands.

**Minimal lease cryptography.** A determined attacker can extract the DPAPI-stored lease. Bounded by the 30-day window, and now also by the machine check on every refresh (§8.6.1).

**The fingerprint ratchet has a theoretical cost.** Because a strong match updates the binding's stored fingerprint, an identity can in principle be walked from one machine toward another in small steps across successive refreshes, each individually a strong match. Far-fetched in practice — it needs physical access, patience across 30-day refresh cycles, and every step to stay within strong-match tolerance — and the alternative is worse: without ratcheting, ordinary accumulated maintenance eventually locks a healthy machine out for no identifiable reason. Recorded so the trade is deliberate rather than discovered. If it ever needs tightening, the lever is bounding total drift from the original reading, not abandoning the ratchet.

**No estimates here, on purpose.** Week counts belong in the sprint plan, made against this scope after the work has been sized — not asserted alongside a design.

---

*End of Pilot V1 implementation spec, revision 3.*
