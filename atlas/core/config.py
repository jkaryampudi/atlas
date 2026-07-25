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
    # F-019/F-020: SECURE BY DEFAULT (True). The app REFUSES to start if its DB
    # runtime role is a PostgreSQL superuser (a superuser can bypass the audit
    # triggers), so a deployment must connect as the least-privilege atlas_app role
    # (migration 0043) or knowingly opt out. The disposable test DB connects as the
    # owner and sets this False in tests/conftest.py. NOTE: this is defence in
    # depth — the audit wall itself is made replica-proof at the DB layer by the
    # ENABLE ALWAYS triggers (migration 0044), which the startup guard asserts
    # UNCONDITIONALLY regardless of this flag.
    db_require_least_privilege: bool = True


def get_settings() -> Settings:
    return Settings()
