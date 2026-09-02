"""Email verification: token generation + SMTP delivery (Resend), with a log-only dev fallback.

When ``MAIL_HOST`` is blank (local dev / unconfigured) the verification link is only logged, never
sent — the whole flow still works, you just read the link from the server log. In prod, mail is
sent via STARTTLS SMTP (Resend: smtp.resend.com). Send failures are logged, not raised, so a flaky
mail provider never breaks registration.
"""
from __future__ import annotations

import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import settings
from ..log import get_logger

log = get_logger(__name__)

TOKEN_TTL = timedelta(hours=24)


def new_token() -> str:
    """A 64-char URL-safe verification token."""
    return secrets.token_hex(32)


def token_expiry() -> datetime:
    return datetime.now(UTC) + TOKEN_TTL


def verification_link(token: str) -> str:
    base = settings.base_url.rstrip("/")
    return f"{base}/ui/#/verify-email?token={token}"


def login_link(token: str) -> str:
    """Magic-link URL: consuming it signs the user in and verifies their email."""
    base = settings.base_url.rstrip("/")
    return f"{base}/ui/#/signin?token={token}"


def _html(link: str) -> str:
    return (
        "<div style=\"font-family:system-ui,sans-serif;max-width:480px;margin:0 auto\">"
        "<h2>Verify your VBallr email</h2>"
        "<p>Click the button below to verify your email address. This link expires in 24 hours.</p>"
        f"<p><a href=\"{link}\" style=\"display:inline-block;padding:10px 18px;background:#e2483d;"
        "color:#fff;border-radius:6px;text-decoration:none\">Verify Email</a></p>"
        f"<p style=\"color:#888;font-size:12px\">Or paste this link: {link}</p>"
        "</div>"
    )


def send_verification(to_email: str, token: str) -> None:
    """Send (or, in dev, log) the verification email. Never raises."""
    link = verification_link(token)
    if not settings.mail_host:
        log.info("email verification (log-only, no MAIL_HOST): %s -> %s", to_email, link)
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Verify your VBallr email"
        msg["From"] = settings.mail_from
        msg["To"] = to_email
        msg.attach(MIMEText(f"Verify your VBallr email: {link}", "plain"))
        msg.attach(MIMEText(_html(link), "html"))
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.mail_username:
                smtp.login(settings.mail_username, settings.mail_password)
            smtp.send_message(msg)
        log.info("sent verification email to %s", to_email)
    except Exception as e:  # never break registration on a mail hiccup
        log.warning("failed to send verification email to %s: %s", to_email, e)


def _login_html(link: str) -> str:
    return (
        "<div style=\"font-family:system-ui,sans-serif;max-width:480px;margin:0 auto\">"
        "<h2>Sign in to VBallr</h2>"
        "<p>Click the button below to sign in. This link expires in 24 hours and can be used once. "
        "If you didn't request it, you can safely ignore this email.</p>"
        f"<p><a href=\"{link}\" style=\"display:inline-block;padding:10px 18px;background:#e2483d;"
        "color:#fff;border-radius:6px;text-decoration:none\">Sign in</a></p>"
        f"<p style=\"color:#888;font-size:12px\">Or paste this link: {link}</p>"
        "</div>"
    )


def send_login_link(to_email: str, token: str) -> None:
    """Send (or, in dev, log) a magic sign-in link. Never raises."""
    link = login_link(token)
    if not settings.mail_host:
        log.info("magic sign-in link (log-only, no MAIL_HOST): %s -> %s", to_email, link)
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Sign in to VBallr"
        msg["From"] = settings.mail_from
        msg["To"] = to_email
        msg.attach(MIMEText(f"Sign in to VBallr: {link}", "plain"))
        msg.attach(MIMEText(_login_html(link), "html"))
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.mail_username:
                smtp.login(settings.mail_username, settings.mail_password)
            smtp.send_message(msg)
        log.info("sent magic sign-in link to %s", to_email)
    except Exception as e:  # never break the request on a mail hiccup
        log.warning("failed to send sign-in link to %s: %s", to_email, e)
