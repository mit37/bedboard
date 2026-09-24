"""Dev/admin convenience: manage a shelter's per-bucket SMS population-map
override (see app/sms_population_map.py for what this is and why it's
needed) without a real admin UI yet -- same pattern as
scripts/seed_dev_coordinator.py.

Every shelter starts with a default bucket -> FABT population_type map
(baked into mock seed data, or app.fabt_client.DEFAULT_SMS_POPULATION_MAP
for a real FABT deployment). Use this script when a specific shelter needs
something different -- e.g. a veteran-only shelter shouldn't accept SMS
bed counts into a generic SINGLE_ADULT bucket, or a shelter simply doesn't
serve one of the three buckets at all.

Usage (uses the same BEDBOARD_DATABASE_URL the running app uses, default
./bedboard.db):

    # Point this shelter's "men" SMS bucket at FABT's VETERAN population type
    # instead of whatever the default map would use:
    python scripts/set_shelter_sms_population_map.py set shelter-123 men VETERAN

    # This shelter doesn't serve families at all -- SMS updates to that
    # bucket should be silently ignored rather than saved anywhere,
    # even if the shelter's own default map would otherwise accept it:
    python scripts/set_shelter_sms_population_map.py disable shelter-123 family

    # Remove the override entirely -- back to whatever the default map says:
    python scripts/set_shelter_sms_population_map.py clear shelter-123 family

    # See what's currently overridden for a shelter:
    python scripts/set_shelter_sms_population_map.py list shelter-123
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import ShelterSmsPopulationMapOverrideModel  # noqa: E402
from app.db.session import get_sessionmaker, init_db  # noqa: E402

BUCKETS = ["women", "men", "family"]


async def _get_row(session, shelter_id: str, bucket: str) -> ShelterSmsPopulationMapOverrideModel | None:
    return (
        await session.execute(
            select(ShelterSmsPopulationMapOverrideModel).where(
                ShelterSmsPopulationMapOverrideModel.shelter_id == shelter_id,
                ShelterSmsPopulationMapOverrideModel.sms_population_type == bucket,
            )
        )
    ).scalar_one_or_none()


async def cmd_set(session, shelter_id: str, bucket: str, fabt_population_type: str) -> None:
    row = await _get_row(session, shelter_id, bucket)
    if row is None:
        session.add(
            ShelterSmsPopulationMapOverrideModel(
                shelter_id=shelter_id, sms_population_type=bucket, fabt_population_type=fabt_population_type
            )
        )
    else:
        row.fabt_population_type = fabt_population_type
    await session.commit()
    print(f"{shelter_id}: SMS bucket {bucket!r} now maps to FABT population type {fabt_population_type!r}.")


async def cmd_disable(session, shelter_id: str, bucket: str) -> None:
    row = await _get_row(session, shelter_id, bucket)
    if row is None:
        session.add(
            ShelterSmsPopulationMapOverrideModel(
                shelter_id=shelter_id, sms_population_type=bucket, fabt_population_type=None
            )
        )
    else:
        row.fabt_population_type = None
    await session.commit()
    print(f"{shelter_id}: SMS bucket {bucket!r} disabled -- SMS updates to it will be ignored.")


async def cmd_clear(session, shelter_id: str, bucket: str) -> None:
    row = await _get_row(session, shelter_id, bucket)
    if row is None:
        print(f"{shelter_id}: no override for bucket {bucket!r} -- nothing to clear.")
        return
    await session.delete(row)
    await session.commit()
    print(f"{shelter_id}: override for bucket {bucket!r} removed -- back to the default map.")


async def cmd_list(session, shelter_id: str) -> None:
    rows = (
        await session.execute(
            select(ShelterSmsPopulationMapOverrideModel).where(
                ShelterSmsPopulationMapOverrideModel.shelter_id == shelter_id
            )
        )
    ).scalars().all()
    if not rows:
        print(f"{shelter_id}: no overrides -- using the default map for every bucket.")
        return
    for row in rows:
        if row.fabt_population_type is None:
            print(f"  {row.sms_population_type}: DISABLED")
        else:
            print(f"  {row.sms_population_type}: -> {row.fabt_population_type}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Override a bucket to a specific FABT population type")
    p_set.add_argument("shelter_id")
    p_set.add_argument("bucket", choices=BUCKETS)
    p_set.add_argument("fabt_population_type", help='e.g. "VETERAN", "WOMEN_ONLY"')

    p_disable = sub.add_parser("disable", help="Disable a bucket for this shelter entirely")
    p_disable.add_argument("shelter_id")
    p_disable.add_argument("bucket", choices=BUCKETS)

    p_clear = sub.add_parser("clear", help="Remove the override, falling back to the default map")
    p_clear.add_argument("shelter_id")
    p_clear.add_argument("bucket", choices=BUCKETS)

    p_list = sub.add_parser("list", help="Show current overrides for a shelter")
    p_list.add_argument("shelter_id")

    args = parser.parse_args()

    await init_db()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if args.command == "set":
            await cmd_set(session, args.shelter_id, args.bucket, args.fabt_population_type)
        elif args.command == "disable":
            await cmd_disable(session, args.shelter_id, args.bucket)
        elif args.command == "clear":
            await cmd_clear(session, args.shelter_id, args.bucket)
        elif args.command == "list":
            await cmd_list(session, args.shelter_id)


if __name__ == "__main__":
    asyncio.run(main())
