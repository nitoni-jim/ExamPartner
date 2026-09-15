"""
tests/test_attachments.py — Sprint A acceptance tests.

Covers every case the sprint plan names for Sprint A, plus the ownership
and retake cases the plan implies but does not list.

Run: pytest tests/test_attachments.py -v

The storage-layer tests need no database and no R2. The route tests use
FastAPI's TestClient against a SQLite database and the local storage
backend, so they run in CI without provisioning a bucket.
"""
import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import storage_service  # noqa: E402
from services.storage_service import (  # noqa: E402
    RejectedUpload,
    canonicalise,
    sniff_content_type,
)

W, H = 120, 80


def _upright() -> Image.Image:
    """Asymmetric on both axes AND both diagonals, so any wrong rotation is
    detectable. A square or symmetric fixture would pass while rotated."""
    img = Image.new("RGB", (W, H))
    px = img.load()
    for x in range(W):
        for y in range(H):
            left, top = x < W // 2, y < H // 2
            px[x, y] = (
                (255, 0, 0) if (left and top) else
                (0, 255, 0) if (not left and top) else
                (0, 0, 255) if (left and not top) else
                (255, 255, 255)
            )
    return img


# PIL rotate() is counter-clockwise for positive angles.
#   tag 6 = "rotate 90 CW to display"  -> stored is upright rotated 90 CCW
#   tag 8 = "rotate 90 CCW to display" -> stored is upright rotated 90 CW
_PRE = {
    1: lambda im: im,
    3: lambda im: im.rotate(180, expand=True),
    6: lambda im: im.rotate(90, expand=True),
    8: lambda im: im.rotate(270, expand=True),
}


def _jpeg_with_orientation(orientation: int, gps: bool = False) -> bytes:
    stored = _PRE[orientation](_upright())
    exif = Image.Exif()
    exif[274] = orientation          # Orientation
    exif[271] = "TestPhone"          # Make
    if gps:
        # GPSInfo must be written through the IFD API — assigning a dict to
        # tag 34853 directly fails inside Pillow's rational packer.
        gps_ifd = exif.get_ifd(0x8825)
        gps_ifd[1] = "N"
        gps_ifd[2] = (5.0, 30.0, 0.0)
        gps_ifd[3] = "E"
        gps_ifd[4] = (3.0, 22.0, 0.0)
    out = io.BytesIO()
    stored.save(out, format="JPEG", quality=95, exif=exif)
    return out.getvalue()


