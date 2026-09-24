"""Tests for app.crypto: HMAC phone hashing + AES-256-GCM phone encryption."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from app.crypto import decrypt_phone, encrypt_phone, hash_phone

PHONE = "+14155551234"
OTHER_PHONE = "+16505559999"


def test_hash_phone_is_deterministic():
    assert hash_phone(PHONE) == hash_phone(PHONE)


def test_hash_phone_differs_for_different_numbers():
    assert hash_phone(PHONE) != hash_phone(OTHER_PHONE)


def test_hash_phone_is_not_the_plaintext():
    digest = hash_phone(PHONE)
    assert PHONE.encode("utf-8") not in digest
    assert len(digest) == 32  # SHA-256 digest size


def test_encrypt_decrypt_round_trips():
    blob = encrypt_phone(PHONE)
    assert decrypt_phone(blob) == PHONE


def test_encrypt_phone_is_not_the_plaintext():
    blob = encrypt_phone(PHONE)
    assert PHONE.encode("utf-8") not in blob


def test_encrypt_phone_is_nondeterministic_random_nonce_per_call():
    # Same input, two calls -> different ciphertext (random nonce), but
    # both still decrypt back to the same plaintext. This is exactly what
    # makes phone_encrypted unusable as a lookup key (unlike phone_hash).
    blob_a = encrypt_phone(PHONE)
    blob_b = encrypt_phone(PHONE)
    assert blob_a != blob_b
    assert decrypt_phone(blob_a) == PHONE
    assert decrypt_phone(blob_b) == PHONE


def test_decrypt_rejects_tampered_ciphertext():
    blob = bytearray(encrypt_phone(PHONE))
    blob[-1] ^= 0xFF  # flip a bit in the auth tag
    with pytest.raises(InvalidTag):
        decrypt_phone(bytes(blob))


def test_hash_and_encrypt_keys_are_independently_derived():
    # If the HMAC and AES keys were accidentally the same raw key (no HKDF
    # info-label separation), this wouldn't itself be detectable from the
    # outputs directly, but a basic sanity check: hashing and encrypting
    # the same input must not produce the same bytes as each other (that
    # would indicate the "encryption" is just leaking the hash, or vice
    # versa, e.g. from a broken/no-op key derivation).
    assert hash_phone(PHONE) not in encrypt_phone(PHONE)
