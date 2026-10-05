# Cloud session brief — ExamPartner Windows licensing

**For:** the implementation session working on `nitoni-jim/ExamPartner`
**Owner:** Nitoni (solo developer; works on this in evenings and weekends)
**Date:** 5 October 2026

Read this first, then `backend/CLAUDE.md`, then the Pilot V1 implementation spec. This brief answers the setup questions and settles the open spec gaps. Where it disagrees with the spec, this brief wins and the spec gets updated to match.

---

## 1. What you own

You own the **FastAPI backend** for seat-pool licensing, in `backend/`, on a branch.

- Schema in `db.py` (both the SQLite and Postgres branches)
- `services/licensing_time.py`, `services/licensing_service.py`, `services/fingerprint_service.py`
- `routes/licensing.py` and its registration in `app.py`
- `require_account_owner()` in `services/access_control.py`
- The acceptance tests in `backend/tests/`

## 2. What you do not own

- **The WinUI 3 client.** It is built locally on Windows. You cannot compile or run it here, so do not create it, scaffold it, or write C# for it.
- **The `windows/` folder.** Leave it alone if it exists.
- **The fingerprint survey collector.** Already written and tested — `Collect-Fingerprint.ps1`, `Run-Collector.bat`, `README.txt`. Do not rewrite it. If you think it has a bug, say so rather than replacing it.
- **Android, the PWA in `frontend/`, the content pipeline, `paper_rules`.** Out of scope entirely.
- **`main`.** Never push to it, never merge your own PR.
- **Secrets.** Do not create, print, or commit a signing key. Read `LICENCE_SIGNING_KEY` from the environment and fail clearly when it is absent.

## 3. Setup answers

**Repository.** `nitoni-jim/ExamPartner`. The backend is in `backend/`, the PWA in `frontend/`. Android is **not** in this repo. Read `backend/db.py`, `backend/config.py`, `backend/services/paper_rules_service.py`, `backend/services/access_control.py`, `backend/services/auth_utils.py`, `backend/app.py` and `backend/tests/conftest.py` before proposing anything.

Yes — check §2 of the spec against the live code and report any drift. The conventions in it were derived from the repository at commit `c7422c8`, so they should hold, but verify rather than trust.

**Branch and deploy.** Work on `licensing/pilot-v1`. Open a PR. Never push to `main`, never merge. Nitoni reviews and merges in the GitHub web UI.

While your branch is open, Nitoni does not hand-upload files under `backend/` through the GitHub website. If you see an unexpected commit touching your files, stop and say so rather than merging around it.

**Postgres.** Run a throwaway Postgres 16 in this workspace. It installs cleanly — `apt-get install -y postgresql` then `initdb` and `pg_ctl` on a non-default port — and this has been verified in a container identical to yours. **Do not touch Neon**, not even a dev branch, and do not read `DATABASE_URL` if one is set in the environment. Your tests create and drop tables; production holds live paying users' data.

## 4. Rulings on the three gaps

### A. Degenerate machines must not hit the ambiguity queue every 30 days — accepted

Your reading is correct and this was the most serious defect in the spec. Implement the fix as you proposed, with one addition.

On **refresh** (not on activation), a binding whose stored `fingerprint_confidence` is `weak` or `degenerate` may refresh normally when **installation continuity** holds:

1. The presented `installation_id` equals the binding's current `installation_id`, and
2. The supporting signals — Windows `MachineGuid`, disk serial, MAC — match the snapshot stored on the binding.

Continuity broken → ambiguity path, as §8.6.1 says. Continuity intact → refresh normally.

This is sound because the two cases need different evidence. Recognising a machine *after a reinstall* requires firmware identity, which a degenerate machine does not have. Confirming *the same installation is still running* only requires that nothing about it changed — and `MachineGuid`, disk serial and MAC are good at exactly that, while being useless for the first job. It is also what the architecture handoff's corrected fingerprint paragraph already calls for: "installation/server history and, where necessary, administrator confirmation."

**The addition: the ratchet must cover the supporting snapshot too.** §8.6.1 ratchets `machine_fingerprint_hash` forward on a strong match. Do the same for the supporting signals on a successful continuity refresh, or the first disk swap on a degenerate machine breaks continuity and locks it out — reintroducing the bug one maintenance event later.

