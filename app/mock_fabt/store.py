"""In-memory fake of the upstream FABT platform's data (shelters, the latest
availability count per (shelter_id, population_type), and reservations).

This backs InMemoryFabtClient (app/mock_fabt/client.py) and, through it, the
standalone HTTP mock server (app/mock_fabt/server.py) -- together these let
the BedBoard sidecar run and be tested completely standalone, without a real
Java/Spring FABT deployment.

Assumption: the PRD doesn't pin down a couple of details this mock still
needs to make concrete decisions on, so:
  - FabtClient.list_shelters() takes no tenant_id in the frozen ABC, so this
    store treats "all shelters" as one flat namespace; only get_wallboard's
    tenant_id (via list_shelters(tenant_id=...)) narrows to one tenant. All
    seed shelters share a single tenant_id ("santa-clara-county"), matching
    the PRD's single-county demo scope.
  - A reservation hold's expiry isn't specified anywhere in the frozen
    contract files, so this mock defaults new holds to expire
    DEFAULT_HOLD_MINUTES after creation; a real FABT deployment would set
    its own policy.

Security invariant (PRD's top requirement, BB-*): a confidential
domestic-violence shelter (is_dv=True) must never be exposed through
enumeration. Only a direct get_shelter(shelter_id) lookup by a caller that
already knows the id may return one -- list_shelters() and anything built
from it (e.g. the wallboard) silently exclude DV shelters. This mirrors
FABT's real "opaque referral by token" design for DV shelters, even though
the token flow itself is out of scope here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.schemas import (
    AvailabilitySnapshot,
    Reservation,
    ReservationStatus,
    ShelterSummary,
    SmsPopulationType,
)

DEFAULT_HOLD_MINUTES = 15

_TENANT_ID = "santa-clara-county"


class InMemoryFabtStore:
    """Plain-Python in-memory store standing in for FABT's Postgres tables."""

    def __init__(self, now: datetime | None = None) -> None:
        # Never call datetime.now() at import time -- seed offsets are
        # computed relative to this constructor-time "now" so tests are
        # deterministic when a fixed `now` is passed in.
        now = now if now is not None else datetime.now(timezone.utc)

        self._shelters: dict[str, ShelterSummary] = {}
        # (shelter_id, population_type) -> latest AvailabilitySnapshot.
        self._counts: dict[tuple[str, str], AvailabilitySnapshot] = {}
        self._reservations: dict[str, Reservation] = {}

        self._seed(now)

    # --- seed data -----------------------------------------------------

    def _seed(self, now: datetime) -> None:
        first_street = ShelterSummary(
            id="shelter-first-street",
            tenant_id=_TENANT_ID,
            name="First Street Shelter",
            is_dv=False,
            pets_ok=True,
            ada=True,
            sms_population_map={
                SmsPopulationType.WOMEN: "women_only",
                SmsPopulationType.MEN: "single_adult",
                SmsPopulationType.FAMILY: "family",
            },
        )
        gateway = ShelterSummary(
            id="shelter-gateway",
            tenant_id=_TENANT_ID,
            name="Gateway Men's Shelter",
            is_dv=False,
            pets_ok=False,
            ada=False,
            sms_population_map={
                SmsPopulationType.WOMEN: "women_only",
                SmsPopulationType.MEN: "single_adult",
                SmsPopulationType.FAMILY: "family",
            },
        )
        willow_glen = ShelterSummary(
            id="shelter-willow-glen",
            tenant_id=_TENANT_ID,
            name="Willow Glen Family Center",
            is_dv=False,
            pets_ok=True,
            ada=True,
            sms_population_map={
                SmsPopulationType.WOMEN: "women_only",
                SmsPopulationType.MEN: "single_adult",
                # Willow Glen's own FABT taxonomy names the family bucket
                # differently from First Street's -- exactly the kind of
                # per-shelter divergence sms_population_map exists to bridge
                # (see schemas.py module docstring).
                SmsPopulationType.FAMILY: "family_unit",
            },
        )
        safe_haven = ShelterSummary(
            id="shelter-safe-haven",
            tenant_id=_TENANT_ID,
            name="Safe Haven Confidential Shelter",
            is_dv=True,
            pets_ok=False,
            ada=True,
            sms_population_map={
                SmsPopulationType.WOMEN: "women_only",
                SmsPopulationType.MEN: "single_adult",
                SmsPopulationType.FAMILY: "family",
            },
        )

        for shelter in (first_street, gateway, willow_glen, safe_haven):
            self._shelters[shelter.id] = shelter

        def seed_count(
            shelter_id: str,
            population_type: str,
            beds_available: int,
            age: timedelta,
            recorded_by: str,
        ) -> None:
            self._counts[(shelter_id, population_type)] = AvailabilitySnapshot(
                shelter_id=shelter_id,
                population_type=population_type,
                beds_available=beds_available,
                recorded_at=now - age,
                recorded_by=recorded_by,
            )

        # First Street: fresh / aging / stale spread across its 3 buckets
        # (default thresholds: fresh < 2h, aging 2-8h, stale >= 8h).
        seed_count("shelter-first-street", "women_only", 4, timedelta(minutes=30), "coord-1")
        seed_count("shelter-first-street", "single_adult", 2, timedelta(hours=5), "coord-1")
        seed_count("shelter-first-street", "family", 0, timedelta(hours=10), "coord-1")

        # Gateway only ever reports the buckets it actually serves -- its
        # "family" population_type is never reported, so it simply won't
        # show up in get_latest_counts.
        seed_count("shelter-gateway", "single_adult", 12, timedelta(hours=1), "coord-2")
        seed_count("shelter-gateway", "women_only", 0, timedelta(hours=3), "coord-2")

        seed_count("shelter-willow-glen", "family_unit", 3, timedelta(minutes=20), "coord-3")
        seed_count("shelter-willow-glen", "women_only", 1, timedelta(hours=9), "coord-3")
        seed_count("shelter-willow-glen", "single_adult", 0, timedelta(hours=1), "coord-3")

        # Safe Haven (DV) -- reachable only via get_shelter/get_latest_counts
        # by id, never through list_shelters or the wallboard.
        seed_count("shelter-safe-haven", "women_only", 2, timedelta(minutes=45), "coord-4")

    # --- shelters --------------------------------------------------------

    def list_shelters(self, tenant_id: str | None = None) -> list[ShelterSummary]:
        """Enumerate shelters, excluding confidential DV shelters.

        Optionally narrowed to one tenant (used by get_wallboard).
        """
        return [
            s
            for s in self._shelters.values()
            if not s.is_dv and (tenant_id is None or s.tenant_id == tenant_id)
        ]

    def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        """Direct id lookup -- may return a DV shelter's record.

        Callers only reach this when they already know the specific
        shelter_id (e.g. from an opaque referral), never by enumerating.
        """
        return self._shelters.get(shelter_id)

    # --- availability counts ---------------------------------------------

    def get_latest_counts(self, shelter_id: str) -> list[AvailabilitySnapshot]:
        return [
            snapshot
            for (sid, _population_type), snapshot in self._counts.items()
            if sid == shelter_id
        ]

    def record_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime,
    ) -> AvailabilitySnapshot:
        snapshot = AvailabilitySnapshot(
            shelter_id=shelter_id,
            population_type=population_type,
            beds_available=beds_available,
            recorded_at=recorded_at,
            recorded_by=recorded_by,
        )
        self._counts[(shelter_id, population_type)] = snapshot
        return snapshot

    # --- reservations ------------------------------------------------------

    def count_active_holds(self, shelter_id: str) -> int:
        # HELD only, matching the real FABT API (confirmed live against a
        # running instance -- GET /api/v1/shelters/{id}/reservations only
        # ever returns currently-HELD reservations; see
        # HttpFabtClient.count_active_holds). Previously also counted
        # CONFIRMED, which the real API never does -- WallboardSite.active_holds
        # would have silently meant different things depending on which
        # FabtClient implementation was wired up the moment a
        # reservation-confirmation flow existed.
        return sum(
            1
            for r in self._reservations.values()
            if r.shelter_id == shelter_id and r.status == ReservationStatus.HELD
        )

    def create_reservation(
        self,
        shelter_id: str,
        population_type: str,
        held_by: str,
        now: datetime | None = None,
    ) -> Reservation:
        now = now if now is not None else datetime.now(timezone.utc)
        reservation = Reservation(
            id=str(uuid.uuid4()),
            shelter_id=shelter_id,
            population_type=population_type,
            status=ReservationStatus.HELD,
            held_by=held_by,
            expires_at=now + timedelta(minutes=DEFAULT_HOLD_MINUTES),
        )
        self._reservations[reservation.id] = reservation
        return reservation

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        return self._reservations.get(reservation_id)
