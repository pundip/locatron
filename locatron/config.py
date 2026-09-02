"""Settings, loaded from environment or .env.

Scoring weights and fuzzy thresholds live here deliberately, so they can be
tuned on the box without a redeploy.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCATRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "locatron"
    mysql_password: str = ""
    mysql_database: str = "ReferenceDB"
    mysql_pool_size: int = 5
    mysql_pool_max_overflow: int = 10
    mysql_pool_recycle_seconds: int = 3600

    redis_url: str = "redis://127.0.0.1:6379/0"
    cache_ttl_seconds: int = 30 * 24 * 3600
    cache_ttl_negative_seconds: int = 7 * 24 * 3600
    cache_enabled: bool = True

    data_dir: str = "/var/lib/locatron"
    sqlite_path: str = "/var/lib/locatron/gazetteer.sqlite"

    root_path: str = "/locatron"
    log_level: str = "INFO"

    # Applied only to separate near-equal candidates, never to override a
    # country stated explicitly in the input.
    country_bias: str = "AUS"
    country_bias_weight: float = 0.05

    fuzzy_locality_min: int = Field(88, ge=0, le=100)
    fuzzy_street_min: int = Field(85, ge=0, le=100)
    fuzzy_city_min: int = Field(90, ge=0, le=100)

    @property
    def mysql_url(self) -> str:
        from urllib.parse import quote_plus

        pw = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://{self.mysql_user}:{pw}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            "?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
