"""deploy/deploy.sh config-installation tests.

These drive the real script through `--config-only`, with `systemctl`, `nginx`
and `sudo` replaced by stubs on PATH. `sudo` passes straight through, so the
genuine `install`/`cmp`/`sed` work happens against scratch directories, while
every reload is recorded rather than performed.

The cases worth pinning down are the ones that used to be done by hand: a
deploy with no config change must touch nothing, and a config that fails
`nginx -t` must not be left installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _repo_posix() -> str:
    """The repo path as bash sees it (/c/dev/... under Git Bash)."""
    out = subprocess.run(
        ["bash", "-c", 'cygpath -u "$1" 2>/dev/null || printf %s "$1"', "bash", str(REPO)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


PROLOGUE = r"""
set -uo pipefail
REPO="$1"

WORK=$(mktemp -d)
STUBS="$WORK/stubs";  mkdir -p "$STUBS"
APP="$WORK/app";      mkdir -p "$APP/deploy/systemd"
SYSD="$WORK/systemd"; mkdir -p "$SYSD"
NGX="$WORK/nginx-site"
export STUB_LOG="$WORK/calls.log"; : > "$STUB_LOG"

# The deploy stages into TMPDIR; point it somewhere countable.
export TMPDIR="$WORK/tmp"; mkdir -p "$TMPDIR"

cp "$REPO"/deploy/systemd/locatron-*.service "$APP/deploy/systemd/"
cp "$REPO"/deploy/nginx.conf "$APP/deploy/"
cp "$REPO"/deploy/deploy.sh  "$APP/deploy/"

printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUBS/sudo"
printf '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n' > "$STUBS/systemctl"
printf '#!/usr/bin/env bash\necho "nginx $*" >> "$STUB_LOG"\nexit "${STUB_NGINX_RC:-0}"\n' \
    > "$STUBS/nginx"
chmod +x "$STUBS/sudo" "$STUBS/systemctl" "$STUBS/nginx"
export PATH="$STUBS:$PATH"

EDGE_VALUE=abc123def4567890

# Anything the deploy staged and failed to clean up.
leftover_tmp() {
    find "$TMPDIR" -maxdepth 1 -name 'locatron-deploy.*' | wc -l | tr -d ' '
}

run_deploy() {
    LOCATRON_APP_DIR="$APP" \
    LOCATRON_SYSTEMD_DIR="$SYSD" \
    LOCATRON_NGINX_SITE="$NGX" \
    bash "$APP/deploy/deploy.sh" "$@" 2>&1
}

report() {
    printf '\n----RC----\n%s\n----CALLS----\n' "$1"
    cat "$STUB_LOG"
    printf '%s\n' '----END----'
}

# Leaves the live files as a previous deploy would: units byte-identical to the
# repo, nginx.conf carrying a real secret in place of the repo's REPLACE_ME.
put_in_sync() {
    cp "$APP"/deploy/systemd/locatron-*.service "$SYSD/"
    sed "s/REPLACE_ME/$EDGE_VALUE/g" "$APP/deploy/nginx.conf" > "$NGX"
}
"""


def _run(scenario: str) -> tuple[int, str, str]:
    """Run a scenario, returning (deploy rc, deploy output, recorded calls)."""
    proc = subprocess.run(
        ["bash", "-c", PROLOGUE + scenario, "bash", _repo_posix()],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    body, _, rest = proc.stdout.partition("----RC----")
    rc_text, _, rest = rest.partition("----CALLS----")
    calls, _, _ = rest.partition("----END----")
    return int(rc_text.strip()), body, calls.strip()


IN_SYNC = r"""
put_in_sync
rc=0; run_deploy --config-only || rc=$?
report "$rc"
"""


def test_no_config_changes_installs_nothing_and_reloads_nothing() -> None:
    """The requested case: everything already matches, so nothing happens."""
    rc, out, calls = _run(IN_SYNC)

    assert rc == 0, out
    assert "locatron-api.service unchanged" in out
    assert "locatron-bulk.service unchanged" in out
    assert "nginx.conf unchanged" in out
    assert ".service installed" not in out
    assert "nginx.conf installed" not in out

    # Nothing reloaded, restarted, or even tested.
    assert calls == "", f"expected no privileged calls, got:\n{calls}"


def test_no_config_changes_leaves_the_live_secret_alone() -> None:
    """A no-op run must not rewrite the file, secret included."""
    rc, out, _ = _run(
        r"""
