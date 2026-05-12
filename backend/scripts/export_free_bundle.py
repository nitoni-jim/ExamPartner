#!/usr/bin/env python3
"""
scripts/export_free_bundle.py — ExamPartner free bundle generator.

Generates the APK free bundle assets for Android:
  <android-root>/app/src/main/assets/seed/free_bundle_manifest.json
  <android-root>/app/src/main/assets/seed/questions/free_questions.json
  <android-root>/app/src/main/assets/seed/diagrams/<filename>.png

Product rule (locked):
  For every (exam, subject) pair, bundle the MIN(year) objective questions.
  No hardcoded exams, subjects, or years — all values come from the current DB.
  Objective questions only (v1). Theory bundling is deferred.
  Diagrams are fetched from GitHub raw URL — no local backend folder needed.

Usage:

  # Option 1 — DATABASE_URL from local .env or environment variable:
  python scripts/export_free_bundle.py ^
    --android-root "C:\\Users\\USER\\AndroidStudioProjects\\ExamPartner"

  # Option 2 — DATABASE_URL passed directly (not printed in logs):
  python scripts/export_free_bundle.py ^
    --database-url "postgresql://..." ^
    --android-root "C:\\Users\\USER\\AndroidStudioProjects\\ExamPartner"

  # Option 3 — Set env var in PowerShell, then run:
  $env:DATABASE_URL = "postgresql://..."
  python scripts/export_free_bundle.py ^
    --android-root "C:\\Users\\USER\\AndroidStudioProjects\\ExamPartner"

DATABASE_URL priority:
  1. --database-url argument
  2. DATABASE_URL environment variable
  3. .env file (searched in script dir, parent dir, then cwd)

Requirements:
  pip install psycopg2-binary requests
  pip install python-dotenv   (optional, for .env support)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

GITHUB_DIAGRAMS_BASE = (
    "https://raw.githubusercontent.com/nitoni-jim/ExamPartner/main/backend/diagrams"
)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Export ExamPartner free bundle assets for Android APK."
    )
    parser.add_argument(
        "--android-root",
        required=True,
        help=(
            "Path to the Android project root. "
            "Assets written to <android-root>/app/src/main/assets/seed/"
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres connection URL (not logged). Falls back to env var or .env.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download diagrams even if already present in assets folder.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# DATABASE_URL resolution
# ---------------------------------------------------------------------------

def resolve_database_url(cli_url):
    # Priority 1: CLI argument
    if cli_url and cli_url.strip():
        return cli_url.strip()

    # Priority 2: environment variable already in env
    env_val = os.environ.get("DATABASE_URL", "").strip()
    if env_val:
        return env_val

    # Priority 3: .env file
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / ".env",
        script_dir.parent / ".env",
        Path.cwd() / ".env",
    ]
    for dotenv_path in candidates:
        if dotenv_path.exists():
            val = _load_from_dotenv(dotenv_path)
            if val:
                print(f"  Loaded DATABASE_URL from {dotenv_path}")
                return val

    print(
        "\nERROR: DATABASE_URL not found.\n"
        "Options:\n"
        "  1. Pass --database-url \"postgresql://...\"\n"
        "  2. Set $env:DATABASE_URL = \"postgresql://...\" in PowerShell\n"
        "  3. Add DATABASE_URL=... to a .env file beside this script\n"
    )
    sys.exit(1)


def _load_from_dotenv(path):
    """Load DATABASE_URL from a .env file. Uses python-dotenv if available."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return os.environ.get("DATABASE_URL", "").strip() or None
    except ImportError:
        pass
    # Manual parse fallback
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "DATABASE_URL":
                return val.strip().strip('"').strip("'") or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def get_db(database_url):
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)
    try:
        conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
        conn.autocommit = False
        print("  Connected to Postgres (Neon)")
        return conn
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

FREE_BUNDLE_QUERY = """
WITH free_years AS (
    SELECT exam, subject, MIN(year) AS free_year
    FROM questions
    WHERE qtype   = %s
      AND exam    IS NOT NULL
      AND subject IS NOT NULL
      AND year    IS NOT NULL
    GROUP BY exam, subject
)
SELECT
    q.id, q.exam, q.year, q.subject, q.paper, q.section, q.qtype,
    q.page, q.marks, q.question_text, q.options_json, q.answer,
    q.explanation, q.diagrams_json, q.answer_diagrams_json,
    q.explanation_diagrams_json, q.tables_json, q.section_instruction,
    q.passage_id, q.passage_snapshot, q.topic, q.subtopic, q.sort_key
FROM questions q
JOIN free_years fy
  ON q.exam = fy.exam AND q.subject = fy.subject AND q.year = fy.free_year
WHERE q.qtype = %s
ORDER BY q.exam, q.subject, q.year, COALESCE(q.sort_key, 999999999), q.id
"""


