# ExamPartner Windows — Seat-Pool Licensing Architecture

*Final product design and implementation handoff · originally issued 26 September 2026, updated 27 September 2026 · decisions of record*

---

> Status: Architecture locked
>
> This document is the authoritative product-architecture record for ExamPartner Windows licensing. Updated 27 September 2026 with validated implementation findings; implementation mechanics remain governed by the Pilot V1 implementation spec.

**Prepared for the implementation window**

Originally issued 26 September 2026 · Updated 27 September 2026

**Source basis**

- ExamPartner_SeatPool_Backend_Task_Spec.docx

- ExamPartner_Windows_Project_Report_Summary.docx

- ExamPartner_Windows_SeatPool_Architecture_Handoff_2026-09-24.docx

- Product decisions finalized in the licensing-planning discussion through 27 September 2026

- ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec — Revision 3 (implementation validation used to refine architecture-level invariants)

## 1. Executive Summary

ExamPartner Windows will use an institutional seat-pool licensing model for schools, CBT centres and reseller-managed customers. The institution pays for reusable concurrent machine capacity, not for the survival of a particular Windows installation or hardware binding.

> Core promise
>
> If a school owns 20 valid seats, it may format, repair, reinstall or replace its computers and continue using those same 20 seats throughout the paid subscription period. A reformat is a recovery event, not a repurchase event.

**The final model combines four layers that must remain conceptually separate:**

- Subscription entitlement — one-year commercial right to a defined seat-pool size.

- Concurrent capacity — pool_size defines how many Windows machines may be actively authorized at one time. Unused capacity is computed; it does not require pre-created “free seat” database rows or numbered permanent seats.

- Activation identity — server-side authorization linking one current physical machine to the institution’s concurrent capacity. It can survive a recognized reinstall and becomes permanently revoked when explicitly released.

- Offline licence lease — a signed, renewable 30-day authorization that allows the activated PC to continue working without internet.

Bulk installation is handled through a temporary 24-hour Deployment Session. The deployment code only authorizes new machines to join a specific seat pool; it is not a permanent activation key and does not itself represent ownership of a licence.

## 2. Existing Project Assumptions to Preserve

- Windows client: native WinUI 3 / .NET.

- Architecture scope: per-machine independent installations with local offline storage; no local server and no LAN sync.

- Primary Windows buyers: schools, CBT centres, institutions and reseller-managed customers.

- Backend: existing FastAPI service/route conventions and existing Neon PostgreSQL connection setup.

- Baseline backend entities remain accounts, seat_pools, institution_seats and seat_activation_log, but their semantics must follow this final handoff: seat_pools represent purchased concurrent capacity, while institution_seats represents activation bindings rather than pre-created seat inventory.

- Hardware-fingerprint generation is a Windows-client concern; this document defines the product behavior, not a specific fingerprinting implementation.

## 3. Final Licensing Principles

Seat/capacity ownership: The account/seat pool owns the entitlement. A machine only consumes one unit of concurrent capacity while its activation is active; it never permanently owns a numbered seat.

Concurrent-capacity rule: Each active activation consumes one unit of the pool’s purchased capacity. Available capacity is derived from pool_size minus active bindings.

**Reformatting is free recovery:** Formatting Windows, reinstalling ExamPartner, changing storage, repairing the PC or replacing a failed computer does not require buying another seat.

Released capacity is reusable: Release revokes the old activation and immediately reduces the active-binding count, making that unit of capacity available again.

**No permanent per-device key:** ExamPartner Windows must not use a lifetime device key as the ownership model.

**Offline-first, not offline-forever:** The annual subscription lasts one year, but each machine receives a renewable 30-day offline licence lease.

**Explicit revocation wins:** A deliberately released/revoked activation must never be silently restored only because the same hardware returns.

**Deployment codes are temporary authority:** A Deployment Session code permits onboarding into a pool; it is not the licence itself.

## 4. Terminology and Identity Model

Implementation must keep the following identities distinct. Collapsing them into one hardware_id will make reformat recovery, cloning protection and support history unnecessarily difficult.