Note what remains true: a degenerate machine that is genuinely reinstalled, or has its disk replaced, *does* go to the admin. That is correct. For a machine with no firmware identity, a hardware change is genuinely indistinguishable from a different machine, and a human should decide.

### B. Per-signal matching, hash for audit — accepted

Correct; §8.4 and §8.2 contradict each other and your resolution is the right one. Match signal by signal against the pool's active bindings. Keep `machine_fingerprint_hash` for the audit log and as an exact-match fast path.

**One requirement:** define the normalisation explicitly and in one place — trim, upper-case, strip the placeholder values (`To Be Filled By O.E.M.`, `Default string`, `System Serial Number` and the rest), then hash the signals in a fixed documented order. Otherwise the fast path misses for cosmetic reasons and you get an exact-match lookup that silently never matches. The survey collector already implements this filter in `Clean-Value`; mirror its list rather than inventing a second one.

### C. Client collects its lease by resending with `ambiguity_id` — accepted

As proposed. Two requirements:

- **Single use.** Once consumed, the ambiguity row cannot be replayed. `open → resolved → consumed`, and a consumed row is refused.
- **It expires.** An unresolved ambiguity older than some window (7 days is fine) becomes `expired` and is refused. The `status` column already allows this.

Keep the auth rules as the spec has them: an activation resend still runs under owner authentication; a refresh resend needs only the lease.

## 5. The current unit of work

Spec §13 steps 2–5, and nothing beyond:

1. `services/licensing_time.py` — `now()`, `now_iso()`, `to_datetime()`, `is_expired()`, plus the injectable test clock gated on `ALLOW_TEST_CLOCK`. Route `access_control.is_paid_user()` through `to_datetime()` to fix the SQLite `TypeError`.
2. The licensing schema in `db.py`, both engines, plus the non-swallowed index path and `assert_licensing_constraints()`.
3. `require_account_owner()`.
4. `claim_capacity()` — **write acceptance test 4 before the implementation**, with the barrier-synchronised harness the spec specifies, against local Postgres.

**Stop there.** No endpoints, no activation algorithm, no lease signing, no fingerprint service. Those come in a later unit once this one is reviewed. A narrow first delivery tells Nitoni whether `backend/CLAUDE.md` is doing its job before anything larger is handed over.

## 6. How to work

- Follow `backend/CLAUDE.md` exactly. The conventions in it are not style preferences; most of them are failure modes that are silent when you get them wrong.
- Do not refactor adjacent code, reorder imports, or reformat files. Change what the task needs.
- Do not condense or delete existing comments. They record production failures and are the only record of several hard-won decisions.
- Add `cryptography` to `requirements.txt` **only** when you reach lease signing, which is not this unit.
- Run the test suite before opening the PR. `test_app_imports.py` and `test_no_undefined_names.py` must stay green.
- In the PR description, state what you verified by running versus what you believe by reading. Keep those separate.

## 7. If you find another gap

Do what you just did: stop, state it, propose a fix, wait. That is the behaviour this project wants, and it has already caught three real defects plus one that would have broken every degenerate lab PC.

Two qualifications. Be specific about why it blocks code rather than listing everything you noticed — a gap that only changes a docstring is not a blocker. And when the fix is yours to judge and reversible, propose it, implement it behind a clear note in the PR, and let review catch it; reserve stopping for decisions that are expensive to undo or that change product behaviour.

## 8. Not yours to decide — raised with Nitoni separately

Do not act on these; they may change the spec under you.

- **Whether `assert_licensing_constraints()` should stop the whole backend from booting.** As specified it does. That backend also serves live paying Android users who have nothing to do with seat licensing, so a missing licensing index would take the whole platform down. Implement it as specified for now, but keep the assertion in one function that is easy to change into a licensing-routes-only gate.
- **Whether Render auto-deploys from `main`.** Affects merge timing, not your branch.
- **The architecture handoff's date.** The spec cites `…2026-09-26.docx`. You believe the current one is 27 September. Flag it; do not silently change the citation. The 26 September file is the one Nitoni uploaded, and the fingerprint correction may exist only in the implementation spec and not in the handoff at all — which would mean the handoff needs updating, not the reference.
