# Local agent brief — ExamPartner Windows client

**For:** the local agent working on Nitoni's own Windows machine (`docs/WORKING_MODEL.md` §0.5 and §3)
**Owner:** Nitoni (solo developer; evenings and weekends)
**Date:** 17 October 2026 · **Status:** revision 2 — adds the five test PCs (§4.1) and the preliminary-survey checks to L0 and L1. Replaces the 12 October brief.

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
- **Hardware identifiers in public places.** The repository and its PRs are **public**. No serial number, MAC address, UUID or MachineGuid from a real machine goes into a commit, a test fixture, a PR description or a report. Refer to machines by their labels (§4.1).

## 4. Facts you can rely on

- **Server.** During development the client talks to the **production** backend on Render, against one clearly named internal test school with a five-seat pool, which Nitoni creates with the admin endpoints. There is no staging server. **The API base URL must be configuration, never a constant in code**, so staging can be added later without a code change. Nitoni gives you the URL and the test owner's credentials.
- **What the server implements today** (backend Unit 2, merged 12 October 2026 and **deployed to production**):
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

### 4.1 The five test PCs

Nitoni ran the v1 collector on these five machines on 15–16 October (`docs/PLANNING_STATE.md` §9). They are the test set for L0 and L1. Nitoni holds their v1 survey files privately; they never enter the repository.

| Label | Machine | Windows | What it tests |
|---|---|---|---|
| `PERSONAL-PC-01` | Dell Latitude 5410 laptop | 11 Pro 25H2 | A UUID built by Dell's scheme from the service tag |
| `OFFICE-PC-01` | HP 250 G10 laptop | 11 Pro 25H2 | A UUID that is the system serial in ASCII. Its only PCI network adapter is Wi-Fi; a USB Ethernet dongle is attached. An NVMe serial ending in a full stop. |
| `OFFICE-PC-02` | HP EliteDesk 800 G5 tower | **10 Pro 1903** (build 18362) | **The oldest OS** — collector, client runtime, installer and TLS to Render. A USB Wi-Fi card is attached. |
| `OFFICE-PC-03` | HP EliteDesk 800 G4 DM | 10 Pro 20H2 | Same-model control pair with PC-04: identical model, board and CPU ID; serials and network addresses differ only in their last characters |
| `OFFICE-PC-04` | HP EliteDesk 800 G4 DM | 10 Pro 22H2 | The other half of the pair. Its current Wi-Fi address is randomised. |

If a machine is unavailable, say so in your report; do not substitute another one silently.

## 5. Work units

Do them in this order. For each unit, send a **pre-code report** first (§6), wait for go-ahead, then build, then report and stop.

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

**Test it on all five PCs in §4.1**, as a standard user, and **from a USB stick** on at least two of them. The stick must appear in the disk list with `is_system_disk: false` and must not be chosen as the system disk. On each PC, check:

- **v1 → v2 agreement.** Every value v1 recorded is **identical, character for character**, in v2's output for the same machine — including the NVMe disk serials with their underscores and trailing full stop. Ignore only `collected_at_utc`, `schema_version` and the `diagnostics` that this unit changes. Any difference is a finding for planning, not something to fix in the collector.
- **Exactly one** disk has `is_system_disk: true`.
- **Network adapters:**
  - USB adapters (PC-01's Ethernet dongle, PC-02's Wi-Fi card) are recorded, with a PnP ID that does **not** begin `PCI\`.
  - PC-01's Wi-Fi adapter **does** begin `PCI\`.
  - PC-04's Wi-Fi `permanent_address` is recorded, and is not the randomised current address.
  - Bluetooth entries, if recorded, do not begin `PCI\`.
- **`uuid_encodes_serial`** is `"ascii"` on OFFICE-PC-01, `"dell_scheme"` on PERSONAL-PC-01, and `"no"` on PC-02, PC-03 and PC-04.
- **PC-02** (Windows 10 1903, PowerShell 5.1) runs the collector with no errors, including `MSFT_NetAdapter`.

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
   - the activation session, activate and refresh;
   - `request_id` generated and **persisted before the first attempt**, and reused on retry (contract §9.2);
   - the error envelope mapped to the technician messages in §9.3;
   - the transport rules and timeouts in §9.4;
   - the activation token held **in memory only**;
   - the owner's password never stored or logged.
6. **Console harness** — for Nitoni to activate a real PC against the internal test school, print the result, and dump the stored state, never the token or password. It also has a **local-only** option to write the `fingerprint_signals` it would send to a file, for comparison with collector v2 on the same PC. That file is private, like survey output.

**Exact preservation of raw values.** The server only trims and upper-cases, so a string altered anywhere on the client never matches. Unit-test it: a **synthetic** NVMe-style serial (made up — underscore-grouped hex ending in a full stop), plus values with leading or trailing spaces, must reach the request body unchanged. What must be unchanged is the string the server decodes from the JSON, not the escaped bytes on the wire. On each real PC, every value in the harness's signals file must equal the collector v2 value for the same key.

**Done means:**
- the §6.4 vectors pass;
- the clock rule is unit-tested with an injected clock;
- all **five §4.1 PCs** are activated against production, and each lease verifies with the kid 1 key. In particular:
  - **PC-03 and PC-04** each get their own seat (`activated`) — neither restores onto the other's binding, and neither raises an ambiguity;
  - **PC-02** activates on Windows 10 1903, over TLS to Render;
  - **PC-01** activates with its USB dongle present in `mac_addresses`;
- a second run on a PC gives `restored`, or a replay — not a second seat;
- each PC's harness signals file matches its collector v2 output.

The five PCs fill the five-seat test pool exactly. Do not activate any other machine without asking Nitoni.

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
- offline labs;
- **Windows 10 1903 (build 18362)**, the oldest build in the test set. School labs may run builds as old.

## 6. How to work

- **Pre-code report before every unit:**
  - your environment (Windows version, .NET SDK, Windows App SDK versions, as installed);
  - any drift between this brief, the contract and reality;
  - your plan;
  - anything that blocks you.

  Then wait.
- **OS floor (L1 report).** State the minimum Windows build that your chosen .NET version, Windows App SDK version and packaging route each support. Cite the source, and say plainly whether build 18362 is inside all three. If it is not, **stop and report**: dropping old lab PCs is planning's and Nitoni's decision, not a version choice to make quietly.
- **Ed25519 in .NET.** Check whether the .NET version you target includes Ed25519 in the base library. If it does not, propose a library — for example NSec or BouncyCastle — in the L1 report, with your reason. Do not pick one silently. It must also run on build 18362.
- **Every claim in a report is either verified by running or believed by reading.** Keep the two separate, as the backend reports do.
- **Real-machine facts beat assumptions.** If a WMI property behaves differently from the contract's description on a real PC, that is a finding for planning, not something to work around quietly.
- **No secrets or hardware identifiers** in code, logs, test fixtures, PRs or reports.

## 7. Open — not yours to decide

- **The 10 November trial's scope beyond activation and offline licence checking:** which exam features, content packs and screens the trial needs. Nitoni decides; planning briefs it.
- **When collector v2 goes to the pilot school's lab** — after L0 passes on the five §4.1 PCs.
