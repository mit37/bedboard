"""Client interface the sidecar uses to talk to the FABT API.

The sidecar never touches Postgres directly (per the architecture in the
PRD) -- everything goes through this interface with a scoped service
account. `HttpFabtClient` is the real implementation (REST over httpx)
for when a real FABT deployment exists. `app/mock_fabt/client.py`
provides an in-memory implementation with the same interface for local
dev and tests, so the whole sidecar runs standalone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import httpx

from app.schemas import (
    PopulationCount,
    Reservation,
    ShelterSummary,
    WallboardSnapshot,
)


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
    """Real implementation: talks to the FABT Spring Boot API over REST.

    Endpoint paths are assumed per the PRD's data model and are the one
    thing that will need reconciling against FABT's actual OpenAPI spec
    before this class is used against a real deployment -- see README
    "Known gaps".
    """

    def __init__(self, base_url: str, service_account_token: str, timeout: float = 5.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {service_account_token}"},
            timeout=timeout,
        )

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

    async def list_shelters(self) -> list[ShelterSummary]:
        resp = await self._get("/api/shelters")
        return [ShelterSummary(**s) for s in resp.json()]

    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        resp = await self._client.get(f"/api/shelters/{shelter_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise FabtApiError(f"GET shelter {shelter_id} -> {resp.status_code}")
        return ShelterSummary(**resp.json())

    async def get_latest_counts(self, shelter_id: str) -> list[PopulationCount]:
        resp = await self._get(f"/api/shelters/{shelter_id}/counts")
        return [PopulationCount(**c) for c in resp.json()]

    async def post_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> PopulationCount:
        payload = {
            "population_type": population_type,
            "beds_available": beds_available,
            "recorded_by": recorded_by,
        }
        if recorded_at is not None:
            payload["recorded_at"] = recorded_at.isoformat()
        resp = await self._post(f"/api/shelters/{shelter_id}/snapshots", json=payload)
        return PopulationCount(**resp.json())

    async def get_wallboard(self, tenant_id: str) -> WallboardSnapshot:
        resp = await self._get(f"/api/tenants/{tenant_id}/wallboard")
        return WallboardSnapshot(**resp.json())

    async def count_active_holds(self, shelter_id: str) -> int:
        resp = await self._get(f"/api/shelters/{shelter_id}/reservations/active-count")
        return int(resp.json()["count"])

    async def create_reservation(
        self, shelter_id: str, population_type: str, held_by: str
    ) -> Reservation:
        resp = await self._post(
            f"/api/shelters/{shelter_id}/reservations",
            json={"population_type": population_type, "held_by": held_by},
        )
        return Reservation(**resp.json())
