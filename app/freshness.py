"""BB-2: FRESH < 2h, AGING 2-8h, STALE > 8h. Shared by mock_fabt, wallboard, nudge."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.schemas import Freshness


def compute_freshness(recorded_at: datetime, now: datetime, settings: Settings) -> Freshness:
    age_hours = (now - recorded_at).total_seconds() / 3600
    if age_hours < settings.aging_hours_threshold:
        return Freshness.FRESH
    if age_hours < settings.stale_hours_threshold:
        return Freshness.AGING
    return Freshness.STALE
