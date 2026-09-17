"""
tests/test_app_imports.py — the app loads.

Deliberately the cheapest test in the suite: no database, no credentials, no
network, no fixtures. It runs in about a second.

It exists because of a specific failure. routes/attachments.py uses UploadFile
and Form, which FastAPI parses with python-multipart. That dependency was
missing from requirements.txt, and FastAPI raises at ROUTE-DEFINITION time —
during import, before any request is served. The whole service failed to boot.
The first anyone saw of it was a Render deploy log.

Nothing in the suite caught it, because every test that imports the app was
wrapped in a try/except that turned the failure into a skip. A skipped test
reads as "not applicable here" and gets scrolled past; a failing one does not.

So this test does NOT catch its exception. If the app cannot be imported, the
real traceback is what you want to read — a missing dependency, a syntax error
in a route, a circular import, a typo in a router name. Wrapping that in a
friendly message throws away the only useful information.
"""


def test_app_imports():
    from app import app
    assert app is not None


def test_attachment_route_is_registered():
    """The upload endpoint is reachable.

    A router can import cleanly and still not be wired into the app — the
    include_router line is a separate edit from the import line, and missing
    it produces a 404 at runtime with nothing wrong anywhere in the logs.
    """
    from app import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/theory/attachment" in paths, (
        f"POST /theory/attachment is not registered. Check that app.py has "
        f"include_router(attachments_router). Registered theory paths: "
        f"{sorted(p for p in paths if 'theory' in p)}"
    )
