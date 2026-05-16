"""Minimal SMTP-backed email delivery for auth flows."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import settings


logger = logging.getLogger(__name__)

_DEV_ENVS = {"dev", "development", "local", "test"}


class EmailDeliveryError(RuntimeError):
    """Raised when an email should have been delivered but was not."""


def _is_dev_env() -> bool:
    return settings.app_env.strip().lower() in _DEV_ENVS


def _email_hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def _password_reset_message(
    *,
    to_email: str,
    reset_url: str,
    expires_minutes: int,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Reset your Lumen password"
    msg["From"] = settings.smtp_from_email.strip()
    msg["To"] = to_email
    msg.set_content(
        "\n".join(
            (
                "We received a request to reset your Lumen password.",
                "",
                f"Open this link to choose a new password: {reset_url}",
                "",
                f"This link expires in {expires_minutes} minutes.",
                "If you did not request this, you can ignore this email.",
            )
        )
    )
    return msg


def _send_smtp_message(msg: EmailMessage) -> None:
    host = settings.smtp_host.strip()
    port = settings.smtp_port
    timeout = settings.smtp_timeout_seconds
    username = settings.smtp_username.strip()
    password = settings.smtp_password
    context = ssl.create_default_context()

    if settings.smtp_use_tls:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if settings.smtp_starttls:
            smtp.starttls(context=context)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)


async def send_password_reset_email(
    *,
    to_email: str,
    reset_url: str,
    expires_minutes: int,
) -> None:
    """Send a password reset link.

    In dev/test without SMTP, log the link instead of attempting network
    delivery. Production configuration is validated at settings load time.
    """

    if not settings.smtp_host.strip():
        if _is_dev_env():
            logger.warning(
                "password_reset_email_dev_delivery",
                extra={
                    "to_email_hash": _email_hash(to_email),
                    "reset_url": reset_url,
                    "expires_minutes": expires_minutes,
                },
            )
            return
        raise EmailDeliveryError("SMTP_HOST is not configured")

    msg = _password_reset_message(
        to_email=to_email,
        reset_url=reset_url,
        expires_minutes=expires_minutes,
    )
    try:
        await asyncio.to_thread(_send_smtp_message, msg)
    except Exception as exc:  # noqa: BLE001
        raise EmailDeliveryError("password reset email delivery failed") from exc


__all__ = ["EmailDeliveryError", "send_password_reset_email"]
