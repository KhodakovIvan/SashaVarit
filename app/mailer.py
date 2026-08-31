from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import Settings


def smtp_configured(settings: Settings) -> bool:
    return bool(settings.smtp_user and settings.smtp_password and settings.smtp_from and settings.order_email_to)


def send_order_email(
    settings: Settings,
    subject: str,
    body: str,
    xls_bytes: bytes,
    filename: str,
) -> None:
    if not settings.smtp_user or not settings.smtp_password:
        raise RuntimeError("В .env не заданы SMTP_USER / SMTP_PASSWORD")

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = settings.order_email_to
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(
        xls_bytes,
        maintype="application",
        subtype="vnd.ms-excel",
        filename=filename,
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