put_in_sync
rc=0; run_deploy --config-only || rc=$?
grep -c REPLACE_ME "$NGX" | sed 's/^/placeholders=/'
grep -o "$EDGE_VALUE" "$NGX" | head -n1 | sed 's/^/recovered=/'
report "$rc"
"""
    )
    assert rc == 0
    assert "placeholders=0" in out
    assert "recovered=abc123def4567890" in out


def test_changed_unit_is_installed_and_daemon_reloaded_once() -> None:
    rc, out, calls = _run(
        r"""
put_in_sync
echo "# drifted" >> "$SYSD/locatron-api.service"
rc=0; run_deploy --config-only || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "locatron-api.service installed" in out
    assert "locatron-bulk.service unchanged" in out
    # One reload for the whole batch, and nginx left alone.
    assert calls.splitlines() == ["systemctl daemon-reload"]


def test_changed_nginx_is_tested_then_reloaded() -> None:
    rc, out, calls = _run(
        r"""
put_in_sync
echo "# drifted" >> "$NGX"
rc=0; run_deploy --config-only || rc=$?
grep -c REPLACE_ME "$NGX" | sed 's/^/placeholders=/'
report "$rc"
"""
    )
    assert rc == 0, out
    assert "nginx.conf installed" in out
    assert "nginx reloaded" in out
    # Tested before reloading, and the secret survived the reinstall.
    assert calls.splitlines() == ["nginx -t", "systemctl reload nginx"]
    assert "placeholders=0" in out


def test_failed_nginx_test_restores_the_previous_config() -> None:
    """A broken config must never be left in place."""
    rc, out, calls = _run(
        r"""
put_in_sync
echo "# the live config, which must come back" >> "$NGX"
cp "$NGX" "$WORK/expected"
export STUB_NGINX_RC=1
rc=0; run_deploy --config-only || rc=$?
if cmp -s "$NGX" "$WORK/expected"; then echo "restored=yes"; else echo "restored=no"; fi
report "$rc"
"""
    )
    assert rc != 0, "a failed nginx -t must exit non-zero"
    assert "restoring the previous config" in out
    assert "restored=yes" in out, "the previous config was not put back"
    # Reloaded after the restore, so what is running matches what is on disk.
    assert calls.splitlines() == ["nginx -t", "systemctl reload nginx"]


def test_refuses_to_install_placeholder_when_no_secret_to_recover() -> None:
    """REPLACE_ME must never reach the live config: it 444s all edge traffic."""
    rc, out, calls = _run(
        r"""
cp "$APP"/deploy/systemd/locatron-*.service "$SYSD/"
rm -f "$NGX"
rc=0; run_deploy --config-only || rc=$?
if [[ -f "$NGX" ]]; then echo "live=written"; else echo "live=absent"; fi
report "$rc"
"""
    )
    assert rc != 0
    assert "Refusing to write REPLACE_ME" in out
    assert "live=absent" in out, "refused, yet something was still written"
    assert "nginx" not in calls


def test_placeholder_in_installed_file_does_not_count_as_a_secret() -> None:
    """An installed config still carrying REPLACE_ME has nothing to recover."""
    rc, out, _ = _run(
        r"""
cp "$APP"/deploy/systemd/locatron-*.service "$SYSD/"
cp "$APP/deploy/nginx.conf" "$NGX"
rc=0; run_deploy --config-only || rc=$?
report "$rc"
"""
    )
    assert rc != 0
    assert "Refusing to write REPLACE_ME" in out


