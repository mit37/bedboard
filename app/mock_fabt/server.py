"""Standalone FastAPI app exposing BedBoard's own simplified internal
FABT-client contract (InMemoryFabtClient's methods, as plain REST) so the
whole sidecar can run against *something* over real HTTP with zero setup.
Run it on its own with:

    uvicorn app.mock_fabt.server:app --port 8000

Note: this does NOT mirror the real finding-a-bed-tonight API's actual wire
format (nested shelter/constraints/capacities/availability objects,
bedsTotal/bedsOccupied, `X-API-Key` auth, `/api/v1/...` paths -- see
app.fabt_client.HttpFabtClient's docstring, which was reconciled against
the real Spring controllers). This mock predates that reconciliation and
intentionally stays simpler: it's a fake *simplified* upstream for local
dev/testing of the rest of the sidecar, not a stand-in for real FABT's
exact HTTP shape. HttpFabtClient is tested separately, against
real-shaped fixtures, in tests/test_http_fabt_client.py.

Every route requires `Authorization: Bearer <fabt_api_key>` (see
app.config.Settings.fabt_api_key), returning 401 if missing/wrong.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from app.config import get_settings
from app.mock_fabt.client import InMemoryFabtClient
from app.mock_fabt.store import InMemoryFabtStore
from app.schemas import PopulationCount, Reservation, ShelterSummary, WallboardSnapshot

app = FastAPI(title="Mock FABT API (BedBoard's simplified internal contract)")

_store = InMemoryFabtStore()
_client = InMemoryFabtClient(_store)


async def require_service_account(request: Request) -> None:
    settings = get_settings()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or token != settings.fabt_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


class SnapshotRequest(BaseModel):
    population_type: str
    beds_available: int
    recorded_by: str
    recorded_at: datetime | None = None


class ReservationRequest(BaseModel):
    population_type: str
    held_by: str


@app.get(
    "/api/shelters",
    response_model=list[ShelterSummary],
    dependencies=[Depends(require_service_account)],
)
async def list_shelters() -> list[ShelterSummary]:
    return await _client.list_shelters()


@app.get(
    "/api/shelters/{shelter_id}",
    response_model=ShelterSummary,
    dependencies=[Depends(require_service_account)],
)
async def get_shelter(shelter_id: str) -> ShelterSummary:
    shelter = await _client.get_shelter(shelter_id)
    if shelter is None:
        raise HTTPException(status_code=404, detail="shelter not found")
    return shelter


@app.get(
    "/api/shelters/{shelter_id}/counts",
    response_model=list[PopulationCount],
    dependencies=[Depends(require_service_account)],
)
async def get_counts(shelter_id: str) -> list[PopulationCount]:
    return await _client.get_latest_counts(shelter_id)


@app.post(
    "/api/shelters/{shelter_id}/snapshots",
    response_model=PopulationCount,
    dependencies=[Depends(require_service_account)],
)
async def post_snapshot(shelter_id: str, body: SnapshotRequest) -> PopulationCount:
    return await _client.post_snapshot(
        shelter_id=shelter_id,
        population_type=body.population_type,
        beds_available=body.beds_available,
        recorded_by=body.recorded_by,
        recorded_at=body.recorded_at,
    )


@app.get(
    "/api/tenants/{tenant_id}/wallboard",
    response_model=WallboardSnapshot,
    dependencies=[Depends(require_service_account)],
)
async def get_wallboard(tenant_id: str) -> WallboardSnapshot:
    return await _client.get_wallboard(tenant_id)


@app.get(
    "/api/shelters/{shelter_id}/reservations/active-count",
    dependencies=[Depends(require_service_account)],
)
async def get_active_holds(shelter_id: str) -> dict[str, int]:
    count = await _client.count_active_holds(shelter_id)
    return {"count": count}


@app.post(
    "/api/shelters/{shelter_id}/reservations",
    response_model=Reservation,
    dependencies=[Depends(require_service_account)],
)
async def create_reservation(shelter_id: str, body: ReservationRequest) -> Reservation:
    return await _client.create_reservation(
        shelter_id=shelter_id,
        population_type=body.population_type,
        held_by=body.held_by,
    )
