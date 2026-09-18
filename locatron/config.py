"""Settings, loaded from environment or .env.

The .env file is looked up in fixed places, never relative to the working
directory, because the checkout and the env file do not sit together on the
container (/opt/locatron/app vs /opt/locatron/.env). A cwd-relative lookup that
finds nothing falls back to every default silently, which shows up as a MySQL
"Connection refused" against 127.0.0.1 rather than a config error. The lookup
runs when Settings is constructed, not at import, so it cannot break an import.

Scoring weights and fuzzy thresholds live here deliberately, so they can be
tuned on the box without a redeploy.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module-level so tests can point them at tmp_path.
REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_ENV_FILE = Path("/opt/locatron/.env")
ENV_FILE_OVERRIDE_VAR = "LOCATRON_ENV_FILE"


def env_files() -> list[Path]:
    """Existing .env files, lowest precedence first.

    Order: repo root, /opt/locatron/.env, then $LOCATRON_ENV_FILE if set.
    pydantic-settings applies later files over earlier ones, and real
    environment variables override all of them.

    The working directory is deliberately not a candidate: it made behaviour
    depend on where the caller was standing, and a cwd of /root as the
    locatron user hit a PermissionError on /root/.env.
    """
    candidates = [REPO_ROOT / ".env", SYSTEM_ENV_FILE]
    override = os.environ.get(ENV_FILE_OVERRIDE_VAR)
    if override:
        candidates.append(Path(override))

    found: list[Path] = []
    for c in candidates:
        # is_file() raises PermissionError when a parent directory cannot be
        # searched. Any OSError means the candidate is unusable, not fatal.
        try:
            if not c.is_file():
                continue
            c = c.resolve()
        except OSError:
            continue
        # The override may name a file already listed. Keep the later
        # (higher-precedence) slot.
        if c in found:
            found.remove(c)
        found.append(c)
    return found


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCATRON_",
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
    # Per-token similarity floor. Jaro-Winkler weights the start of a string,
    # so every 'MELBOURNE <anything>' scores ~90 against both 'MELBOURNE' and
    # 'MELBOURNE AIRPORT'. A whole-string score cannot tell those apart; a
    # per-token one can. See _explains_every_token.
    fuzzy_token_min: int = Field(80, ge=0, le=100)

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
    # A country_bucket value that implies a country without naming one
    # ('The land down under'). Weaker than a stated country.
    score_country_implied: float = Field(0.62, ge=0.0, le=1.0)
    score_admin1_only: float = Field(0.74, ge=0.0, le=1.0)

    # Ambiguity. Dominance is the winner's share of size (population for world
    # cities, address_count for AU localities) against its nearest rival:
    #   dominance = winner / (winner + runner_up)
    # 0.5 means a dead heat, 1.0 means the runner-up is negligible. The penalty
    # is full at a dead heat and tapers to nothing at score_dominance_clear.
    # This is what keeps Springfield honest without special-casing it: six US
    # Springfields sit near 0.60 and land below low_confidence_threshold, while
    # Delhi IN at 0.9997 and Melbourne AU at 0.983 are clear outright.
    score_ambiguity_penalty_max: float = Field(0.34, ge=0.0, le=1.0)
    score_dominance_clear: float = Field(0.98, ge=0.5, le=1.0)

    # An Australian locality named with no state and no postcode is weaker
    # evidence than a world city of the same name: the locality gazetteer is
    # 18.5k mostly-obscure suburbs, Cities is populated places. Applied only
    # when the input carries no Australian signal at all.
    score_locality_unqualified_penalty: float = Field(0.08, ge=0.0, le=1.0)

    # Runner-ups within this much of the winner are emitted as candidates.
    candidate_margin: float = Field(0.15, ge=0.0, le=1.0)
    candidate_max: int = Field(8, ge=0, le=50)

    # Below this, a resolved answer is still returned but flagged low-confidence
    # so the caller (and locatron_unresolved) can act on it.
    low_confidence_threshold: float = Field(0.55, ge=0.0, le=1.0)

    # Ceiling for a gazetteer-level answer. Exact name plus a state plus a
    # postcode sums past 1.0 and saturates, which reports a locality lookup as
    # certainty. Above this is reserved for an exact G-NAF address match, which
    # identifies one property rather than a named area.
    score_max: float = Field(0.98, ge=0.0, le=1.0)

    def __init__(self, **values: Any) -> None:
        # Resolve env files at construction rather than in model_config, which
        # would evaluate them once at import. An explicit _env_file still wins.
        values.setdefault("_env_file", env_files())
        super().__init__(**values)

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
