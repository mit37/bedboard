"""Tests for app.admin.router: the coordinator_phone and
shelter_sms_population_map_override CRUD UI. Same isolated-app pattern as
tests/test_webhook.py (standalone FastAPI app + InMemoryFabtClient + a
local sqlite engine, not app.main's cached globals)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.router import router as admin_router
from app.crypto import decrypt_phone
from app.db.models import Base, CoordinatorPhoneModel, ShelterSmsPopulationMapOverrideModel
from app.db.session import get_session
from app.mock_fabt.client import InMemoryFabtClient
from app.mock_fabt.store import InMemoryFabtStore
from app.wallboard.router import get_fabt_client

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def app_and_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    store = InMemoryFabtStore(now=NOW)
    client = InMemoryFabtClient(store)

    app = FastAPI()
    app.include_router(admin_router)

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_fabt_client] = lambda: client

    yield app, sessionmaker
    await engine.dispose()


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- coordinators --------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinators_page_lists_none_initially(app_and_sessionmaker):
    app, _ = app_and_sessionmaker
    async with await _client(app) as client:
        resp = await client.get("/admin/coordinators")
    assert resp.status_code == 200
    assert "No coordinators registered yet" in resp.text
    # Shelter dropdown is populated from the real (mock) FABT shelters.
    assert "First Street Shelter" in resp.text


@pytest.mark.asyncio
async def test_register_coordinator_stores_hash_and_encrypted_not_plaintext(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with await _client(app) as client:
        resp = await client.post(
            "/admin/coordinators",
            data={
                "shelter_id": "shelter-first-street",
                "user_id": "jane",
                "phone_e164": "+14155551234",
                "shift": "day",
                "locale": "en",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "+14155551234" not in resp.headers["location"]  # never the raw number in a URL

    async with sessionmaker() as session:
        rows = (await session.execute(select(CoordinatorPhoneModel))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == "jane"
        assert row.shelter_id == "shelter-first-street"
        assert row.active is True
        # Not stored anywhere in plaintext; only recoverable via decrypt.
        assert b"+14155551234" not in row.phone_encrypted
        assert decrypt_phone(row.phone_encrypted) == "+14155551234"


@pytest.mark.asyncio
async def test_register_coordinator_flash_message_preserves_leading_plus(app_and_sessionmaker):
    # Regression: a hand-built "?flash=...+..." query string treats a
    # literal "+" (the start of every E.164 number) as a space when
    # decoded back out -- _redirect()/urlencode() must round-trip it.
    app, _ = app_and_sessionmaker
    async with await _client(app) as client:
        redirect = await client.post(
            "/admin/coordinators",
            data={
                "shelter_id": "shelter-first-street",
                "user_id": "jane",
                "phone_e164": "+14155551234",
                "shift": "day",
                "locale": "en",
            },
            follow_redirects=False,
        )
        page = await client.get(redirect.headers["location"])
    assert "Registered +1" in page.text


@pytest.mark.asyncio
async def test_duplicate_phone_number_is_rejected_not_duplicated(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    payload = {
        "shelter_id": "shelter-first-street",
        "user_id": "jane",
        "phone_e164": "+14155551234",
        "shift": "day",
        "locale": "en",
    }
    async with await _client(app) as client:
        await client.post("/admin/coordinators", data=payload, follow_redirects=False)
        resp = await client.post("/admin/coordinators", data={**payload, "user_id": "someone-else"}, follow_redirects=False)

    assert "error=1" in resp.headers["location"]
    async with sessionmaker() as session:
        rows = (await session.execute(select(CoordinatorPhoneModel))).scalars().all()
        assert len(rows) == 1  # not duplicated


@pytest.mark.asyncio
async def test_toggle_active_flips_status(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with sessionmaker() as session:
        from app.crypto import encrypt_phone, hash_phone

        session.add(
            CoordinatorPhoneModel(
                user_id="jane",
                shelter_id="shelter-first-street",
                phone_hash=hash_phone("+14155551234"),
                phone_encrypted=encrypt_phone("+14155551234"),
                shift="day",
                active=True,
                locale="en",
            )
        )
        await session.commit()
        coordinator_id = (await session.execute(select(CoordinatorPhoneModel))).scalar_one().id

    async with await _client(app) as client:
        await client.post(f"/admin/coordinators/{coordinator_id}/toggle-active", follow_redirects=False)
    async with sessionmaker() as session:
        row = await session.get(CoordinatorPhoneModel, coordinator_id)
        assert row.active is False

    async with await _client(app) as client:
        await client.post(f"/admin/coordinators/{coordinator_id}/toggle-active", follow_redirects=False)
    async with sessionmaker() as session:
        row = await session.get(CoordinatorPhoneModel, coordinator_id)
        assert row.active is True


@pytest.mark.asyncio
async def test_delete_coordinator_removes_row(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with sessionmaker() as session:
        from app.crypto import encrypt_phone, hash_phone

        session.add(
            CoordinatorPhoneModel(
                user_id="jane",
                shelter_id="shelter-first-street",
                phone_hash=hash_phone("+14155551234"),
                phone_encrypted=encrypt_phone("+14155551234"),
                shift="day",
                active=True,
                locale="en",
            )
        )
        await session.commit()
        coordinator_id = (await session.execute(select(CoordinatorPhoneModel))).scalar_one().id

    async with await _client(app) as client:
        await client.post(f"/admin/coordinators/{coordinator_id}/delete", follow_redirects=False)

    async with sessionmaker() as session:
        rows = (await session.execute(select(CoordinatorPhoneModel))).scalars().all()
        assert rows == []


# --- population-map overrides --------------------------------------------


@pytest.mark.asyncio
async def test_population_map_page_shows_default_map_and_shelters(app_and_sessionmaker):
    app, _ = app_and_sessionmaker
    async with await _client(app) as client:
        resp = await client.get("/admin/population-map")
    assert resp.status_code == 200
    assert "WOMEN_ONLY" in resp.text
    assert "First Street Shelter" in resp.text
    assert "No overrides set" in resp.text


@pytest.mark.asyncio
async def test_create_override_redirects_a_bucket(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with await _client(app) as client:
        resp = await client.post(
            "/admin/population-map",
            data={"shelter_id": "shelter-gateway", "sms_population_type": "men", "fabt_population_type": "VETERAN"},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with sessionmaker() as session:
        rows = (await session.execute(select(ShelterSmsPopulationMapOverrideModel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].shelter_id == "shelter-gateway"
        assert rows[0].sms_population_type == "men"
        assert rows[0].fabt_population_type == "VETERAN"


@pytest.mark.asyncio
async def test_blank_fabt_population_type_disables_the_bucket(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with await _client(app) as client:
        await client.post(
            "/admin/population-map",
            data={"shelter_id": "shelter-gateway", "sms_population_type": "family", "fabt_population_type": ""},
            follow_redirects=False,
        )
    async with sessionmaker() as session:
        row = (await session.execute(select(ShelterSmsPopulationMapOverrideModel))).scalar_one()
        assert row.fabt_population_type is None


@pytest.mark.asyncio
async def test_resubmitting_same_bucket_updates_in_place_not_duplicates(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with await _client(app) as client:
        await client.post(
            "/admin/population-map",
            data={"shelter_id": "shelter-gateway", "sms_population_type": "men", "fabt_population_type": "VETERAN"},
            follow_redirects=False,
        )
        await client.post(
            "/admin/population-map",
            data={"shelter_id": "shelter-gateway", "sms_population_type": "men", "fabt_population_type": "YOUTH_18_24"},
            follow_redirects=False,
        )
    async with sessionmaker() as session:
        rows = (await session.execute(select(ShelterSmsPopulationMapOverrideModel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].fabt_population_type == "YOUTH_18_24"


@pytest.mark.asyncio
async def test_delete_override_removes_row(app_and_sessionmaker):
    app, sessionmaker = app_and_sessionmaker
    async with sessionmaker() as session:
        session.add(
            ShelterSmsPopulationMapOverrideModel(
                shelter_id="shelter-gateway", sms_population_type="men", fabt_population_type="VETERAN"
            )
        )
        await session.commit()
        override_id = (await session.execute(select(ShelterSmsPopulationMapOverrideModel))).scalar_one().id

    async with await _client(app) as client:
        await client.post(f"/admin/population-map/{override_id}/delete", follow_redirects=False)

    async with sessionmaker() as session:
        rows = (await session.execute(select(ShelterSmsPopulationMapOverrideModel))).scalars().all()
        assert rows == []
