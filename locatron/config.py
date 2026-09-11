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

    # --- scoring -------------------------------------------------------------
    # Base score by how the name was matched. Ordered: an exact canonical hit
    # should always outrank the same name reached through an alias or fuzz.
    score_exact: float = Field(0.80, ge=0.0, le=1.0)
    score_alias: float = Field(0.72, ge=0.0, le=1.0)
    score_qualifier_stripped: float = Field(0.74, ge=0.0, le=1.0)
    score_fuzzy_max: float = Field(0.62, ge=0.0, le=1.0)

    # Evidence stated in the input itself, added on top of the base score.
    score_explicit_country_bonus: float = Field(0.12, ge=0.0, le=1.0)
    score_explicit_admin1_bonus: float = Field(0.10, ge=0.0, le=1.0)
    score_postcode_bonus: float = Field(0.10, ge=0.0, le=1.0)

    # A bare country or state with no populated place named in the input can
    # never be as good as a city hit, so it starts lower.
    score_country_only: float = Field(0.70, ge=0.0, le=1.0)
    score_admin1_only: float = Field(0.74, ge=0.0, le=1.0)

    # Ambiguity. Dominance is the winner's share of size (population for world
    # cities, address_count for AU localities) against its nearest rival:
    #   dominance = winner / (winner + runner_up)
    # 0.5 means a dead heat, 1.0 means the runner-up is negligible. The penalty
    # scales linearly from full at a dead heat to nothing at total dominance.
    # This is what keeps Springfield honest without special-casing it.
    score_ambiguity_penalty_max: float = Field(0.34, ge=0.0, le=1.0)
    score_dominance_floor: float = Field(0.55, ge=0.5, le=1.0)

    # Runner-ups within this much of the winner are emitted as candidates.
    candidate_margin: float = Field(0.15, ge=0.0, le=1.0)
    candidate_max: int = Field(8, ge=0, le=50)

    # Below this, a resolved answer is still returned but flagged low-confidence
    # so the caller (and locatron_unresolved) can act on it.
    low_confidence_threshold: float = Field(0.55, ge=0.0, le=1.0)

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
