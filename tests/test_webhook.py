"""Integration tests for app.sms.webhook: exercises the real parser, real
messages, real InMemoryFabtClient/Store, and real nudge-resolution together
through a standalone FastAPI app (isolated from the app.main / app.db.session
global singletons, same pattern the module-building agents used).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, CoordinatorPhoneModel, NudgeModel, SmsUpdateLogModel
from app.mock_fabt.client import InMemoryFabtClient
from app.mock_fabt.store import InMemoryFabtStore
from app.sms.webhook import router as sms_router
from app.wallboard.router import get_fabt_client

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
SHELTER_ID = "shelter-first-street"


@pytest_asyncio.fixture
async def engine_and_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, sessionmaker
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_session(engine_and_sessionmaker):
    _, sessionmaker = engine_and_sessionmaker
    async with sessionmaker() as session:
        session.add(
            CoordinatorPhoneModel(
                user_id="user-1",
                shelter_id=SHELTER_ID,
                phone_e164="+14155551234",
                shift="day",
                active=True,
                locale="en",
            )
        )
        await session.commit()
    yield sessionmaker


@pytest.fixture
def app_and_store(engine_and_sessionmaker, seeded_session):
    _, sessionmaker = engine_and_sessionmaker
    store = InMemoryFabtStore(now=NOW)
    client = InMemoryFabtClient(store)

    app = FastAPI()
    app.include_router(sms_router)
    app.state.fabt_client = client

    from app.db.session import get_session as real_get_session

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[real_get_session] = _override_get_session
    # sms_router imports get_session directly for its Depends(...) default;
    # override the same symbol object it actually binds to.
    from app.sms import webhook as webhook_module

    app.dependency_overrides[webhook_module.get_session] = _override_get_session
    app.dependency_overrides[get_fabt_client] = lambda: client

    return app, store, sessionmaker


async def _post_sms(app: FastAPI, from_number: str, body: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/sms/inbound", data={"From": from_number, "Body": body})


@pytest.mark.asyncio
async def test_full_bed_count_update_saves_and_confirms(app_and_store):
    app, store, sessionmaker = app_and_store

    resp = await _post_sms(app, "+14155551234", "W 3 M 1 F 0")

    assert resp.status_code == 200
    assert "text/xml" in resp.headers["content-type"] or "application/xml" in resp.headers["content-type"]
    assert "3 women" in resp.text
    assert "1 men" in resp.text
    assert "0 family" in resp.text

    counts = store.get_latest_counts(SHELTER_ID)
    by_type = {c.population_type: c.beds_available for c in counts}
    assert by_type["women_only"] == 3
    assert by_type["single_adult"] == 1
    assert by_type["family"] == 0

    async with sessionmaker() as session:
        log_rows = (await session.execute(select(SmsUpdateLogModel))).scalars().all()
        assert len(log_rows) == 1
        assert log_rows[0].result == "saved"
        assert log_rows[0].shelter_id == SHELTER_ID


@pytest.mark.asyncio
async def test_partial_update_only_touches_mentioned_bucket(app_and_store):
    app, store, _ = app_and_store
    before = {c.population_type: c.beds_available for c in store.get_latest_counts(SHELTER_ID)}

    resp = await _post_sms(app, "+14155551234", "women 7")

    assert "7 women" in resp.text
    after = {c.population_type: c.beds_available for c in store.get_latest_counts(SHELTER_ID)}
    assert after["women_only"] == 7
    # Buckets not mentioned in the SMS are untouched.
    assert after["single_adult"] == before["single_adult"]
    assert after["family"] == before["family"]


@pytest.mark.asyncio
async def test_unregistered_number_is_rejected_and_logged(app_and_store):
    app, store, sessionmaker = app_and_store

    resp = await _post_sms(app, "+19995550000", "W 3 M 1 F 0")

    assert "isn't registered" in resp.text or "not registered" in resp.text.lower()
    async with sessionmaker() as session:
        rows = (await session.execute(select(SmsUpdateLogModel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].result == "rejected"
        assert rows[0].shelter_id is None


@pytest.mark.asyncio
async def test_garbage_input_returns_help_text_and_logs_parse_error(app_and_store):
    app, _, sessionmaker = app_and_store

    resp = await _post_sms(app, "+14155551234", "asdkfjasldkfj")

    assert "BedBoard" in resp.text  # help text mentions the product name
    async with sessionmaker() as session:
        rows = (await session.execute(select(SmsUpdateLogModel))).scalars().all()
        assert rows[-1].result == "parse_error"


@pytest.mark.asyncio
async def test_fresh_update_resolves_pending_nudge(app_and_store):
    app, store, sessionmaker = app_and_store

    async with sessionmaker() as session:
        session.add(
            NudgeModel(
                shelter_id=SHELTER_ID,
                level=1,
                sent_at=NOW - timedelta(hours=1),
                resolved_at=None,
            )
        )
        await session.commit()

    await _post_sms(app, "+14155551234", "men 5")

    async with sessionmaker() as session:
        nudges = (
            await session.execute(select(NudgeModel).where(NudgeModel.shelter_id == SHELTER_ID))
        ).scalars().all()
        assert len(nudges) == 1
        assert nudges[0].resolved_at is not None
