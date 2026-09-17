"""
tests/conftest.py — shared test setup.

Puts the backend root on sys.path once, so individual test files do not each
repeat a sys.path.insert. pytest inserts the rootdir only under some
invocations, and `python -m pytest` versus `pytest` differ on whether the
working directory lands on the path — this removes the difference.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
