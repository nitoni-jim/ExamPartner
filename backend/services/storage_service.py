"""
services/storage_service.py — object storage for candidate diagram attachments.

Sprint A. Deliberately knows nothing about grading, questions or users: it
takes bytes in and hands back a storage key, and hands the bytes back when
asked. All ownership and lifecycle logic lives in attachment_service.py.

Two backends:
  - R2 (Cloudflare, S3-compatible) when R2_BUCKET and credentials are set
  - local filesystem otherwise

The local backend is not a toy — it is how the whole upload path is
developed and tested without provisioning a bucket, and it mirrors the
pattern already used for RESEND_API_KEY (absent in dev, the feature
degrades rather than crashing). It is NOT safe for production: Render's
filesystem is ephemeral, so a deploy loses every stored attachment.
is_durable() reports which backend is live; the health check surfaces it.

R2 over S3: grading re-reads each image (candidate plus, where supplied,
the reference diagram), and every re-read on S3 is billed egress. R2's
egress is zero-rated. Client-side data cost is already a constraint for
this user base and the same argument applies to the server side.

--- EXIF canonicalisation ---

Images are transposed to their upright orientation and re-encoded WITHOUT
metadata before they are stored. The stored raster is canonical: no
consumer downstream ever has to know that Orientation exists.

This is not hypothetical tidiness. During development three automated
extractions returned plausible-looking wrong answers from a sideways
photograph before anyone noticed the tag — the failure mode is silent and
expensive, because a rotated diagram grades as a real but wrong answer
rather than as an error.

Stripping the rest of the metadata happens at the same point and for a
different reason: phone photographs carry GPS coordinates and device
identifiers, the platform serves secondary-school candidates, and nothing
in the grading path reads either field.
"""
import hashlib
import io
import os
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

import config
from config import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Sourced from config.py rather than os.getenv, per that module's own rule:
# "Import from here instead of reading os.getenv in multiple places." Bound
# to module-level names so tests can monkeypatch them.
R2_BUCKET            = config.R2_BUCKET
R2_ENDPOINT_URL      = config.R2_ENDPOINT_URL
R2_ACCESS_KEY_ID     = config.R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY = config.R2_SECRET_ACCESS_KEY
R2_REGION            = config.R2_REGION

# Local dev fallback root.
LOCAL_ATTACHMENTS_DIR = os.getenv(
    "LOCAL_ATTACHMENTS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "attachments"),
)

# Upload size cap, applied to the ORIGINAL bytes before canonicalisation.
#
# 12 MB, not 40. Android downscales to roughly 1600 px before upload
# (Sprint C), which lands well under 1 MB for a phone photograph of a
# notebook page. The headroom exists for the case where downscaling is
# skipped or fails and the raw capture is sent — a 12-megapixel JPEG is
# typically 3-6 MB. Anything past 12 MB is a PDF scan or a mistake, and
# both are better refused at the edge than carried through canonicalisation.
MAX_UPLOAD_BYTES = config.ATTACHMENT_MAX_BYTES

# Longest edge of the stored raster. Server-side backstop only — the real
# downscale happens on the client to save the student's data. 2000 px is
# deliberately above the Sprint C client target so the server never becomes
# the binding constraint on letterform detail; see the faint-pencil risk in
# the sprint plan.
MAX_STORED_EDGE_PX = config.ATTACHMENT_MAX_EDGE_PX

JPEG_QUALITY = int(os.getenv("ATTACHMENT_JPEG_QUALITY", "90"))

ACCEPTED_CONTENT_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
})

_EXT_FOR_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


class StorageError(Exception):
    """Raised for backend faults. Callers map this to a 502/503 and,
    critically, to 'no charge' — a storage fault is ours, not the
    student's."""


class RejectedUpload(Exception):
    """Raised for client-side faults: unsupported type, oversize, corrupt
    image. Callers map this to a 400."""


# ---------------------------------------------------------------------------
# Content-type detection — from bytes, never from the filename
# ---------------------------------------------------------------------------

def sniff_content_type(data: bytes) -> Optional[str]:
    """
    Returns the content type implied by the leading bytes, or None.

    The client-supplied filename and Content-Type header are both ignored
    everywhere in this module. A PNG renamed .jpg is a normal thing for a
    gallery picker to produce and must be accepted on its actual bytes; the
    inverse — trusting a .jpg label on something that is not an image — is
    the part that matters for safety.
    """
    if not data or len(data) < 12:
        return None

    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

def canonicalise(data: bytes, content_type: str) -> Tuple[bytes, str, Optional[int], Optional[int]]:
    """
    Returns (canonical_bytes, content_type, width, height).

    Images: orientation applied, all metadata dropped, re-encoded in the
    same family (JPEG stays JPEG, PNG stays PNG, WebP stays WebP). Format
    is preserved rather than normalised to JPEG because a PNG upload is
    usually a screenshot or a scan, and putting JPEG artefacts on line art
    would attack exactly the letterform detail the naming criteria depend on.

    PDFs pass through unchanged — there is no orientation tag to resolve and
    no metadata worth the risk of rewriting the file. Rasterisation for
    grading is a Sprint B concern and happens at read time, not here.
    """
    if content_type == "application/pdf":
        return data, content_type, None, None

    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise RejectedUpload(f"Image could not be decoded: {type(exc).__name__}")

    # Apply Orientation, then discard it. exif_transpose returns a copy with
    # the tag already removed from its info dict.
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        logger.warning("exif_transpose failed; storing untransposed raster")

    if MAX_STORED_EDGE_PX > 0 and max(img.size) > MAX_STORED_EDGE_PX:
        img.thumbnail((MAX_STORED_EDGE_PX, MAX_STORED_EDGE_PX), Image.LANCZOS)

    out = io.BytesIO()
    if content_type == "image/jpeg":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    elif content_type == "image/png":
        img.save(out, format="PNG", optimize=True)
    elif content_type == "image/webp":
        img.save(out, format="WEBP", quality=JPEG_QUALITY)
    else:
        raise RejectedUpload(f"Unsupported content type: {content_type}")

    return out.getvalue(), content_type, img.size[0], img.size[1]


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------