| **Concept**                   | **Meaning**                                                                                                    | **Key behavior**                                                                                                     |
|-------------------------------|----------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| **Physical machine identity** | The underlying computer, inferred from a composite hardware fingerprint.                                       | Should survive Windows reinstall and routine component changes.                                                      |
| **Installation identity**     | One specific ExamPartner installation on Windows.                                                              | Changes after reinstall; useful for audit and clone detection.                                                       |
| **Activation identity**       | Server-side authorization linking one current physical machine to the institution’s concurrent capacity.       | Can survive a recognized reinstall; becomes permanently revoked when explicitly released.                            |
| Seat / capacity unit          | Customer-facing unit of reusable concurrent capacity inside a pool; not necessarily a persistent database row. | Unused capacity is computed from pool_size minus active bindings. No numbered permanent seat assignment is required. |
| **Offline licence lease**     | Signed local proof that the current activation may operate offline.                                            | Valid for 30 days at a time and never beyond subscription expiry.                                                    |

## 5. Machine Identity Policy

Machine identity must be tolerant of normal maintenance while still distinguishing genuinely different computers and cloned installations.

**Do not use any one fragile value as the machine identity.**

- Windows installation or OS-generated machine ID — can change after formatting.

- SSD/HDD serial — storage replacement is normal maintenance.

- MAC address — adapters and addresses can change.

- Computer name — administrator-editable.

- RAM — upgrades are common.

**Recommended fingerprint philosophy**

Use a composite of comparatively stable system characteristics, primarily system/SMBIOS UUID, motherboard/baseboard identity, BIOS/system serial and supporting CPU information. Matching should be confidence-based rather than requiring exact equality across every signal. The precise weighting/normalization algorithm is an implementation detail to be validated on real machines.

Fingerprint quality is not guaranteed: SMBIOS UUIDs and related firmware identifiers may be missing, degenerate, duplicated across a batch, or otherwise non-unique. The system must never assume that one hardware field is globally unique.

Ambiguous identity must not silently merge two PCs or consume/restore capacity incorrectly. Weak, duplicated or degenerate evidence must route to additional server/install history and, when necessary, administrator/support confirmation.

Installation identity is not a substitute for physical identity, because a cloned image may copy it. However, the same installation identity appearing from materially different machine contexts is a clone/consistency signal that should be logged and reviewed.

Under a strong recognized maintenance match, the stored current fingerprint may advance with the machine so ordinary repairs and upgrades do not accumulate into a false mismatch. Exact ratchet rules and drift limits remain implementation details.

| **Change**                                  | **Product interpretation**                                                   |
|---------------------------------------------|------------------------------------------------------------------------------|
| Windows formatted / ExamPartner reinstalled | Same machine when hardware match is strong.                                  |
| SSD/HDD replaced                            | Same machine.                                                                |
| RAM upgraded/replaced                       | Same machine.                                                                |
| Computer renamed                            | Same machine.                                                                |
| Wi-Fi/Ethernet adapter changed              | Same machine.                                                                |
| BIOS updated                                | Normally same machine.                                                       |
| CPU replaced                                | Usually same machine if stronger system identity still matches.              |
| Motherboard replaced                        | Usually a new machine; old seat can be released and reused at no extra cost. |
| Whole PC replaced                           | New machine.                                                                 |
| Cloned Windows image on another PC          | New machine; each physical PC needs its own seat.                            |

> Critical invariant
>
> Hardware recognition can restore an ACTIVE, non-revoked activation after a recognized reinstall. It must never override an explicit administrator release/revocation.

## 6. Activation Decision Tree

Fresh activation requires internet and appropriate institution-owner, ExamPartner-admin or valid Deployment Session authority. Before consuming available concurrent capacity, the backend must first check whether the machine can be restored to an existing active activation.

**A. Local licence still valid —** Run normally offline. If internet is available, perform a lightweight licence refresh; do not start a fresh activation flow.

B. No currently usable local lease — Go online. If the PC still holds a validly signed but expired lease for an active activation and the subscription remains current, it may recover through normal licence refresh after machine verification; this is not a fresh activation. A truly fresh installation with no activation requires authorized activation.

**C. Subscription invalid/expired —** Do not issue a new activation or renewed offline lease beyond the paid entitlement.

D. Strong match to ACTIVE, non-revoked machine — Treat as recognized reinstall/restoration. Preserve the existing activation/capacity claim, update the current installation identity and issue a new 30-day offline lease. Do not consume additional capacity.

**E. Ambiguous possible match —** Do not silently consume another seat or hijack an existing activation. Require administrator resolution when confidence is insufficient.

**F. Matching historical activation is REVOKED —** Do not restore it. Proceed only as a new activation and require an available seat.

