"""
routes/attachments.py — candidate diagram upload for AI theory grading.

Sprint A. A separate router rather than an addition to routes/theory.py so
that Sprint A and Sprint B never touch the same file, which is what lets
storage run in parallel with the content regeneration.

Endpoints:
  POST /theory/attachment
    multipart/form-data:
      file                 required — JPEG, PNG, WebP or PDF, by BYTES
      question_id          required
      attempt_key          required — client-generated, stable for one
                           submission, so a retake replaces rather than appends
      sub_question_label   optional — e.g. "(a)"; omit for a whole-question diagram
    200: { ok, attachment_key, content_type, byte_size, width, height, expires_at }
    400: not an accepted type, empty, oversize, or undecodable
    401: not authenticated
    502: storage backend fault

  GET /theory/attachment/{storage_key}/meta
    Ownership-checked metadata. Lets the client confirm an upload survived
    before it sends the grading request — the check that keeps a dropped
    connection from costing a credit.

No endpoint serves attachment BYTES to a client. Candidate scripts are read
only by the backend grading path. Adding a byte-serving route later means
re-examining whether an unguessable key plus an ownership check is enough
for a URL that could end up in a browser history or a screenshot.

--- Billing ---

Nothing here consumes a grading credit. Upload is free and must stay free:
the whole reason upload completes before the grading request is sent is so
that a failure at this stage costs the student nothing. Credit accounting
begins in grade_theory(), after _fetch_question_data().
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from config import logger
from services import storage_service
from services.attachment_service import create_attachment, get_attachment_record
from services.auth_utils import get_current_user

router = APIRouter(tags=["theory"])


def _require_identifier(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return identifier


@router.post("/theory/attachment")
async def upload_attachment(
    file: UploadFile = File(...),
    question_id: str = Form(...),
    attempt_key: str = Form(...),
    sub_question_label: Optional[str] = Form(None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    identifier = _require_identifier(user)

    question_id = (question_id or "").strip()
    if not question_id:
        raise HTTPException(status_code=400, detail="question_id is required.")

    attempt_key = (attempt_key or "").strip()
    if not attempt_key:
        raise HTTPException(status_code=400, detail="attempt_key is required.")
    if len(attempt_key) > 128:
        raise HTTPException(status_code=400, detail="attempt_key is too long.")

    label = (sub_question_label or "").strip() or None
    if label and len(label) > 32:
        raise HTTPException(status_code=400, detail="sub_question_label is too long.")

    # Read with a hard ceiling rather than trusting Content-Length, which is
    # client-supplied like the filename. One byte over the cap is enough to
    # reject; there is no reason to buffer the rest of a 40 MB upload.
    ceiling = storage_service.MAX_UPLOAD_BYTES + 1
    try:
        raw = await file.read(ceiling)
    except Exception as exc:
        logger.exception("Failed reading uploaded file")
        raise HTTPException(status_code=400, detail="Upload could not be read.") from exc
    finally:
        await file.close()

    try:
        result = create_attachment(
            identifier=identifier,
            question_id=question_id,
            sub_question_label=label,
            attempt_key=attempt_key,
            raw=raw,
        )
    except storage_service.RejectedUpload as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except storage_service.StorageError as exc:
        logger.error("Attachment storage fault: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not save your diagram. Please try again — you have not been charged.",
        )

    if not result.get("durable"):
        # Loud, because a production deploy running on the local backend
        # loses every attachment on restart and the symptom (missing file at
        # grading time) points nowhere near the cause.
        logger.warning(
            "Attachment stored on the NON-DURABLE local backend — R2 is not configured."
        )

    return {
        "ok":             True,
        "attachment_key": result["attachment_key"],
        "content_type":   result["content_type"],
        "byte_size":      result["byte_size"],
        "width":          result["width"],
        "height":         result["height"],
        "expires_at":     result["expires_at"],
    }


@router.get("/theory/attachment/{storage_key:path}/meta")
def attachment_meta(
    storage_key: str,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    identifier = _require_identifier(user)

    record = get_attachment_record(storage_key, identifier)
    # 404 for both "no such key" and "not yours". Distinguishing them would
    # turn this endpoint into an oracle for whether a given key exists.
    if not record or record["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Attachment not found.")

    return {
        "ok":                 True,
        "attachment_key":     record["storage_key"],
        "question_id":        record["question_id"],
        "sub_question_label": record["sub_question_label"],
        "content_type":       record["content_type"],
        "byte_size":          record["byte_size"],
        "width":              record["width"],
        "height":             record["height"],
        "status":             record["status"],
        "expires_at":         record["expires_at"],
        "created_at":         record["created_at"],
    }
