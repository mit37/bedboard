# BedBoard sidecar

BedBoard's own original-code layer, per the [PRD & Tech Spec](docs/BedBoard%20%E2%80%94%20PRD%20%26%20Tech%20Spec.pdf): a Python 3.12 / FastAPI
service that sits beside the open-source **Finding A Bed Tonight** (FABT)
platform and adds the county-pilot pieces FABT doesn't already have —
SMS bed updates, stale-count nudges, the Here4You wallboard, and
Spanish/Vietnamese translations.

**This build is the sidecar only.** It runs standalone against an
in-memory mock of the FABT API by default (`BEDBOARD_USE_MOCK_FABT=true`),
but `HttpFabtClient` (the real-FABT path) has been reconciled against and
live-tested against an actual running instance of the forked
[finding-a-bed-tonight](https://github.com/mit37/finding-a-bed-tonight)
backend — see "FABT integration" below. See "Known gaps" for what's still
mocked and what a real pilot still needs.

## What's implemented (PRD requirement IDs)

| ID | Requirement | Where |
|----|---|---|
| BB-2 | Freshness (FRESH/AGING/STALE) | [`app/freshness.py`](app/freshness.py) |
| BB-5 | SMS bed update | [`app/sms/parser.py`](app/sms/parser.py), [`app/sms/webhook.py`](app/sms/webhook.py) |
| BB-6 | Stale-count nudge + escalation | [`app/nudge/scheduler.py`](app/nudge/scheduler.py) |
| BB-7 | Here4You wallboard | [`app/wallboard/router.py`](app/wallboard/router.py) |
| BB-8 | DV opaque referral (partial) | `is_dv` filtering in [`app/mock_fabt/store.py`](app/mock_fabt/store.py) — see gaps |
| BB-10 | Spanish + Vietnamese | [`app/sms/messages.py`](app/sms/messages.py), [`app/nudge/messages.py`](app/nudge/messages.py) |
| — | Per-shelter SMS population-map override | [`app/sms_population_map.py`](app/sms_population_map.py) |

Not built in this slice: BB-1/3/4/9/11/12/13 and the FABT PWA itself all
live in FABT (Java/Spring), which this build doesn't fork — see the PRD's
"build vs. adopt" call.

## Architecture

```
Twilio SMS <--> app/sms/webhook.py --+
                                      |--> app.fabt_client.FabtClient (ABC)
app/wallboard/router.py (SSE) -------+          |
                                                 +-- HttpFabtClient  -> real FABT (github.com/mit37/finding-a-bed-tonight)
app/nudge/scheduler.py (APScheduler) --+        +-- InMemoryFabtClient -> app/mock_fabt (default, this repo)

BedBoard's own tables (app/db/models.py, via SQLAlchemy async):
  coordinator_phone, sms_update_log, nudge
```

The sidecar never touches FABT's Postgres directly (per the PRD's
architecture diagram) — everything upstream goes through `FabtClient`.
Swapping `InMemoryFabtClient` for `HttpFabtClient` (already written, in
[`app/fabt_client.py`](app/fabt_client.py)) is the entire integration
surface for pointing this at a real FABT deployment.

## FABT integration