G. New machine + available capacity — Atomically claim one unit of concurrent capacity, create the activation and issue the initial offline lease.

H. New machine + no available capacity — Return a clear pool-full response. An administrator must release an existing machine or increase entitlement; the deployment/client must never auto-evict another PC.

## 7. Reformat, Repair and Replacement Recovery

Recovery must never depend on the old installation still functioning. This is a primary product requirement.

**Normal recognized reinstall**

If the same physical machine is confidently recognized and its existing activation is still active, ExamPartner restores that activation. The new installation becomes the current installation for that activation; no seat is added and no purchase occurs.

**Manual release / replacement**

If the old PC is dead, already formatted, intentionally retired or no longer confidently recognized, an authorized administrator releases the old activation. The activation is marked revoked, the active-binding count drops immediately, and the rebuilt/replacement PC activates normally if capacity is available.

The backend does not need to preserve a numbered seat assignment for the replacement machine. The customer owns concurrent capacity, not a permanent seat object.

## 8. Offline Licence Lease

The commercial subscription and the offline licence period are different concepts.

| **Item**                   | **Final rule**                                                                                                                                                                                           |
|----------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Subscription term**      | One year.                                                                                                                                                                                                |
| **Offline licence lease**  | 30 days, renewable.                                                                                                                                                                                      |
| **Maximum offline expiry** | Never beyond the annual subscription expiry date.                                                                                                                                                        |
| **Refresh behavior**       | Automatic, lightweight, signed-lease based, machine-verified, and independent of content synchronization or application updates.                                                                         |
| **Normal reconnect**       | Not a reinstall and not a fresh activation. Even an expired signed lease may self-recover online if the activation remains active, the subscription is current and the presenting machine is recognized. |
| **After explicit release** | Online old machine is denied immediately; offline old machine can run only until its already-issued 30-day lease expires, then renewal is denied.                                                        |

> Why 30 days
>
> Remote release and unlimited one-year offline validity cannot safely coexist. A 30-day renewable lease bounds the duplicate-use window while remaining practical for schools with intermittent internet.

Licence refresh traffic should be tiny: activation/installation identifiers, licence state, timestamps and a signed response. It must not trigger large question-bank downloads. Content synchronization remains a separate flow.

Lease expiry is an offline boundary, not destruction of the activation. A machine that has been offline longer than 30 days may reconnect and self-recover through normal refresh when the signed lease is authentic, the activation is still active, the annual subscription is current and the presenting machine is recognized.

Every licence refresh must verify both the signed lease and the presenting machine. A copied lease presented by a clearly different physical machine must not receive a renewed lease; weak or degenerate identity should route to ambiguity resolution rather than silently merging machines.

A stale lease serial or duplicated installation identity is evidence of possible cloning or restored old state, not automatic proof of abuse. Record and surface the signal while avoiding unnecessary denial of legitimate recovery.

## 9. Institution Administrator Seat Management

The institution account is the control point for recovery. Administrator credentials are not permanently stored on the lab PCs.

**Recommended dashboard summary**

- Institution name and subscription expiry date.

- Seat pool(s): purchased capacity, active bindings and available capacity (computed).

- Active machine list with human-friendly machine label, activation date and last successful licence check.

- History/audit access for activation, recognized reinstall, release/replacement and administrative actions.

**Core administrator actions**

- Rename machine — changes only the human-readable label.

- Release seat — revokes the current activation and makes one unit of concurrent capacity available immediately.

- Replace/Reinstall computer — UI wording may wrap the same internal release-then-activate lifecycle.

- View history — preserves machine/seat activity for support and abuse analysis.

Optional release reason values such as reformat, replacement, hardware failure, retired or other may be captured for audit/support. They must not gate whether a legitimate release is allowed.

## 10. Administrator and Reseller Roles

| **Role**                      | **Scope**                                | **Typical permissions**                                                                                                                                                                     |
|-------------------------------|------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Institution Admin**         | One institution/account                  | View capacity/machines, rename, release, create deployment sessions, authorize fresh activation where applicable, and manage renewal/resizing choices.                                      |
| **Reseller Admin**            | Only reseller-authorized customers/pools | Provision/manage allowed pools and, where policy permits, create deployment sessions or assist recovery.                                                                                    |
| **ExamPartner Support/Admin** | Platform support scope                   | Exceptional recovery, audit review and support overrides.                                                                                                                                   |
| **Windows client**            | Its authorized institution/pool context  | Refresh its own signed lease and participate in recognized restoration. Fresh activation requires institution/admin authority or a valid Deployment Session. Cannot release other machines. |

