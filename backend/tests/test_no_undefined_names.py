"""
tests/test_no_undefined_names.py — nothing references a name that does not exist.

Catches the class of bug that has shipped twice now and both times reached a
Render deploy log: code that is syntactically valid, parses cleanly, passes
every unit test that does not import it, and then raises the instant Python
executes the module.

    NameError: name 'List' is not defined

ast.parse() does not catch this. pytest does not catch it unless something
imports the module, and the modules most likely to carry it — routes and
services that pull in config and the database — are exactly the ones unit
tests tend to import lazily or not at all.

Checks F821 (undefined name) ONLY. Unused imports and other style findings are
deliberately ignored: this test must stay at zero on a codebase that has never
been linted, or it becomes noise and gets skipped.
"""
import os
import subprocess
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "reports"}


def _python_files():
    for root, dirs, files in os.walk(BACKEND_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_no_undefined_names():
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("pyflakes not installed — pip install -r requirements-dev.txt")

    files = sorted(_python_files())
    assert files, "no Python files found to check"

    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *files],
        capture_output=True, text=True,
    )

    undefined = [
        line for line in proc.stdout.splitlines()
        if "undefined name" in line or "may be undefined" in line
    ]

    assert not undefined, (
        "Undefined names found. Each of these raises at import time and takes "
        "the whole service down on deploy:\n  " + "\n  ".join(undefined)
    )