def _corners(img: Image.Image):
    w, h = img.size
    px = img.load()
    snap = lambda p: tuple(255 if c > 170 else 0 for c in p[:3])
    return (
        snap(px[w // 4, h // 4]),
        snap(px[3 * w // 4, h // 4]),
        snap(px[w // 4, 3 * h // 4]),
        snap(px[3 * w // 4, 3 * h // 4]),
    )


UPRIGHT_CORNERS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))


# ---------------------------------------------------------------------------
# EXIF orientation — the specific defect found in development
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_rotated_jpeg_stores_upright(orientation):
    """The plan's headline Sprint A test. Three automated extractions
    returned plausible-looking WRONG answers from a sideways photograph
    before the tag was noticed — a rotated diagram grades as a real but
    wrong answer, not as an error, which is why this is silent."""
    raw = _jpeg_with_orientation(orientation)
    canonical, ctype, w, h = canonicalise(raw, sniff_content_type(raw))
    result = Image.open(io.BytesIO(canonical))

    assert _corners(result) == UPRIGHT_CORNERS, f"EXIF {orientation} not resolved"
    assert (w, h) == (W, H)
    assert ctype == "image/jpeg"


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_orientation_tag_not_carried_forward(orientation):
    """Store the canonical raster, never the original with a tag — so no
    future consumer re-encounters the trap."""
    raw = _jpeg_with_orientation(orientation)
    canonical, _, _, _ = canonicalise(raw, sniff_content_type(raw))
    assert not Image.open(io.BytesIO(canonical)).getexif().get(274)


def test_gps_and_device_metadata_stripped():
    """Phone photographs carry GPS and device identifiers, the platform
    serves secondary-school candidates, and nothing in the grading path
    reads either."""
    raw = _jpeg_with_orientation(1, gps=True)
    assert Image.open(io.BytesIO(raw)).getexif().get(34853), "fixture has no GPS"

    canonical, _, _, _ = canonicalise(raw, sniff_content_type(raw))
    exif = Image.open(io.BytesIO(canonical)).getexif()
    assert not exif.get(34853), "GPS survived canonicalisation"
    assert not exif.get(271), "device Make survived canonicalisation"


# ---------------------------------------------------------------------------
# Content type — from bytes, not from the filename
# ---------------------------------------------------------------------------

def test_png_renamed_jpg_is_accepted_on_its_bytes():
    buf = io.BytesIO()
    _upright().save(buf, format="PNG")
    assert sniff_content_type(buf.getvalue()) == "image/png"


def test_png_is_not_converted_to_jpeg():
    """Same family in, same family out. Converting line art to JPEG would
    attack exactly the letterform detail naming criteria depend on."""
    buf = io.BytesIO()
    _upright().save(buf, format="PNG")
    canonical, ctype, _, _ = canonicalise(buf.getvalue(), "image/png")
    assert ctype == "image/png"
    assert Image.open(io.BytesIO(canonical)).format == "PNG"


def test_non_image_masquerading_as_jpg_is_rejected():
    assert sniff_content_type(b"#!/bin/sh\nrm -rf /\n" + b"x" * 64) is None


@pytest.mark.parametrize("data", [b"", b"short"])
def test_empty_and_truncated_are_rejected(data):
    assert sniff_content_type(data) is None


def test_pdf_passes_through_unchanged():
    pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
    out, ctype, w, h = canonicalise(pdf, "application/pdf")
    assert out == pdf and ctype == "application/pdf" and w is None and h is None


def test_corrupt_image_raises_rejected_not_storage_error():
    """A truncated upload is the student's problem (400), not a backend
    fault (502) — and the distinction decides whether they are charged."""
    truncated = _jpeg_with_orientation(1)[:40]
    with pytest.raises(RejectedUpload):
        canonicalise(truncated, "image/jpeg")


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------

def test_oversize_upload_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_service, "MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(storage_service, "LOCAL_ATTACHMENTS_DIR", str(tmp_path))
    with pytest.raises(RejectedUpload) as exc:
        storage_service.store_upload(
            raw=b"\xff\xd8\xff" + b"x" * 4096,
            identifier="student@example.com",
            question_id="WAEC_2020_BIOLOGY_THEORY_Q6",
        )
    assert "too large" in str(exc.value).lower()


def test_downscale_backstop_does_not_upscale(monkeypatch):
    """The server must never be the binding constraint on letterform
    detail — a small image passes through at its own size."""
    monkeypatch.setattr(storage_service, "MAX_STORED_EDGE_PX", 2000)
    raw = _jpeg_with_orientation(1)
    _, _, w, h = canonicalise(raw, "image/jpeg")
    assert (w, h) == (W, H)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_key_does_not_contain_the_raw_identifier():
    """Identifiers are email addresses and phone numbers; an object key is
    the wrong place for either."""
    ident = "student@example.com"
    key = storage_service.build_key(
        storage_service.hash_identifier(ident), "WAEC_2020_BIO_Q6", "image/jpeg"
    )
    assert ident not in key and "student" not in key
    assert key.endswith(".jpg")


def test_keys_are_unique_per_upload():
    h = storage_service.hash_identifier("a@b.com")
    keys = {storage_service.build_key(h, "Q1", "image/jpeg") for _ in range(200)}
    assert len(keys) == 200


def test_key_is_confined_to_the_attachments_root(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_service, "LOCAL_ATTACHMENTS_DIR", str(tmp_path))
    with pytest.raises(storage_service.StorageError):
        storage_service._local_path("../../etc/passwd")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_stored_bytes_round_trip_identically(tmp_path, monkeypatch):
    """Acceptance: retrievable upright and byte-identical from the backend."""
    monkeypatch.setattr(storage_service, "LOCAL_ATTACHMENTS_DIR", str(tmp_path))
    meta = storage_service.store_upload(
        raw=_jpeg_with_orientation(6),
        identifier="student@example.com",
        question_id="WAEC_2020_BIOLOGY_THEORY_Q6",
    )
    fetched = storage_service.get_object(meta["storage_key"])
    assert storage_service.sha256_bytes(fetched) == meta["sha256"]
    assert _corners(Image.open(io.BytesIO(fetched))) == UPRIGHT_CORNERS


def test_local_backend_reports_itself_as_non_durable(monkeypatch):
    """A production deploy on the local backend loses every attachment on
    restart, and the symptom points nowhere near the cause."""
    monkeypatch.setattr(storage_service, "R2_BUCKET", "")
    assert storage_service.is_durable() is False


# ---------------------------------------------------------------------------
# Route-level — auth and retake
#
# These need the app, a SQLite DB and the theory_attachments table from the
# db.py patch. Skipped automatically until those are in place.
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(storage_service, "LOCAL_ATTACHMENTS_DIR", str(tmp_path / "att"))
    try:
        from fastapi.testclient import TestClient
        from app import app
        from db import init_db
    except Exception as exc:
        pytest.skip(f"app not importable in this environment: {exc}")
    init_db()
    return TestClient(app)


def test_unauthenticated_upload_is_refused(client):
    resp = client.post(
        "/theory/attachment",
        files={"file": ("d.jpg", _jpeg_with_orientation(1), "image/jpeg")},
        data={"question_id": "WAEC_2020_BIO_Q6", "attempt_key": "att-1"},
    )
    assert resp.status_code == 401


def test_retake_replaces_rather_than_appends(client, auth_headers):
    """Same (user, question, label, attempt_key) — the second upload
    supersedes the first, so grading never finds two candidate images for
    one sub-question.

    Requires an `auth_headers` fixture from the existing test suite.
    """
    payload = {
        "question_id": "WAEC_2020_BIO_Q6",
        "attempt_key": "att-1",
        "sub_question_label": "(a)",
    }
    first = client.post(
        "/theory/attachment",
        files={"file": ("d.jpg", _jpeg_with_orientation(1), "image/jpeg")},
        data=payload, headers=auth_headers,
    ).json()
    second = client.post(
        "/theory/attachment",
        files={"file": ("d.jpg", _jpeg_with_orientation(3), "image/jpeg")},
        data=payload, headers=auth_headers,
    ).json()

    assert first["attachment_key"] != second["attachment_key"]
    assert client.get(
        f"/theory/attachment/{first['attachment_key']}/meta", headers=auth_headers
    ).status_code == 404
    assert client.get(
        f"/theory/attachment/{second['attachment_key']}/meta", headers=auth_headers
    ).status_code == 200


def test_another_users_key_is_not_readable(client, auth_headers, other_auth_headers):
    """The unguessable key is defence in depth, not the access control."""
    key = client.post(
        "/theory/attachment",
        files={"file": ("d.jpg", _jpeg_with_orientation(1), "image/jpeg")},
        data={"question_id": "WAEC_2020_BIO_Q6", "attempt_key": "att-1"},
        headers=auth_headers,
    ).json()["attachment_key"]

    assert client.get(
        f"/theory/attachment/{key}/meta", headers=other_auth_headers
    ).status_code == 404
