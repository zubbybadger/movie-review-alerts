"""Send alerts.

Three channels, each turned on by setting its secrets:
  * Email via Gmail: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (sends to yourself, or to EMAIL_TO)
  * ntfy (free push notifications): NTFY_TOPIC
  * Twilio (real SMS): TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO
Every configured channel is used.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

log = logging.getLogger(__name__)


def _ntfy(client: httpx.Client, title: str, body: str, url: str | None, tags: str) -> None:
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": body, "tags": [tags]}
    if url:
        payload["click"] = url
    # JSON publishing keeps emoji and accents intact (HTTP headers can't carry them).
    client.post(f"{server}/", json=payload).raise_for_status()


def _twilio(client: httpx.Client, title: str, body: str, url: str | None) -> None:
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    text = f"{title}\n{body}" + (f"\n{url}" if url else "")
    client.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]),
        data={"From": os.environ["TWILIO_FROM"], "To": os.environ["TWILIO_TO"], "Body": text},
    ).raise_for_status()


def _email(title: str, body: str, url: str | None) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = f"Review bot <{sender}>"
    msg["To"] = os.environ.get("EMAIL_TO") or sender
    msg.set_content(body + (f"\n\nRead the review: {url}" if url else ""))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        # App passwords are shown with spaces; Gmail accepts them either way.
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        smtp.send_message(msg)


def channels() -> list[str]:
    out = []
    if os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"):
        out.append("email")
    if os.environ.get("NTFY_TOPIC"):
        out.append("ntfy")
    if all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO")):
        out.append("twilio")
    return out


def send(client: httpx.Client, title: str, body: str, url: str | None = None, tags: str = "movie_camera") -> bool:
    """Send on every configured channel. Returns True if at least one succeeded."""
    active = channels()
    if not active:
        log.info("no alert channel configured; would have sent:\n%s\n%s\n%s", title, body, url or "")
        return False
    ok = False
    for name in active:
        try:
            if name == "email":
                _email(title, body, url)
            elif name == "ntfy":
                _ntfy(client, title, body, url, tags)
            else:
                _twilio(client, title, body, url)
            ok = True
        except (httpx.HTTPError, smtplib.SMTPException, OSError) as exc:
            log.error("%s alert failed: %s", name, exc)
    return ok
