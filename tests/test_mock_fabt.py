"""Tests for the mock FABT module.

Covers InMemoryFabtClient directly (against a deterministic, fixed-`now`
InMemoryFabtStore) and the standalone HTTP server (app/mock_fabt/server.py)
via httpx's ASGI transport, so no real network is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import get_settings
from app.mock_fabt.client import InMemoryFabtClient
from app.mock_fabt.store import InMemoryFabtStore
from app.schemas import Freshness

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def make_client(now: datetime = NOW) -> InMemoryFabtClient:
    store = InMemoryFabtStore(now=now)
    return InMemoryFabtClient(store)


# --- InMemoryFabtClient ------------------------------------------------


@pytest.mark.asyncio
async def test_list_shelters_excludes_dv_shelter():
    client = make_client()
    shelters = await client.list_shelters()

    assert len(shelters) >= 3
    assert all(not s.is_dv for s in shelters)
    assert "shelter-safe-haven" not in {s.id for s in shelters}


@pytest.mark.asyncio
async def test_get_shelter_can_still_fetch_dv_shelter_by_id():
    client = make_client()
    dv_shelter = await client.get_shelter("shelter-safe-haven")

    assert dv_shelter is not None
    assert dv_shelter.is_dv is True


@pytest.mark.asyncio
async def test_get_shelter_unknown_id_returns_none():
    client = make_client()
    assert await client.get_shelter("no-such-shelter") is None


@pytest.mark.asyncio
async def test_post_snapshot_then_get_latest_counts_reflects_new_value():
    client = make_client()
    shelter_id = "shelter-first-street"

    # post_snapshot's freshness is computed against the real wall clock (the
    # frozen FabtClient ABC gives it no `now` override), so only assert on
    # the stored value here; freshness determinism is checked below via
    # get_latest_counts(now=NOW), which does accept an override.
    posted = await client.post_snapshot(
        shelter_id=shelter_id,
        population_type="women_only",
        beds_available=7,
        recorded_by="coord-test",
        recorded_at=NOW - timedelta(minutes=10),
    )
    assert posted.beds_available == 7
    assert posted.recorded_by == "coord-test"

    counts = await client.get_latest_counts(shelter_id, now=NOW)
    women_only = next(c for c in counts if c.population_type == "women_only")

    assert women_only.beds_available == 7
    assert women_only.recorded_by == "coord-test"
    assert women_only.freshness == Freshness.FRESH  # 10 min old at NOW

    # Other buckets for the same shelter are untouched.
    single_adult = next(c for c in counts if c.population_type == "single_adult")
    assert single_adult.freshness == Freshness.AGING  # seeded 5h old


@pytest.mark.asyncio
async def test_get_wallboard_excludes_dv_and_flags_stale_sites():
    client = make_client()
    wallboard = await client.get_wallboard("santa-clara-county", now=NOW)

    site_ids = {site.shelter_id for site in wallboard.sites}
    assert "shelter-safe-haven" not in site_ids
    assert {"shelter-first-street", "shelter-gateway", "shelter-willow-glen"} <= site_ids

    # First Street's "family" bucket was seeded 10h old (>= 8h stale threshold).
    first_street = next(s for s in wallboard.sites if s.shelter_id == "shelter-first-street")
    assert first_street.is_stale is True

    # Gateway's oldest seeded count is 3h old -- aging, not stale.
    gateway = next(s for s in wallboard.sites if s.shelter_id == "shelter-gateway")
    assert gateway.is_stale is False


@pytest.mark.asyncio
async def test_create_reservation_and_count_active_holds():
    client = make_client()
    shelter_id = "shelter-gateway"

    assert await client.count_active_holds(shelter_id) == 0

    reservation = await client.create_reservation(
        shelter_id=shelter_id, population_type="single_adult", held_by="coord-5"
    )
    assert reservation.shelter_id == shelter_id
    assert reservation.expires_at is not None

    assert await client.count_active_holds(shelter_id) == 1


# --- HTTP surface (app/mock_fabt/server.py) -- BedBoard's own simplified
# internal contract, not real FABT's wire format (see that module's
# docstring). HttpFabtClient itself is tested against real-shaped
# fixtures in tests/test_http_fabt_client.py.


@pytest.fixture
def auth_headers() -> dict[str, str]:
    token = get_settings().fabt_api_key
    return {"Authorization": f"Bearer {token}"}


def _server_transport() -> httpx.ASGITransport:
    from app.mock_fabt.server import app as mock_app

    return httpx.ASGITransport(app=mock_app)


@pytest.mark.asyncio
async def test_http_list_shelters_requires_auth():
    async with httpx.AsyncClient(transport=_server_transport(), base_url="http://test") as http:
        resp = await http.get("/api/shelters")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_http_list_shelters_returns_shelters_and_excludes_dv(auth_headers):
    async with httpx.AsyncClient(transport=_server_transport(), base_url="http://test") as http:
        resp = await http.get("/api/shelters", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 3
    assert all(not s["is_dv"] for s in body)
    assert "shelter-safe-haven" not in {s["id"] for s in body}


@pytest.mark.asyncio
async def test_http_post_snapshot_end_to_end(auth_headers):
    async with httpx.AsyncClient(transport=_server_transport(), base_url="http://test") as http:
        post_resp = await http.post(
            "/api/shelters/shelter-gateway/snapshots",
            headers=auth_headers,
            json={
                "population_type": "single_adult",
                "beds_available": 9,
                "recorded_by": "coord-http-test",
            },
        )
        assert post_resp.status_code == 200
        posted = post_resp.json()
        assert posted["beds_available"] == 9
        assert posted["population_type"] == "single_adult"
        assert posted["freshness"] == "fresh"

        counts_resp = await http.get(
            "/api/shelters/shelter-gateway/counts", headers=auth_headers
        )
        assert counts_resp.status_code == 200
        updated = next(
            c for c in counts_resp.json() if c["population_type"] == "single_adult"
        )
        assert updated["beds_available"] == 9
        assert updated["recorded_by"] == "coord-http-test"


@pytest.mark.asyncio
async def test_http_post_snapshot_without_token_is_rejected():
    async with httpx.AsyncClient(transport=_server_transport(), base_url="http://test") as http:
        resp = await http.post(
            "/api/shelters/shelter-gateway/snapshots",
            json={
                "population_type": "single_adult",
                "beds_available": 1,
                "recorded_by": "coord-x",
            },
        )

    assert resp.status_code == 401
