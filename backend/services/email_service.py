"""
services/email_service.py — outbound email for ExamPartner via Resend.

Only used for password reset codes. All other user communication is
in-app. This module is intentionally narrow.

Environment variables (set in Render):
  RESEND_API_KEY      — Resend API key (re_xxxx...). Required for email to send.
  SUPPORT_EMAIL_FROM  — Verified sender address, e.g. "ExamPartner <noreply@exampartner.com>".

Behaviour when RESEND_API_KEY is absent (local dev):
  send_reset_code() logs a warning and returns False instead of raising.
  This lets the backend start and respond correctly in dev even without
  an email provider configured.
"""

import logging
from typing import Optional

from config import RESEND_API_KEY, SUPPORT_EMAIL_FROM

logger = logging.getLogger("exampartner")

# ---------------------------------------------------------------------------
# Email content
# ---------------------------------------------------------------------------

_RESET_SUBJECT = "Your ExamPartner Password Reset Code"

_RESET_BODY_TEXT = """\
Hi,

You requested a password reset for your ExamPartner account.

Your reset code is:

    {code}

This code expires in 30 minutes and can only be used once.

If you did not request a password reset, you can ignore this email.
Your password will not change.

— ExamPartner Support
"""

_RESET_BODY_HTML = """\
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;max-width:480px;margin:auto;padding:24px;">
  <h2 style="color:#1a56db;">ExamPartner Password Reset</h2>
  <p>You requested a password reset. Use the code below inside the app.</p>
  <div style="
    display:inline-block;
    background:#f0f4ff;
    border:2px solid #1a56db;
    border-radius:8px;
    padding:16px 32px;
    font-size:2rem;
    font-weight:bold;
    letter-spacing:0.3em;
    color:#1a56db;
    margin:16px 0;
  ">{code}</div>
  <p style="color:#555;font-size:0.9rem;">
    This code expires in <strong>30 minutes</strong> and can only be used once.
  </p>
  <p style="color:#555;font-size:0.9rem;">
    If you did not request this, you can safely ignore this email.
    Your password will not change.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="color:#999;font-size:0.8rem;">ExamPartner — Exam prep for JAMB, WAEC, and NECO students.</p>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_reset_code(to_email: str, code: str) -> bool:
    """
    Send a password reset code to the given email address via Resend.

    Returns True on success, False on failure (errors are logged, not raised).
    The caller must never tell the user whether this succeeded or failed
    — the response to the forgot-password endpoint is always generic.

    Args:
        to_email: Recipient email address (from users.email).
        code:     The raw 6-digit reset code to include in the email.
                  Never stored — this is the only place it appears as plaintext.
    """
    if not RESEND_API_KEY:
        logger.warning(
            "email_service: RESEND_API_KEY not set — skipping reset email to %s (dev mode)",
            _redact_email(to_email),
        )
        return False

    try:
        import resend  # pip install resend
        resend.api_key = RESEND_API_KEY

        params: resend.Emails.SendParams = {
            "from":    SUPPORT_EMAIL_FROM,
            "to":      [to_email],
            "subject": _RESET_SUBJECT,
            "text":    _RESET_BODY_TEXT.format(code=code),
            "html":    _RESET_BODY_HTML.format(code=code),
        }

        response = resend.Emails.send(params)
        logger.info(
            "email_service: reset code sent to %s — resend id=%s",
            _redact_email(to_email),
            getattr(response, "id", "?"),
        )
        return True

    except ImportError:
        logger.error(
            "email_service: 'resend' package not installed. "
            "Add 'resend' to requirements.txt."
        )
        return False

    except Exception as exc:
        logger.error(
            "email_service: failed to send reset code to %s — %s: %s",
            _redact_email(to_email),
            type(exc).__name__,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _redact_email(email: str) -> str:
    """Redact email for safe logging: 'user@example.com' → 'u***@example.com'."""
    try:
        local, domain = email.split("@", 1)
        return f"{local[0]}***@{domain}"
    except Exception:
        return "***"
