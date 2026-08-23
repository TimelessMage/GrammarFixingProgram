"""Email notifications (replaces Pushbullet).

Uses Gmail SMTP with an App Password.
Secrets on the Space: GMAIL_ADDRESS, GMAIL_APP_PASSWORD
(Google Account -> Security -> 2-Step Verification -> App passwords)
"""
import os
import smtplib
from email.message import EmailMessage


def send(to_addr, subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
            s.send_message(msg)
    except Exception as e:  # an email failure should never kill a job
        print(f"[mailer] could not send to {to_addr}: {e}", flush=True)
