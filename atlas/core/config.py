"""Central configuration. All secrets arrive via environment; nothing is read from DB or code."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas"
    redis_url: str = "redis://localhost:6379/0"
    trading_mode: str = "paper"  # 'paper' | 'live' — live additionally requires daily arming
    base_currency: str = "AUD"
    limit_mode: str = "small_aum"  # ADR-0001 decision 2
    daily_llm_budget_usd: float = 10.0  # cost circuit breaker (reasoning plane, Phase 2+)
    eodhd_api_key: str = ""  # empty -> fixture adapter is used
    # F-019/F-020: when true, the app REFUSES to start / marks itself unhealthy if
    # its DB runtime role is a PostgreSQL superuser (a superuser bypasses the audit
    # append-only + monotonic-anchor triggers via session_replication_role).
    # Production/.env sets it true and connects as the least-privilege atlas_app
    # role; dev/test leave it false (the disposable test DB connects as owner).
    db_require_least_privilege: bool = False


def get_settings() -> Settings:
    return Settings()
