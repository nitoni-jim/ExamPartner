# Planning state — ExamPartner Windows seat-pool licensing

**Handover from the planning windows of 5–21 October 2026.** Read this first in a new planning window, then `docs/WORKING_MODEL.md`.
**State as of:** 21 October 2026 (evening) · `main` at `d3c15b4`, before this upload. **The backend for the trial is complete and deployed**: Units 1–3b are merged, and production runs `d3c15b4`. This revision records Unit 3 done, refreshes the local agent's brief to match (revision 4), records **Nitoni's trial-scope decision of 21 October** (§1, §5), and hands over to the next planning window (§2, §11).

---

## 1. Where things stand

| Area | State |
|---|---|
| **Backend Unit 1** — time helpers, schema, owner check, `claim_capacity()` | Merged as PR #37 (`3a8e313`). Deployed. |
| **Backend Unit 2** — fingerprint rules, activation session, activate and restore, lease signing, error envelope, pilot setup endpoints | Merged as PR #38 (`bdaff67`). 372 tests passing, 0 skipped. **Deployed to production** (confirmed by Nitoni, 17 October): signing key configured as kid 1, `/health` returns `"licensing": "ready"`. |
| **Android smoke test after the Unit 2 deploy** | Passed: the live Android app loads questions from the production backend. Auth, payment and device registration were **not** re-tested as part of this. |
| **Backend Unit 3a** — refresh, one fail-closed request history (brief §9 R1) covering every operation including release and resolution, ambiguity resolve and collect, release, admin ambiguity list, R2 race test | **Merged** as PR #39 (`6172561`), 21 October. Planning verified it independently: 521 passed, 0 skipped; every lock and J8 guard removed by hand fails its tests; planning's own end-to-end run passed 38 of 38. Record in brief §4.K K0. **Deployed** with Unit 3b. No change from survey finding S1 (§9.3). |
| **Backend Unit 3b** — licence session token, owner reads and rename, account editing, pool resizing, signals review, test-clock endpoint, contract v1.1 | **Merged** as PR #40 (`ef689cb`; merge `d3c15b4`), 21 October. Planning verified it independently: 599 passed, 0 skipped; the rename, account and pool-edit locks each fail their tests when removed by hand, and the shrink test fails only when the claim lock goes (L3); planning's own end-to-end run passed 38 of 38. Record in brief §4.M. **Deployed** 21 October: Render live on `d3c15b4`, startup clean, `/health` ready. |
| **API contract** | `docs/LICENCE_API_CONTRACT.md` v1, 8 October 2026; §7.3 clarified 10 October; additive amendments for Unit 3a on 19 and 20 October (§9.2, §9.3, §10, §11). **v1.1** on 21 October for Unit 3b: the licence session token and the owner endpoints (§1, §3, §4, §10A, §11). All changes additive; change log §13. Signed-lease test vectors in §6.4. |
| **WinUI client** | Not started. Brief: `docs/LOCAL_AGENT_BRIEF.md`, **revision 4** (21 October) — the whole of contract v1.1 is live; L0 collector v2, L1 licence core (refresh and ambiguity collection now testable against production), L2 activation UI, packaging decision. L0 and L1 must pass on the two guaranteed PCs; the other three are opportunistic. |
| **Survey collector** | v1 in `tools/fingerprint-survey/`. Records the identity signals but not the continuity fields; v2 is local-agent work unit L0. |
| **Hardware survey** | **Preliminary v1 run done** on five of Nitoni's own PCs, 15–16 October (§9). It is the spec §7.4 first pass, not the school survey. Thresholds stay provisional until the school survey (spec §13 step 12). |
| **Internal test school** | Not created yet (§2). |
| **10 November trial scope** | **Decided** by Nitoni, 21 October (§11). The full record, with its commercial and school context, is the private project document `claude/Agreed_Trial_Scope_2026-10-21.md` — **never commit it to this public repository**. Not yet planned in detail. |

## 2. Waiting on Nitoni right now

1. **Upload this file, `docs/LOCAL_AGENT_BRIEF.md`, `docs/CLOUD_SESSION_BRIEF.md` and `docs/WORKING_MODEL.md`** in one commit, replacing the existing versions (upload over them; do not delete first). Archive both Unit 3 Cloud Sessions; no backend unit is open.
2. **Create the internal test school** for client development (§6 has the commands). The local agent's L1 needs it.
3. **Start the local agent on A0 and L0** (§11.3), in two separate local sessions if you like: A0 only reads the Android project.
4. **Run P1's coverage report** when planning sends it (§11.3).
5. **Keep the five v1 survey files** privately, outside the repository. L0 compares v2 output against them machine by machine — on the two guaranteed PCs, and on the others whenever you have access.

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
- **Lock order:** account → pool → ambiguity → binding (H3, J6, L5). A path that takes more than one takes them in that order. Today only resolve (pool → ambiguity) and collection (ambiguity → binding) take two; the account lock is never taken with another.
- **The client:**
  - It stores its lease with **DPAPI in `LocalMachine` scope**.
  - The **clock rule** allows 24 hours of rollback; beyond that it needs an online refresh. A rollback never revokes and never uses a seat.