## 11. Bulk Deployment: Temporary Deployment Sessions

> Locked deployment model
>
> Bulk installation uses a temporary Deployment Session. The deployment code authorizes machines to join one specific seat pool. It is not a permanent licence key, activation PIN or ownership token.

**Deployment Session properties**

- Scoped to one institution/seat pool.

- Administrator chooses a maximum number of new machine activations for the session.

- Expires automatically after 24 hours.

- Stops earlier when its activation allowance is exhausted.

- Administrator can cancel it early.

- If deployment takes longer, the administrator creates a new session; do not extend one code for several days by default.

- Cannot activate beyond the pool’s currently available concurrent capacity.

- Cannot release or evict an existing machine.

- Activated PCs remain licensed after the deployment code expires because their own activation/lease is independent of the deployment session.

- Recognized reinstall/restoration should not consume additional capacity and should not be counted as a new-machine allocation for the deployment session.

For small deployments, the product may also permit direct administrator-authenticated activation. For labs, Deployment Sessions are the preferred operational workflow because technicians do not need the administrator password on every PC.

## 12. Annual Renewal and Seat-Pool Resizing

Renewal extends commercial entitlement. It does not reinstall or freshly activate retained computers.

**Normal renewal**

The backend keeps the existing account, pool, active bindings, machine fingerprints, activation IDs and current installation IDs. Renewal extends the subscription expiry. Existing PCs receive new 30-day offline leases during their normal refresh flow under the renewed term.

**Increasing seats at renewal**

Existing activations remain unchanged. Increasing pool_size increases available concurrent capacity. Example: 20 → 30 means the current active machines stay active and up to 10 additional machines may be authorized.

**Decreasing seats at renewal**

If active machines are at or below the new pool size, the new entitlement applies immediately. If active machines exceed the new size, do not randomly deactivate machines: the pool enters a transitional over-capacity state and the institution administrator chooses which activations to retire. Existing machines remain active during that transition, but new activations must not increase usage while the active count is at or above pool_size.

> Renewal invariant
>
> Renewal ≠ installation. Renewal ≠ activation. Renewal = extension/resizing of the institution’s commercial entitlement.

**Subscription expiry without renewal**

Expiry of the annual subscription must not uninstall ExamPartner, delete downloaded exam content, erase student progress, remove local settings, or discard the stored machine/activation history. The installation remains intact. The backend simply stops issuing new offline licence leases beyond the paid subscription term. A PC may continue only until the earlier of its current 30-day lease expiry or the annual subscription expiry. Once authorization has expired, licensed ExamPartner functionality requires renewal.

When the institution later renews, retained active machines must resume through the normal licence-refresh path using their existing machine, activation and installation identities. They must not be forced through a fresh installation or fresh seat activation solely because the subscription had expired.

## 13. Data Model Semantics and Audit History

The four baseline entities remain useful, but the implementation must represent current state separately from immutable history.

**accounts**

Institution or reseller identity and subscription/account state.

**seat_pools**

Purchased concurrent capacity. pool_size may change on renewal; available capacity is computed from pool_size minus active activation bindings.

**institution_seats**

Activation bindings. A row represents a machine’s current or historical claim on capacity, not a pre-created seat. Active binding = not revoked; there is no required “free seat” row or numbered permanent seat. Revoked rows/history are retained so explicit release can never be silently undone.

**seat_activation_log / audit history**

Immutable history. Must preserve enough context to reconstruct prior activation bindings even after capacity has been released and reused by another machine.

**The audit history should conceptually preserve:**

- pool/account context and the purchased capacity in effect where relevant;

- activation/release/restore event type;

- activation identity and installation identity where relevant;

- machine label and server-safe fingerprint snapshot/representation;

- timestamp and performed_by actor;

- optional reason/context for release/replacement/support recovery.

## 14. Abuse Protection Philosophy

Do not implement the original suggested “more than 3 activations in 30 days = reject” rule as the defining product policy. Normal school maintenance can legitimately involve multiple reformats or replacements.

V1 should enforce hard capacity and authorization boundaries while retaining rich history for anomaly detection. Suspicious behavior is unusual churn across many different machines relative to a small purchased pool, not simply the fact that one seat was reassigned several times.

- Same-machine licence refresh is not a new activation.

