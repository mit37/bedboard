"""BB-6 stale-count nudge job: "If a site is STALE at 5pm or 9pm, text the
on-shift coordinator; escalate to the site lead after 30 min [if still
unresolved]."

The PRD is silent or ambiguous on a few exact behaviors. Assumptions made
here (each is a real product decision, not a TODO):

1. "On-shift coordinator" -- there is no real shift-schedule data in this
   slice to pick a single on-shift person from CoordinatorPhoneModel.shift.
   We nudge ALL active coordinators for the shelter on level 1. Picking a
   true on-shift coordinator is a follow-up once real shift data exists.
2. run_stale_check's one-line signature in the spec omits send_sms, but the
   surrounding description requires an injectable send function, so it is
   accepted as an explicit parameter here (matching run_escalation_check).
3. A shelter with zero PopulationCount rows has nothing to evaluate, so it
   is treated as "not stale" by this job rather than as an error condition
   -- a shelter with literally no counts ever reported is a data-onboarding
   problem, not something a bed-count-staleness nudge should page for.
4. "Don't spam a duplicate nudge every run if one is already pending"
   applies to both writing a second NudgeModel row *and* re-texting
   coordinators -- while a level-1 nudge is unresolved for a shelter, we
   neither create another row nor send another SMS for it.
5. Symmetrically, run_escalation_check will not create a second level-2
   record (or re-page the lead) for a shelter that already has an
   unresolved level-2 nudge. The escalation check is expected to run on a
   short interval (see start_scheduler) so it can catch the 30-minute mark
   promptly, which means the same still-unresolved level-1 nudge would
   otherwise be re-escalated on every poll until someone clears it.
6. Per the spec, escalating to level 2 does NOT resolve the level-1 nudge.
   It only gets resolved when the shelter is found fresh again (either by
   a later run_escalation_check call, or by resolve_nudges_for_shelter
   being called from the integration step that ingests a fresh count).
7. If a shelter has no active "lead"-shift coordinator, the level-2 record
   is still created (so the escalation is on record and won't be
   re-attempted every poll) but no SMS is sent, per the spec.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import CoordinatorPhoneModel, NudgeModel
from app.fabt_client import FabtClient
from app.freshness import compute_freshness
from app.nudge.messages import render_escalation, render_nudge
from app.schemas import Freshness, Locale, NudgeLevel, NudgeRecord, PopulationCount

# (to_phone_e164, body) -> None. A real Twilio call is wired in by a later
# integration step; here we just await whatever coroutine function is passed.
SendSms = Callable[[str, str], Awaitable[None]]


def _locale_from_str(value: str | None) -> Locale:
    """Best-effort coercion of a stored locale string to the Locale enum."""
    if value is not None:
        try:
            return Locale(value)
        except ValueError:
            pass
    return Locale.EN


def _any_stale(counts: list[PopulationCount], now: datetime, settings) -> bool:
    return any(
        compute_freshness(count.recorded_at, now, settings) == Freshness.STALE
        for count in counts
    )


def _elapsed(now: datetime, sent_at: datetime) -> timedelta:
    """now - sent_at, tolerant of a naive `sent_at`.

    SQLite has no real timezone-aware storage type, so SQLAlchemy's sqlite
    dialect round-trips `DateTime(timezone=True)` values (the type used by
    the frozen NudgeModel.sent_at column) through a plain ISO string and
    can hand back a naive datetime on read even though we always write an
    aware one. Comparing wall-clock values by stripping tzinfo from
    whichever side has it is correct as long as `now` is supplied in a
    timezone consistent with whatever produced earlier `now` values -- true
    both for the real scheduler (always settings.tzinfo) and for tests
    (one fixed tz throughout).
    """
    if (now.tzinfo is None) != (sent_at.tzinfo is None):
        now = now.replace(tzinfo=None)
        sent_at = sent_at.replace(tzinfo=None)
    return now - sent_at


async def _active_coordinators(
    session: AsyncSession, shelter_id: str, shift: str | None = None
) -> list[CoordinatorPhoneModel]:
    stmt = select(CoordinatorPhoneModel).where(
        CoordinatorPhoneModel.shelter_id == shelter_id,
        CoordinatorPhoneModel.active.is_(True),
    )
    if shift is not None:
        stmt = stmt.where(CoordinatorPhoneModel.shift == shift)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _has_unresolved_nudge(
    session: AsyncSession, shelter_id: str, level: NudgeLevel
) -> bool:
    result = await session.execute(
        select(NudgeModel.id).where(
            NudgeModel.shelter_id == shelter_id,
            NudgeModel.level == level.value,
            NudgeModel.resolved_at.is_(None),
        )
    )
    return result.first() is not None


async def run_stale_check(
    now: datetime,
    fabt: FabtClient,
    session: AsyncSession,
    send_sms: SendSms,
) -> list[NudgeRecord]:
    """Text every active coordinator of a newly-STALE, non-DV shelter.

    Skips shelters that already have an unresolved level-1 nudge, so a
    coordinator who hasn't responded yet doesn't get texted again every
    time this job runs. Returns the NudgeRecord(s) created this run.
    """
    settings = get_settings()
    created: list[NudgeRecord] = []

    shelters = await fabt.list_shelters()
    for shelter in shelters:
        if shelter.is_dv:
            continue

        counts = await fabt.get_latest_counts(shelter.id)
        if not _any_stale(counts, now, settings):
            continue

        if await _has_unresolved_nudge(session, shelter.id, NudgeLevel.COORDINATOR):
            continue

        coordinators = await _active_coordinators(session, shelter.id)

        session.add(
            NudgeModel(
                shelter_id=shelter.id,
                level=NudgeLevel.COORDINATOR.value,
                sent_at=now,
                resolved_at=None,
            )
        )

        for coordinator in coordinators:
            body = render_nudge(shelter.name, _locale_from_str(coordinator.locale))
            await send_sms(coordinator.phone_e164, body)

        created.append(
            NudgeRecord(
                shelter_id=shelter.id,
                level=NudgeLevel.COORDINATOR,
                sent_at=now,
                resolved_at=None,
            )
        )

    await session.commit()
    return created


async def run_escalation_check(
    now: datetime,
    fabt: FabtClient,
    session: AsyncSession,
    send_sms: SendSms,
) -> list[NudgeRecord]:
    """Escalate unresolved level-1 nudges that have aged past the
    escalation window to level 2, paging the shelter's site lead(s).

    A level-1 nudge whose shelter has since gone fresh again is resolved
    in place instead of being escalated. Returns the level-2 NudgeRecord(s)
    created this run.
    """
    settings = get_settings()
    escalation_window = timedelta(minutes=settings.nudge_escalation_minutes)
    created: list[NudgeRecord] = []

    pending = await session.execute(
        select(NudgeModel).where(
            NudgeModel.level == NudgeLevel.COORDINATOR.value,
            NudgeModel.resolved_at.is_(None),
        )
    )
    for nudge in pending.scalars().all():
        if _elapsed(now, nudge.sent_at) < escalation_window:
            continue

        counts = await fabt.get_latest_counts(nudge.shelter_id)
        if not _any_stale(counts, now, settings):
            nudge.resolved_at = now
            continue

        if await _has_unresolved_nudge(session, nudge.shelter_id, NudgeLevel.LEAD):
            continue

        shelter = await fabt.get_shelter(nudge.shelter_id)
        shelter_name = shelter.name if shelter is not None else nudge.shelter_id

        leads = await _active_coordinators(session, nudge.shelter_id, shift="lead")

        session.add(
            NudgeModel(
                shelter_id=nudge.shelter_id,
                level=NudgeLevel.LEAD.value,
                sent_at=now,
                resolved_at=None,
            )
        )

        for lead in leads:
            body = render_escalation(shelter_name, _locale_from_str(lead.locale))
            await send_sms(lead.phone_e164, body)

        created.append(
            NudgeRecord(
                shelter_id=nudge.shelter_id,
                level=NudgeLevel.LEAD,
                sent_at=now,
                resolved_at=None,
            )
        )

    await session.commit()
    return created


async def resolve_nudges_for_shelter(
    session: AsyncSession, shelter_id: str, now: datetime
) -> None:
    """Clear every unresolved nudge for a shelter.

    Called by a later integration step whenever a fresh count comes in for
    the shelter, so pending nudges (level 1 or 2) clear themselves.
    """
    result = await session.execute(
        select(NudgeModel).where(
            NudgeModel.shelter_id == shelter_id,
            NudgeModel.resolved_at.is_(None),
        )
    )
    for nudge in result.scalars().all():
        nudge.resolved_at = now
    await session.commit()


def start_scheduler(
    fabt: FabtClient,
    session_factory: async_sessionmaker[AsyncSession],
    send_sms: SendSms,
):
    """Wire run_stale_check / run_escalation_check to a real clock.

    Not unit-tested (per the task's scope note) -- the interesting logic
    above takes an explicit `now` and is tested directly. This wrapper
    just supplies real time and a fresh session per invocation.

    The stale check runs at get_settings().nudge_check_hours (5pm/9pm by
    default) in get_settings().nudge_timezone, per the PRD. The escalation
    check runs on a short fixed interval instead of at those same two
    hours, since a 30-minute escalation deadline falls at an arbitrary
    clock time relative to when a level-1 nudge went out, not at 5pm/9pm.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    settings = get_settings()
    tz = settings.tzinfo
    scheduler = AsyncIOScheduler(timezone=tz)

    async def _stale_check_job() -> None:
        now = datetime.now(tz)
        async with session_factory() as session:
            await run_stale_check(now, fabt, session, send_sms)

    async def _escalation_check_job() -> None:
        now = datetime.now(tz)
        async with session_factory() as session:
            await run_escalation_check(now, fabt, session, send_sms)

    hours = ",".join(str(hour) for hour in settings.nudge_check_hours)
    scheduler.add_job(
        _stale_check_job,
        CronTrigger(hour=hours, minute=0, timezone=tz),
        id="nudge_stale_check",
        replace_existing=True,
    )
    scheduler.add_job(
        _escalation_check_job,
        "interval",
        minutes=5,
        id="nudge_escalation_check",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