def _safe_json(val):
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def row_to_question(row):
    """Convert DB row to API response shape matching Android Question model."""
    r = dict(row)
    return {
        "id":                   r.get("id"),
        "exam":                 r.get("exam"),
        "year":                 r.get("year"),
        "subject":              r.get("subject"),
        "paper":                r.get("paper"),
        "section":              r.get("section"),
        "type":                 r.get("qtype"),   # API = "type", DB = "qtype"
        "page":                 r.get("page"),
        "marks":                r.get("marks"),
        "question_text":        r.get("question_text"),
        "options":              _safe_json(r.get("options_json")),
        "answer":               r.get("answer"),
        "explanation":          _safe_json(r.get("explanation")),
        "diagrams":             _safe_json(r.get("diagrams_json"))             or [],
        "answer_diagrams":      _safe_json(r.get("answer_diagrams_json"))      or [],
        "explanation_diagrams": _safe_json(r.get("explanation_diagrams_json")) or [],
        "tables":               _safe_json(r.get("tables_json"))               or {},
        "section_instruction":  r.get("section_instruction"),
        "passage_id":           r.get("passage_id"),
        "passage_snapshot":     _safe_json(r.get("passage_snapshot")),
        "topic":                r.get("topic"),
        "subtopic":             r.get("subtopic"),
        "sub_questions":        None,
        "solution_steps":       None,
        "difficulty":           None,
    }


# ---------------------------------------------------------------------------
# Diagram discovery
# ---------------------------------------------------------------------------

INLINE_DIAGRAM_RE = re.compile(r"\[\[diagram:([^\]]+)\]\]")


def extract_diagram_filenames(question):
    """
    Collect all diagram filenames referenced by a question from:
      - diagrams, answer_diagrams, explanation_diagrams fields
      - [[diagram:filename]] tokens in question_text and explanation
    Returns sorted list of clean filenames (diagram: prefix stripped).
    """
    filenames = set()
    for field in ("diagrams", "answer_diagrams", "explanation_diagrams"):
        val = question.get(field)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    filenames.add(item.strip())
    for text_field in ("question_text", "explanation"):
        text = question.get(text_field)
        if isinstance(text, str):
            for m in INLINE_DIAGRAM_RE.finditer(text):
                filenames.add(m.group(1).strip())
        elif isinstance(text, list):
            for part in text:
                if isinstance(part, str):
                    for m in INLINE_DIAGRAM_RE.finditer(part):
                        filenames.add(m.group(1).strip())
    return sorted(filenames)


# ---------------------------------------------------------------------------
# GitHub diagram download
# ---------------------------------------------------------------------------

def diagram_github_url(exam, year, filename):
    return f"{GITHUB_DIAGRAMS_BASE}/{exam.upper()}/{year}/{filename}"


