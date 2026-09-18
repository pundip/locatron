"""Env file discovery.

The failure this guards against is silent: a missed .env falls back to every
default, and mysql_host=127.0.0.1 looks like a network problem.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from locatron import config


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.setattr(config, "SYSTEM_ENV_FILE", tmp_path / "opt" / ".env")
    monkeypatch.delenv(config.ENV_FILE_OVERRIDE_VAR, raising=False)
    monkeypatch.delenv("LOCATRON_MYSQL_HOST", raising=False)
    config.get_settings.cache_clear()
    yield repo
    config.get_settings.cache_clear()


def _write_env(path: Path, host: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"LOCATRON_MYSQL_HOST={host}\n", encoding="utf-8")
    return path


def test_repo_root_env_found_from_other_cwd(
    isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_env(isolated / ".env", "repo-host")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert config.env_files() == [env.resolve()]
    assert config.get_settings().mysql_host == "repo-host"


def test_override_var_beats_repo_root(
    isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(isolated / ".env", "repo-host")
    override = _write_env(tmp_path / "custom" / "locatron.env", "override-host")
    monkeypatch.setenv(config.ENV_FILE_OVERRIDE_VAR, str(override))
    monkeypatch.chdir(tmp_path)

    assert config.env_files()[-1] == override.resolve()
    assert config.get_settings().mysql_host == "override-host"


def test_real_env_var_beats_file(
    isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(isolated / ".env", "repo-host")
    monkeypatch.setenv("LOCATRON_MYSQL_HOST", "env-host")
    monkeypatch.chdir(tmp_path)

    assert config.get_settings().mysql_host == "env-host"


def test_no_env_file_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert config.env_files() == []
    assert config.get_settings().mysql_host == "127.0.0.1"


def test_cwd_env_is_not_a_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env", "cwd-host")
    monkeypatch.chdir(tmp_path)

    assert config.env_files() == []
    assert config.get_settings().mysql_host == "127.0.0.1"


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions, and root ignores permission bits",
)
def test_unreadable_candidate_dir_is_skipped(
    isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The container case: /root/.env from a non-root user. Stat of a file
    # inside a mode-000 directory raises PermissionError from is_file().
    env = _write_env(isolated / ".env", "repo-host")
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setattr(config, "SYSTEM_ENV_FILE", locked / ".env")
    monkeypatch.setenv(config.ENV_FILE_OVERRIDE_VAR, str(locked / "override.env"))
    locked.chmod(0)
    try:
        assert config.env_files() == [env.resolve()]
        assert config.get_settings().mysql_host == "repo-host"
    finally:
        locked.chmod(stat.S_IRWXU)


def test_oserror_from_candidate_is_skipped(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Portable version of the above, so Windows runs still cover the handling.
    _write_env(isolated / ".env", "repo-host")

    def denied(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_file", denied)

    assert config.env_files() == []
