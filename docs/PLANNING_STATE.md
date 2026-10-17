# Planning state — ExamPartner Windows seat-pool licensing

**Handover from the planning window of 5–12 October 2026.** Read this first in a new planning window, then `docs/WORKING_MODEL.md`.
**State as of:** 12 October 2026 · `main` at `bdaff67` (PR #38 merged).

---

## 1. Where things stand

| Area | State |
|---|---|
| **Backend Unit 1** — time helpers, schema, owner check, `claim_capacity()` | Merged as PR #37 (`3a8e313`). Deployed; `/health` showed `"licensing": "ready"`. |
| **Backend Unit 2** — fingerprint rules, activation session, activate and restore, lease signing, error envelope, pilot setup endpoints | Merged as PR #38 (`bdaff67`). 372 tests passing, 0 skipped. **Production deploy pending** (§2). |
| **Backend Unit 3** — refresh, collecting resolved ambiguities, release, owner reads, support endpoints, test clock | Not briefed. **Must include brief §9** (mandatory request-ID history rule, and a tightened race test). |
| **API contract** | `docs/LICENCE_API_CONTRACT.md` v1, 8 October 2026; §7.3 clarified 10 October. Signed-lease test vectors in §6.4. |
| **WinUI client** | Not started. First brief: `docs/LOCAL_AGENT_BRIEF.md` (12 October) — L0 collector v2, L1 licence core, L2 activation UI, packaging decision. |
| **Survey collector** | v1 in `tools/fingerprint-survey/`. Records the identity signals but not the continuity fields; v2 is local-agent work unit L0. |
| **Hardware survey** | Not run yet. Thresholds stay provisional until the school survey (spec §13 step 12). |

## 2. Waiting on Nitoni right now

1. **Deploy Unit 2.**
   - Generate the production Ed25519 key locally, outside the repository.
   - Set `LICENCE_SIGNING_KEY` and `LICENCE_SIGNING_KID=1` on Render. Leave `MIN_WINDOWS_APP_VERSION` **absent** (an empty value closes licensing). Keep `ALLOW_TEST_CLOCK` absent.
   - Turn off or clear Windows clipboard history around the paste.
   - Deploy manually, and confirm `/health` shows `"licensing": "ready"`. If a log line starts `LICENSING UNAVAILABLE`, bring that line to planning — it never contains the key.
2. **Create the internal test school** for client development (§6 has the commands).
3. **Decide the 10 November trial's scope** beyond activation and offline licence checking.
4. **Start the local agent** on `docs/LOCAL_AGENT_BRIEF.md`.

## 3. Documents and what each is for

| Path | Role |
|---|---|
| `docs/WORKING_MODEL.md` | Who does what across planning, the Cloud Session and the local agent; settled versus open decisions (§6). |
| `docs/LICENCE_API_CONTRACT.md` | The only place client and server shapes meet. **Wins on any wire detail.** |
| `docs/CLOUD_SESSION_BRIEF.md` | Backend brief. §4.A–H rulings, §5 current unit (Unit 2, done), **§9 carried into Unit 3**. |
| `docs/LOCAL_AGENT_BRIEF.md` | WinUI client brief. |
| `docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md` | The business rules, kept consistent with the brief rulings (amended through 11 October). |
| `docs/ExamPartner_Windows_SeatPool_Architecture_Handoff.md` | Product decisions of record, including the fingerprint correction. |
| `backend/CLAUDE.md` | Backend conventions, including the standing licensing rules. |
| `tools/fingerprint-survey/` | The survey collector (v1). |

The project attached to the planning chat holds copies of the main docs, kept in step by planning. **The repository is the source of truth.**

## 4. The decisions that shape everything (full text in the brief and spec)

- **Capacity** is a count of active bindings, enforced in one function (`claim_capacity()`) under a pool-row lock. No slot numbers. Over-capacity after a shrink is a state, never an eviction.
- **A licensing failure closes licensing, never the backend.** The Android app on the same backend must keep starting. The licensing index is checked live at every binding write. `/health` reports licensing readiness without a database query.
- **Activation:**
  - It runs under a **15-minute activation-only token**, obtained with owner or admin credentials — not `/auth/login`. It registers no device. Owner and admin status are re-checked at activation.
  - The **lease** is Ed25519 over the ASCII payload segment, with `kid` 0 reserved for the published TEST key. A missing, malformed or TEST signing key → licensing unavailable.
  - Matching is **signal by signal**, never by hash. Identity *quality* (of a reading) and match *strength* (against a binding) are separate concepts (brief F1).
  - A **degenerate or weak PC** goes to admin review only if its system disk or onboard network card matches an active binding (F2). `MachineGuid` is **not** an activation signal (H1).
- **Retries.** Every state-changing request carries a client `request_id`. Replays return the identical result. Ambiguity creation is race-safe through its own pool-row lock (H2).
- **Lock order** (H3): never hold a binding lock and then take a pool lock; if a path needs both, take the pool first.
- **The client:**
  - It stores its lease with **DPAPI in `LocalMachine` scope**.
  - The **clock rule** allows 24 hours of rollback; beyond that it needs an online refresh. A rollback never revokes and never uses a seat.
- **Deployment:**
  - Render deploys **manually**. The trial is the **native WinUI client**.
  - Development uses **production Render with an internal test school**; the base URL stays configurable.

## 5. Open

- **The 10 November trial's scope** beyond activation and offline licence checking (Nitoni).
- **Unit 3 brief** — planning's next backend task. It must carry brief §9 R1 (one authoritative 24-hour request-ID history, five required tests) and R2 (make the ambiguity race test fail deterministically when its lock is removed). Unit 3's refresh is the first code that could take both a binding and a pool lock, so H3 applies.
- **Collector v2 → school survey → freeze thresholds**, before licensing is called complete.

## 6. Creating the internal test school (Nitoni)

The owner must be an existing ExamPartner user; register one for testing if needed. Run in PowerShell with the real base URL:

```powershell
$Base = "https://<your-render-backend>"
$admin = Invoke-RestMethod -Method Post -Uri "$Base/auth/login" -ContentType "application/json" `
  -Body (@{ identifier = "<admin identifier>"; password = "<admin password>" } | ConvertTo-Json)
$H = @{ Authorization = "Bearer $($admin.token)" }

$acct = Invoke-RestMethod -Method Post -Uri "$Base/institutions/accounts" -Headers $H -ContentType "application/json" `
  -Body (@{ name = "ZZ INTERNAL TEST SCHOOL - not a customer"; owner_identifier = "<test owner email>"; account_type = "institution" } | ConvertTo-Json)
$aid = $acct.account.id

$pool = Invoke-RestMethod -Method Post -Uri "$Base/institutions/accounts/$aid/seat_pools" -Headers $H -ContentType "application/json" `
  -Body (@{ pool_size = 5; label = "Dev lab" } | ConvertTo-Json)

Invoke-RestMethod -Method Patch -Uri "$Base/institutions/accounts/$aid/subscription" -Headers $H -ContentType "application/json" `
  -Body (@{ subscription_status = "active"; subscription_expires_at = "2027-01-31T23:59:59Z" } | ConvertTo-Json)

"account $aid  pool $($pool.seat_pool.id)"
```

Logging in **without** a `device_id` registers no device. The response shapes (`account.id`, `seat_pool.id`) are the merged Unit 2 code's, checked end to end by planning on 12 October. Retire the test data before the real pilot school is created.

## 7. How planning has worked — keep doing this

- **Decide only in documents.** A ruling that is not on `main` does not exist for the implementers. Update the docs, Nitoni uploads them, and only then brief or give a go-ahead.
- **Pre-code report, then go-ahead, then the unit, then the PR.** Every implementer unit starts with a report; planning answers it with written rulings (brief §4.D, §4.H).
- **Verify independently; don't relay.** For each PR, planning:
  - clones the branch, reruns the full suite, and repeats spec test 6 (the claim lock removed by hand);
  - removes race guards by hand and checks that their tests fail;
  - runs its own end-to-end script through the real app, separate from the PR's tests.
- **ChatGPT cross-reviews in parallel** (Nitoni relays it). Accept its points on the merits; say plainly where planning was wrong. Unit 2 shipped two corrections it found: ambiguity collection identity, and the fail-closed version gate.
- **Uploading.** Nitoni uploads docs through the GitHub web UI. Uploading a file of the same name replaces it in one commit, so never delete first. Nothing is uploaded into `backend/` while a backend branch is open.
- **Planning's environment.** Clone read-only with `git clone --depth 1 https://github.com/nitoni-jim/exampartner` (public repository). Local Postgres 16 is on `127.0.0.1:5433/ep_test`, with Python 3.11 in `/opt/ep-venv`. A container reset can need `ep-pg-start`, or the setup script rerun.

## 8. Known small issues (not blocking)

- The SQLite startup warning `duplicate column name: revoke_reason` predates licensing and is harmless.
- Unit 2's ambiguity race test fails 2 of 3 times, not every time, when its lock is removed — brief §9 R2.
- The repository is public. Survey output (serial numbers, MAC addresses) must never be committed.
