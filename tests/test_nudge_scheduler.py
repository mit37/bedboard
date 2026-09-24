"""Tests for app.nudge.scheduler (BB-6).

Uses an in-memory SQLite AsyncSession built from app.db.models.Base.metadata
(a local engine/sessionmaker, not app.db.session's cached global engine) and
a small local fake of the FabtClient ABC, so this module has no dependency
on app.mock_fabt or any other module being built concurrently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base, CoordinatorPhoneModel, NudgeModel
from app.fabt_client import FabtClient
from app.freshness import compute_freshness
from app.nudge.scheduler import (
    resolve_nudges_for_shelter,
    run_escalation_check,
    run_stale_check,
)
from app.schemas import (
    NudgeLevel,
    PopulationCount,
    Reservation,
    ShelterSummary,
    WallboardSnapshot,
)

NOW = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)


# --- local fake FabtClient (implements the ABC directly, per ground rules) ---


class FakeFabtClient(FabtClient):
    def __init__(self, shelters: list[ShelterSummary]):
        self._shelters = {s.id: s for s in shelters}
        self._counts: dict[str, list[PopulationCount]] = {}

    def set_counts(self, shelter_id: str, counts: list[PopulationCount]) -> None:
        self._counts[shelter_id] = counts

    async def list_shelters(self) -> list[ShelterSummary]:
        return list(self._shelters.values())

    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        return self._shelters.get(shelter_id)

    async def get_latest_counts(self, shelter_id: str) -> list[PopulationCount]:
        return self._counts.get(shelter_id, [])

    async def post_snapshot(
        self, shelter_id, population_type, beds_available, recorded_by, recorded_at=None
    ) -> PopulationCount:
        raise NotImplementedError("not exercised by nudge scheduler tests")

    async def get_wallboard(self, tenant_id: str) -> WallboardSnapshot:
        raise NotImplementedError("not exercised by nudge scheduler tests")

    async def count_active_holds(self, shelter_id: str) -> int:
        return 0

    async def create_reservation(self, shelter_id, population_type, held_by) -> Reservation:
        raise NotImplementedError("not exercised by nudge scheduler tests")


class RecordingSendSms:
    """Records (to_phone_e164, body) for every call, in call order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, to_phone_e164: str, body: str) -> None:
        self.calls.append((to_phone_e164, body))


def _dt_eq(a: datetime, b: datetime) -> bool:
    """Datetime equality tolerant of SQLite's DateTime(timezone=True)
    columns coming back naive after a DB round trip even though we always
    write aware datetimes in -- see app/nudge/scheduler.py's _elapsed
    docstring for why the frozen NudgeModel columns behave this way on
    SQLite. Both sides are compared as wall-clock values.
    """
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=None)
        b = b.replace(tzinfo=None)
    return a == b


def make_shelter(shelter_id: str = "shelter-1", name: str = "Riverside Shelter", is_dv: bool = False) -> ShelterSummary:
    return ShelterSummary(id=shelter_id, tenant_id="tenant-1", name=name, is_dv=is_dv)


def make_count(
    recorded_at: datetime, now: datetime, population_type: str = "women_only", beds: int = 3
) -> PopulationCount:
    """Builds a PopulationCount with `.freshness` actually computed from
    `recorded_at`/`now` (via the same compute_freshness real FabtClient
    implementations use), rather than a hardcoded placeholder -- the
    scheduler trusts `.freshness` directly (see app/nudge/scheduler.py's
    _any_stale), so a fake client that doesn't compute it correctly would
    silently test nothing.
    """
    return PopulationCount(
        population_type=population_type,
        beds_available=beds,
        recorded_at=recorded_at,
        recorded_by="staff-1",
        freshness=compute_freshness(recorded_at, now, get_settings()),
    )


async def _add_coordinator(
    session: AsyncSession,
    shelter_id: str,
    phone: str,
    shift: str | None = None,
    active: bool = True,
    locale: str = "en",
) -> None:
    session.add(
        CoordinatorPhoneModel(
            user_id=f"user-{phone}",
            shelter_id=shelter_id,
            phone_e164=phone,
            shift=shift,
            active=active,
            locale=locale,
        )
    )
    await session.commit()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


# --- (1) recent count -> no nudge ---


@pytest.mark.asyncio
async def test_fresh_count_produces_no_nudge(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=1), now=NOW)])
    send_sms = RecordingSendSms()

    created = await run_stale_check(NOW, fabt, session, send_sms)

    assert created == []
    assert send_sms.calls == []
    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert rows == []


# --- (2) >8h-old count -> exactly one level-1 nudge, one SMS per active coordinator ---


