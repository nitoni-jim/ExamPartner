# ExamPartner Windows — Licence API Contract

**Version:** 1 · **Date:** 8 October 2026 · **Owner:** Planning
**Both implementers build against this document.** The backend (Cloud Session) and the WinUI client (local agent) cannot see each other; this is the only place their shapes meet. If something you need is not here, **stop and report it** — do not invent a field, a code or a behaviour, because the other side is building against the same page and a guess silently diverges.

This contract governs the wire format and the client–server behaviour. The *business rules* behind it — capacity, matching, ambiguity, release — remain in `docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md` and `docs/CLOUD_SESSION_BRIEF.md`. Where this document and the spec disagree about a field name, a format or an error code, **this document wins** and the spec is updated to match.

---

## 1. Decisions this contract rests on

| Decision | Source |
|---|---|
| A technician activates a PC with a **15-minute, activation-only session**, obtained with the institution owner's (or an ExamPartner admin's) credentials. It is not `/auth/login`, registers no device against the owner's Android device allowance, and authorises activation only. After activation the PC holds the **signed lease**, never an account token. It is the precursor of the later 24-hour Deployment Session. | Nitoni, 8 Oct 2026 |
| A PC's clock may run up to **24 hours behind** the highest time previously accepted. Further back requires an **online refresh**. A rollback never revokes the activation or consumes a seat. | Nitoni, 8 Oct 2026 |
| The client sends **raw** hardware values. All normalisation, hashing and matching happen **on the server**, so a matching fix is a server deploy, not an update to every lab PC. | Planning |
| Every request carries the client's version, so a broken client can be stopped with `426`. | Planning |
| Every state-changing request carries a client-generated `request_id`, so a retry after a lost response never consumes a second seat and never looks like a clone. | Planning |
| The lease signature covers the exact bytes transmitted. The client verifies before it parses and never re-serialises. | Planning |

---

## 2. Conventions

