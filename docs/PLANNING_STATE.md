# Planning state — ExamPartner Windows seat-pool licensing

**Handover from the planning windows of 5–19 October 2026.** Read this first in a new planning window, then `docs/WORKING_MODEL.md`.
**State as of:** 21 October 2026 (afternoon) · `main` at `0fea83a`, before this upload. This revision records Unit 3a merged, the Unit 3b brief (brief §4.K, §5), its pre-code rulings (§4.L) and contract v1.1. The Unit 3 plan and deadline are in §1, §2, §5 and §10, and the licence session token decision in §4.

---

## 1. Where things stand

| Area | State |
|---|---|
| **Backend Unit 1** — time helpers, schema, owner check, `claim_capacity()` | Merged as PR #37 (`3a8e313`). Deployed. |
| **Backend Unit 2** — fingerprint rules, activation session, activate and restore, lease signing, error envelope, pilot setup endpoints | Merged as PR #38 (`bdaff67`). 372 tests passing, 0 skipped. **Deployed to production** (confirmed by Nitoni, 17 October): signing key configured as kid 1, `/health` returns `"licensing": "ready"`. |
| **Android smoke test after the Unit 2 deploy** | Passed: the live Android app loads questions from the production backend. Auth, payment and device registration were **not** re-tested as part of this. |
| **Backend Unit 3a** — refresh, one fail-closed request history (brief §9 R1) covering every operation including release and resolution, ambiguity resolve and collect, release, admin ambiguity list, R2 race test | **Merged** as PR #39 (`6172561`), 21 October, six days ahead of target. Planning verified it independently: 521 passed, 0 skipped; every lock and J8 guard removed by hand fails its tests; planning's own end-to-end run passed 38 of 38. Record in brief §4.K K0. **Deploy pending** (§2). No change from survey finding S1 (§9.3). |
| **Backend Unit 3b** — licence session token, owner reads and rename, account editing, pool resizing, signals review, test-clock endpoint, contract v1.1 | **Pre-code report approved** 21 October (brief §4.L); ready to implement after this upload, aiming to finish 21 October. Briefed in brief §4.K and §5, contract v1.1 §10A. |
| **API contract** | `docs/LICENCE_API_CONTRACT.md` v1, 8 October 2026; §7.3 clarified 10 October; additive amendments for Unit 3a on 19 and 20 October (§9.2, §9.3, §10, §11). **v1.1** on 21 October for Unit 3b: the licence session token and the owner endpoints (§1, §3, §4, §10A, §11). All changes additive; change log §13. Signed-lease test vectors in §6.4. |
| **WinUI client** | Not started. Brief: `docs/LOCAL_AGENT_BRIEF.md`, revision 3 (19 October) — L0 collector v2, L1 licence core, L2 activation UI, packaging decision. L0 and L1 must pass on the two guaranteed PCs; the other three are opportunistic. |
| **Survey collector** | v1 in `tools/fingerprint-survey/`. Records the identity signals but not the continuity fields; v2 is local-agent work unit L0. |
| **Hardware survey** | **Preliminary v1 run done** on five of Nitoni's own PCs, 15–16 October (§9). It is the spec §7.4 first pass, not the school survey. Thresholds stay provisional until the school survey (spec §13 step 12). |
| **Internal test school** | Not created yet (§2). |

## 2. Waiting on Nitoni right now