@pytest.mark.asyncio
async def test_stale_count_creates_one_nudge_and_texts_active_coordinators(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001", shift="day")
    await _add_coordinator(session, "shelter-1", "+15551110002", shift="evening")
    await _add_coordinator(session, "shelter-1", "+15551110003", shift="day", active=False)
    send_sms = RecordingSendSms()

    created = await run_stale_check(NOW, fabt, session, send_sms)

    assert len(created) == 1
    assert created[0].shelter_id == "shelter-1"
    assert created[0].level == NudgeLevel.COORDINATOR

    texted = {phone for phone, _ in send_sms.calls}
    assert texted == {"+15551110001", "+15551110002"}  # inactive coordinator skipped
    assert len(send_sms.calls) == 2

    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].level == NudgeLevel.COORDINATOR.value
    assert rows[0].resolved_at is None


# --- (3) running run_stale_check again immediately does not duplicate ---


@pytest.mark.asyncio
async def test_repeated_stale_check_does_not_duplicate_pending_nudge(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001")
    send_sms = RecordingSendSms()

    first = await run_stale_check(NOW, fabt, session, send_sms)
    second = await run_stale_check(NOW, fabt, session, send_sms)

    assert len(first) == 1
    assert second == []
    assert len(send_sms.calls) == 1  # not re-texted on the second run

    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert len(rows) == 1


# --- (4) escalation before 30 minutes does nothing ---


@pytest.mark.asyncio
async def test_escalation_does_nothing_before_window(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001")
    send_sms = RecordingSendSms()
    await run_stale_check(NOW, fabt, session, send_sms)

    escalated = await run_escalation_check(NOW + timedelta(minutes=10), fabt, session, send_sms)

    assert escalated == []
    assert len(send_sms.calls) == 1  # only the original level-1 text
    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].resolved_at is None


# --- (5) after 30+ minutes, still stale -> level-2 nudge, only "lead" shift paged ---


@pytest.mark.asyncio
async def test_escalation_after_window_pages_only_leads(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001", shift="day")
    await _add_coordinator(session, "shelter-1", "+15551119999", shift="lead")
    send_sms = RecordingSendSms()
    await run_stale_check(NOW, fabt, session, send_sms)

    later = NOW + timedelta(minutes=31)
    escalated = await run_escalation_check(later, fabt, session, send_sms)

    assert len(escalated) == 1
    assert escalated[0].shelter_id == "shelter-1"
    assert escalated[0].level == NudgeLevel.LEAD

    phones_texted = [phone for phone, _ in send_sms.calls]
    # The lead is also an active coordinator, so it gets the level-1 text
    # like everyone else, plus the level-2 escalation text -- two total.
    assert phones_texted.count("+15551119999") == 2
    assert phones_texted.count("+15551110001") == 1  # day coordinator only texted once (level-1)

    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert sorted(r.level for r in rows) == [1, 2]


# --- (6) shelter became fresh before the 30-minute mark -> resolve instead of escalate ---


@pytest.mark.asyncio
async def test_escalation_resolves_nudge_if_shelter_became_fresh(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001")
    send_sms = RecordingSendSms()
    await run_stale_check(NOW, fabt, session, send_sms)

    later = NOW + timedelta(minutes=31)
    # A fresh count came in during the escalation window (e.g. the coordinator
    # texted an update) but resolve_nudges_for_shelter hasn't run yet.
    fabt.set_counts("shelter-1", [make_count(later - timedelta(minutes=5), now=later)])

    escalated = await run_escalation_check(later, fabt, session, send_sms)

    assert escalated == []
    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].level == NudgeLevel.COORDINATOR.value
    assert _dt_eq(rows[0].resolved_at, later)


# --- (7) resolve_nudges_for_shelter clears unresolved nudges ---


@pytest.mark.asyncio
async def test_resolve_nudges_for_shelter_clears_unresolved_rows(session: AsyncSession) -> None:
    fabt = FakeFabtClient([make_shelter()])
    fabt.set_counts("shelter-1", [make_count(NOW - timedelta(hours=9), now=NOW)])
    await _add_coordinator(session, "shelter-1", "+15551110001", shift="day")
    await _add_coordinator(session, "shelter-1", "+15551119999", shift="lead")
    send_sms = RecordingSendSms()
    await run_stale_check(NOW, fabt, session, send_sms)
    later = NOW + timedelta(minutes=31)
    await run_escalation_check(later, fabt, session, send_sms)  # still stale -> level-2 created

    resolve_at = later + timedelta(minutes=5)
    await resolve_nudges_for_shelter(session, "shelter-1", resolve_at)

    rows = (await session.execute(select(NudgeModel))).scalars().all()
    assert len(rows) == 2
    assert all(_dt_eq(row.resolved_at, resolve_at) for row in rows)
