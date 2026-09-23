"""Shared data contract between BedBoard sidecar modules and the FABT API.

This is the one file every module (SMS parser, nudge scheduler, wallboard,
mock FABT, FABT client) imports from, so it is written first and treated as
frozen for the rest of the build. See PRD requirement IDs referenced in
docstrings (BB-1..BB-13).

Note on population types: the PRD's upstream FABT taxonomy (page 8) lists
"single adult, family, women only, veteran, youth" as bed_capacity
population types, but the SMS update example (BB-5) uses a simpler
women/men/family split ("W 3 M 1 F 0"). BedBoard treats the SMS-facing
buckets (SmsPopulationType) as a fixed 3-value set and resolves them against
whatever population_type keys a given shelter is configured with in FABT
(see ShelterSummary.sms_population_map). This is a real open question for
the real FABT integration -- flagged in README "Known gaps".
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Locale(str, Enum):
    EN = "en"
    ES = "es"
    VI = "vi"


class SmsPopulationType(str, Enum):
    """The 3 buckets a coordinator can update over SMS (BB-5)."""

    WOMEN = "women"
    MEN = "men"
    FAMILY = "family"


class Freshness(str, Enum):
    """BB-2: FRESH < 2h, AGING 2-8h, STALE > 8h."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"


class ReservationStatus(str, Enum):
    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    # Added after reconciling against the real FABT API (see
    # app/fabt_client.py's HttpFabtClient docstring) -- FABT's own
    # ReservationStatus enum has this extra terminal state.
    CANCELLED_SHELTER_DEACTIVATED = "cancelled_shelter_deactivated"


# --- FABT entities (subset of the upstream API the sidecar depends on) ---


class ShelterSummary(BaseModel):
    id: str
    tenant_id: str
    name: str
    is_dv: bool = False
    pets_ok: bool = False
    ada: bool = False
    # Maps the 3 SMS buckets to this shelter's actual FABT population_type
    # keys, e.g. {"women": "women_only", "men": "single_adult", "family": "family"}.
    sms_population_map: dict[SmsPopulationType, str] = Field(default_factory=dict)


class AvailabilitySnapshot(BaseModel):
    """Append-only row; latest row per (shelter_id, population_type) = current count."""

    shelter_id: str
    population_type: str
    beds_available: int
    recorded_at: datetime
    recorded_by: str


class PopulationCount(BaseModel):
    population_type: str
    beds_available: int
    recorded_at: datetime
    recorded_by: str
    freshness: Freshness


class Reservation(BaseModel):
    id: str
    shelter_id: str
    population_type: str
    status: ReservationStatus
    held_by: str
    expires_at: datetime | None = None


class WallboardSite(BaseModel):
    """One row of the Here4You wallboard grid (BB-7)."""

    shelter_id: str
    name: str
    counts: list[PopulationCount]
    is_stale: bool
    active_holds: int


class WallboardSnapshot(BaseModel):
    generated_at: datetime
    sites: list[WallboardSite]


# --- SMS parsing contract (BB-5) ---


class ParsedSmsUpdate(BaseModel):
    counts: dict[SmsPopulationType, int]
    raw_text: str
    locale: Locale


class SmsParseError(BaseModel):
    reason: str  # "unrecognized_format" | "negative_count" | "out_of_range" | "empty"
    raw_text: str


# --- BedBoard sidecar's own persisted entities (page 9 SQL, extended) ---


class CoordinatorPhone(BaseModel):
    user_id: str
    shelter_id: str
    phone_e164: str
    shift: str | None = None  # 'day' | 'evening' | 'overnight'
    active: bool = True
    # Extension beyond the PRD's literal SQL: needed to pick SMS reply
    # language and satisfy BB-10 (Spanish/Vietnamese) for the SMS channel.
    locale: Locale = Locale.EN


class SmsUpdateLogEntry(BaseModel):
    phone_e164: str
    shelter_id: str | None
    raw_text: str
    parsed: dict | None
    result: str  # 'saved' | 'rejected' | 'parse_error'
    ts: datetime


class NudgeLevel(int, Enum):
    COORDINATOR = 1
    LEAD = 2


class NudgeRecord(BaseModel):
    shelter_id: str
    level: NudgeLevel
    sent_at: datetime
    resolved_at: datetime | None = None