def test_missing_unit_file_is_installed_rather_than_skipped() -> None:
    """A never-installed unit is a change, not an absence."""
    rc, out, calls = _run(
        r"""
put_in_sync
rm -f "$SYSD/locatron-bulk.service"
rc=0; run_deploy --config-only || rc=$?
if [[ -f "$SYSD/locatron-bulk.service" ]]; then echo "bulk=present"; else echo "bulk=missing"; fi
report "$rc"
"""
    )
    assert rc == 0, out
    assert "locatron-bulk.service installed" in out
    assert "bulk=present" in out
    assert calls.splitlines() == ["systemctl daemon-reload"]


def test_flag_parsing() -> None:
    """--no-config parses, conflicts are caught, and junk is still rejected.

    The --no-config skip path itself is not reachable through --config-only, so
    this covers the parsing rather than the skip.
    """
    rc, out, _ = _run(
        r"""
run_deploy --no-config --help >/dev/null 2>&1; echo "no_config_parses=$?"
run_deploy --no-config --config-only >/dev/null 2>&1; echo "mutually_exclusive=$?"
run_deploy --nonsense >/dev/null 2>&1; echo "unknown=$?"
report 0
"""
    )
    assert rc == 0
    assert "no_config_parses=0" in out
    assert "mutually_exclusive=1" in out
    assert "unknown=1" in out


def test_staging_is_cleaned_up_after_a_successful_install() -> None:
    rc, out, _ = _run(
        r"""
put_in_sync
echo "# drifted" >> "$NGX"
rc=0; run_deploy --config-only || rc=$?
echo "leftover=$(leftover_tmp)"
report "$rc"
"""
    )
    assert rc == 0, out
    assert "nginx.conf installed" in out
    assert "leftover=0" in out, "staging file survived a successful run"


def test_staging_is_cleaned_up_when_the_run_fails() -> None:
    """The trap, not the happy path, is what has to remove these."""
    rc, out, _ = _run(
        r"""
put_in_sync
echo "# drifted" >> "$NGX"
export STUB_NGINX_RC=1
rc=0; run_deploy --config-only || rc=$?
echo "leftover=$(leftover_tmp)"
report "$rc"
"""
    )
    assert rc != 0, "nginx -t failed, so the run must fail"
    assert "leftover=0" in out, "staging file survived a failed run"


def test_staging_is_cleaned_up_when_the_install_is_refused() -> None:
    """The die() before any install must not leak its staging file either."""
    rc, out, _ = _run(
        r"""
cp "$APP"/deploy/systemd/locatron-*.service "$SYSD/"
rm -f "$NGX"
rc=0; run_deploy --config-only || rc=$?
echo "leftover=$(leftover_tmp)"
report "$rc"
"""
    )
    assert rc != 0
    assert "Refusing to write REPLACE_ME" in out
    assert "leftover=0" in out, "staging file survived a refused install"


def test_nothing_is_staged_inside_the_nginx_config_directory() -> None:
    """Staging must never land where nginx might read it."""
    rc, out, _ = _run(
        r"""
mkdir -p "$WORK/nginx/sites-available" "$WORK/nginx/sites-enabled"
NGX="$WORK/nginx/sites-available/locatron"
put_in_sync
echo "# drifted" >> "$NGX"
rc=0; run_deploy --config-only || rc=$?
echo "available=$(find "$WORK/nginx/sites-available" -type f | wc -l | tr -d ' ')"
echo "enabled=$(find "$WORK/nginx/sites-enabled" -type f | wc -l | tr -d ' ')"
report "$rc"
"""
    )
    assert rc == 0, out
    # Only the config itself, and nothing at all in sites-enabled.
    assert "available=1" in out
    assert "enabled=0" in out