1. **Deploy Unit 3a** manually on Render (PR #39 is merged); confirm `/health` shows `"licensing": "ready"`. The deploy adds one table, one column and one index.
2. **Upload this file, `docs/CLOUD_SESSION_BRIEF.md` and `docs/LICENCE_API_CONTRACT.md`** in one commit, replacing the existing versions (upload over them; do not delete first). The Unit 3b Cloud Session has started and its pre-code report is approved (brief §4.L); **give it the go-ahead only after this upload**, because it builds from `main`.
3. **Create the internal test school** for client development (§6 has the commands). The local agent's L1 needs it.
4. **Decide the 10 November trial's scope** beyond activation and offline licence checking.
5. **Start the local agent** on `docs/LOCAL_AGENT_BRIEF.md`.
6. **Keep the five v1 survey files** privately, outside the repository. L0 compares v2 output against them machine by machine — on the two guaranteed PCs, and on the others whenever you have access.

## 3. Documents and what each is for

| Path | Role |
|---|---|
| `docs/WORKING_MODEL.md` | Who does what across planning, the Cloud Session and the local agent; settled versus open decisions (§6). |
| `docs/LICENCE_API_CONTRACT.md` | The only place client and server shapes meet. **Wins on any wire detail.** |
| `docs/CLOUD_SESSION_BRIEF.md` | Backend brief. §4.A–K rulings, §5 current unit (**Unit 3b**). |
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
  - It runs under a **15-minute licence session token**, obtained with owner or admin credentials — not `/auth/login`. It registers no device. Owner and admin status are re-checked at activation.
  - The **lease** is Ed25519 over the ASCII payload segment, with `kid` 0 reserved for the published TEST key. A missing, malformed or TEST signing key → licensing unavailable.
  - Matching is **signal by signal**, never by hash. Identity *quality* (of a reading) and match *strength* (against a binding) are separate concepts (brief F1).
  - Signals are compared by **exact equality after normalisation** — no prefix or similarity matching. An identifier carried by two or more active bindings in a pool is unusable in that pool (F1).
  - A PC is **restored** only on exactly one strong match (same usable UUID *and* a shared usable serial). Weak matches go to admin review as an ambiguity, never to an automatic restore.
  - A **degenerate or weak PC** goes to admin review only if its system disk or onboard network card matches an active binding (F2). `MachineGuid` is **not** an activation signal (H1).
- **Retries.** Every state-changing licensing operation covered by the request history carries a `request_id`; the explicit exceptions — F11's setup routes and the test clock — are in brief §4.K K7. Replays return the stored outcome, with a fresh `server_time`. Ambiguity creation is race-safe through its own pool-row lock (H2).
- **Lock order** (H3): never hold a binding lock and then take a pool lock; if a path needs both, take the pool first.
- **The client:**
  - It stores its lease with **DPAPI in `LocalMachine` scope**.
  - The **clock rule** allows 24 hours of rollback; beyond that it needs an online refresh. A rollback never revokes and never uses a seat.
- **Deployment:**
  - Render deploys **manually**. The trial is the **native WinUI client**.
  - Development uses **production Render with an internal test school**; the base URL stays configurable.
- **The licence session token** (Nitoni, 19 October; built in Unit 3b, brief §4.K K1; contract v1.1 §1, §10A). The 15-minute token from `/licence/activation-session` becomes a **licensing-only management** token: activate, the owner's machine list and history, rename and release — nothing else, and useless on every Android route. It stays short-lived, memory-only, registers no device, and is bound to the signed-in identity's accounts. The WinUI client never uses `/auth/login`. Owner endpoints also keep accepting the ordinary bearer token, for PowerShell and support. The endpoint path stays `/licence/activation-session`; the contract's wording changes from "activation-only token" in v1.1.

## 5. Open

- **The 10 November trial's scope** beyond activation and offline licence checking (Nitoni).
- **Whether the trial's WinUI client gets an owner screen** (machine list, release, rename) — decided with the trial scope, not in Unit 3. School self-service needs those screens; ambiguity resolution stays with ExamPartner either way.
- **Collector v2 → rerun on the two guaranteed PCs (others when available) → school survey → freeze thresholds**, before licensing is called complete. The freeze must also answer survey question S1 (§9.3).
- **A live activation on Windows 10 1903 must happen before any school install.** OFFICE-PC-02 is no longer guaranteed, so this is not part of L1's "done". If PC-02 is not reached during L1, test on PC-02 or another 1903-or-older PC before the 10 November trial. If none is available, the pilot school's own PCs are the first test, and the OS-floor finding in the L1 report becomes the only evidence.

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

Logging in **without** a `device_id` registers no device. The response shapes (`account.id`, `seat_pool.id`) are the merged Unit 2 code's, checked end to end by planning on 12 October. Keep five seats: room for both guaranteed PCs and the three opportunistic ones (brief §4.1). Retire the test data before the real pilot school is created.

## 7. How planning has worked — keep doing this

- **Decide only in documents.** A ruling that is not on `main` does not exist for the implementers. Update the docs, Nitoni uploads them, and only then brief or give a go-ahead.
- **Pre-code report, then go-ahead, then the unit, then the PR.** Every implementer unit starts with a report; planning answers it with written rulings (brief §4.D, §4.H).
- **Verify independently; don't relay.** For each PR, planning:
  - clones the branch, reruns the full suite, and repeats spec test 6 (the claim lock removed by hand);
  - removes race guards by hand and checks that their tests fail;
  - runs its own end-to-end script through the real app, separate from the PR's tests.
- **ChatGPT cross-reviews in parallel** (Nitoni relays it). Accept its points on the merits; say plainly where planning was wrong. Unit 2 shipped two corrections it found: ambiguity collection identity, and the fail-closed version gate. On the survey, its repo check confirmed the exact-matching and Wi-Fi findings. Its review also corrected planning three times on S1:
- the wording, since both machines also have a distinct baseboard serial;
- the Dell diagnostic, which must verify the serial, not just the marker;
- the freeze question, which must be scoped to machines whose UUID encodes the serial.
- **Uploading.** Nitoni uploads docs through the GitHub web UI. Uploading a file of the same name replaces it in one commit, so never delete first. Nothing is uploaded into `backend/` while a backend branch is open.
- **Survey files** reach planning from Nitoni directly, never through the repository. Planning's documents record aggregate findings and machine labels only — no serial, MAC, UUID or MachineGuid.
- **Planning's environment.** Clone read-only with `git clone --depth 1 https://github.com/nitoni-jim/exampartner` (public repository). Local Postgres 16 is on `127.0.0.1:5433/ep_test`, with Python 3.11 in `/opt/ep-venv`. A container reset can need `ep-pg-start`, or the setup script rerun.

## 8. Known small issues (not blocking)

- The SQLite startup warning `duplicate column name: revoke_reason` predates licensing and is harmless.
- Unit 2's ambiguity race test fails 2 of 3 times, not every time, when its lock is removed — brief §9 R2.
- The repository is public. Survey output (serial numbers, MAC addresses) must never be committed.
- `fingerprint_service.py`'s header says the thresholds were set from two machines. That is still true of the code; the five-PC survey changed no threshold.

## 9. Preliminary survey — v1 collector, five PCs, 15–16 October

### 9.1 The machines

Labels only. Model names identify no machine.

All five were available for the v1 run. For later testing, only **PERSONAL-PC-01 and OFFICE-PC-01 are guaranteed** (Nitoni, 19 October). The other three are tested when access allows, and no unit's completion depends on them.

| Label | Machine | Windows | Later testing | Role in later tests |
|---|---|---|---|---|
| `PERSONAL-PC-01` | Dell Latitude 5410 laptop | 11 Pro 25H2 | Guaranteed | UUID built by Dell's scheme from the service tag (S1) |
| `OFFICE-PC-01` | HP 250 G10 laptop | 11 Pro 25H2 | Guaranteed | UUID is the system serial in ASCII (S1); its only PCI adapter is Wi-Fi; USB Ethernet dongle attached |
| `OFFICE-PC-02` | HP EliteDesk 800 G5 tower | **10 Pro 1903** (build 18362) | Opportunistic | Oldest OS — compatibility and activation test machine; USB Wi-Fi card attached |
| `OFFICE-PC-03` | HP EliteDesk 800 G4 DM | 10 Pro 20H2 | Opportunistic | Same-model control pair with PC-04 |
| `OFFICE-PC-04` | HP EliteDesk 800 G4 DM | 10 Pro 22H2 | Opportunistic | Same-model control pair; current Wi-Fi address is randomised |

### 9.2 What it showed

Verified by reading the five files and, for the server's behaviour, `backend/services/fingerprint_service.py` and `licensing_service.py` at `d60d6ff`:

- **Collection.** All five complete, no class failures, every UUID usable.
- **Uniqueness.** No UUID, system serial, baseboard serial or MachineGuid appears on two machines.
- **BIOS serial equals system serial on all five.** The server already counts serials as a set, so it counts that value once. v1's `strong_signals_present: 4` overstates it; L0 fixes the diagnostic.
- **Same-batch PCs look nearly alike.** PC-03 and PC-04 share model, board product and CPU ID (PC-02 shares the CPU ID too). Their baseboard serials and onboard Ethernet addresses differ only in the last characters. The server matches on exact normalised equality, so they stay apart. **Verified on 19 October**: planning replayed their v1 identity readings through `fingerprint_service` at `50df3c0`. They come out as `mismatch` in both directions, whether the pool holds one of them or both, and each is rated `strong`. A live two-seat activation in L1 confirms this when the pair is available; it is no longer required.
- **Network adapters.** Two of five have USB adapters. PC-01's only PCI adapter is Wi-Fi. PC-04's current Wi-Fi address has the locally-administered bit set. The server takes onboard adapters by the `PCI\` PnP prefix, prefers `permanent_address`, rejects locally administered addresses, and does **not** require Ethernet — so PC-01 has a continuity adapter. v1 cannot exercise any of this; v2 can.
- **Disk serials.** The two NVMe disks report serials grouped by underscores and ending in a full stop. The server only trims and upper-cases, so the value must reach it exactly as Windows reports it. L0 and L1 check that. *Believed, not verified:* a storage-driver change can change how the same NVMe disk reports its serial; watch for it.
- **Old Windows.** Three of five run Windows 10:
  - PC-02 (1903) and PC-03 (20H2) are on versions whose servicing ended years ago.
  - PC-04 runs 22H2, whose normal support ended on 14 October 2025 unless the machine is covered by Extended Security Updates.
  - School labs are likely to be similar or older.
  - This does not decide ExamPartner's OS floor. Microsoft's support status and whether the client runs are separate questions.
  - The L1 report checks the OS floor by reading, on any machine. A live test of the runtime, installer and TLS on 1903 happens on PC-02 if it is available during L1; otherwise it stays the open item in §5, to close before any school install.

### 9.3 S1 — a UUID built from a serial is not independent evidence

**Finding.** On two of the five machines the SMBIOS UUID is derived from the system serial:
- **OFFICE-PC-01 (HP 250 G10):** the UUID's 16 bytes, in Windows' little-endian field order, are the system serial in ASCII plus padding. Verified by decoding.
- **PERSONAL-PC-01 (Dell):** the UUID begins with Dell's marker and encodes the service tag, which is also the system serial. Verified by decoding: seven of its bytes, with the top bit cleared, are exactly the service tag's seven characters. Changing one character of the serial makes the test fail. Dell's encoding is not a plain ASCII copy. The byte positions come from this one machine, so collector v2 checks the characters, not their order (brief L0).

Both machines also have a **distinct baseboard serial**, so neither rests on a single value (the Dell's baseboard serial carries the service tag plus a board-specific part). But where the UUID is built from the system serial, "same UUID plus shared serial" can be one fact counted twice.

**Where it matters.** Restore needs exactly one strong match: same usable UUID and a shared usable serial. Suppose two *distinct* PCs on a derived-UUID platform carry the same **non-placeholder** system serial. The second PC strong-matches the first and restores onto its binding, so two PCs share one seat silently. The pool rule that disqualifies shared identifiers does not catch this: a restore never creates the second binding that would bring the count to two.

- A **placeholder** serial is normalised away, leaving no shared serial — at most a weak match, so admin review. That case is safe.
- The cost of the unsafe case is **seat leakage**, not lockout. No school loses access.

**Ruling.**
1. **No backend change now** — not in Unit 2, not in Unit 3. The evidence is two dissimilar machines with unique serials; the risk needs duplicated serials across distinct machines, which only a lab survey can show.
2. **L0 records it.** Collector v2 adds `uuid_encodes_serial` and an independent-signal count (brief L0).
3. **The threshold freeze (spec §13 step 12) must answer S1:** among machines whose `uuid_encodes_serial` is **not** `"no"`, does the same non-placeholder system serial occur on two distinct physical PCs?
   - If **none**: keep the rule and record the evidence.
   - If **any**: the two-signal strong rule is shown unsafe on that platform. Change it so a serial the UUID encodes cannot be the second signal. The candidate is to require the baseboard serial or the system disk as the second signal on those machines; the decision is made then, with the data.
   - **Out of scope for S1:** a serial shared by machines whose UUIDs are independent and different. Those machines cannot strong-match, because sharing a serial without the UUID is only a weak match and goes to admin review. Record such a case as survey evidence, not as this failure.
4. When the spec is next amended, its §7.3 survey questions gain S1.

## 10. Unit 3 plan and deadline (Nitoni, 19 October)

**The Cloud Session is available until 4 November 2026.** All of Unit 3 is finished before then, and before the 10 November trial. It is split into two PRs **for review only** — nothing is deferred past the trial.

| When | What |
|---|---|
| 21 October | Unit 3a merged (done, six days early). Unit 3b briefed. |
| About 30 October | Unit 3b PR, then planning's verification |
| By 4 November | Unit 3b PR. Both PRs implemented and reviewed enough to leave the Cloud Session. |
| 5–9 November | Merge, manual Render deploy, production end-to-end tests, Windows integration, trial preparation |
| 10 November | School trial |

Planning verifies each PR as §7 describes. If the 4 November date comes under pressure, planning says so as soon as it is visible, with what would not make it — never by quietly cutting scope.

**Operating the trial before any owner screen exists.** Release and ambiguity resolution are HTTP endpoints only. Planning writes PowerShell scripts for them, as §6 does for the test school, once 3a is merged.