- Recognized reinstall is not a new seat consumption event.

- Explicit activation release/reassignment remains allowed during the valid subscription.

- Future anomaly thresholds should be configurable and informed by real deployment data.

## 15. Backend Invariants

- New activation must never increase active usage beyond the pool’s current pool_size. A deliberate renewal shrink may temporarily leave active bindings above pool_size; that over-capacity state is allowed only as a transition while the administrator chooses which machines to retire.

- Activation of a genuinely new machine must claim capacity atomically/serially so concurrent requests cannot oversubscribe the pool.

- Repeated requests from an already-active recognized machine must not consume additional concurrent capacity.

- Recognized reinstall may restore an active, non-revoked activation without consuming additional concurrent capacity.

- Explicit revocation/release must prevent that activation from being automatically restored.

- Release must preserve history and make one unit of concurrent capacity reusable immediately without purchase.

- After explicit release, an old offline installation may continue only until its already-issued lease expires; it must not renew. By contrast, an expired lease on an activation that is still active may self-recover online if the subscription is current and the presenting machine is recognized.

- Licence refresh must not be treated as activation and must not trigger content synchronization. Refresh must validate the signed lease and the presenting machine before issuing a new lease.

- A Deployment Session may only consume currently available concurrent capacity in its scoped pool, cannot release existing activations, and expires after 24 hours or its activation limit.

- Administrator/reseller/support authorization must be scope-checked; no client-provided seat/account identifier can bypass ownership checks.

- Renewal/resizing must preserve retained activations and never require reinstalling retained PCs.

- Subscription expiry must preserve the installed app, downloaded content, local progress/settings and retained activation records; expiry only prevents issuance of licence authorization beyond the paid term. Renewal restores entitlement through normal licence refresh, not reinstall/re-activation.

- Reseller/customer pools must remain logically isolated.

## 16. Acceptance Scenarios

Normal activation: pool_size=20 with 18 active bindings. New PC activates. Result: 19 active, 1 unit of available capacity; activation created and 30-day lease issued.

**Duplicate/retry:** Same active PC repeats activation after timeout. Result: existing activation returned/restored; no second seat consumed.

**Windows format — recognized machine:** ExamPartner is reinstalled on the same physical PC. Strong hardware match + active non-revoked activation. Result: existing activation restored, current installation ID updated, no new seat consumed.

Formatted/dead PC — manual recovery: Old installation unavailable. Admin releases old activation remotely. Result: activation revoked and one unit of concurrent capacity becomes available immediately; rebuilt/replacement PC activates at no extra cost.

Intentional release while old PC remains offline: Admin releases PC-A and PC-B consumes the newly available capacity. PC-A may run only until its existing 30-day lease expires; any later refresh is denied.

Pool full: Active bindings equal pool_size. New PC attempts activation/deployment. Result: clear no-capacity response; no automatic eviction.

**24-hour lab deployment:** Admin creates deployment session for up to 20 new PCs. Code expires after 24 hours or 20 new activations. Activated PCs remain licensed after expiry.

**Renewal unchanged:** Institution renews same seat count. Existing activations remain; normal refresh begins issuing leases under new annual expiry.

Expired then renewed: Annual subscription expires. Installed PCs keep local content, progress and configuration but cannot receive licence authorization beyond the paid term. After renewal, retained active machines regain entitlement through normal licence refresh without reinstalling ExamPartner or consuming new seats.

Renewal increase: pool_size 20 → 30. Existing machines untouched; 10 additional concurrent activations become available.

Renewal decrease above active usage: pool_size 20 / 18 active → renew at 15. Pool is temporarily over-capacity; no machine is randomly killed and no new activation increases usage. Admin chooses any 3 activations to retire; remaining 15 continue without reinstall.

Cloned installation: Windows/App image copied to another physical PC. Different physical fingerprint means new machine; it requires its own available concurrent capacity.

Revoked hardware returns: Exact former machine returns after explicit release. Hardware match does not restore the revoked activation; it can only activate again as a new activation if concurrent capacity is available.

Expired lease self-recovery: Active PC has been offline for 40 days and its 30-day lease has expired. It reconnects with an authentic signed lease; activation is still active, subscription current and machine recognized. Result: normal refresh issues a new lease without admin involvement or fresh activation.

Copied lease on another PC: A valid signed lease from PC-A is presented by a clearly different physical PC-B. Result: PC-B does not receive a renewed lease; the event is flagged, while PC-A’s activation remains untouched.