def build_key(user_hash: str, question_id: str, content_type: str) -> str:
    """
    Storage key layout:
        theory/<YYYY>/<MM>/<user_hash>/<question_id>/<random>.<ext>

    The user identifier is hashed rather than embedded. Identifiers are
    email addresses and phone numbers; an object key is the wrong place for
    either, and a hash still gives a per-user prefix for the retention
    sweep and for support lookups.

    The random component makes the key unguessable, which is what stops a
    key leaked in a log from being a working handle on someone else's
    script. Ownership is still checked in the database on every read —
    the randomness is defence in depth, not the access control.
    """
    now = datetime.now(timezone.utc)
    ext = _EXT_FOR_TYPE.get(content_type, "bin")
    safe_qid = "".join(c if (c.isalnum() or c in "-_") else "_" for c in question_id)[:80]
    return (
        f"theory/{now:%Y}/{now:%m}/{user_hash[:16]}/{safe_qid}/"
        f"{secrets.token_urlsafe(18)}.{ext}"
    )


def hash_identifier(identifier: str) -> str:
    return hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def is_durable() -> bool:
    """True when a real object store is configured."""
    return bool(R2_BUCKET and R2_ENDPOINT_URL and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name=R2_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )


def _local_path(key: str) -> str:
    # Keys are built by build_key() and contain no traversal, but this is a
    # filesystem write driven by a request path, so normalise and confine it.
    root = os.path.abspath(LOCAL_ATTACHMENTS_DIR)
    full = os.path.abspath(os.path.join(root, key))
    if not full.startswith(root + os.sep):
        raise StorageError("Refusing to write outside the attachments root.")
    return full


def put_object(key: str, data: bytes, content_type: str) -> None:
    if is_durable():
        try:
            _r2_client().put_object(
                Bucket=R2_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Diagram scripts are candidate work; they are read only
                # through the authenticated backend path, never by URL.
                CacheControl="private, no-store",
            )
        except Exception as exc:
            logger.exception("R2 put_object failed for key=%s", key)
            raise StorageError(f"Object storage write failed: {type(exc).__name__}") from exc
        return

    path = _local_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    except Exception as exc:
        logger.exception("Local attachment write failed for key=%s", key)
        raise StorageError(f"Local storage write failed: {type(exc).__name__}") from exc


def get_object(key: str) -> bytes:
    if is_durable():
        try:
            resp = _r2_client().get_object(Bucket=R2_BUCKET, Key=key)
            return resp["Body"].read()
        except Exception as exc:
            logger.exception("R2 get_object failed for key=%s", key)
            raise StorageError(f"Object storage read failed: {type(exc).__name__}") from exc

    path = _local_path(key)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        raise StorageError(f"Attachment object missing: {key}")
    except Exception as exc:
        raise StorageError(f"Local storage read failed: {type(exc).__name__}") from exc


def delete_object(key: str) -> None:
    """Best-effort. A failed delete is logged and swallowed: the database row
    is the record of truth for retention, and a resilient sweep that retries
    next run beats one that aborts partway through."""
    try:
        if is_durable():
            _r2_client().delete_object(Bucket=R2_BUCKET, Key=key)
            return
        path = _local_path(key)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.warning("Attachment delete failed for key=%s — will retry on next sweep", key)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def store_upload(
    raw: bytes,
    identifier: str,
    question_id: str,
) -> dict:
    """
    Validates, canonicalises and stores one upload.

    Raises RejectedUpload (client fault → 400) or StorageError (our fault →
    502, no charge).

    Returns: {storage_key, content_type, byte_size, width, height, sha256}
    """
    if not raw:
        raise RejectedUpload("Uploaded file is empty.")

    if len(raw) > MAX_UPLOAD_BYTES:
        raise RejectedUpload(
            f"File is too large ({len(raw) // (1024 * 1024)} MB). "
            f"Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    content_type = sniff_content_type(raw)
    if content_type is None or content_type not in ACCEPTED_CONTENT_TYPES:
        raise RejectedUpload(
            "Unsupported file type. Upload a JPEG, PNG, WebP image or a PDF."
        )

    canonical, content_type, width, height = canonicalise(raw, content_type)

    key = build_key(hash_identifier(identifier), question_id, content_type)
    put_object(key, canonical, content_type)

    return {
        "storage_key":  key,
        "content_type": content_type,
        "byte_size":    len(canonical),
        "width":        width,
        "height":       height,
        "sha256":       sha256_bytes(canonical),
    }