- **Transport.** HTTPS only. Base URL is configuration, not code. JSON bodies, UTF-8, `Content-Type: application/json`.
- **Names.** `snake_case` everywhere.
- **Unknown fields.** Both sides **ignore** fields they do not recognise. New optional fields may appear in responses without a version change; never treat an unknown field as an error.
- **Optional fields.** An optional request field may be omitted or sent as `null`; the server treats both identically.
- **Timestamps on the wire.** RFC 3339, UTC, whole seconds, literal `Z`: `2026-10-08T09:00:00Z`. Always exactly this shape. (The backend's internal `now_iso()` produces `+00:00` with microseconds; that format never appears on the wire.)
- **`server_time`.** Every response body — success and error — carries `server_time`, the server's clock at the time of the response. The client uses it for its clock rule (§8).
- **IDs.** Server-generated IDs (`binding_id`, `installation_id`, `account_id`, `seat_pool_id`, `ambiguity_id`) are opaque 32-character lowercase hex strings. Never parse them. `request_id` is a client-generated UUID v4 string.
- **`client` object.** Every request body includes:

```json
"client": {
  "platform": "windows",
  "app_version": "1.0.0",
  "os_version": "10.0.22631"
}
```

`app_version` is semantic `MAJOR.MINOR.PATCH`. `os_version` is `Environment.OSVersion.Version` as a string; it is informational only.

---

## 3. Endpoints

| Method and path | Authorised by | Purpose |
|---|---|---|
| `POST /licence/activation-session` | Owner or admin **credentials** in the body | Exchange credentials for a 15-minute activation token and the list of pools the caller may activate into |
| `POST /licence/activate` | `Authorization: Bearer <activation_token>` | Activate this PC in a pool, or restore its activation after a reinstall |
| `POST /licence/refresh` | The **signed lease** in the body | Renew this PC's lease; no account involved |

No other endpoint is called by the Windows client in Pilot V1. Owner and admin endpoints (release, machine lists, ambiguity resolution) are not part of this contract.

---

## 4. `POST /licence/activation-session`

The technician enters the institution owner's ExamPartner email and password on the PC. The client sends them once, receives a short-lived token, and **discards the password immediately** — it is never written to disk, logged or retained in memory beyond this call.

### Request

```json
{
  "identifier": "owner@school.edu.ng",
  "password": "…",
  "client": { "platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631" }
}
```

### Response `200`

```json
{
  "activation_token": "opaque string",
  "expires_at": "2026-10-08T09:15:00Z",
  "actor_role": "institution_owner",
  "pools": [
    {
      "pool_id": "5e4d3c2b1a0f9e8d7c6b5a4938271605",
      "label": "Computer Lab",
      "account_id": "0d9c8b7a6f5e4d3c2b1a09f8e7d6c5b4",
      "account_name": "Example Secondary School",
      "pool_size": 20,
      "active": 18,
      "available": 2,
      "over_capacity": false,
      "pool_status": "active",
      "subscription_status": "active",
      "subscription_expires_at": "2027-09-30T23:59:59Z"
    }
  ],
  "server_time": "2026-10-08T09:00:00Z"
}
```

- `actor_role` is `institution_owner` or `exampartner_admin`.
- `pools` lists every pool of every account the caller owns. For an ExamPartner admin it lists **all** pools, each with its `account_name`, so Nitoni can activate a pilot PC without holding the school's password.
- Pools that are suspended, over capacity or on a lapsed subscription **are listed**, with their state, so the client can explain why activation will fail rather than hiding the pool.
- `activation_token` is opaque to the client. Use `expires_at` from the response; never decode the token. The client holds it **in memory only** and drops it after activation completes, after `expires_at`, or when the app closes.
- When there is exactly one pool the client may preselect it, but shows which pool it is before activating.

### Errors

`invalid_credentials` (401), `no_licensing_access` (403), `client_upgrade_required` (426), `licensing_unavailable` (503), plus the transport failures in §9.4.

---

## 5. `POST /licence/activate`

### Request

Header: `Authorization: Bearer <activation_token>`. A normal ExamPartner user token is **refused** here.

```json
{
  "request_id": "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b9c",
  "pool_id": "5e4d3c2b1a0f9e8d7c6b5a4938271605",
  "machine_label": "LAB-PC-07",
  "installation_id": null,
  "ambiguity_id": null,
  "fingerprint_signals": { "…": "see §7" },
  "client": { "platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631" }
}
```

| Field | Type | Rule |
|---|---|---|
| `request_id` | string, UUID v4 | Required. See §9.2. |
| `pool_id` | string | Required. Must be a pool the session's caller may activate into. |
| `machine_label` | string | Required. Trimmed, 1–64 characters. Human-friendly; the owner can rename it later. |
| `installation_id` | string \| null | The installation ID this PC already holds **locally**, if any (an app reinstall without a Windows reinstall can leave one). `null` on a fresh PC. The server treats it as a **clone signal only** — never as the identity of a new binding. |
| `ambiguity_id` | string \| null | Set only when collecting a resolved ambiguity (§10). |
| `fingerprint_signals` | object | Required. §7. |

### Response `200`

```json
{
  "result": "activated",
  "lease": "<signed lease, §6>",
  "binding_id": "6f1c2e0a9b8d4c7e5a3f2b1d0c9e8f7a",
  "installation_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "lease_expires_at": "2026-11-07T09:00:00Z",
  "machine_label": "LAB-PC-07",
  "account_name": "Example Secondary School",
  "pool_label": "Computer Lab",
  "server_time": "2026-10-08T09:00:00Z"
}
```

- `result` is `activated` (a new seat was used) or `restored` (this PC was recognised after a reinstall; **no new seat was used**). Show the technician which one happened.
- `installation_id` is **always server-issued**. Store it with the lease, replacing any local value. It also appears inside the lease.
- `lease_expires_at` repeats the lease's `expires_at` for display; the lease is authoritative.

### Errors, in the order the server checks them

FastAPI's own `422` for a body that does not match the schema comes first, before any of these, because it is raised before the route runs. Then: `activation_session_invalid` (401) → `client_upgrade_required` (426) → `invalid_request` (400) → `fingerprint_unreadable` (400) → `licensing_unavailable` (503) → `pool_access_denied` (403) → `pool_inactive` (409) → `subscription_inactive` (402) → `ambiguity_*` (409) → `pool_full` (409). When several apply, the first in this order is returned, so a suspended school is told about its subscription, not told to free a seat.

---

## 6. The signed lease

### 6.1 Format

```
<payload_segment>.<signature_segment>
```

- `payload_segment` = base64url, **no padding**, of the UTF-8 JSON payload bytes.
- `signature_segment` = base64url, **no padding**, of the 64-byte Ed25519 signature.
- **The signature is computed over the ASCII bytes of `payload_segment` exactly as transmitted** — not over decoded JSON, and not over re-serialised JSON.

Client verification, in this order:

1. Split on the single `.`. Exactly two non-empty segments, base64url alphabet only (`A–Z a–z 0–9 - _`), **no `=`**. Otherwise invalid.
2. Decode `payload_segment` and parse the JSON only far enough to read `kid`. Select the embedded public key for that `kid`; unknown `kid` → invalid.
3. Verify the signature over the ASCII bytes of `payload_segment`. Failure → invalid.
4. Only now parse and trust the payload. Require `v == 1` and `typ == "ep.licence.lease"`.

The client **never re-serialises** the payload and never depends on field order. It stores the lease string exactly as received.

### 6.2 Payload

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Lease format version. `1`. |
| `typ` | string | `"ep.licence.lease"`. Stops any other signed object being accepted as a lease. |
| `kid` | int | Signing key id. `0` is reserved for the published TEST key (§6.4). Production keys start at `1`. |
| `binding_id` | string | The activation identity. |
| `installation_id` | string | This installation. Must equal the client's stored value. |
| `account_id` | string | |
| `seat_pool_id` | string | |
| `machine_fingerprint_hash` | string | 64 lowercase hex. A server-side snapshot. **The client never computes or compares it.** |
| `lease_serial` | int | Increases on each new lease. Equal on an idempotent replay (§9.2). |
| `issued_at` | timestamp | |
| `expires_at` | timestamp | `min(issued_at + 30 days, subscription_expires_at)`. |
| `subscription_expires_at` | timestamp | For display ("subscription ends …"). |

Display-only values (`machine_label`, `account_name`, `pool_label`) are **not** in the payload, so renaming a machine never requires re-signing.

### 6.3 Server serialisation

The server serialises the payload as `json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)`. That makes the bytes deterministic, and Ed25519 is deterministic, so re-issuing an unchanged lease produces **byte-identical** output — which is what makes the idempotent replay in §9.2 exact. Clients must not rely on this ordering; it is stated so the backend keeps it stable.

### 6.4 Test vectors

**TEST KEY ONLY.** Derived from a published string, so anyone can recompute it. It must never be configured as `LICENCE_SIGNING_KEY`, and a release build of the client must never trust `kid 0`.

```
TEST seed (base64, 32-byte Ed25519 private seed):  vaqSxX9ClSwoMECP/J92vJWuEQYqyktbMlFU8YPu+Lg=
TEST public key (base64, 32-byte raw):            fnYIDrqbBrz13D83z4RbYt9zzmDtbuoZ/4rCCROaEXQ=
kid:                                              0
```

Payload JSON as the server serialises it:

```
{"account_id":"0d9c8b7a6f5e4d3c2b1a09f8e7d6c5b4","binding_id":"6f1c2e0a9b8d4c7e5a3f2b1d0c9e8f7a","expires_at":"2026-11-07T09:00:00Z","installation_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","issued_at":"2026-10-08T09:00:00Z","kid":0,"lease_serial":3,"machine_fingerprint_hash":"fd2ec5b521ad183152a5a69ee0acda6c271abbc60a97da48942225da41214566","seat_pool_id":"5e4d3c2b1a0f9e8d7c6b5a4938271605","subscription_expires_at":"2027-09-30T23:59:59Z","typ":"ep.licence.lease","v":1}
```

**Valid** — must verify, and must decode to exactly the payload above:

```
eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5nX2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAyNi0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4MjkzYTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjowLCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFhZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJzZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlwdGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNlLmxlYXNlIiwidiI6MX0.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfggqRQy59pgqFsORu3zCi6YmyZBOiA7RDw
```

**Tampered** — `lease_serial` changed from 3 to 4, original signature kept. Must be **rejected**:

```
eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5nX2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAyNi0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4MjkzYTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjowLCJsZWFzZV9zZXJpYWwiOjQsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFhZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJzZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlwdGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNlLmxlYXNlIiwidiI6MX0.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfggqRQy59pgqFsORu3zCi6YmyZBOiA7RDw
```

**Wrong key** — same payload, signed with a different key. Must be **rejected**:

```
eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5nX2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAyNi0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4MjkzYTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjowLCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFhZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJzZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlwdGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNlLmxlYXNlIiwidiI6MX0.7BQrUeYISJVVA_rZq1mkCA4E3HYq_w-Zp6Rxve6HFCfHtV8BmMdwnD925NS8qH_IAqyw1lYJT7DmSZJU141FDg
```

**Padded** — the valid lease with `=` padding added to the payload segment. Must be **rejected** (step 1 of §6.1, and the signature no longer matches):

```
eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5nX2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAyNi0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4MjkzYTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjowLCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFhZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJzZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlwdGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNlLmxlYXNlIiwidiI6MX0=.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfggqRQy59pgqFsORu3zCi6YmyZBOiA7RDw
```

Both implementations should carry these four as unit tests. They were generated with the Python `cryptography` package, the library the backend will use.

### 6.5 Keys

- **Backend:** `LICENCE_SIGNING_KEY` = standard base64 of the 32-byte raw Ed25519 private seed (one line, so it pastes cleanly into Render). `LICENCE_SIGNING_KID` = the matching integer, `1` for the first production key. If either is absent or malformed, or the key equals the published TEST seed above, licensing is **unavailable** (503 on every licence endpoint, ERROR in the log) — the backend still starts (brief §4.E).
- **Client:** a compiled-in map of `kid` → 32-byte public key. Release builds contain production kids only (`≥ 1`); development builds may also contain `kid 0`.
- **Generating the production key (Nitoni, once, on his own machine — never in a chat):**

```python
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
k = Ed25519PrivateKey.generate()
seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print("LICENCE_SIGNING_KEY =", base64.b64encode(seed).decode())   # Render secret only
print("public key (kid 1)  =", base64.b64encode(pub).decode())    # goes into the client source
```

Key rotation, hardware-backed storage and anti-tamper remain deferred (spec §8.7); `kid` exists so rotation needs no format change.

---

## 7. `fingerprint_signals`

### 7.1 Rules for the client

- Read every value from the **exact source in the table** — the same class and property the survey collector (`tools/fingerprint-survey/Collect-Fingerprint.ps1`) reads — so production readings and survey readings are the same values. Example of why this matters: the SMBIOS UUID read from the raw firmware table has a different byte order from `Win32_ComputerSystemProduct.UUID`; a client reading it the other way would never match.
- Send values **raw**: exactly the string WMI returns, untrimmed, unnormalised, placeholders included (`"To Be Filled By O.E.M."` is sent as-is). `null` only when the property itself is null or the instance does not exist.
- **Never** apply the collector's `Clean-Value`, never hash, never drop entries. Filtering and normalisation are the server's job (§7.4).
- Read CIM classes from `root\cimv2` unless stated. A standard user can read all of them; no elevation.
- If a class cannot be read at all (WMI error, service stopped), put its name in `collection_errors` rather than sending nulls. A failed read is **not** an absent value — conflating them would make a working PC look like a degenerate one.

### 7.2 Shape

```json
"fingerprint_signals": {
  "schema": 1,
  "smbios_uuid": "4C4C4544-0042-3510-8051-B4C04F4D3732",
  "system_manufacturer": "Dell Inc.",
  "system_model": "OptiPlex 3080",
  "system_serial": "B5QMT32",
  "system_version": "",
  "baseboard_manufacturer": "Dell Inc.",
  "baseboard_product": "0RD3Y3",
  "baseboard_serial": "/B5QMT32/CNFCW0012345/",
  "baseboard_version": "A00",
  "bios_manufacturer": "Dell Inc.",
  "bios_version": "2.11.0",
  "bios_serial": "B5QMT32",
  "bios_release_date": "2023-05-10T00:00:00",
  "cpu_name": "Intel(R) Core(TM) i5-10500 CPU @ 3.10GHz",
  "cpu_manufacturer": "GenuineIntel",
  "cpu_processor_id": "BFEBFBFF000A0653",
  "cpu_family": 205,
  "cpu_cores": 6,
  "cpu_logical": 12,
  "windows_machine_guid": "1f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
  "total_physical_memory_bytes": 8441204736,
  "disk_serials": [
    {
      "index": 0,
      "model": "SAMSUNG MZVLB256HBHQ-000L7",
      "serial": "S4ELNX0N123456",
      "interface": "SCSI",
      "media_type": "Fixed hard disk media",
      "pnp_device_id": "SCSI\\DISK&VEN_NVME&PROD_SAMSUNG_MZVLB256\\5&1A2B3C4D&0&000000",
      "size_bytes": 256052966400,
      "is_system_disk": true
    }
  ],
  "mac_addresses": [
    {
      "mac": "B4:45:06:12:34:56",
      "permanent_address": "B44506123456",
      "name": "Realtek PCIe GbE Family Controller",
      "pnp_device_id": "PCI\\VEN_10EC&DEV_8168&SUBSYS_09A41028&REV_15\\01000000684CE00000",
      "net_connection_id": "Ethernet"
    }
  ],
  "collection_errors": []
}
```

(Values are illustrative, not from a real machine.)

### 7.3 Sources

Key names are the survey collector's JSON keys, so survey files can be replayed through the server's matcher unchanged. `disk_serials` and `mac_addresses` keep the collector's names but carry more per entry. Three collector fields are deliberately **not** sent: `windows_install` (product ID and install date), `computer_name` and `domain` — administrator-editable, useless for identity, and no reason to collect them from a school's PCs.


| Key | Type | Source |
|---|---|---|
| `schema` | int | Constant `1`. |
| `smbios_uuid` | string\|null | `Win32_ComputerSystemProduct.UUID` |
| `system_manufacturer` | string\|null | `Win32_ComputerSystemProduct.Vendor` |
| `system_model` | string\|null | `Win32_ComputerSystemProduct.Name` |
| `system_serial` | string\|null | `Win32_ComputerSystemProduct.IdentifyingNumber` |
| `system_version` | string\|null | `Win32_ComputerSystemProduct.Version` |
| `baseboard_manufacturer` | string\|null | `Win32_BaseBoard.Manufacturer` |
| `baseboard_product` | string\|null | `Win32_BaseBoard.Product` |
| `baseboard_serial` | string\|null | `Win32_BaseBoard.SerialNumber` |
| `baseboard_version` | string\|null | `Win32_BaseBoard.Version` |
| `bios_manufacturer` | string\|null | `Win32_BIOS.Manufacturer` |
| `bios_version` | string\|null | `Win32_BIOS.SMBIOSBIOSVersion` |
| `bios_serial` | string\|null | `Win32_BIOS.SerialNumber` |
| `bios_release_date` | string\|null | `Win32_BIOS.ReleaseDate`, formatted `yyyy-MM-ddTHH:mm:ss` with no zone, as the collector writes it |
| `cpu_name`, `cpu_manufacturer`, `cpu_processor_id` | string\|null | First `Win32_Processor` instance: `Name`, `Manufacturer`, `ProcessorId` |
| `cpu_family`, `cpu_cores`, `cpu_logical` | int\|null | Same instance: `Family`, `NumberOfCores`, `NumberOfLogicalProcessors` |
| `windows_machine_guid` | string\|null | Registry `HKLM\SOFTWARE\Microsoft\Cryptography`, value `MachineGuid`, read through the **64-bit view** (`RegistryView.Registry64`). A 32-bit process otherwise reads the redirected hive and silently gets nothing. |
| `total_physical_memory_bytes` | int\|null | `Win32_ComputerSystem.TotalPhysicalMemory` |
| `disk_serials[]` | array | **Every** `Win32_DiskDrive` instance, including USB disks. Entry keys and sources: `index` ← `Index`, `model` ← `Model`, `serial` ← `SerialNumber`, `interface` ← `InterfaceType`, `media_type` ← `MediaType`, `pnp_device_id` ← `PNPDeviceID`, `size_bytes` ← `Size` |
| `disk_serials[].is_system_disk` | bool | `true` for the disk holding the Windows system drive: follow `Win32_DiskDrive` → `Win32_DiskDriveToDiskPartition` → `Win32_LogicalDiskToPartition` to the `Win32_LogicalDisk` whose `DeviceID` equals `%SystemDrive%`. Exactly one disk should be `true`; if it cannot be determined, all are `false`. |
| `mac_addresses[]` | array | Every `Win32_NetworkAdapter` with `PhysicalAdapter = true` and a non-null `MACAddress`. Entry keys and sources: `mac` ← `MACAddress`, `name` ← `Name`, `pnp_device_id` ← `PNPDeviceID`, `net_connection_id` ← `NetConnectionID` |
| `mac_addresses[].permanent_address` | string\|null | `MSFT_NetAdapter.PermanentAddress` from `root\StandardCimv2`, matched on `InterfaceIndex`. This is the burned-in address, which survives Windows' random hardware addresses on Wi-Fi. `null` if unavailable. |
| `collection_errors` | string[] | Names of classes (or `"MachineGuid"`) that could not be read at all. Empty when everything was read. |

### 7.4 What the server does with them (for information)

The server, in `services/fingerprint_service.py`, owns all of this; the client relies on none of it:

- **Normalises**: trims, upper-cases, maps placeholder text to absent. The placeholder list starts as the collector's `Clean-Value` list and grows as survey data shows new ones.
- **Filters**: identity uses the firmware signals; installation continuity (brief §4.A) uses `windows_machine_guid`, the **system disk** serial and onboard network adapters (by `pnp_device_id` prefix and `permanent_address`), ignoring USB disks, USB and virtual adapters.
- **Hashes** a fixed, documented subset into `machine_fingerprint_hash`, and **matches signal by signal** with provisional thresholds (spec §8.2) until the survey freezes them.
- If `collection_errors` names `Win32_ComputerSystemProduct`, `Win32_BaseBoard` or `Win32_BIOS`, the reading is unusable: the server refuses with `fingerprint_unreadable` rather than classifying the PC as degenerate.

---

## 8. Client lease behaviour

### 8.1 Storage

- Store the lease string, `installation_id`, the highest `lease_serial` seen, `time_high_water` (§8.3) and any pending `ambiguity_id` together, encrypted with **DPAPI in `LocalMachine` scope**, in a file under `%ProgramData%\ExamPartner\`.
- **Not `CurrentUser` scope.** School lab PCs have several Windows accounts; per-user storage would tie the licence to whichever student was signed in during activation.
- Never store the owner's password or the activation token.

### 8.2 Offline validity

The lease permits offline use when **all** hold:

1. It verifies per §6.1.
2. Its `installation_id` equals the stored `installation_id`.
3. Its `lease_serial` ≥ the highest serial stored. A lower serial is rejected (an old copy restored over a newer one).
4. The clock rule (§8.3) passes.
5. `local_now < expires_at`.

### 8.3 Clock rule

- `time_high_water` is kept in protected storage. It only moves **forward**, to the larger of its current value and: the `server_time` of every successful response, and `local_now` at every successful offline validation.
- **Exception:** after a successful online `refresh` or `activate`, set `time_high_water = server_time` exactly — the server is authoritative, and this repairs a high-water mark pushed forward by a clock that once ran fast.
- If `local_now < time_high_water − 24 hours`, the PC is in **clock fault**. The lease is not usable offline. The client attempts an online refresh and shows the technician: *"This computer's date and time are wrong. Correct them, or connect to the internet."* Clock fault never deletes the lease, never revokes anything and never uses a seat.
- **Online overrides the clock.** While a refresh has succeeded during the current app session, the PC may operate regardless of its local clock, because the server has just validated it. Offline validity goes back to the rule above when the app restarts.
- A clock that runs **fast** makes the lease look expired; that is handled by the ordinary expiry path (refresh when online).

### 8.4 When to refresh

- At app start when online, if the lease was issued more than 24 hours ago, if it is within 7 days of `expires_at`, or if the PC is in clock fault or holds an expired lease.
- Then once every 24 hours while the app runs.
- A failed refresh is **not** an error to show while the lease is still valid offline. Show a banner only when fewer than 7 days remain, or on any of the non-retryable errors in §9.3.
- **An expired lease still refreshes** (spec §8.6). Always try; never send an expired-lease PC to activation without first attempting a refresh.

---

## 9. `POST /licence/refresh`

### 9.1 Request and response

No `Authorization` header. The lease is the authorisation.

```json
{
  "request_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
  "lease": "<the stored lease string, exactly as received>",
  "ambiguity_id": null,
  "fingerprint_signals": { "…": "§7" },
  "client": { "platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631" }
}
```

Response `200`:

```json
{
  "result": "refreshed",
  "lease": "<new signed lease>",
  "lease_expires_at": "2026-11-07T09:00:00Z",
  "server_time": "2026-10-08T09:00:00Z"
}
```

Replace the stored lease only after the new one passes §8.2 checks 1–3.

### 9.2 Idempotency — `request_id`

- Generate a fresh UUID v4 **before the first attempt** of each logical operation, **persist it** (with the lease state), and reuse it for every retry of that operation.
- Start a new `request_id` only after a **definitive** answer: any `2xx`, or a non-retryable error (§9.3).
- **Activate:** a repeat of the same `request_id` with the same `pool_id` and the same fingerprint returns the **same outcome** — the same binding, a lease for it, no second seat, no ambiguity — within 24 hours of the first attempt. The same `request_id` with a different body → `request_id_reused`.
- **Refresh:** a repeat of the binding's most recent refresh `request_id` returns the **current lease unchanged** — same `lease_serial`, byte-identical — and is not counted as a stale-serial signal. Without this, every lost refresh response on a weak connection would look like a cloned lease.
- Collecting an ambiguity (§10) uses a **new** `request_id`.

### 9.3 Error body and codes

Every error is:

```json
{
  "detail": {
    "code": "pool_full",
    "message": "Human-readable English, safe to show",
    "retryable": false,
    "server_time": "2026-10-08T09:00:00Z"
  }
}
```

Some codes add fields, listed below. This is the shape FastAPI produces from `HTTPException(detail={...})`, so the backend needs no custom handler.

| HTTP | `code` | Endpoints | Retryable | Extra fields | What the client tells the technician |
|---|---|---|---|---|---|
| 400 | `invalid_request` | all | no | `field` | Client bug. Log it; "Something went wrong — contact ExamPartner." |
| 400 | `fingerprint_unreadable` | activate, refresh | no | `classes` | "Windows could not report this PC's hardware. Restart the PC, check the *Windows Management Instrumentation* service, and try again." |
| 401 | `invalid_credentials` | activation-session | no | — | "Email or password is incorrect." |
| 401 | `activation_session_invalid` | activate | no | — | "Your sign-in has expired. Sign in again to activate." |
| 402 | `subscription_inactive` | activate, refresh | no | `subscription_expires_at` | "This school's ExamPartner subscription has ended. Contact the school administrator." On refresh, the current lease stays usable until its own expiry. |
| 403 | `no_licensing_access` | activation-session | no | — | "This account does not manage any ExamPartner computers." |
| 403 | `pool_access_denied` | activate | no | — | "This account cannot activate computers in that lab." Returned identically for a pool that does not exist. |
| 403 | `lease_invalid` | refresh | no | — | The lease is malformed, unsigned, or signed by an unknown key. Discard it and go to activation. |
| 403 | `lease_revoked` | refresh, activate (ambiguity collection) | no | — | "This computer's licence was released by the school. An administrator must activate it again, which uses a seat." **Discard the lease.** |
| 403 | `machine_mismatch` | refresh | no | — | "This licence belongs to a different computer." Discard the lease and go to activation. If this *is* the same PC after a motherboard replacement, the administrator releases the old activation first. |
| 409 | `pool_inactive` | activate | no | — | "This lab's licence pool is suspended. Contact ExamPartner." |
| 409 | `pool_full` | activate | no | `pool_size`, `active` | "All seats in this lab are in use. Release a computer that is no longer used, or add seats." **Never evicts anything.** |
| 409 | `ambiguity_pending` | activate, refresh | later | `ambiguity_id` | "ExamPartner needs to confirm this computer's identity. This usually takes up to one working day." Store `ambiguity_id`; see §10. On refresh, the current lease stays usable until its expiry. |
| 409 | `ambiguity_expired` | activate, refresh | no | — | The request went unresolved for 7 days. Clear `ambiguity_id` and start again without it. |
| 409 | `ambiguity_consumed` | activate, refresh | no | — | Already collected. Clear `ambiguity_id`; if the PC has no valid lease, start again. |
| 409 | `ambiguity_mismatch` | activate, refresh | no | — | The PC presenting the `ambiguity_id` is not the one that raised it. Clear it and start again. |
| 409 | `request_id_reused` | activate, refresh | no | — | Client bug: a `request_id` was reused for a different request. Generate a new one. |
| 426 | `client_upgrade_required` | all | no | `min_app_version` | "Update ExamPartner to continue." The current lease stays usable offline until its expiry. |
| 500 | `internal_error` | all | yes | — | Retry with backoff. |
| 503 | `licensing_unavailable` | all | yes | — | "Licensing is temporarily unavailable. Try again later." The current lease stays usable offline. |

**Unknown codes.** A future server may add codes. A client that sees an unrecognised `code` acts on the HTTP status: `4xx` = not retryable, show `message`; `5xx` = retryable.

### 9.4 Transport failures

Treat these as **retryable**, and do not try to parse them as the error body above: connection failures, timeouts, TLS errors, any non-JSON body (Render serves HTML pages during deploys and cold starts), and `502`, `503` or `504` without a JSON `detail.code`.

One exception: FastAPI's own `422` response, whose `detail` is an **array** of validation errors rather than an object. It is **not** retryable — the request is malformed — so treat it as `invalid_request`.

Timeouts: 15 seconds to connect, 60 seconds for the whole request (a cold Render instance can take tens of seconds). Interactive retries (activation): back off 2, 4, 8, 16, 30 seconds, at most 5 attempts, then show the error. Background retries (refresh): try again at the next scheduled refresh.

---

## 10. Collecting a resolved ambiguity

When `activate` or `refresh` returns `409 ambiguity_pending`, an ExamPartner admin decides whether the PC is an existing activation (`recognize_existing`) or a new machine (`treat_as_new`; capacity is claimed at that moment, spec §5.2).

1. Store `ambiguity_id` in protected storage.
2. Offer a **"Check again"** button. Do not poll automatically more often than once an hour.
3. To collect, resend the **same kind of request** with `ambiguity_id` set and a **new** `request_id`:
   - **Refresh-raised:** resend `refresh` with the current lease. No sign-in needed.
   - **Activation-raised:** resend `activate`, which requires a **fresh activation session** — the 15-minute token will usually have expired by the time an admin has resolved it. The technician signs in again.
4. Outcomes: still open → `ambiguity_pending` again; resolved → `200` with a lease (`result` is `restored` for `recognize_existing`, `activated` for `treat_as_new`); otherwise `ambiguity_expired`, `ambiguity_consumed`, `ambiguity_mismatch` or `lease_revoked`.
5. Clear `ambiguity_id` on any definitive answer.

An ambiguity is single-use and expires after 7 days unresolved (brief §4.C).

---

## 11. Server obligations not visible on the wire

For the Cloud Session; listed here because breaking any of them breaks the client.

- **The activation token is useless as an account token, and an account token is useless here.** Sign activation tokens with a key **derived for this purpose only** — e.g. HMAC-SHA256 keyed with `HMAC(JWT_SECRET, "exampartner-activation-v1")` — not with `make_token()`. `auth_utils.read_token()` checks only signature and expiry, not a type, so a token signed the ordinary way would be accepted by `get_current_user()` as a full login on every Android route. A test must prove `get_current_user()` returns `None` for an activation token, and that `/licence/activate` refuses an ordinary user token.
- **`/licence/activation-session` registers no device** and never calls `register_device()`. It verifies the password the way `/auth/login` does.
- **Activation tokens expire 15 minutes after issue.** They carry the caller's identifier and role, and authorise only `/licence/activate`.
- **Store `request_id`s** so §9.2 holds: the activation's `request_id` on the binding (or a request log), and the most recent refresh `request_id` per binding. These need new columns in the next unit's schema.
- **Licensing unavailable is a 503, never a crash**, including when the signing key is missing, malformed or equal to the TEST seed (§6.5), and when startup recorded licensing as unavailable — in addition to the live index check inside every binding write (brief §4.E).
- **Error precedence** follows §5, so entitlement problems outrank capacity.
- **Every response carries `server_time`**, in the §2 format.
- **Lease serialisation** follows §6.3 exactly, so replays are byte-identical.
- **Do not log refreshes individually** (spec §3.5); do log every denial.
- **No rate limiting in V1.** `/licence/activation-session` accepts a password exactly as `/auth/login` does and is exposed to the same guessing risk; adding a limit to both is a separate, later change.

---

## 12. Changing this contract

- **Additive** changes — a new optional response field, a new error code, a new optional request field — need only a dated note in §13. Clients already ignore unknown fields and handle unknown codes (§9.3).
- **Breaking** changes — renaming or removing a field, changing a format, changing what a code means — need a new path (`/licence/v2/…`) or a new lease `v`, and both implementers told before either builds against it.
- Implementers propose changes as findings to the planning surface. Neither side edits this document.

## 13. Change log

| Date | Change |
|---|---|
| 8 October 2026 | Version 1. |
