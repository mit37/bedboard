"""Resolves BedBoard's own per-shelter override of the SMS bucket ->
FABT population_type mapping.

FABT has no concept of this mapping. Every FabtClient implementation
supplies its own DEFAULT for a shelter via ShelterSummary.sms_population_map
-- app.mock_fabt bakes per-shelter defaults into its seed data;
app.fabt_client.HttpFabtClient applies one global default
(DEFAULT_SMS_POPULATION_MAP) to every real shelter, since real FABT's
population-type taxonomy genuinely can vary shelter to shelter and there's
no way to infer the right mapping automatically from FABT's API (see
README "Known gaps"). This module lets an admin correct that per shelter,
per bucket -- via app.db.models.ShelterSmsPopulationMapOverrideModel,
managed today by scripts/set_shelter_sms_population_map.py (no admin UI
yet, same as coordinator_phone) -- without either FabtClient
implementation needing to know overrides exist. It's resolved at the one
place that actually needs the effective map: app/sms/webhook.py.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShelterSmsPopulationMapOverrideModel
from app.schemas import ShelterSummary, SmsPopulationType


async def resolve_sms_population_map(
    session: AsyncSession, shelter: ShelterSummary
) -> dict[SmsPopulationType, str]:
    """Effective bucket -> FABT population_type map for one shelter.

    Starts from the shelter's own default map (whatever the FabtClient
    behind it supplied) and applies any BedBoard-side override on top,
    bucket by bucket:
      - no override row for a bucket -> keep the shelter's own default
        (including "not present at all" if the shelter's default map
        doesn't serve that bucket either)
      - override row with a FABT population_type -> use it instead
      - override row with fabt_population_type = NULL -> remove the
        bucket entirely (this shelter explicitly does not accept SMS
        updates for it, even if the default map would have allowed it)
    A row referencing an unrecognized sms_population_type (e.g. a
    left-over value from a schema change) is skipped defensively rather
    than raising -- a corrupt/stale override row should never take down
    the SMS webhook for a shelter's other, valid buckets.
    """
    rows = (
        await session.execute(
            select(ShelterSmsPopulationMapOverrideModel).where(
                ShelterSmsPopulationMapOverrideModel.shelter_id == shelter.id
            )
        )
    ).scalars().all()

    effective = dict(shelter.sms_population_map)
    for row in rows:
        try:
            bucket = SmsPopulationType(row.sms_population_type)
        except ValueError:
            continue
        if row.fabt_population_type is None:
            effective.pop(bucket, None)
        else:
            effective[bucket] = row.fabt_population_type
    return effective


async def get_override_row(
    session: AsyncSession, shelter_id: str, bucket: str
) -> ShelterSmsPopulationMapOverrideModel | None:
    return (
        await session.execute(
            select(ShelterSmsPopulationMapOverrideModel).where(
                ShelterSmsPopulationMapOverrideModel.shelter_id == shelter_id,
                ShelterSmsPopulationMapOverrideModel.sms_population_type == bucket,
            )
        )
    ).scalar_one_or_none()


async def upsert_override(
    session: AsyncSession, shelter_id: str, bucket: str, fabt_population_type: str | None
) -> None:
    """Set the override row for (shelter_id, bucket) to fabt_population_type
    (None = disabled). Shared by scripts/set_shelter_sms_population_map.py
    and app/admin/router.py so both write paths agree on the same
    tolerant-of-a-concurrent-insert upsert behavior.

    The check-then-insert below is not atomic; two concurrent writers for
    the same (shelter_id, bucket) can both see no existing row and both
    try to insert one, tripping the table's UniqueConstraint on the
    second commit. Retry once as an update against the row the other
    writer just created, instead of raising a raw IntegrityError.
    """
    row = await get_override_row(session, shelter_id, bucket)
    if row is not None:
        row.fabt_population_type = fabt_population_type
        await session.commit()
        return

    session.add(
        ShelterSmsPopulationMapOverrideModel(
            shelter_id=shelter_id, sms_population_type=bucket, fabt_population_type=fabt_population_type
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        row = await get_override_row(session, shelter_id, bucket)
        if row is None:
            raise
        row.fabt_population_type = fabt_population_type
        await session.commit()
