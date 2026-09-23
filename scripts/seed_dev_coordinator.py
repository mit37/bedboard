"""Dev convenience: register one demo coordinator so you can text the local
BedBoard sidecar without standing up a real admin UI (out of scope for this
slice -- coordinator_phone rows are BedBoard's own table, see app/db/models.py).

Usage (uses the same BEDBOARD_DATABASE_URL the running app uses, default
./bedboard.db):

    python scripts/seed_dev_coordinator.py +15551234567 shelter-first-street --locale en
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.models import CoordinatorPhoneModel  # noqa: E402
from app.db.session import get_sessionmaker, init_db  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone_e164", help='e.g. "+15551234567"')
    parser.add_argument("shelter_id", help='e.g. "shelter-first-street"')
    parser.add_argument("--user-id", default="dev-coordinator")
    parser.add_argument("--shift", default="day", choices=["day", "evening", "overnight", "lead"])
    parser.add_argument("--locale", default="en", choices=["en", "es", "vi"])
    args = parser.parse_args()

    await init_db()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CoordinatorPhoneModel(
                user_id=args.user_id,
                shelter_id=args.shelter_id,
                phone_e164=args.phone_e164,
                shift=args.shift,
                active=True,
                locale=args.locale,
            )
        )
        await session.commit()
    print(f"Registered {args.phone_e164} as a coordinator for {args.shelter_id} ({args.shift}, {args.locale}).")


if __name__ == "__main__":
    asyncio.run(main())