def download_diagram(url, dest, force):
    """Download diagram. Returns True if downloaded, False if skipped. Exits on error."""
    if dest.exists() and not force:
        return False
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"\nERROR: Diagram not found on GitHub (HTTP {resp.status_code})")
        print(f"  URL: {url}")
        print("  Fix missing diagrams in GitHub before building the APK.")
        sys.exit(1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return True


# ---------------------------------------------------------------------------
# Bundle version
# ---------------------------------------------------------------------------

def compute_bundle_version(questions):
    """
    Deterministic SHA256-based version. Changes only when bundled question
    IDs change. Stable across repeated runs on the same data.
    """
    ids = sorted(q["id"] for q in questions if q.get("id"))
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{date}_{digest}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    android_root  = Path(args.android_root).resolve()
    seed_dir      = android_root / "app" / "src" / "main" / "assets" / "seed"
    out_manifest  = seed_dir / "free_bundle_manifest.json"
    out_q_dir     = seed_dir / "questions"
    out_questions = out_q_dir / "free_questions.json"
    out_diagrams  = seed_dir / "diagrams"

    print("=" * 60)
    print("ExamPartner — Free Bundle Export")
    print("=" * 60)
    print(f"\n  Android root : {android_root}")
    print(f"  Output dir   : {seed_dir}")

    if not (android_root / "app").exists():
        print(
            f"\nERROR: 'app' folder not found under {android_root}.\n"
            "  Check --android-root points to the Android project root."
        )
        sys.exit(1)

    # ── Resolve DATABASE_URL ──────────────────────────────────────────────────
    print("\n[1/6] Resolving database connection...")
    database_url = resolve_database_url(args.database_url)
    print("  DATABASE_URL resolved (not printed for security)")

    # ── Connect and query ─────────────────────────────────────────────────────
    print("\n[2/6] Querying free-year objective questions...")
    conn = get_db(database_url)
    cur  = conn.cursor()
    cur.execute(FREE_BUNDLE_QUERY, ("objective", "objective"))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("ERROR: No objective questions found in database. Aborting.")
        sys.exit(1)

    questions = [row_to_question(dict(row)) for row in rows]
    print(f"  Found {len(questions)} questions")

    # ── Per-subject summary ───────────────────────────────────────────────────
    subject_map = {}
    for q in questions:
        key = (q["exam"], q["subject"])
        if key not in subject_map:
            subject_map[key] = {
                "exam":          q["exam"],
                "subject":       q["subject"],
                "freeYear":      q["year"],
                "qtype":         "objective",
                "questionCount": 0,
                "diagramCount":  0,
            }
        subject_map[key]["questionCount"] += 1

    print(f"  Exam/subject pairs: {len(subject_map)}")
    for (exam, subject), info in sorted(subject_map.items()):
        print(f"    {exam:6} / {subject:40} year={info['freeYear']}  q={info['questionCount']}")

    # ── Discover diagrams ─────────────────────────────────────────────────────
    print("\n[3/6] Discovering diagram references...")
    diagram_map = {}  # filename -> (exam, year)
    for q in questions:
        for fname in extract_diagram_filenames(q):
            diagram_map[fname] = (q["exam"], q["year"])

    # Update diagramCount per subject
    for fname, (exam, year) in diagram_map.items():
        for (e, s), info in subject_map.items():
            if e == exam and info["freeYear"] == year:
                info["diagramCount"] += 1

    print(f"  Unique diagrams: {len(diagram_map)}")
    for fname, (exam, year) in sorted(diagram_map.items()):
        print(f"    {exam}/{year}/{fname}")

    # ── Download diagrams from GitHub ─────────────────────────────────────────
    out_diagrams.mkdir(parents=True, exist_ok=True)
    diagram_manifest_entries = []

    if diagram_map:
        print(f"\n[4/6] Fetching {len(diagram_map)} diagram(s) from GitHub...")
        downloaded = skipped = 0
        for fname, (exam, year) in sorted(diagram_map.items()):
            url  = diagram_github_url(exam, year, fname)
            dest = out_diagrams / fname
            if download_diagram(url, dest, force=args.force):
                downloaded += 1
                print(f"    Downloaded : {fname}")
            else:
                skipped += 1
                print(f"    Cached     : {fname}")
            diagram_manifest_entries.append({
                "filename":  fname,
                "exam":      exam,
                "year":      year,
                "assetPath": f"seed/diagrams/{fname}",
            })
        print(f"  Done: {downloaded} downloaded, {skipped} already cached")
    else:
        print("\n[4/6] No diagrams referenced — skipping.")

    # ── Write questions JSON ──────────────────────────────────────────────────
    print("\n[5/6] Writing question JSON...")
    out_q_dir.mkdir(parents=True, exist_ok=True)
    with open(out_questions, "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = out_questions.stat().st_size / 1024
    print(f"  Written : {out_questions}")
    print(f"  Size    : {size_kb:.1f} KB  ({len(questions)} questions)")

    # ── Write manifest ────────────────────────────────────────────────────────
    print("\n[6/6] Writing manifest...")
    bundle_version = compute_bundle_version(questions)
    manifest = {
        "bundleVersion":  bundle_version,
        "generatedAt":    datetime.now(timezone.utc).isoformat(),
        "qtypes":         ["objective"],
        "totalQuestions": len(questions),
        "totalDiagrams":  len(diagram_map),
        "items": sorted(
            subject_map.values(),
            key=lambda x: (x["exam"], x["subject"])
        ),
        "diagrams": diagram_manifest_entries,
    }
    with open(out_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"  Written : {out_manifest}")
    print(f"  Version : {bundle_version}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Export complete.")
    print(f"  Questions : {len(questions)}")
    print(f"  Subjects  : {len(subject_map)}")
    print(f"  Diagrams  : {len(diagram_map)}")
    print(f"  Version   : {bundle_version}")
    print(f"\nNext steps:")
    print(f"  1. Review generated files under: {seed_dir}")
    print(f"  2. Rebuild the APK — assets are included automatically.")
    print(f"  3. To update: update the DB, rerun this script, rebuild.")
    print("=" * 60)


if __name__ == "__main__":
    main()