- **Deployment:**
  - Render deploys **manually**. The trial is the **native WinUI client**.
  - Development uses **production Render with an internal test school**; the base URL stays configurable.
- **The licence session token** (Nitoni, 19 October; built in Unit 3b, brief §4.K K1; contract v1.1 §1, §10A). The 15-minute token from `/licence/activation-session` becomes a **licensing-only management** token: activate, the owner's machine list and history, rename and release — nothing else, and useless on every Android route. It stays short-lived, memory-only, registers no device, and is bound to the signed-in identity's accounts. The WinUI client never uses `/auth/login`. Owner endpoints also keep accepting the ordinary bearer token, for PowerShell and support. The endpoint path stays `/licence/activation-session`; the contract's wording changes from "activation-only token" in v1.1.

## 5. Open

- **The Windows plan for the trial (§11)** — units P0–R, with their dependencies and a cut line. The scope is decided; the risk is the schedule.
- **The owner screen is out of the trial** unless Nitoni adds it (§11.1). Its backend is live (contract v1.1 §10A); adding it later is client work only.
- **Backend Unit 4** (§11.3 B4) — school AI credits and content entitlement. Not yet designed; briefed after P1; due by 4 November.
- **Spec drift to fold back into the spec** (brief J7, K0): spec test 28 expects `401`; §8.6 and §8.6.1 are behind I1–I2. Not urgent; the brief wins until then.
- **Collector v2 → rerun on the two guaranteed PCs (others when available) → school survey → freeze thresholds**, before licensing is called complete. The freeze must also answer survey question S1 (§9.3).
- **A live activation on Windows 10 1903 must happen before any school install.** OFFICE-PC-02 is no longer guaranteed, so this is not part of L1's "done". If PC-02 is not reached during L1, test on PC-02 or another 1903-or-older PC before the 10 November trial. If none is available, the pilot school's own PCs are the first test, and the OS-floor finding in the L1 report becomes the only evidence.

## 6. Creating the internal test school (Nitoni)

The owner must be an existing ExamPartner user; register one for testing if needed. Run in PowerShell with the real base URL:

