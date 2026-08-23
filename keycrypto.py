"""Encrypts the user's API keys for the trip from Render to the GitHub Actions
worker. Both sides derive the same key from SECRET_KEY, so the value visible in
the workflow run's inputs is unreadable ciphertext."""
import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet():
    digest = hashlib.sha256(os.environ["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
