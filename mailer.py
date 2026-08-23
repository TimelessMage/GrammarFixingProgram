"""Email notifications.

Two transports, picked automatically:
  1. Brevo HTTP API  - if BREVO_API_KEY is set. Works everywhere, including
     Render's free tier (which blocks classic email ports). Free: brevo.com,
     300 emails/day, no card. Verify your sender address there, then set
     BREVO_API_KEY (and optionally BREVO_SENDER, defaults to GMAIL_ADDRESS).
  2. Gmail SMTP      - otherwise. Works on GitHub Actions / your own PC,
     NOT on Render free (its network blocks SMTP ports).
"""
import os
import smtplib
from email.message import EmailMessage

import requests


def send(to_addr, subject, body):
    try:
        if os.environ.get("BREVO_API_KEY"):
            _send_brevo(to_addr, subject, body)
        else:
            _send_gmail(to_addr, subject, body)
    except Exception as e:  # an email failure should never kill a job
        print(f"[mailer] could not send to {to_addr}: {e}", flush=True)


def _send_brevo(to_addr, subject, body):
    sender = os.environ.get("BREVO_SENDER") or os.environ["GMAIL_ADDRESS"]
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": os.environ["BREVO_API_KEY"], "Content-Type": "application/json"},
        json={"sender": {"email": sender, "name": "Redline"},
              "to": [{"email": to_addr}],
              "subject": subject, "textContent": body},
        timeout=20)
    r.raise_for_status()


def _send_gmail(to_addr, subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
