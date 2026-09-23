"""In-memory FabtClient implementation for local dev/tests -- zero setup, no
network. This is what the sidecar should use by default outside of a real
FABT deployment.

Every method reads/writes an InMemoryFabtStore instance directly. Freshness
is computed here (not in the store) via app.freshness.compute_freshness,
matching the rest of the sidecar's convention that freshness classification
lives in one place.

Assumption: WallboardSite.is_stale is set to True if ANY of a shelter's
reported population counts is STALE, not only when all of them are. A
coordinator who has stopped reporting for one population bucket (e.g.
"family") while still reporting others still needs the wallboard/nudge flow
to flag that shelter for follow-up, so "any stale count" is the safer
reading of BB-2/BB-7 in the absence of a more precise PRD rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.fabt_client import FabtClient
from app.freshness import compute_freshness
from app.mock_fabt.store import InMemoryFabtStore
from app.schemas import (
    Freshness,
    PopulationCount,
    Reservation,
    ShelterSummary,
    WallboardSite,
    WallboardSnapshot,
)


class InMemoryFabtClient(FabtClient):
    def __init__(self, store: InMemoryFabtStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings if settings is not None else get_settings()

    async def list_shelters(self) -> list[ShelterSummary]:
        return self._store.list_shelters()

    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        return self._store.get_shelter(shelter_id)

    async def get_latest_counts(
        self, shelter_id: str, now: datetime | None = None
    ) -> list[PopulationCount]:
        now = now if now is not None else datetime.now(timezone.utc)
        snapshots = self._store.get_latest_counts(shelter_id)
        return [
            PopulationCount(
                population_type=snapshot.population_type,
                beds_available=snapshot.beds_available,
                recorded_at=snapshot.recorded_at,
                recorded_by=snapshot.recorded_by,
                freshness=compute_freshness(snapshot.recorded_at, now, self._settings),
            )
            for snapshot in snapshots
        ]

    async def post_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> PopulationCount:
        now = datetime.now(timezone.utc)
        recorded_at = recorded_at if recorded_at is not None else now
        snapshot = self._store.record_snapshot(
            shelter_id=shelter_id,
            population_type=population_type,
            beds_available=beds_available,
            recorded_by=recorded_by,
            recorded_at=recorded_at,
        )
        return PopulationCount(
            population_type=snapshot.population_type,
            beds_available=snapshot.beds_available,
            recorded_at=snapshot.recorded_at,
            recorded_by=snapshot.recorded_by,
            freshness=compute_freshness(snapshot.recorded_at, now, self._settings),
        )

    async def get_wallboard(
        self, tenant_id: str, now: datetime | None = None
    ) -> WallboardSnapshot:
        now = now if now is not None else datetime.now(timezone.utc)
        sites: list[WallboardSite] = []
        for shelter in self._store.list_shelters(tenant_id=tenant_id):
            counts = await self.get_latest_counts(shelter.id, now=now)
            sites.append(
                WallboardSite(
                    shelter_id=shelter.id,
                    name=shelter.name,
                    counts=counts,
                    is_stale=any(c.freshness == Freshness.STALE for c in counts),
                    active_holds=self._store.count_active_holds(shelter.id),
                )
            )
        return WallboardSnapshot(generated_at=now, sites=sites)

    async def count_active_holds(self, shelter_id: str) -> int:
        return self._store.count_active_holds(shelter_id)

    async def create_reservation(
        self, shelter_id: str, population_type: str, held_by: str
    ) -> Reservation:
        return self._store.create_reservation(
            shelter_id=shelter_id, population_type=population_type, held_by=held_by
        )
