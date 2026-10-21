# Local agent brief — ExamPartner Windows client

**For:** the local agent working on Nitoni's own Windows machine (`docs/WORKING_MODEL.md` §0.5 and §3)
**Owner:** Nitoni (solo developer; evenings and weekends)
**Date:** 21 October 2026 · **Status:** revision 4 — the whole backend is live (contract v1.1), so refresh and ambiguity collection are built and tested against production in L1. The trial scope is decided, so §5 now runs from A0 and L0 to the full trial app (L2–L8). Two test PCs are guaranteed (§4.1). Replaces the 19 October brief.

Read this first, then `docs/WORKING_MODEL.md`, then `docs/LICENCE_API_CONTRACT.md` in full. The contract is the only description of the server you may build against. Where this brief and the contract disagree on a wire detail, the contract wins; report the disagreement.

---

## 1. What this product is, in one paragraph

ExamPartner is a Nigerian WAEC/NECO/JAMB exam-preparation platform. Its Android app is live. This work adds a **native Windows (WinUI 3 / .NET) client** for schools and tutorial centres, licensed per PC from a **seat pool**: a school buys N seats, a technician activates each lab PC once, and the PC then runs **offline** on a signed 30-day lease that it renews whenever it is online. The first school trial is **10 November 2026**, on this native client — not the web app.

## 2. What you own

- **`windows/`** in the repository — the WinUI 3 client and everything that builds, tests and packages it. Create it; it does not exist yet.
- **Reading the Android project, for A0 only** (§5) — read-only, nothing copied out of it into anything public.
- **`tools/fingerprint-survey/`**, for **work unit L0 only** (§5). Nitoni assigns it to you because revising the collector needs a real Windows machine to test on. After L0 it returns to planning.
- **All real-machine testing:** running the survey collector, activating real PCs, and checking DPAPI, the clock rule and offline behaviour.

## 3. What you do not own

- **`backend/`** — never touch it. If the client needs a backend change, write it up as a finding for planning; planning briefs the backend separately.
- **`docs/`** — planning's. You read it; you do not edit it. Propose changes as findings.
- **API shapes.** Build against the contract. If something you need is missing, **stop and report**. Do not invent a field, a code or a behaviour: the backend is built against the same document from another machine, and a guess silently diverges.
- **`main`.** Work on a branch, open a PR, never merge your own.
- **Secrets.** The production **private** signing key never touches this machine's repository, your code, your logs or your messages. You only ever need the **public** key (§4).
- **Hardware identifiers in public places.** The repository and its PRs are **public**. No serial number, MAC address, UUID or MachineGuid from a real machine goes into a commit, a test fixture, a PR description or a report. Refer to machines by their labels (§4.1).

## 4. Facts you can rely on

- **Server.** During development the client talks to the **production** backend on Render, against one clearly named internal test school with a five-seat pool, which Nitoni creates with the admin endpoints. There is no staging server. **The API base URL must be configuration, never a constant in code**, so staging can be added later without a code change. Today that URL is `https://exampartner-backend.onrender.com`. Nitoni gives you the test owner's credentials and the test pool's ID.
- **What the server implements today: all of contract v1.1**, deployed to production on 21 October 2026 (backend Units 1–3b, `main` at `d3c15b4`):
  - `POST /licence/activation-session` (contract §4), which issues the **licence session token**;
  - `POST /licence/activate` (contract §5);
  - `POST /licence/refresh` (contract §9), including expired-lease self-recovery and machine verification;
  - collecting a resolved ambiguity on both endpoints (contract §10);
  - the owner endpoints (contract §10A): the school's pools, computers and history, rename and release;
  - signed leases (contract §6) and the error envelope (contract §9.3).

  Nothing in the contract returns `404` any more because it is unbuilt. Treat any unexpected status by contract §9.3's unknown-code rule and §9.4's transport rules.
- **Contract v1.1 changes to note** (contract §13, 19–21 October):
  - the `client` object, and `426 client_upgrade_required`, apply only to `activation-session`, `activate` and `refresh` (§2); the §10A routes carry none;
  - the 15-minute token is the **licence session token** (§1, §4). The path `/licence/activation-session` and the field `activation_token` keep their names;
  - one 24-hour request history covers every operation (§9.2): a `request_id` belongs to one logical request, and reusing it for anything else is `request_id_reused`, even across endpoints;
  - `pool_inactive` can now come from refresh, `lease_revoked` from a replayed activation whose seat was since released, and `machine_mismatch` also covers a superseded installation (§9.3).