```powershell
$Base = "https://exampartner-backend.onrender.com"
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

## 10. Unit 3 — done; the road to 10 November

**Unit 3 is finished, merged and deployed on 21 October**, 14 days before the 4 November Cloud Session deadline (Nitoni's plan of 19 October). It went in two PRs for review only; nothing was deferred.

The schedule from here to the trial is §11.4.

The Cloud Session remains available until 4 November if a backend fix is needed. Any backend change still goes brief → pre-code report → rulings on `main` → PR → planning's verification (§7).

## 11. The 10 November trial — decided scope, and the Windows plan

### 11.1 The decision (Nitoni, 21 October)

Full record, with its commercial and school context, in the **private** project document `claude/Agreed_Trial_Scope_2026-10-21.md`. Never commit it, or its commercial details, to this public repository.

| In the trial | Boundary |
|---|---|
| Native WinUI 3 client | Not a web wrapper |
| Objective CBT | **Mandatory**: timed exam, navigation, submission, results, review |
| Theory CBT | **Mandatory**: follows the real paper's section and sub-part rules |
| AI Theory Grader | **Mandatory**: online; 10 free credits as **one shared school pool** |
| Study | Included: Android's proven behaviour is the specification |
| Game | Included: Android's game adapted; no multiplayer or leaderboards |
| Content | WAEC, NECO and JAMB from **verified** content only — coverage audited before any breadth is promised |
| Offline | Study, CBT and Game from installed content packs, within the lease rules |
| **Not** in the trial | Student logins, a teacher dashboard, automated AI-credit purchasing, **and the owner-management screen** (contract §10A) unless Nitoni adds it — release, rename, signals and pool changes go through planning's admin scripts |

**AI credits:** one shared balance of 10 for the school, not per PC or per student. Android's per-user quotas are untouched. The server must hold the balance, identify the school authoritatively, debit under concurrency, make retries idempotent, refund or never charge a failed grading, and report the balance and an exhausted state. Android's `ai_grading_usage` is **not** assumed to fit. Automated top-ups are deferred; an auditable admin grant is enough for the trial.

Where the handoff is behind the repository (the repository wins): Unit 3 is merged and deployed (§1); the token is the licence session token (§4); the guaranteed office PC is **OFFICE-PC-01** (§9.1).

### 11.2 What the code says today — read by planning at `d3c15b4`

Believed by reading, not yet tested:

- **G1 — Grading is per user.** `POST /theory/grade` and `GET /ai-grading/quota` authorise with `get_current_user()` and count in `ai_grading_usage`, one row per user per period. A lab PC has no user and must never hold an account token. **School grading needs a new route authorised by the PC's signed lease**, debiting the school's balance. It can reuse the grading engine (`services/theory_service.py`, `theory_rubric_grading.py`) unchanged. → backend Unit 4.
- **G2 — Full content needs a paid user.** `/questions/objective`, `/questions/theory`, `/questions/study`, `/study/*` and `/cbt/*` pass `is_paid_user()` from a user token; a caller with no user gets the free tier. **A licensed lab PC cannot fetch full content as things stand.** How packs reach the lab is a decision (P1): a lease-authorised pack export, or packs built by an admin and shipped with the installer. Either is backend Unit 4 or an admin tool, not a client workaround.
- **G3 — Paper rules exist.** `/paper-rules/resolve` and `/cbt/theory-questions` serve the theory paper. Whether sections, compulsory questions, choices and sub-parts are enforced end to end is **not verified**. → P1.
- **G4 — Assets.** Attachments are stored in R2. How question images, passages, tables and formulae are stored and served — and so what a pack must carry — is **not verified**. → P1 and A0.
- **G5 — Coverage is unknown here.** Content lives in Neon, which planning never touches. → P1, using a read-only report Nitoni runs.

### 11.3 The bounded units

| Unit | Who | What | Needs first |
|---|---|---|---|
| **P0** | Planning | Operating scripts: resolve an ambiguity, list signals, release or rename a PC, suspend or resize a pool, create the test school (§6) | — |
| **P1** | Planning | Read-only reconciliation: G2–G5; the content and paper-rules APIs; a coverage report by exam, subject, year, objective and theory, and marking-scheme status, run by Nitoni; the pack-delivery decision | — |
| **A0** | Local agent, read-only | Inspect the **Android project** (not in GitHub) and report the behaviour and contracts of Study, Objective CBT, Theory CBT, Game, the AI Grader, offline content and assets, and results. Rules and behaviour, never Kotlin or Compose code. Report delivered privately to planning. | — |
| **L0** | Local agent | Collector v2 (brief L0) | — |
| **L1** | Local agent | Licence core (brief L1) | L0, test school, P0's release script |
| **B4** | Cloud Session | Backend Unit 4: lease-authorised school grading on a shared credit ledger, and content entitlement for licensed PCs, as P1 decides | P1 (G1, G2); brief → pre-code report → rulings → PR. **Done by 4 November.** |
| **L2** | Local agent | App shell, activation and lease-status screens, and the **installer**, on the 1903 floor | L1 |
| **L3** | Local agent | Content store and renderer: install a verified pack, render questions with passages, tables, images and formulae faithfully | L2; P1 and A0 (pack format, assets) |
| **L4** | Local agent | Objective CBT | L3; A0 |
| **L5** | Local agent | Theory CBT with sections and sub-parts, and AI grading through B4 | L4 (shared exam engine); B4 deployed for grading |
| **L6** | Local agent | Study | L3; A0 |
| **L7** | Local agent | Game | L3; A0 |
| **L8** | Local agent | Hardening: offline, interrupted network, restart, clock, installer on real PCs | L4–L7 |
| **R** | Nitoni, with planning | Full trial rehearsals on real PCs, the Windows 10 1903 check (§5), content-fidelity checks, go/no-go | L8 |

**What runs in parallel:** P0, P1, A0 and L0 can all start now. B4 runs in the Cloud Session alongside L1–L3. L4–L7 share one client codebase, so by default they run one after another. L6 or L7 can run in a second local session **only** if planning splits folder ownership first.

**What blocks what:** B4 waits on P1. L3 waits on P1, A0 and the pack decision. L5's grading waits on B4 being deployed (its paper UI can come first). R waits on everything.

### 11.4 Schedule and the cut line

There are 19 days to 10 November, for a solo developer working evenings and weekends. Targets, not promises:

| By | Milestone |
|---|---|
| 24 October | P0, P1, A0 and L0 done; test school created; B4 briefed |
| 28 October | L1 done on both guaranteed PCs; B4 pre-code report ruled |
| 1 November | L2 and L3 done; B4 PR open. **Checkpoint**: Nitoni confirms or trims scope |
| 4 November | B4 merged and deployed (Cloud Session deadline) |
| 7 November | L4–L7 done |
| 8–9 November | L8 and R: rehearsals and go/no-go |

**The proposed cut line, if the 1 November checkpoint shows slippage** — Nitoni's decision, never a quiet cut: protect Objective CBT, Theory CBT and AI grading, which are mandatory. Then Study, which reuses L3's renderer. The Game goes first. Real-PC testing, offline behaviour, the installer, content fidelity and full rehearsals are **not** on the cut line.
