# Local agent brief — ExamPartner Windows client

**For:** the local agent working on Nitoni's own Windows machine (`docs/WORKING_MODEL.md` §0.5 and §3)
**Owner:** Nitoni (solo developer; evenings and weekends)
**Date:** 12 October 2026 · **Status:** first brief

Read this first, then `docs/WORKING_MODEL.md`, then `docs/LICENCE_API_CONTRACT.md` in full. The contract is the only description of the server you may build against. Where this brief and the contract disagree on a wire detail, the contract wins; report the disagreement.

---

## 1. What this product is, in one paragraph

ExamPartner is a Nigerian WAEC/NECO/JAMB exam-preparation platform. Its Android app is live. This work adds a **native Windows (WinUI 3 / .NET) client** for schools and tutorial centres, licensed per PC from a **seat pool**: a school buys N seats, a technician activates each lab PC once, and the PC then runs **offline** on a signed 30-day lease that it renews whenever it is online. The first school trial is **10 November 2026**, on this native client — not the web app.

## 2. What you own

- **`windows/`** in the repository — the WinUI 3 client and everything that builds, tests and packages it. Create it; it does not exist yet.
- **`tools/fingerprint-survey/`**, for **work unit L0 only** (§5). Nitoni assigns it to you because revising the collector needs a real Windows machine to test on. After L0 it returns to planning.
- **All real-machine testing:** running the survey collector, activating real PCs, and checking DPAPI, the clock rule and offline behaviour.

## 3. What you do not own

- **`backend/`** — never touch it. If the client needs a backend change, write it up as a finding for planning; planning briefs the backend separately.
- **`docs/`** — planning's. You read it; you do not edit it. Propose changes as findings.
- **API shapes.** Build against the contract. If something you need is missing, **stop and report**. Do not invent a field, a code or a behaviour: the backend is built against the same document from another machine, and a guess silently diverges.
- **`main`.** Work on a branch, open a PR, never merge your own.
- **Secrets.** The production **private** signing key never touches this machine's repository, your code, your logs or your messages. You only ever need the **public** key (§4).

## 4. Facts you can rely on

- **Server.** During development the client talks to the **production** backend on Render, against one clearly named internal test school and pool that Nitoni creates with the admin endpoints. There is no staging server. **The API base URL must be configuration, never a constant in code**, so staging can be added later without a code change. Nitoni gives you the URL.
- **What the server implements today** (backend Unit 2, merged 12 October 2026):
  - `POST /licence/activation-session` (contract §4);
  - `POST /licence/activate` (contract §5);
  - signed leases (contract §6);
  - the error envelope (contract §9.3).
- **Not built yet** (backend Unit 3): `POST /licence/refresh`, collecting a *resolved* ambiguity, release. Build the client side of refresh to the contract now — it is fully specified in §9 — but expect any refresh call to return 404 until Unit 3 is deployed. Treat that 404 as "refresh not available"; keep using the current lease while it is valid, and never discard it.
- **Signing keys.**
  - **kid 1 — production.** Nitoni holds its public key in `%USERPROFILE%\ExamPartner-Secrets\LICENCE_SIGNING_PUBLIC_KEY_KID1.txt` (base64 of 32 raw bytes). The public key is **not** secret: compile it into the client.
  - **kid 0 — the published TEST key** (contract §6.4). Debug builds only; a release build must not trust it.
- **Test vectors.** Contract §6.4 holds a TEST public key, a valid lease, and three that must be rejected. They are your first unit tests.
- **Hardware readings.** Read every signal from **exactly** the source in contract §7.3 — the same WMI class and property the survey collector uses — and send values **raw**: untrimmed, placeholders included, never hashed or filtered. The server does all normalisation.

## 5. Work units

Do them in this order. For each unit, send a **pre-code report** first (§6), wait for go-ahead, then build, then report and stop.

### L0 — Survey collector v2 (`tools/fingerprint-survey/`)

The current collector records the identity signals but **not** the continuity fields the contract depends on (contract §7.3 notes this). Revise it so that survey data can exercise the server's continuity rules:

