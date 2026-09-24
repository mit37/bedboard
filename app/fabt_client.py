"""Client interface the sidecar uses to talk to the FABT API.

The sidecar never touches Postgres directly (per the architecture in the
PRD) -- everything goes through this interface with a scoped service
account. `HttpFabtClient` is the real implementation (REST over httpx)
for when a real FABT deployment exists. `app/mock_fabt/client.py`
provides an in-memory implementation with the same interface for local
dev and tests, so the whole sidecar runs standalone.

`HttpFabtClient` is written against the REAL API of the forked
`finding-a-bed-tonight` platform (github.com/mit37/finding-a-bed-tonight,
forked from ccradle/finding-a-bed-tonight), reconciled by reading its
actual Spring controllers -- not guessed. See its class docstring below
for the specific endpoints, auth mechanism, and the one real semantic gap
this adapter has to bridge (FABT wants bedsTotal/bedsOccupied; BedBoard's
SMS flow only ever knows "beds available").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx

from app.schemas import (
    Freshness,
    PopulationCount,
    Reservation,
    ReservationStatus,
    ShelterSummary,
    SmsPopulationType,
    WallboardSite,
    WallboardSnapshot,
)

# FABT's real PopulationType enum (org.fabt.shelter.domain.PopulationType)
# has 7 values; BedBoard's SMS flow only ever reports 3 simplified buckets
# (BB-5: "W 3 M 1 F 0"). This is the platform-wide default mapping applied
# to every shelter by HttpFabtClient. A real pilot may need this to vary
# per shelter (e.g. a veteran-only shelter shouldn't report into
# SINGLE_ADULT) -- that would need a small per-shelter override table,
# which is out of scope for this slice; see README "Known gaps".
DEFAULT_SMS_POPULATION_MAP: dict[SmsPopulationType, str] = {
    SmsPopulationType.WOMEN: "WOMEN_ONLY",
    SmsPopulationType.MEN: "SINGLE_ADULT",
    SmsPopulationType.FAMILY: "FAMILY_WITH_CHILDREN",
}

_FRESHNESS_FROM_FABT: dict[str, Freshness] = {
    "FRESH": Freshness.FRESH,
    "AGING": Freshness.AGING,
    "STALE": Freshness.STALE,
}


class FabtClient(ABC):
    @abstractmethod
    async def list_shelters(self) -> list[ShelterSummary]: ...

    @abstractmethod
    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None: ...

    @abstractmethod
    async def get_latest_counts(self, shelter_id: str) -> list[PopulationCount]: ...

    @abstractmethod
    async def post_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> PopulationCount: ...

    @abstractmethod
    async def get_wallboard(self, tenant_id: str) -> WallboardSnapshot: ...

    @abstractmethod
    async def count_active_holds(self, shelter_id: str) -> int: ...

    @abstractmethod
    async def create_reservation(
        self, shelter_id: str, population_type: str, held_by: str
    ) -> Reservation: ...


class FabtApiError(RuntimeError):
    pass


class HttpFabtClient(FabtClient):
    """Real implementation: talks to the actual finding-a-bed-tonight
    Spring Boot API over REST, reconciled against its source
    (org.fabt.shelter.api.ShelterController, org.fabt.availability.api.
    AvailabilityController, org.fabt.reservation.api.*,
    org.fabt.shared.security.ApiKeyAuthenticationFilter).

    Auth: `X-API-Key` header (NOT `Authorization: Bearer`). FABT has a
    separate, distinct M2M mechanism from its user-login JWT flow: a
    COC_ADMIN-scoped API key (`shelterId: null` at creation), created by a
    human FABT admin via `POST /api/v1/api-keys`, sent back on every
    request. Tenant scope is derived entirely server-side from the key --
    there is no tenant_id anywhere in the request or response.

    Two real semantic gaps vs. BedBoard's simpler internal model, both
    bridged here rather than left as TODOs:

    1. FABT's `PATCH /api/v1/shelters/{id}/availability` takes
       `bedsTotal`/`bedsOccupied`, not a plain "beds available" number --
       `bedsAvailable` is always derived server-side. BedBoard's SMS flow
       (BB-5) only ever captures "how many beds are open right now" from a
       coordinator's text, never total capacity (which changes rarely, when
       a shelter physically adds/removes beds, not every shift). So
       `post_snapshot` here first reads the shelter's current `bedsTotal`
       for that population type (from `GET /api/v1/shelters/{id}`'s
       `capacities[]`, which is unrelated to the live `availability[]`
       numbers) and derives `bedsOccupied = bedsTotal - beds_available`.
       This preserves BedBoard's "just tell us what's open" SMS UX while
       still round-tripping through FABT's real capacity model correctly.
       Raises FabtApiError if the shelter has no configured capacity for
       that population type (can't derive an occupied count from nothing).

       Verified live against a real running instance of the forked FABT
       backend (2026-09-23): when a population type has an active hold,
       the returned `beds_available` is LOWER than the value passed in --
       FABT's server-side formula is `bedsTotal - bedsOccupied -
       bedsOnHold`, so a hold is subtracted on top of whatever
       bedsOccupied this method derives. This is correct behavior (a held
       bed shouldn't show as available to a second placer), but it means
       the PopulationCount this method returns is not simply an echo of
       the request -- callers should always use the *returned* value as
       the authoritative count, never assume it equals `beds_available`
       as passed in.

    2. `POST /api/v1/reservations` has no `heldBy` field -- the reservation
       is attributed to whichever identity authenticated the request (here,
       the BedBoard service-account API key itself, not an individual
       outreach worker), and there's no separate field for "who is this
       hold for" beyond free-text `notes`. `create_reservation`'s `held_by`
       argument is therefore passed through as the reservation's `notes`
       for traceability -- it does NOT make FABT attribute the hold to that
       specific person. (This method isn't currently called by any
       BedBoard module -- reservations/holds are FABT PWA's job per the
       PRD's non-goals -- it's implemented for interface completeness.)
    """

    def __init__(self, base_url: str, api_key: str, tenant_id: str = "unknown-tenant", timeout: float = 5.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
        self._tenant_id = tenant_id

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.get(path, **kwargs)
        if resp.status_code >= 400:
            raise FabtApiError(f"GET {path} -> {resp.status_code}: {resp.text}")
        return resp

    async def _post(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.post(path, **kwargs)
        if resp.status_code >= 400:
            raise FabtApiError(f"POST {path} -> {resp.status_code}: {resp.text}")
        return resp

    async def _patch(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.patch(path, **kwargs)
        if resp.status_code >= 400:
            raise FabtApiError(f"PATCH {path} -> {resp.status_code}: {resp.text}")
        return resp

    def _shelter_from_list_item(self, item: dict) -> ShelterSummary:
        s = item["shelter"]
        # GET /api/v1/shelters doesn't include `constraints` (pets/ADA) --
        # only the single-shelter detail endpoint does. Nothing in the
        # sidecar currently reads pets_ok/ada (search/filtering is FABT
        # PWA's job, not BedBoard's per the PRD's non-goals), so this is a
        # safe, documented default rather than an N+1 detail fetch per
        # shelter just to populate fields nobody consumes yet.
        return ShelterSummary(
            id=s["id"],
            tenant_id=self._tenant_id,
            name=s["name"],
            is_dv=s["dvShelter"],
            pets_ok=False,
            ada=False,
            sms_population_map=DEFAULT_SMS_POPULATION_MAP,
        )

    def _shelter_from_detail(self, detail: dict) -> ShelterSummary:
        s = detail["shelter"]
        constraints = detail.get("constraints") or {}
        return ShelterSummary(
            id=s["id"],
            tenant_id=self._tenant_id,
            name=s["name"],
            is_dv=s["dvShelter"],
            pets_ok=bool(constraints.get("petsAllowed", False)),
            ada=bool(constraints.get("wheelchairAccessible", False)),
            sms_population_map=DEFAULT_SMS_POPULATION_MAP,
        )

    async def _get_shelter_detail(self, shelter_id: str) -> dict | None:
        resp = await self._client.get(f"/api/v1/shelters/{shelter_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise FabtApiError(f"GET shelter {shelter_id} -> {resp.status_code}: {resp.text}")
        return resp.json()

    async def list_shelters(self) -> list[ShelterSummary]:
        resp = await self._get("/api/v1/shelters")
        shelters = [self._shelter_from_list_item(item) for item in resp.json()]
        # Defense-in-depth: FABT already excludes DV shelters from this
        # endpoint for API keys without dvAccess, but BedBoard's own DV
        # invariant (never show a DV shelter in enumeration/wallboard)
        # should hold regardless of what a given key happens to be scoped
        # to -- mirrors app/mock_fabt/store.py's same filter.
        return [s for s in shelters if not s.is_dv]

    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        detail = await self._get_shelter_detail(shelter_id)
        return self._shelter_from_detail(detail) if detail is not None else None

    async def get_latest_counts(self, shelter_id: str) -> list[PopulationCount]:
        detail = await self._get_shelter_detail(shelter_id)
        if detail is None:
            return []
        counts = []
        for a in detail.get("availability", []):
            counts.append(
                PopulationCount(
                    population_type=a["populationType"],
                    beds_available=a["bedsAvailable"],
                    recorded_at=datetime.fromisoformat(a["snapshotTs"]),
                    # FABT's shelter-detail read endpoint doesn't expose who
                    # recorded a count (only the availability PATCH response
                    # does, via `updatedBy`) -- empty string documents that
                    # gap rather than inventing an attribution.
                    recorded_by="",
                    freshness=_FRESHNESS_FROM_FABT.get(a.get("dataFreshness", ""), Freshness.STALE),
                )
            )
        return counts

    async def post_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> PopulationCount:
        detail = await self._get_shelter_detail(shelter_id)
        if detail is None:
            raise FabtApiError(f"Cannot post a snapshot for unknown shelter {shelter_id}")
        capacity = next(
            (c for c in detail.get("capacities", []) if c["populationType"] == population_type),
            None,
        )
        if capacity is None:
            raise FabtApiError(
                f"Shelter {shelter_id} has no configured bed capacity for population type "
                f"{population_type!r}; cannot derive bedsOccupied for FABT's availability "
                "PATCH from a beds_available-only update."
            )
        beds_total = capacity["bedsTotal"]
        beds_occupied = max(beds_total - beds_available, 0)

        resp = await self._patch(
            f"/api/v1/shelters/{shelter_id}/availability",
            json={
                "populationType": population_type,
                "bedsTotal": beds_total,
                "bedsOccupied": beds_occupied,
                "acceptingNewGuests": beds_available > 0,
                "notes": "Updated via BedBoard sidecar",
                "overflowBeds": 0,
            },
        )
        data = resp.json()
        return PopulationCount(
            population_type=data["populationType"],
            beds_available=data["bedsAvailable"],
            recorded_at=datetime.fromisoformat(data["snapshotTs"]),
            recorded_by=data.get("updatedBy") or recorded_by,
            freshness=_FRESHNESS_FROM_FABT.get(data.get("dataFreshness", ""), Freshness.FRESH),
        )

    async def get_wallboard(self, tenant_id: str) -> WallboardSnapshot:
        # The real API's tenant scope is fixed by which API key we
        # authenticated with (see class docstring), not by this parameter
        # -- there is nowhere to actually apply a different tenant_id.
        # Rather than silently ignoring a caller-supplied tenant_id that
        # doesn't match (which would return this client's real, full
        # tenant data for what the caller thought was a different/empty
        # tenant -- InMemoryFabtClient, by contrast, DOES filter by
        # tenant_id, so this divergence was invisible in dev/tests and
        # surprising in production), fail closed on a mismatch instead.
        if tenant_id != self._tenant_id:
            raise FabtApiError(
                f"get_wallboard called with tenant_id={tenant_id!r}, but this client is "
                f"scoped to tenant_id={self._tenant_id!r} (real FABT derives tenant scope "
                "from the API key server-side, not from this parameter) -- refusing rather "
                "than silently returning a different tenant's data."
            )
        sites: list[WallboardSite] = []
        for shelter in await self.list_shelters():
            counts = await self.get_latest_counts(shelter.id)
            sites.append(
                WallboardSite(
                    shelter_id=shelter.id,
                    name=shelter.name,
                    counts=counts,
                    is_stale=any(c.freshness == Freshness.STALE for c in counts),
                    active_holds=await self.count_active_holds(shelter.id),
                )
            )
        return WallboardSnapshot(generated_at=datetime.now(timezone.utc), sites=sites)

    async def count_active_holds(self, shelter_id: str) -> int:
        # GET /api/v1/shelters/{id}/reservations already returns only
        # currently-HELD reservations for that shelter (there's no
        # dedicated count endpoint -- ReservationService.countActiveHolds
        # is internal-only). Filter by status defensively anyway rather
        # than trusting that default never changes silently upstream.
        resp = await self._get(f"/api/v1/shelters/{shelter_id}/reservations")
        return sum(1 for r in resp.json() if r.get("status") == "HELD")

    async def create_reservation(
        self, shelter_id: str, population_type: str, held_by: str
    ) -> Reservation:
        resp = await self._post(
            "/api/v1/reservations",
            json={"shelterId": shelter_id, "populationType": population_type, "notes": held_by},
        )
        data = resp.json()
        return Reservation(
            id=data["id"],
            shelter_id=data["shelterId"],
            population_type=data["populationType"],
            status=ReservationStatus(data["status"].lower()),
            held_by=held_by,
            expires_at=datetime.fromisoformat(data["expiresAt"]) if data.get("expiresAt") else None,
        )