- **The owner endpoints exist, but the screens are not yours yet.** Whether the trial's client has an owner screen is undecided (§7). Do not build §10A screens until planning briefs them. L1's API client covers the activation and refresh lifecycle only.
- **Signing keys.**
  - **kid 1 — production.** Nitoni holds its public key in `%USERPROFILE%\ExamPartner-Secrets\LICENCE_SIGNING_PUBLIC_KEY_KID1.txt` (base64 of 32 raw bytes). The public key is **not** secret: compile it into the client.
  - **kid 0 — the published TEST key** (contract §6.4). Debug builds only; a release build must not trust it.
- **Test vectors.** Contract §6.4 holds a TEST public key, a valid lease, and three that must be rejected. They are your first unit tests.
- **Hardware readings.** Read every signal from **exactly** the source in contract §7.3 — the same WMI class and property the survey collector uses — and send values **raw**: untrimmed, placeholders included, never hashed or filtered. The server does all normalisation.

### 4.1 The five test PCs

Nitoni ran the v1 collector on these five machines on 15–16 October (`docs/PLANNING_STATE.md` §9). Nitoni holds their v1 survey files privately; they never enter the repository.

Only **two are guaranteed** for repeat testing. L0 and L1 are complete when they pass on those two. The other three are **opportunistic**: when Nitoni has access, rerun collector v2 and activation on them and report the results; when he does not, the unit is not blocked.

| Label | Machine | Windows | Availability | What it tests |
|---|---|---|---|---|
| `PERSONAL-PC-01` | Dell Latitude 5410 laptop | 11 Pro 25H2 | **Guaranteed** | A UUID built by Dell's scheme from the service tag. An NVMe serial ending in a full stop. |
| `OFFICE-PC-01` | HP 250 G10 laptop | 11 Pro 25H2 | **Guaranteed** | A UUID that is the system serial in ASCII. Its only PCI network adapter is Wi-Fi; a USB Ethernet dongle is attached. An NVMe serial ending in a full stop. |
| `OFFICE-PC-02` | HP EliteDesk 800 G5 tower | **10 Pro 1903** (build 18362) | Opportunistic | **The oldest OS** — collector, client runtime, installer and TLS to Render. A USB Wi-Fi card is attached. |
| `OFFICE-PC-03` | HP EliteDesk 800 G4 DM | 10 Pro 20H2 | Opportunistic | Same-model control pair with PC-04: identical model, board and CPU ID; serials and network addresses differ only in their last characters |
| `OFFICE-PC-04` | HP EliteDesk 800 G4 DM | 10 Pro 22H2 | Opportunistic | The other half of the pair. Its current Wi-Fi address is randomised. |

In every report, list which opportunistic PCs you tested and which you could not reach. Do not substitute another machine silently.

## 5. Work units

The trial on 10 November is a **full educational app**, not a licensing demo (`PLANNING_STATE.md` §11). The units, in order:

| Unit | What | Starts after |
|---|---|---|
| **A0** | Read-only inspection of the Android project | — (can run alongside L0, in its own session) |
| **L0** | Survey collector v2 | — |
| **L1** | Licence core | L0 |
| **L2** | App shell, activation and lease status, installer | L1 |
| **L3** | Content store and renderer | L2, and planning's content decisions |
| **L4** | Objective CBT | L3 |
| **L5** | Theory CBT and AI grading | L4; backend Unit 4 for grading |
| **L6** | Study | L3 |
| **L7** | Game | L3 |
| **L8** | Hardening on real PCs | L4–L7 |

A0, L0 and L1 are briefed in full below. **L2–L8 are outlined**: planning briefs each in full before it starts, from A0's report and planning's own content and API review. For every unit, send a **pre-code report** first (§6), wait for go-ahead, then build, then report and stop.

### A0 — Inspect the Android project (read-only)

The Android app is on Nitoni's machine and **not in GitHub**. Its behaviour is the specification for the Windows trial app, but its Kotlin and Compose code cannot be carried over to WinUI, and must never be copied anywhere public.

- **Read only.** Change nothing in the Android project, build nothing, commit nothing from it. Nitoni tells you where it is.
- **Report, for each of Study, Objective CBT, Theory CBT, the Game, the AI Theory Grader, and results and review:**
  - the user flow, screen by screen, and the rules: timers, navigation, submission, scoring, review, lives and question selection in the Game;
  - every backend call it makes: path, parameters, request and response fields used, and how errors are shown;
  - what it stores locally, and how it works offline;
  - how theory papers present sections, compulsory and choice questions, and sub-parts, and what it sends for grading (the `sub_answers` shape);
  - how it renders passages, tables, images, diagrams and formulae, and where those assets come from;
  - how it decides what content a user may see, including any free-tier or year restrictions.