- **Add, per disk:** `index`, `media_type`, `pnp_device_id` and `is_system_disk`, determined exactly as contract §7.3 specifies (`Win32_DiskDrive` → `Win32_DiskDriveToDiskPartition` → `Win32_LogicalDiskToPartition` → the `Win32_LogicalDisk` matching `%SystemDrive%`).
- **Add, per network adapter:** `pnp_device_id`, `net_connection_id`, and `permanent_address` from `MSFT_NetAdapter` (`root\StandardCimv2`), matched on `InterfaceIndex`.
- **Add `collection_errors`** with the same meaning as the contract's.
- **Fix the strong-signals diagnostic.** It must count **distinct** serial values: `bios_serial` usually equals `system_serial`, and today it is counted twice. Also stop using `first_disk_serial` and `first_mac` in the CSV as though order meant anything. Use the system disk's serial and the first **onboard** (`PCI\`) adapter's permanent address instead.
- **Keep every existing JSON key and its meaning,** and bump `schema_version` to 2.
- **Keep it read-only, offline and runnable without administrator rights** — test as a standard user.
- **Keep `Clean-Value`** for the survey's own diagnostics only. The raw values must also be recorded, so the server's normaliser can be checked against them.

**Test it** on Nitoni's laptop and an office PC, including **from a USB stick**. The stick must appear in the disk list with `is_system_disk: false` and must not be chosen as the system disk.

**Survey output is private.** Never commit JSON or CSV output: the repository is **public**, and the files contain serial numbers and MAC addresses. Nitoni sends them to planning directly.

### L1 — Licence core (no UI)

A .NET class library inside `windows/`, plus a console harness. It contains:

1. **Signals reader** — builds `fingerprint_signals` exactly as contract §7.2–§7.3. Read `MachineGuid` through the **64-bit registry view**. Fill `collection_errors` for any class that fails to read, rather than sending nulls.
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
   - the activation session, activate and refresh;
   - `request_id` generated and **persisted before the first attempt**, and reused on retry (contract §9.2);
   - the error envelope mapped to the technician messages in §9.3;
   - the transport rules and timeouts in §9.4;
   - the activation token held **in memory only**;
   - the owner's password never stored or logged.
6. **Console harness** — for Nitoni to activate a real PC against the internal test school, print the result, and dump the stored state, never the token or password.

**Done means:**
- the §6.4 vectors pass;
- the clock rule is unit-tested with an injected clock;
- one real PC has been activated against production and its lease verifies with the kid 1 key;
- a second run on the same PC gives `restored`, or a replay — not a second seat.

### L2 — Activation UI and lease status (WinUI 3)

The technician's flow:
1. Sign in (contract §4).
2. Pick a pool.
3. Name the PC.
4. Activate.
5. See the result: `activated`, or `restored` with the PC's existing name.

Plus a status view — licensed until when, last refresh, and any clock fault or pending ambiguity, with the exact §9.3 messages. **Scope beyond this is not decided** (§7). Do not build exam-content features until planning briefs them.

### Packaging — decide early, build alongside L2

A finished app that cannot be installed on twenty lab PCs is not a deliverable (`WORKING_MODEL.md` §3). Your L1 pre-code report must recommend a packaging route, with its trade-offs. The candidates are MSIX with a sideloading certificate, or unpackaged with an installer. It must suit:
- per-machine installation by a school technician without the Microsoft Store;
- several Windows user accounts per PC, which is why storage is `LocalMachine` scope;
- offline labs.

## 6. How to work

- **Pre-code report before every unit:**
  - your environment (Windows version, .NET SDK, Windows App SDK versions, as installed);
  - any drift between this brief, the contract and reality;
  - your plan;
  - anything that blocks you.

  Then wait.
- **Ed25519 in .NET.** Check whether the .NET version you target includes Ed25519 in the base library. If it does not, propose a library — for example NSec or BouncyCastle — in the L1 report, with your reason. Do not pick one silently.
- **Every claim in a report is either verified by running or believed by reading.** Keep the two separate, as the backend reports do.
- **Real-machine facts beat assumptions.** If a WMI property behaves differently from the contract's description on a real PC, that is a finding for planning, not something to work around quietly.
- **No secrets** in code, logs, test fixtures or reports.

## 7. Open — not yours to decide

- **The 10 November trial's scope beyond activation and offline licence checking:** which exam features, content packs and screens the trial needs. Nitoni decides; planning briefs it.
- **When collector v2 goes to the pilot school's lab** — after L0 is tested on Nitoni's machines.
