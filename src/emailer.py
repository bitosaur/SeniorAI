from __future__ import annotations

import logging
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("seniorai.emailer")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def send_scan(
    gmail_user: str,
    gmail_app_password: str,
    recipient: str,
    image_path: Path,
    subject: str | None = None,
    timeout: int = 30,
) -> None:
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg["Subject"] = subject or f"Scan: {image_path.name}"
    msg.set_content(
        f"Scan captured at {image_path.stem.replace('scan_', '')}.\n"
        "Sent automatically from SeniorAI."
    )

    with image_path.open("rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype="image", subtype="jpeg", filename=image_path.name)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
            smtp.login(gmail_user, gmail_app_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, socket.error) as e:
        log.error("SMTP send failed: %s", e)
        raise

    log.info("Emailed %s to %s", image_path.name, recipient)
