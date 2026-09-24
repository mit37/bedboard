"""Hashing and encryption for coordinator phone numbers at rest.

The PRD's own schema sketch (page 9) pairs an HMAC lookup hash
(`phone_hash`) with a separately encrypted column (`phone_encrypted`)
instead of storing the E.164 number in plain text -- a DB dump alone
shouldn't reveal shelter coordinators' personal phone numbers. Two
different primitives for two different needs:

- `hash_phone`: deterministic HMAC-SHA256, so an inbound SMS's From
  number can be looked up by an indexed equality match
  (`WHERE phone_hash = ...`) without ever decrypting every row to find
  it. Deterministic by design -- the same number always hashes the same
  way, which is exactly what makes it usable as a lookup key (and exactly
  why it must never be used anywhere a random/unique value is needed).
- `encrypt_phone` / `decrypt_phone`: AES-256-GCM (authenticated, random
  nonce per call), so the real number can still be recovered when it's
  actually needed -- BB-6 nudges must know the real number to text a
  coordinator through Twilio.

Both keys are derived via HKDF from one operator-supplied secret
(Settings.phone_encryption_key) rather than managing two independent
secrets: proper key separation between the two primitives without
doubling what an operator has to generate, store, and rotate.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

_HASH_KEY_INFO = b"bedboard-phone-hash-v1"
_ENCRYPT_KEY_INFO = b"bedboard-phone-encrypt-v1"
_NONCE_LEN = 12  # AES-GCM standard nonce length


def _derive_key(info: bytes, length: int = 32) -> bytes:
    master = get_settings().phone_encryption_key.encode("utf-8")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return hkdf.derive(master)


def hash_phone(phone_e164: str) -> bytes:
    """Deterministic HMAC-SHA256 of the E.164 number -- used as the DB
    lookup key (CoordinatorPhoneModel.phone_hash, SmsUpdateLogModel.phone_hash).
    """
    key = _derive_key(_HASH_KEY_INFO)
    return hmac.new(key, phone_e164.encode("utf-8"), hashlib.sha256).digest()


def encrypt_phone(phone_e164: str) -> bytes:
    """AES-256-GCM encrypt the E.164 number. Returns nonce || ciphertext
    (AESGCM.encrypt already appends its 16-byte auth tag to the
    ciphertext) as one blob, so decrypt_phone only needs the blob back.
    """
    key = _derive_key(_ENCRYPT_KEY_INFO)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, phone_e164.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_phone(blob: bytes) -> str:
    """Inverse of encrypt_phone. Raises cryptography.exceptions.InvalidTag
    if `blob` was tampered with or encrypted under a different key."""
    key = _derive_key(_ENCRYPT_KEY_INFO)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
