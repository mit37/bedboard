"""Tests app.sms_population_map.resolve_sms_population_map in isolation,
independent of any FabtClient or the webhook."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, ShelterSmsPopulationMapOverrideModel
from app.schemas import ShelterSummary, SmsPopulationType
from app.sms_population_map import resolve_sms_population_map

SHELTER_ID = "shelter-1"


def _shelter(sms_population_map: dict) -> ShelterSummary:
    return ShelterSummary(
        id=SHELTER_ID,
        tenant_id="dev-coc",
        name="Test Shelter",
        is_dv=False,
        pets_ok=False,
        ada=False,
        sms_population_map=sms_population_map,
    )


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_override_falls_back_to_shelter_default(session):
    shelter = _shelter({SmsPopulationType.WOMEN: "WOMEN_ONLY", SmsPopulationType.MEN: "SINGLE_ADULT"})
    effective = await resolve_sms_population_map(session, shelter)
    assert effective == {SmsPopulationType.WOMEN: "WOMEN_ONLY", SmsPopulationType.MEN: "SINGLE_ADULT"}


@pytest.mark.asyncio
async def test_override_replaces_bucket(session):
    shelter = _shelter({SmsPopulationType.MEN: "SINGLE_ADULT"})
    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id=SHELTER_ID, sms_population_type="men", fabt_population_type="VETERAN"
        )
    )
    await session.commit()

    effective = await resolve_sms_population_map(session, shelter)
    assert effective[SmsPopulationType.MEN] == "VETERAN"


@pytest.mark.asyncio
async def test_override_adds_bucket_not_in_default_map(session):
    # Shelter's own default map doesn't serve "family" at all (e.g. Gateway).
    shelter = _shelter({SmsPopulationType.WOMEN: "WOMEN_ONLY"})
    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id=SHELTER_ID, sms_population_type="family", fabt_population_type="FAMILY_WITH_CHILDREN"
        )
    )
    await session.commit()

    effective = await resolve_sms_population_map(session, shelter)
    assert effective[SmsPopulationType.FAMILY] == "FAMILY_WITH_CHILDREN"


@pytest.mark.asyncio
async def test_null_override_disables_bucket_even_if_default_map_has_it(session):
    shelter = _shelter({SmsPopulationType.FAMILY: "FAMILY_WITH_CHILDREN"})
    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id=SHELTER_ID, sms_population_type="family", fabt_population_type=None
        )
    )
    await session.commit()

    effective = await resolve_sms_population_map(session, shelter)
    assert SmsPopulationType.FAMILY not in effective


@pytest.mark.asyncio
async def test_overrides_for_other_shelters_are_ignored(session):
    shelter = _shelter({SmsPopulationType.MEN: "SINGLE_ADULT"})
    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id="a-different-shelter", sms_population_type="men", fabt_population_type="VETERAN"
        )
    )
    await session.commit()

    effective = await resolve_sms_population_map(session, shelter)
    assert effective[SmsPopulationType.MEN] == "SINGLE_ADULT"


@pytest.mark.asyncio
async def test_unrecognized_bucket_value_is_skipped_defensively(session):
    shelter = _shelter({SmsPopulationType.WOMEN: "WOMEN_ONLY"})
    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id=SHELTER_ID, sms_population_type="not-a-real-bucket", fabt_population_type="SOMETHING"
        )
    )
    await session.commit()

    effective = await resolve_sms_population_map(session, shelter)
    assert effective == {SmsPopulationType.WOMEN: "WOMEN_ONLY"}