- **Describe behaviour and contracts in prose and tables.** Quote at most a few lines of code where a rule cannot be stated otherwise. No source files, no screenshots of code.
- **The report goes to planning directly**, not into this repository. It describes a private codebase.
- **Separate what you verified by running the app from what you read in the code**, as every report does (§6).

### L0 — Survey collector v2 (`tools/fingerprint-survey/`)

The current collector records the identity signals but **not** the continuity fields the contract depends on (contract §7.3 notes this). Revise it so that survey data can exercise the server's continuity rules:

- **Add, per disk:** `index`, `media_type`, `pnp_device_id` and `is_system_disk`, determined exactly as contract §7.3 specifies (`Win32_DiskDrive` → `Win32_DiskDriveToDiskPartition` → `Win32_LogicalDiskToPartition` → the `Win32_LogicalDisk` matching `%SystemDrive%`).
- **Add, per network adapter:** `pnp_device_id`, `net_connection_id`, and `permanent_address` from `MSFT_NetAdapter` (`root\StandardCimv2`), matched on `InterfaceIndex`.
- **Add `collection_errors`** with the same meaning as the contract's.
- **Fix the strong-signals diagnostic.** `strong_signals_present` must count **distinct** serial values plus the UUID: `bios_serial` usually equals `system_serial`, and today it is counted twice. Also stop using `first_disk_serial` and `first_mac` in the CSV as though order meant anything. Use the system disk's serial and the first **onboard** (`PCI\`) adapter's permanent address instead.
- **Add `uuid_encodes_serial`** to `diagnostics`. Survey finding S1: some manufacturers build the UUID from the system serial, so the two are not independent evidence. Values:
  - `"ascii"` — the UUID's 16 bytes, in either byte order (`[Guid]::Parse(...).ToByteArray()` order, or the order of the hex string as written), contain the trimmed system serial as ASCII. Only test serials of 5 characters or more.
  - `"dell_scheme"` — the UUID begins `4C4C4544` (Dell's marker) **and** decoding it yields this machine's system serial. Dell encodes the service tag, but not as plain ASCII. The test: take the UUID's 16 bytes in the order of the hex string as written, and clear the top bit (`-band 0x7F`) of bytes 5, 6, 9, 10, 13, 14 and 15 (counting from 0). Those seven characters must be **exactly the characters of the trimmed, upper-cased system serial, in any order**, and the serial must be 7 characters long. The positions come from one real Dell (PERSONAL-PC-01, verified by planning), so the test deliberately ignores their order.
  - `"dell_marker_unverified"` — the marker is present but the test above fails. **Report it as a finding** in your L0 report: either this machine's encoding differs from the one sample, or the UUID is not the serial.
  - `"no"` — none of the above.
  - `null` — the UUID or the system serial is absent.
- **Add `independent_strong_signals`** to `diagnostics`: the distinct serials, plus 1 for the UUID only when `uuid_encodes_serial` is `"no"`. Any other value, `"dell_marker_unverified"` included, counts the UUID as not independent.
- **Keep every other existing JSON key and its meaning,** and bump `schema_version` to 2.
- **Keep it read-only, offline and runnable without administrator rights** — test as a standard user.
- **Keep `Clean-Value`** for the survey's own diagnostics only. The raw values must also be recorded, so the server's normaliser can be checked against them.

**Mandatory tests — both guaranteed PCs** (PERSONAL-PC-01 and OFFICE-PC-01), as a standard user, each also run **from a USB stick**. The stick must appear in the disk list with `is_system_disk: false` and must not be chosen as the system disk. On each, check:

- **v1 → v2 agreement.** Every value v1 recorded is **identical, character for character**, in v2's output for the same machine — including the NVMe disk serials with their underscores and trailing full stop. Ignore only `collected_at_utc`, `schema_version` and the `diagnostics` that this unit changes. Any difference is a finding for planning, not something to fix in the collector.
- **Exactly one** disk has `is_system_disk: true`.
- **Network adapters:**
  - PC-01's USB Ethernet dongle is recorded, with a PnP ID that does **not** begin `PCI\`.
  - PC-01's Wi-Fi adapter **does** begin `PCI\`, and its `permanent_address` is recorded.
  - Bluetooth entries, if recorded, do not begin `PCI\`.
- **`uuid_encodes_serial`** is `"ascii"` on OFFICE-PC-01 and `"dell_scheme"` on PERSONAL-PC-01.
- **Synthetic test of the `"no"` value.** Neither guaranteed PC gives `"no"`, so test the classifier as a function. Feed it made-up UUID and serial pairs, never real ones:
  - an independent UUID → `"no"`;
  - a UUID built from a made-up serial's ASCII → `"ascii"`;
  - a Dell-marker UUID whose characters don't match the serial → `"dell_marker_unverified"`;
  - a missing serial → `null`.

**Opportunistic tests — when Nitoni has access.** Run the same checks, plus:
- **OFFICE-PC-02:** the USB Wi-Fi card's PnP ID does not begin `PCI\`, and the collector runs with no errors on Windows 10 1903 and PowerShell 5.1, including `MSFT_NetAdapter`.
- **OFFICE-PC-04:** its Wi-Fi `permanent_address` is recorded, and is not the randomised current address.
- **PC-02, PC-03 and PC-04:** `uuid_encodes_serial` is `"no"`.

**Survey output is private.** Never commit JSON or CSV output; Nitoni sends the files to planning directly. Your L0 report and PR give results **per machine label as pass/fail**, with no identifiers.

### L1 — Licence core (no UI)

A .NET class library inside `windows/`, plus a console harness. It contains:

1. **Signals reader** — builds `fingerprint_signals` exactly as contract §7.2–§7.3. Read `MachineGuid` through the **64-bit registry view**. Fill `collection_errors` for any class that fails to read, rather than sending nulls. Send **every** adapter and disk the contract names, USB ones included; the server filters.
2. **Lease verifier** — contract §6.1, step by step: format check, select the key by `kid`, verify the signature over the ASCII payload segment, then parse. The four §6.4 vectors are unit tests.
3. **Protected storage** — DPAPI in **`LocalMachine`** scope, under `%ProgramData%\ExamPartner\` (contract §8.1). It holds:
   - the lease string, exactly as received;
   - `installation_id`;
   - the highest `lease_serial` seen;
   - `time_high_water`;
   - any pending `ambiguity_id`;
   - the in-flight `request_id`.
4. **Offline validity and the clock rule** — contract §8.2–§8.3 exactly, including the 24-hour rollback tolerance and "online overrides the clock".
5. **API client:**
   - the activation session, activate and refresh, and collecting a resolved ambiguity on both (contract §10), always under a **new** `request_id`;
   - `request_id` generated and **persisted before the first attempt**, and reused on retry (contract §9.2);
   - the error envelope mapped to the technician messages in §9.3;
   - the transport rules and timeouts in §9.4;
   - the licence session token held **in memory only**, for the current session, and dropped when the technician finishes, at `expires_at`, or when the app closes (contract §4);
   - the owner's password never stored or logged.
6. **Console harness** — for Nitoni to activate a real PC against the internal test school, print the result, and dump the stored state, never the token or password. It also has a **local-only** option to write the `fingerprint_signals` it would send to a file, for comparison with collector v2 on the same PC. That file is private, like survey output.

**Exact preservation of raw values.** The server only trims and upper-cases, so a string altered anywhere on the client never matches. Unit-test it: a **synthetic** NVMe-style serial (made up — underscore-grouped hex ending in a full stop), plus values with leading or trailing spaces, must reach the request body unchanged. What must be unchanged is the string the server decodes from the JSON, not the escaped bytes on the wire. On each real PC, every value in the harness's signals file must equal the collector v2 value for the same key.

**Done means** — all of these, on the two guaranteed PCs:
- the §6.4 vectors pass;
- the clock rule is unit-tested with an injected clock;
- **PERSONAL-PC-01 and OFFICE-PC-01** are both activated against production, each gets its own seat, and each lease verifies with the kid 1 key;
- **OFFICE-PC-01** activates with its USB dongle present in `mac_addresses`;
- a second run on each gives `restored`, or a replay — not a second seat;
- **refresh works against production:** a refresh issues a new lease that verifies with the kid 1 key, with `lease_serial` one higher; repeating that refresh's `request_id` returns the **identical** lease, with no new serial (contract §9.2);
- **release reaches the client:** Nitoni releases one guaranteed PC with planning's script; that PC's next refresh gets `lease_revoked`, and the client **discards** its lease and shows the §9.3 message. Re-activating it then claims a seat again (a release is never undone by recognition);
- each PC's harness signals file matches its collector v2 output.

**Opportunistic, when Nitoni has access — not required for done:**
- **PC-03 and PC-04** each get their own seat (`activated`): neither restores onto the other's binding, and neither raises an ambiguity. Planning has already replayed their v1 readings through the server's matching code and they do not match (`PLANNING_STATE.md` §9.2). This is the live confirmation.
- **PC-02** activates on Windows 10 1903, over TLS to Render. If it is not tested in L1, it remains an open item that must be closed before any school install (`PLANNING_STATE.md` §5).

The test pool has five seats, which is room for all five PCs. Do not activate any machine outside §4.1 without asking Nitoni.

### L2 — App shell, activation, lease status and the installer

The first WinUI 3 screens, and the installer that puts them on a lab PC.
- **Activation:** the technician signs in (contract §4), picks a pool, names the PC, activates, and sees `activated` or `restored` with the PC's existing name.
- **Lease status:** licensed until when, last refresh, and any clock fault or pending ambiguity, with "Check again" (contract §10), all using the exact §9.3 messages.
- **The app shell** the educational units plug into: navigation, an offline indicator, and a licence check at start that blocks the educational features without a valid lease.
- **The installer:** your L1 pre-code report recommends a packaging route — MSIX with a sideloading certificate, or unpackaged with an installer — with its trade-offs. It must suit per-machine installation by a school technician without the Microsoft Store; several Windows user accounts per PC (hence `LocalMachine` storage); offline labs; and **Windows 10 1903 (build 18362)**. A finished app that cannot be installed on twenty lab PCs is not a deliverable (`WORKING_MODEL.md` §3).
- **No owner screen.** Contract §10A's owner routes are live, but they are **out of the trial** unless Nitoni adds them (§7). Release, rename and pool changes go through planning's admin scripts.

### L3–L8 — outlined; each briefed in full before it starts

- **L3 — Content store and renderer.** Install a verified content pack, store it locally, and render every question faithfully: wording, options, passages, tables, images, diagrams, and mathematical and scientific notation, with explanations. **Never generate or substitute a question.** How packs are built and delivered is planning's decision, pending its content review: a licensed PC cannot fetch full content from the current routes, which serve paid users only (`PLANNING_STATE.md` §11.2 G2).
- **L4 — Objective CBT.** A timed exam from an installed paper: navigation, flagging, submission, scoring, results and review, to A0's behaviour.
- **L5 — Theory CBT and AI grading.** The real paper's sections, compulsory and choice questions, and sub-parts; answers written offline and kept locally; **grading online only**, through backend Unit 4's school-credit route, showing the remaining balance and a clear message when it is exhausted. No offline grading queue is promised.
- **L6 — Study.** Browse by subject, year and topic, with explanations, to A0's behaviour.
- **L7 — Game.** Android's game adapted: three lives a session, randomised questions, by subject, from licensed content. A0 confirms the exact rules. No multiplayer or leaderboards.
- **L8 — Hardening on real PCs.** Offline use, a network dropping mid-exam, restarts, the clock rule, and the installer on both guaranteed PCs and, if reachable, OFFICE-PC-02 on Windows 10 1903.

Then the **trial rehearsal** (Nitoni, with planning): full runs on real PCs, content-fidelity checks, and a go/no-go before 10 November.

## 6. How to work

- **Pre-code report before every unit:**
  - your environment (Windows version, .NET SDK, Windows App SDK versions, as installed);
  - any drift between this brief, the contract and reality;
  - your plan;
  - anything that blocks you.

  Then wait.
- **OS floor (L1 report).** State the minimum Windows build that your chosen .NET version, Windows App SDK version and packaging route each support. Cite the source, and say plainly whether build 18362 is inside all three. If it is not, **stop and report**: dropping old lab PCs is planning's and Nitoni's decision, not a version choice to make quietly. This check is by reading, so it applies whether or not OFFICE-PC-02 is available.
- **Ed25519 in .NET.** Check whether the .NET version you target includes Ed25519 in the base library. If it does not, propose a library — for example NSec or BouncyCastle — in the L1 report, with your reason. Do not pick one silently. It must also run on build 18362.
- **Every claim in a report is either verified by running or believed by reading.** Keep the two separate, as the backend reports do.
- **Real-machine facts beat assumptions.** If a WMI property behaves differently from the contract's description on a real PC, that is a finding for planning, not something to work around quietly.
- **No secrets or hardware identifiers** in code, logs, test fixtures, PRs or reports.

## 7. Open — not yours to decide

- **The trial scope is decided** (`PLANNING_STATE.md` §11): Objective CBT, Theory CBT and AI grading (mandatory), Study and the Game, offline from verified content, no student logins and no teacher dashboard. **Changing it is Nitoni's decision.** If a unit runs late, report it; never trim a feature quietly.
- **The owner screen** (contract §10A) is out of the trial unless Nitoni adds it.
- **Content breadth:** which exams, subjects and years ship is decided from planning's coverage review, never assumed.
- **When collector v2 goes to the pilot school's lab** — after L0 passes on the two guaranteed §4.1 PCs.
