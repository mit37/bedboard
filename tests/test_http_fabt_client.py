"""Tests HttpFabtClient (app/fabt_client.py) against a small fake FABT
server built to the REAL API's wire shape -- reconciled against the actual
finding-a-bed-tonight Spring controllers (see fabt_client.HttpFabtClient's
docstring for the exact source references), not BedBoard's own simplified
internal contract (that's app/mock_fabt, tested separately).

This fixture app is intentionally local to this test file rather than a
reusable module: it exists only to prove HttpFabtClient's request/response
parsing and the bedsTotal/bedsOccupied derivation are correct, not to serve
as another "run the whole sidecar standalone" option.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException, Request

from app.fabt_client import FabtApiError, HttpFabtClient
from app.schemas import Freshness

API_KEY = "test-api-key-abc123"
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def build_fake_fabt_app() -> FastAPI:
    """A tiny FastAPI app shaped exactly like the real FABT endpoints
    HttpFabtClient calls, with a couple of in-memory shelters."""
    app = FastAPI()

    shelters = {
        "shelter-1": {
            "shelter": {
                "id": "shelter-1",
                "name": "Riverside Emergency Shelter",
                "dvShelter": False,
            },
            "constraints": {"petsAllowed": True, "wheelchairAccessible": True},
            "capacities": [
                {"populationType": "WOMEN_ONLY", "bedsTotal": 10, "bedsOccupied": 6},
                {"populationType": "SINGLE_ADULT", "bedsTotal": 5, "bedsOccupied": 5},
            ],
            "availability": [
                {
                    "populationType": "WOMEN_ONLY",
                    "bedsAvailable": 4,
                    "snapshotTs": _iso(NOW - timedelta(minutes=30)),
                    "dataFreshness": "FRESH",
                },
                {
                    "populationType": "SINGLE_ADULT",
                    "bedsAvailable": 0,
                    "snapshotTs": _iso(NOW - timedelta(hours=10)),
                    "dataFreshness": "STALE",
                },
            ],
        },
        "shelter-dv": {
            "shelter": {"id": "shelter-dv", "name": "Safe Haven", "dvShelter": True},
            "constraints": {"petsAllowed": False, "wheelchairAccessible": True},
            "capacities": [],
            "availability": [],
        },
    }
    reservations: dict[str, list[dict]] = {"shelter-1": [{"id": "r1", "status": "HELD"}]}
    posted_snapshots: list[dict] = []

    async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/api/v1/shelters")
    async def list_shelters(request: Request):
        await require_api_key(request.headers.get("x-api-key"))
        return [
            {"shelter": s["shelter"], "availabilitySummary": {}}
            for s in shelters.values()
            if not s["shelter"]["dvShelter"]
        ]

    @app.get("/api/v1/shelters/{shelter_id}")
    async def get_shelter(shelter_id: str, request: Request):
        await require_api_key(request.headers.get("x-api-key"))
        if shelter_id not in shelters:
            raise HTTPException(status_code=404)
        return shelters[shelter_id]

    @app.patch("/api/v1/shelters/{shelter_id}/availability")
    async def patch_availability(shelter_id: str, body: dict, request: Request):
        await require_api_key(request.headers.get("x-api-key"))
        posted_snapshots.append({"shelter_id": shelter_id, **body})
        beds_total = body["bedsTotal"]
        beds_occupied = body["bedsOccupied"]
        return {
            "populationType": body["populationType"],
            "bedsTotal": beds_total,
            "bedsOccupied": beds_occupied,
            "bedsAvailable": beds_total - beds_occupied,
            "snapshotTs": _iso(NOW),
            "dataFreshness": "FRESH",
            "updatedBy": "coordinator-42",
        }

    @app.get("/api/v1/shelters/{shelter_id}/reservations")
    async def shelter_reservations(shelter_id: str, request: Request):
        await require_api_key(request.headers.get("x-api-key"))
        return reservations.get(shelter_id, [])

    @app.post("/api/v1/reservations")
    async def create_reservation(body: dict, request: Request):
        await require_api_key(request.headers.get("x-api-key"))
        return {
            "id": "new-reservation-id",
            "shelterId": body["shelterId"],
            "populationType": body["populationType"],
            "status": "HELD",
            "expiresAt": _iso(NOW + timedelta(minutes=90)),
        }

    app.state.posted_snapshots = posted_snapshots
    return app


@pytest.fixture
def client_and_app():
    app = build_fake_fabt_app()
    http_client = HttpFabtClient(base_url="http://fake-fabt", api_key=API_KEY, tenant_id="dev-coc")
    http_client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fake-fabt",
        headers={"X-API-Key": API_KEY},
    )
    return http_client, app


@pytest.mark.asyncio
async def test_list_shelters_excludes_dv_and_maps_fields(client_and_app):
    client, _ = client_and_app
    shelters = await client.list_shelters()
    assert [s.id for s in shelters] == ["shelter-1"]
    assert shelters[0].name == "Riverside Emergency Shelter"
    assert shelters[0].is_dv is False
    assert shelters[0].tenant_id == "dev-coc"


@pytest.mark.asyncio
async def test_get_shelter_maps_constraints_and_can_return_dv(client_and_app):
    client, _ = client_and_app
    shelter = await client.get_shelter("shelter-1")
    assert shelter.pets_ok is True
    assert shelter.ada is True

    dv_shelter = await client.get_shelter("shelter-dv")
    assert dv_shelter is not None
    assert dv_shelter.is_dv is True

    assert await client.get_shelter("nonexistent") is None


@pytest.mark.asyncio
async def test_get_latest_counts_maps_fabt_freshness_directly(client_and_app):
    client, _ = client_and_app
    counts = await client.get_latest_counts("shelter-1")
    by_type = {c.population_type: c for c in counts}
    assert by_type["WOMEN_ONLY"].beds_available == 4
    assert by_type["WOMEN_ONLY"].freshness == Freshness.FRESH
    assert by_type["SINGLE_ADULT"].freshness == Freshness.STALE


@pytest.mark.asyncio
async def test_post_snapshot_derives_beds_occupied_from_capacity(client_and_app):
    client, app = client_and_app
    result = await client.post_snapshot(
        shelter_id="shelter-1",
        population_type="WOMEN_ONLY",
        beds_available=7,
        recorded_by="coord-1",
    )
    # bedsTotal=10 (from capacities), beds_available=7 -> bedsOccupied should be derived as 3
    sent = app.state.posted_snapshots[-1]
    assert sent["bedsTotal"] == 10
    assert sent["bedsOccupied"] == 3
    assert sent["acceptingNewGuests"] is True
    assert result.beds_available == 7  # bedsTotal(10) - bedsOccupied(3) echoed back
    assert result.recorded_by == "coordinator-42"  # FABT's updatedBy wins over the param


@pytest.mark.asyncio
async def test_post_snapshot_unknown_population_type_raises(client_and_app):
    client, _ = client_and_app
    with pytest.raises(FabtApiError, match="no configured bed capacity"):
        await client.post_snapshot(
            shelter_id="shelter-1",
            population_type="VETERAN",  # not in shelter-1's seeded capacities
            beds_available=2,
            recorded_by="coord-1",
        )


@pytest.mark.asyncio
async def test_count_active_holds_filters_by_held_status(client_and_app):
    client, _ = client_and_app
    assert await client.count_active_holds("shelter-1") == 1
    assert await client.count_active_holds("shelter-dv") == 0


@pytest.mark.asyncio
async def test_create_reservation_maps_response(client_and_app):
    client, _ = client_and_app
    reservation = await client.create_reservation(
        shelter_id="shelter-1", population_type="WOMEN_ONLY", held_by="outreach-worker-9"
    )
    assert reservation.id == "new-reservation-id"
    assert reservation.status.value == "held"
    assert reservation.expires_at is not None


@pytest.mark.asyncio
async def test_get_wallboard_aggregates_non_dv_shelters(client_and_app):
    client, _ = client_and_app
    snapshot = await client.get_wallboard(tenant_id="dev-coc")
    assert [s.shelter_id for s in snapshot.sites] == ["shelter-1"]
    assert snapshot.sites[0].is_stale is True  # SINGLE_ADULT count is stale
    assert snapshot.sites[0].active_holds == 1


@pytest.mark.asyncio
async def test_wrong_api_key_raises_fabt_api_error():
    app = build_fake_fabt_app()
    client = HttpFabtClient(base_url="http://fake-fabt", api_key="wrong-key", tenant_id="dev-coc")
    client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fake-fabt",
        headers={"X-API-Key": "wrong-key"},
    )
    with pytest.raises(FabtApiError):
        await client.list_shelters()
