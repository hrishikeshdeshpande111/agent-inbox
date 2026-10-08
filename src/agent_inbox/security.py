"""Secrets, hashing, and webhook signature verification.

Design notes:
- Inbox IDs are unguessable capability tokens (not sequential integers).
- Read/write secrets are shown ONCE at creation and stored only as SHA-256
  hashes. A database leak does not hand out inbox access.
- All secret comparisons are constant-time (hmac.compare_digest).
- Webhook senders that sign payloads (Stripe, GitHub, ...) can be verified:
  pass the raw body and the signature header; we check HMAC-SHA256 against
  the inbox's write secret.
"""

import hashlib
import hmac
import secrets


def new_id() -> str:
    """Unguessable public inbox/message identifier."""
    return secrets.token_urlsafe(12)


def new_secret() -> str:
    """High-entropy secret, shown once at creation/rotation."""
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secrets_match(provided: str, stored_hash: str) -> bool:
    """Constant-time comparison of a provided secret against a stored hash."""
    if not provided or not stored_hash:
        return False
    return hmac.compare_digest(hash_secret(provided), stored_hash)


def verify_hmac_sha256(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify a `sha256=<hex>` HMAC signature over the raw request body."""
    try:
        algo, _, hexsig = signature_header.partition("=")
        if algo.strip().lower() != "sha256" or not hexsig.strip():
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, hexsig.strip())
    except Exception:
        return False
