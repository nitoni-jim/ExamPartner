# ExamPartner Windows — Project Report

*Planning summary · 26 September 2026 · framework, scope, licensing rationale and timeline*

---

## Context

ExamPartner is a live Nigerian exam-prep platform (JAMB/WAEC/NECO), Android-first (Kotlin/Jetpack Compose, backend on FastAPI/Render with Neon PostgreSQL, Cloudflare Pages frontend). Solo-developed by Nitoni. The Android app is stable through Sprint 8 (Paystack integration, device limits, offline restore, and more).

## New Initiative

A native Windows application targeting schools, CBT centres, and resellers — a distinct buyer segment from individual mobile students.

## Key decisions locked in this planning phase

### 1. Framework

WinUI 3 / .NET.

- Not .NET MAUI — no cross-platform need, since Android is already separately native.

- Not a web wrapper — the offline requirement rules that out.

### 2. Architecture Scope — Scope B (confirmed against real-world precedent)

Per-machine independent installs, each with local offline storage after an initial content download. No local server, no LAN sync between machines (that more complex "Scope C" model is parked as a possible future product, not needed now).

**Validation:** confirmed directly via a conversation with a real secondary-school IT admin. Competitor TestDriller uses exactly this model — "you download one per system."

### 3. Licensing — Seat-Pool Model (deliberately more customer-friendly than the competitor)

TestDriller ties license keys to hardware; a reformat means buying a new key. ExamPartner instead allows manual deactivation and reassignment of a seat after a reformat — consistent with the existing Android philosophy of an account-tied subscription rather than a harshly hardware-locked one.

### Schema

> **Superseded.** The two bullets below describe the original design. Both were later overturned: there are no per-seat rows and no `free`/`deactivated` states (capacity is a count), and the rolling-window abuse guard was dropped as a product rule. See `ExamPartner_Windows_SeatPool_Architecture_Handoff.md` and the Pilot V1 implementation spec. Kept here as the planning record.

- accounts — an institution or a reseller

- seat_pools — one or more per account (supports resellers managing multiple client schools without needing full records for each client)

- institution_seats — individual hardware-locked seats within a pool (free / active / deactivated)

- seat_activation_log — audit trail; supports an abuse-guard capping reassignments within a rolling 30-day window, so a reformat-triggered reactivation can't be used as a route to unlimited free installs

### 4. Timeline

Kickoff planned for August 2026, target November 2026 — chosen deliberately to land 2–3 months ahead of the real deadline pressure (JAMB ~April, WAEC ~May–June, NECO ~June–July), rather than the softer "September season start" date.

Estimated 12–16 weeks total:

- ~4–5 weeks — core WinUI UI (Study, CBT, Game, Theory Grading, Analytics)

- ~3–4 weeks — offline mode (porting the concepts behind Android's Room / SyncManager / DiagramResolver)

- ~2–3 weeks — seat-pool licensing

- ~1 week — Paystack desktop integration

- ~2–3 weeks — real-machine testing (non-negotiable; same role Nitoni played for Android on the Samsung SM-A055F)

### 5. iOS Status

Suspended / deprioritized in favor of Windows-first, given the institutional/reseller buyer segment. May be revisited later.

### 6. Realistic-Estimate Discipline

A parallel ChatGPT conversation produced significantly more optimistic timelines for both iOS and Windows (e.g. “3–5 weeks for iOS”) that did not account for compile/test cycles, device access, or platform review processes as the actual bottleneck — not code-generation speed. Nitoni is aware of this gap and treats ChatGPT as useful for strategic brainstorming and positioning, not technical estimation, for this project going forward.