Degenerate/duplicate hardware identifiers: Two distinct PCs present weak or duplicated firmware identifiers. Result: the system must not silently merge them; ambiguous restoration/refresh is routed to administrator/support resolution.

## 17. What This Final Handoff Supersedes

Where the earlier backend task spec or 24 September handoff differs, use this final document.

| **Earlier assumption**                       | **Final decision**                                                                                                                                                                                    |
|----------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Persistent deactivated seat state            | No pre-created free-seat inventory is required. Release revokes the activation binding; available capacity is computed from pool_size minus active bindings, while history preserves the old binding. |
| Hard “\>3 activations in 30 days” rejection  | Not the product rule. Enforce capacity and authorization; log/analyze abnormal churn.                                                                                                                 |
| Offline duration still open                  | Locked: 30-day renewable offline lease, never beyond annual subscription expiry.                                                                                                                      |
| Machine identity still open at product level | Locked behavior: composite/tolerant physical identity; exact technical weighting remains implementation detail.                                                                                       |
| Recovery surface still open conceptually     | Institution/admin-controlled remote release is required; exact frontend surface can evolve.                                                                                                           |
| Bulk deployment undecided                    | Locked: temporary pool-scoped Deployment Sessions with 24-hour expiry and activation limit.                                                                                                           |
| Renewal behavior not defined                 | Locked: retained machines do not reinstall/reactivate; pool may increase/decrease with administrator selection when active count exceeds new size.                                                    |

## 18. Implementation Window Priorities

1.  Preserve existing FastAPI service/route conventions and Neon PostgreSQL connection patterns.

2.  Refine the schema so count-based pool capacity, activation identity, installation identity and immutable historical audit records can coexist cleanly without requiring pre-created free-seat rows or numbered seats.

3.  Implement capacity-safe, idempotent activation with serialized/atomic capacity enforcement, recognized reinstall restoration and explicit-revocation precedence.

4.  Implement authorized remote release that works without access to the old installation.

5.  Design the signed 30-day offline licence refresh contract with the Windows client; refresh must verify the presenting machine, permit legitimate self-recovery after lease expiry, and remain independent of content sync/update logic.

6.  Expose machine/seat visibility needed by the institution/reseller management surface.

7.  Implement annual renewal and seat-pool resizing semantics without reactivating retained machines.

8.  Implement temporary Deployment Sessions: pool-scoped, activation-limited, cancelable, 24-hour expiry, no seat-eviction authority.

9.  Retain rich audit history for support, recovery and future anomaly detection.

10. Begin hardware-identity validation with a read-only survey on Nitoni’s Windows laptop and one office Windows PC as a first pass; later validate on the pilot school’s batch of PCs before freezing fingerprint thresholds. Validate offline lease and copied-lease behavior on real Windows machines before considering licensing complete.

## 19. Technical Details Intentionally Left to Implementation

The product architecture is locked. The implementation window still owns the following engineering choices, provided they preserve the behavior in this document:

- Exact fingerprint normalization, hashing, confidence weighting, stored-fingerprint ratchet/drift limits and ambiguity thresholds.

- Exact signed licence/token format and cryptographic scheme.

- Secure Windows storage mechanism for activation and offline licence material.

- Exact API route names, DTO shapes, indexes and transaction/locking/serialization strategy used to enforce count-based capacity safely.

- Exact administrator/dashboard frontend layout and authentication UX.

- Background refresh timing inside the 30-day window and user-warning thresholds near expiry.

- Future anomaly scoring/thresholds after real usage data exists.

## 20. Final Product Principle

> ExamPartner Windows licensing promise
>
> Schools pay for concurrent capacity, not for the survival of a particular Windows installation or a numbered permanent seat. Purchased capacity remains reusable throughout the valid subscription. Activation controls which machines currently consume that capacity; reformat, repair, release and renewal must not permanently consume the customer’s entitlement.

> Implementation instruction
>
> Implement this licensing philosophy, not merely the original CRUD endpoints. The system must enforce concurrent capacity, preserve immutable audit history, support reformat/replacement recovery, recognize legitimate reinstalls, handle weak/duplicate hardware identity safely, verify the presenting machine during licence refresh, bound offline duplication through renewable 30-day leases, and make bulk deployment convenient through 24-hour temporary Deployment Sessions.

End of final architecture handoff
