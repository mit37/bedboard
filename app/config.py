from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BEDBOARD_", extra="ignore")

    # --- FABT upstream API ---
    fabt_api_base_url: str = "http://localhost:8000"
    # Real FABT auth (reconciled against the actual API, see
    # app/fabt_client.py): a COC_ADMIN-scoped API key sent as the
    # `X-API-Key` header, created via `POST /api/v1/api-keys` with
    # `shelterId: null` by a human FABT admin -- not a Bearer JWT.
    fabt_api_key: str = "dev-api-key"
    # FABT's real API derives tenant scope entirely from the API key
    # server-side (no tenant_id in requests or responses) -- this is used
    # only to label ShelterSummary.tenant_id locally, never sent to FABT.
    fabt_tenant_id: str = "unknown-tenant"

    # --- Database (sidecar tables only) ---
    database_url: str = "sqlite+aiosqlite:///./bedboard.db"

    # --- Twilio ---
    twilio_account_sid: str = "dev-account-sid"
    twilio_auth_token: str = "dev-auth-token"
    twilio_from_number: str = "+10000000000"
    # When true, inbound webhook signature validation is skipped (local dev/tests only).
    twilio_validate_signature: bool = False

    # --- Nudge scheduler ---
    nudge_timezone: str = "America/Los_Angeles"
    nudge_check_hours: tuple[int, ...] = (17, 21)  # 5pm and 9pm local
    nudge_escalation_minutes: int = 30
    stale_hours_threshold: float = 8.0
    aging_hours_threshold: float = 2.0

    # --- Wallboard ---
    wallboard_refresh_seconds: int = 30

    default_locale: str = "en"
    supported_locales: tuple[str, ...] = ("en", "es", "vi")

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.nudge_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
