"""BB-2: FRESH < 2h, AGING 2-8h, STALE > 8h. Shared by mock_fabt, wallboard, nudge."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.schemas import Freshness


def compute_freshness(recorded_at: datetime, now: datetime, settings: Settings) -> Freshness:
    # Tolerate a naive `recorded_at` or `now` (e.g. a timestamp parsed from
    # an external API response with no UTC offset, or a value round-tripped
    # through SQLite, which has no real timezone-aware storage type) --
    # comparing wall-clock digits by stripping tzinfo from whichever side
    # has it beats raising TypeError on a mismatch. Only correct as long as
    # both values are in a mutually consistent timezone, which holds for
    # every caller in this codebase.
    if (now.tzinfo is None) != (recorded_at.tzinfo is None):
        now = now.replace(tzinfo=None)
        recorded_at = recorded_at.replace(tzinfo=None)
    age_hours = (now - recorded_at).total_seconds() / 3600
    if age_hours < settings.aging_hours_threshold:
        return Freshness.FRESH
    if age_hours < settings.stale_hours_threshold:
        return Freshness.AGING
    return Freshness.STALE
