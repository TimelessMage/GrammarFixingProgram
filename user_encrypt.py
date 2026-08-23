"""YOUR encryption script plugs in here.

The rest of the app only ever calls these two functions:

  protect_password(plain) -> str      what gets written to the Google Sheet
  verify_password(plain, stored) -> bool   True if a login attempt matches

Replace the bodies below with calls into your own script. Until then, a safe
default (salted PBKDF2 hashing) is in place so the site works out of the box.

Heads-up on one distinction: if your script *encrypts* (reversible), keep its
secret key in an environment variable on the Space, never in this file — this
repo may be public. Hashing (what the default does) needs no secret at all.
"""
import hashlib
import hmac
import os

_ITERATIONS = 300_000


def protect_password(plain: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, _ITERATIONS)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt_hex), _ITERATIONS)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False