FABT was forked to
[mit37/finding-a-bed-tonight](https://github.com/mit37/finding-a-bed-tonight)
and `HttpFabtClient` was written against its **real** Spring controllers
(not guessed) — auth, endpoint paths, request/response shapes, all read
from source. It was then verified live: built the real backend JAR via a
`maven:3.9-eclipse-temurin-25` container, ran it against a local Postgres
with the project's own Flyway migrations + dev seed data, and exercised
every `HttpFabtClient` method (`list_shelters`, `get_shelter`,
`get_latest_counts`, `post_snapshot`, `count_active_holds`,
`get_wallboard`) against the live API — all passing, DV shelter correctly
excluded from every listing.

Real things this surfaced that the PRD didn't call out:

- **Auth is `X-API-Key`, not a Bearer JWT.** FABT has a distinct M2M
  mechanism (`org.fabt.shared.security.ApiKeyAuthenticationFilter`) from
  its user-login flow — a `COC_ADMIN`-scoped key created via
  `POST /api/v1/api-keys` with `shelterId: null`. Tenant scope is derived
  entirely from the key server-side; there's no tenant param anywhere.
- **FABT wants `bedsTotal`/`bedsOccupied`, not "beds available."**
  `PATCH /api/v1/shelters/{id}/availability` derives `bedsAvailable`
  server-side. `HttpFabtClient.post_snapshot` bridges this by reading the
  shelter's current `bedsTotal` and deriving `bedsOccupied` — see its
  docstring in `app/fabt_client.py` for the full reasoning.
- **Active holds reduce `bedsAvailable` below what you post.** FABT's
  formula is `bedsTotal - bedsOccupied - bedsOnHold`, so if a bed is
  currently held, the count that comes back is lower than the SMS
  coordinator's reported number. `app/sms/webhook.py` was fixed to
  confirm with FABT's *returned* value, not the raw SMS input, so a
  coordinator is never told a number the wallboard will then contradict.
- **~~API-key rate limit~~ — confirmed and fixed upstream (2026-09-23).**
  It genuinely wasn't raised anywhere: `application-prod.yml` never set
  `fabt.api-key.rate-limit`, so any real deployment silently ran on
  `ApiKeyAuthenticationFilter`'s own 5/min-per-IP code default, despite
  `docs/FOR-DEVELOPERS.md` describing 1000/min as though it were already
  the default. Confirmed live: booted the real backend with
  `SPRING_PROFILES_ACTIVE=lite,prod` and got `X-RateLimit-Limit: 5`.
  Filed and pushed the fix to the fork
  ([commit 71806cc](https://github.com/mit37/finding-a-bed-tonight/commit/71806cc)):
  `application-prod.yml` now sets `rate-limit: 1000`, and the same live
  check afterward returned `X-RateLimit-Limit: 1000`. This only takes
  effect when a real deployment actually activates the `prod` Spring
  profile (worth double-checking at deploy time) — BedBoard's wallboard
  SSE (one `get_wallboard` call per refresh tick, fanning out to 2 FABT
  calls per shelter) and the nudge scheduler would still blow past the
  5/min dev default on any pilot with more than 1-2 shelters if `prod`
  isn't active.
- **The shelter-list endpoint doesn't include ADA/pets constraints** —
  only the single-shelter detail endpoint does. `HttpFabtClient` defaults
  `pets_ok`/`ada` to `False` from `list_shelters()` rather than doing an
  N+1 detail fetch per shelter, since nothing in the sidecar currently
  reads those two fields (search/filtering is FABT PWA's job).

To run against it yourself: `git clone
https://github.com/mit37/finding-a-bed-tonight.git`, build the backend
(`mvn -DskipTests package` — needs Java 25 + Maven, or run that inside a
`maven:3.9-eclipse-temurin-25` container), start Postgres + load
`infra/scripts/seed-data.sql`, then set `BEDBOARD_USE_MOCK_FABT=false`,
`BEDBOARD_FABT_API_KEY=fabt_demo_key_12345678901234567890123456789012`
(the repo's own dev seed key) and `BEDBOARD_FABT_API_BASE_URL` in this
project's `.env`.

## Run it (standalone, no setup beyond Python)

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then, since `coordinator_phone` is BedBoard's own table and there's no
admin UI yet (out of scope for this slice), register a demo coordinator:

```bash
python scripts/seed_dev_coordinator.py "+15551234567" shelter-first-street --shift day --locale en
```

Optionally, override how a shelter's SMS buckets map onto FABT population
types (also BedBoard's own table, same no-admin-UI-yet situation — see
[`app/sms_population_map.py`](app/sms_population_map.py)):

```bash
python scripts/set_shelter_sms_population_map.py set shelter-gateway men VETERAN
python scripts/set_shelter_sms_population_map.py disable shelter-gateway family
python scripts/set_shelter_sms_population_map.py list shelter-gateway
```

Try it:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/wallboard/santa-clara-county
curl -X POST http://localhost:8000/sms/inbound \
  --data-urlencode "From=+15551234567" --data-urlencode "Body=W 3 M 1 F 0"
```

(Seeded demo shelters: `shelter-first-street`, `shelter-gateway`,
`shelter-willow-glen`, plus `shelter-safe-haven` — a DV shelter that
`list_shelters`/the wallboard always exclude, and that only shows up via a
direct `get_shelter` lookup.)

## Run it with Docker (Postgres instead of SQLite)

```bash
docker compose up --build
```

## Tests

```bash
pytest        # 79 tests: parser, mock FABT, HttpFabtClient (real-shaped fixtures), nudge scheduler, wallboard, SMS population-map overrides, full SMS-to-wallboard integration
```

## Known gaps vs. the full PRD (read before a real pilot)

These are the real open items this slice deliberately left for the next
phase, not bugs:

- **Per-shelter SMS population-map overrides exist but aren't
  admin-UI-managed.** `app.fabt_client.DEFAULT_SMS_POPULATION_MAP`
  (women→`WOMEN_ONLY`, men→`SINGLE_ADULT`, family→`FAMILY_WITH_CHILDREN`)
  is still every shelter's starting point, but a BedBoard admin can now
  redirect or disable any bucket per shelter (e.g. a veteran-only shelter
  pointing "men" at `VETERAN`) via
  `scripts/set_shelter_sms_population_map.py` — see [`app/sms_population_map.py`](app/sms_population_map.py).
  Same "no admin UI yet, CLI script instead" situation as
  `coordinator_phone`.
- **The production API-key rate limit needs confirming before a pilot**
  — see "FABT integration" above.
- **Coordinator phone numbers are hash+encrypted, but the key isn't
  KMS-managed yet.** `coordinator_phone`/`sms_update_log` now store
  `phone_hash` (HMAC-SHA256 lookup) and `phone_encrypted` (AES-256-GCM,
  decrypted only to actually send a nudge SMS) instead of plaintext — see
  [`app/crypto.py`](app/crypto.py). Both keys are HKDF-derived from one
  operator secret, `BEDBOARD_PHONE_ENCRYPTION_KEY`; the app refuses to
  boot with the checked-in dev default when `BEDBOARD_USE_MOCK_FABT=false`
  (mirrors the same pattern the real FABT project uses for its own
  dev-only secrets). Real KMS/HSM-backed key management and rotation is
  still a real pilot's job, not this slice's.
- **DV opacity is only "excluded from listings."** The PRD's full design
  (time-limited referral token + warm handoff call) isn't built — this
  slice only proves the *invariant* (a DV shelter never appears in
  enumeration/wallboard output) holds at the mock-FABT layer.
- **"On-shift coordinator" nudging isn't shift-aware.** There's no real
  shift-schedule data yet, so BB-6 nudges *all* active coordinators for a
  shelter, not just the one on shift. Level-2 escalation does target
  `shift == "lead"` coordinators specifically.
- **Wallboard SSE is polling, not push.** It re-fetches every 30s
  (`BEDBOARD_WALLBOARD_REFRESH_SECONDS`) rather than pushing within 5s of
  a real change, which needs a pub/sub layer FABT doesn't have yet.
- **No Alembic migrations** — `init_db()` just calls
  `Base.metadata.create_all`. Fine for a pilot's 4 tables; add Alembic
  before schema changes get risky.
- **No auth on the API itself** beyond the FABT API-key pattern and
  Twilio signature validation (off by default locally,
  on via `BEDBOARD_TWILIO_VALIDATE_SIGNATURE`). OIDC/county-SSO for admin
  routes isn't built (there are no admin routes yet either).
- **SMS locale token choices are this build's own design**, not something
  the PRD specifies (Spanish M/H/F, Vietnamese N/D/G, both ASCII-only) —
  see the doc comment at the top of `app/sms/parser.py` before this goes
  in front of real Spanish/Vietnamese-speaking coordinators for review.

## Next steps toward the PRD's 6-weekend MVP

1. ~~Fork/deploy FABT's Lite tier, reconcile `HttpFabtClient` against its
   real API.~~ Done — see "FABT integration" above.
2. ~~Build the per-shelter SMS population map override.~~ Done — see
   `app/sms_population_map.py` and `scripts/set_shelter_sms_population_map.py`.
3. ~~Confirm the production API-key rate limit is actually raised.~~ Done
   — it wasn't; fixed upstream, see "Known gaps" above. Still worth
   double-checking at real deploy time that `SPRING_PROFILES_ACTIVE`
   includes `prod`.
4. Deploy FABT + this sidecar to a real (non-laptop) environment and
   create a real `COC_ADMIN` API key for it.
5. ~~Replace plaintext phone storage with hash+encrypt.~~ Done — see
   `app/crypto.py` and "Known gaps" above. Real KMS/HSM key management
   is the remaining piece.
6. Add Alembic, an admin UI for `coordinator_phone` and the SMS
   population-map overrides (replacing both CLI scripts), and the HMIS
   nightly export bridge.
7. Tabletop exercise with Here4You + 2 pilot shelters (PRD's pilot gate).
