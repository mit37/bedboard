"""Tests for app.main's startup safety checks (not the full app/lifespan --
see README for why a full TestClient boot of app.main is deliberately not
exercised in the automated suite: it depends on app.db.session's cached
global engine/settings singletons in ways that fight test isolation)."""

from __future__ import annotations

import pytest

import app.main as main_module
from app.config import DEV_PHONE_ENCRYPTION_KEY, get_settings


def test_allows_dev_encryption_key_in_mock_mode(monkeypatch):
    monkeypatch.setattr(main_module, "USE_MOCK_FABT", True)
    monkeypatch.setattr(get_settings(), "phone_encryption_key", DEV_PHONE_ENCRYPTION_KEY, raising=False)
    main_module._check_phone_encryption_key()  # must not raise


def test_refuses_dev_encryption_key_in_real_mode(monkeypatch):
    monkeypatch.setattr(main_module, "USE_MOCK_FABT", False)
    monkeypatch.setattr(get_settings(), "phone_encryption_key", DEV_PHONE_ENCRYPTION_KEY, raising=False)
    with pytest.raises(RuntimeError, match="BEDBOARD_PHONE_ENCRYPTION_KEY"):
        main_module._check_phone_encryption_key()


def test_allows_a_real_looking_key_in_real_mode(monkeypatch):
    monkeypatch.setattr(main_module, "USE_MOCK_FABT", False)
    monkeypatch.setattr(
        get_settings(), "phone_encryption_key", "a-real-generated-secret-not-the-dev-default", raising=False
    )
    main_module._check_phone_encryption_key()  # must not raise
