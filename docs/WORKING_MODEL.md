# ExamPartner Windows — working model

**Who does what across the three surfaces, and how decisions travel between them.**

Date: 5 October 2026 · Owner: Nitoni (solo; evenings and weekends)

Every surface reads this document and finds its own section. Read your section, and read the others so you know what is not yours.

---

## 0. The one rule that holds the rest together

**These surfaces cannot see each other.** A planning chat does not know what the Cloud Session decided. The Cloud Session cannot read a chat. The local agent knows neither.

**The repository is the only shared memory.** So:

> A decision that is not written into a repo document did not happen.

If the planning surface settles something and it stays in the chat, the implementers never learn it, and they will each invent their own answer. Every ruling, every spec revision, every API shape goes into `docs/` in the repo and is committed. That is not bureaucracy — it is the only channel that connects the three.

Shared documents, all in the repo:

| Path | What it is | Who writes it |
|---|---|---|
| `docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md` | The implementation spec | Planning |
| `docs/WORKING_MODEL.md` | This document | Planning |
| `docs/CLOUD_SESSION_BRIEF.md` | Current backend work unit | Planning |
| `docs/LICENCE_API_CONTRACT.md` | Request/response shapes both sides build against | Planning — **does not exist yet, see §4** |
| `backend/CLAUDE.md` | Backend conventions | Planning; implementers may propose changes |

---

## 1. Planning surface — a chat window

**This is a design and decision surface. It does not implement.**

### Owns

- The implementation spec, and every revision of it
- Rulings on gaps the implementers raise
- The licence API contract (§4) — the one artefact both implementers depend on
- The briefs that scope each implementer's work
- Reviewing what the implementers produce, and deciding whether a reported problem is real
- Keeping the documents consistent with each other

### Does not

- Write production code, attach the repo for editing, create branches, run the test suite, or open PRs
- Scaffold projects or create files in `backend/` or `windows/`
- Ask about workspace state, branch safety, or database setup — those are the implementers' concerns and they have their own briefs

### May

- **Read the repository**, read-only, to ground a spec in the real code rather than a description of it. This matters: the §2 conventions in the spec were derived by reading `db.py` and `paper_rules_service.py`, and reading them is what revealed that the project's uploaded copies were weeks older than `main`.
- Write and test throwaway code to **answer a design question** — a concurrency probe, a parser experiment. That is evidence-gathering, not implementation. It stays out of the repo.
- Write standalone tools that are not part of either codebase, such as the survey collector.

### How to behave

State findings in the planning register: what is wrong, why it changes the design, what you propose, what it costs. Not "I will not write code until you answer" — nobody asked you to write code.

When an implementer reports a problem, decide whether it is real before passing it on. Your value is judgement, not relay.

---

## 2. Cloud Session — backend

**Owns `backend/` on a branch. Nothing else.**

Scope, setup answers, current work unit and standing rules are in `docs/CLOUD_SESSION_BRIEF.md`. In summary: FastAPI licensing backend, branch `licensing/pilot-v1`, PR only, never `main`, local throwaway Postgres only, never Neon.

Reports findings up to the planning surface. Does not change the spec itself.

---

## 3. Local agent — WinUI client

**Owns `windows/` on Nitoni's own machine. Nothing else.**

Everything that needs real Windows lives here, because a Linux container cannot do any of it:

- The WinUI 3 / .NET client
- Packaging and the installer — **budget real time for this; it is absent from the original 12–16 week breakdown and a finished app you cannot install on twenty PCs is not a deliverable**
- DPAPI storage of the signed lease
- Reading SMBIOS and the other fingerprint signals
- Running the survey collector, and all real-machine testing

### Does not

- Touch `backend/`. If the client needs a backend change, that is a finding for the planning surface, which decides and briefs the Cloud Session.
- Invent API shapes. Build against `docs/LICENCE_API_CONTRACT.md`. If something is missing from it, stop and report — do not guess, because the other side is building against the same document and a guess silently diverges.

---

## 4. The contract between the two implementers

Backend and client meet at exactly one place: the licence API. Both will build against it simultaneously, from different machines, unable to see each other.

**Nobody owns it yet. That is the live risk in this setup.** If the Cloud Session invents DTO shapes while the local agent invents its own, both sides will be internally correct and will not interoperate, and the mismatch surfaces at integration — the most expensive place to find it.

Planning writes `docs/LICENCE_API_CONTRACT.md` before either side builds anything that crosses the boundary. It needs:

- Endpoint paths and methods
- Request and response JSON for `activate` and `refresh`, with every field named and typed
- The exact signed-lease payload: field names, order, encoding, signature format
- The `fingerprint_signals` object: which keys, how each is normalised
- Error codes and their meanings — `pool_full`, `lease_revoked`, `machine_mismatch`, the ambiguity 409 — since the client shows a different instruction to a technician for each
- Which errors are retryable

The backend work unit currently in flight (schema, time helpers, capacity) touches none of this, so it is not blocking yet. It blocks before the activation endpoints or any client networking code.

---

## 5. How work flows

```
Nitoni ── decides ──> Planning surface
                        │
                        ├─ writes/updates spec, brief, API contract  ──> repo docs/
                        │
          ┌─────────────┴─────────────┐
          ▼                           ▼
   Cloud Session                Local agent
   backend/ on a branch         windows/ on Nitoni's machine
          │                           │
          └──── findings, PRs ────────┘
                        │
                        ▼
                Planning surface decides
                        │
                        ▼
                 repo docs updated
```

Two ordering rules that matter:

**Repo docs are updated before the implementers are told to proceed.** A ruling given only in chat is invisible to them.

**While a branch is open, Nitoni does not hand-upload files into that branch's folders through the GitHub website.** Web upload replaces file contents wholesale with no merge, so one side's work disappears silently. Changes wanted mid-flight go through the session that owns the folder.

---

## 6. What is settled, and what is not

**Settled:** capacity is a count enforced under a pool-row lock, no slot numbers · release returns capacity immediately and revocation always beats hardware recognition · 30-day signed lease, self-recovering after expiry · refresh authorised by the presented lease *and* machine verification · activation authorised by the institution owner · degenerate machines refresh on installation continuity (§4.A of the Cloud Session brief) · per-signal matching with the hash kept for audit · ambiguity resolved then collected by resending with `ambiguity_id`.

**Open, and Nitoni's to decide:**

- Whether `assert_licensing_constraints()` should stop the whole backend booting, or only disable the licensing routes. As specified it is the former, which would take the live Android API down over a licensing index.
- Whether Render auto-deploys from `main`.
- Whether the architecture handoff needs the fingerprint correction written into it, or already has it. The spec cites the 26 September file; the correction may live only in the spec.
- The MVP scope for 10 November, and whether the WinUI client or the existing PWA is the vehicle for that first school trial.
