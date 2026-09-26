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


# ---------------------------------------------------------------------------
# the "already at <sha>" shortcut and the street mirror
# ---------------------------------------------------------------------------
#
# These drive the real fetch path, which needs a git checkout, a venv and an env
# file. All three are faked in the scenario. The venv's python and locatron are
# stubs that answer the three questions the mirror check asks, so a test can put
# the mirror in any state without building a 55 MB file.

FETCH_PROLOGUE = r"""
set -uo pipefail
REPO="$1"

WORK=$(mktemp -d)
STUBS="$WORK/stubs";  mkdir -p "$STUBS"
APP="$WORK/app"
VENVDIR="$WORK/venv/bin"; mkdir -p "$VENVDIR"
SYSD="$WORK/systemd"; mkdir -p "$SYSD"
NGX="$WORK/nginx-site"
MIRRORDIR="$WORK/mirrordata"; mkdir -p "$MIRRORDIR"
export STUB_LOG="$WORK/calls.log"; : > "$STUB_LOG"

# A checkout that is already up to date with its own origin.
mkdir -p "$APP/deploy"
cp "$REPO"/deploy/deploy.sh "$APP/deploy/"
cp "$REPO"/deploy/nginx.conf "$APP/deploy/" 2>/dev/null || true
mkdir -p "$APP/deploy/systemd"
cp "$REPO"/deploy/systemd/locatron-*.service "$APP/deploy/systemd/" 2>/dev/null || true
git -C "$APP" init -q
git -C "$APP" config user.email t@t
git -C "$APP" config user.name t
git -C "$APP" add -A
git -C "$APP" commit -qm base
git -C "$APP" branch -M master
git -C "$APP" remote add origin "$APP"
git -C "$APP" fetch -q origin 2>/dev/null

ENVF="$WORK/env"; echo "# empty" > "$ENVF"

# A live nginx config carrying a real secret, so the install step behaves as it
# would on the container rather than refusing over REPLACE_ME.
sed "s/REPLACE_ME/abc123def4567890/g" "$REPO/deploy/nginx.conf" > "$NGX"

# Stubs. sudo passes through; the rest record and obey the env.
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUBS/sudo"
printf '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n' > "$STUBS/systemctl"
printf '#!/usr/bin/env bash\necho "nginx $*" >> "$STUB_LOG"\nexit 0\n' > "$STUBS/nginx"
printf '#!/usr/bin/env bash\necho "uv $*" >> "$STUB_LOG"\n' > "$STUBS/uv"
# The service user does not exist here, so ownership calls are recorded only.
printf '#!/usr/bin/env bash\necho "chown $*" >> "$STUB_LOG"\n' > "$STUBS/chown"
printf '#!/usr/bin/env bash\necho "chmod $*" >> "$STUB_LOG"\n' > "$STUBS/chmod"
chmod +x "$STUBS"/sudo "$STUBS"/systemctl "$STUBS"/nginx "$STUBS"/uv
chmod +x "$STUBS"/chown "$STUBS"/chmod
export PATH="$STUBS:$PATH"

# The venv's python answers the three mirror questions.
#   MIRROR_PATH      what sqlite_file resolves to
#   CHECK_MIRROR_RC  exit code for local.check_mirror()
#   DIGEST_RC        0 match, 1 differ, 2 could not tell
cat > "$VENVDIR/python" <<'PYSTUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
    case "$2" in
        *sqlite_file*) echo "$MIRROR_PATH"; exit 0 ;;
        *check_mirror*) echo "python check_mirror" >> "$STUB_LOG"; exit "${CHECK_MIRROR_RC:-0}" ;;
    esac
fi
if [[ "${1:-}" == "-" ]]; then
    cat > /dev/null
    echo "python digest" >> "$STUB_LOG"
    exit "${DIGEST_RC:-0}"
fi
echo "python $*" >> "$STUB_LOG"
PYSTUB
cat > "$VENVDIR/locatron" <<'LCSTUB'
#!/usr/bin/env bash
echo "locatron $*" >> "$STUB_LOG"
if [[ "${1:-}" == "build" ]]; then
    : > "$MIRROR_PATH"
fi
exit 0
LCSTUB
chmod +x "$VENVDIR/python" "$VENVDIR/locatron"

export MIRROR_PATH="$MIRRORDIR/gazetteer.sqlite"

run_deploy() {
    LOCATRON_APP_DIR="$APP" \
    LOCATRON_VENV="$WORK/venv" \
    LOCATRON_ENV_FILE="$ENVF" \
    LOCATRON_SYSTEMD_DIR="$SYSD" \
    LOCATRON_NGINX_SITE="$NGX" \
    LOCATRON_BRANCH=master \
    bash "$APP/deploy/deploy.sh" "$@" 2>&1
}

report() {
    printf '\n----RC----\n%s\n----CALLS----\n' "$1"
    cat "$STUB_LOG"
    printf '%s\n' '----END----'
}
"""


def _run_fetch(scenario: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["bash", "-c", FETCH_PROLOGUE + scenario, "bash", _repo_posix()],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    body, _, rest = proc.stdout.partition("----RC----")
    rc_text, _, rest = rest.partition("----CALLS----")
    calls, _, _ = rest.partition("----END----")
    return int(rc_text.strip()), body, calls.strip()


def test_up_to_date_with_a_fresh_mirror_exits_early() -> None:
    """The shortcut still exists. Nothing is rebuilt and nothing restarts."""
    rc, out, calls = _run_fetch(
        r"""
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=0
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "the mirror is current, nothing to do" in out
    assert "locatron build streets" not in calls
    assert "systemctl restart" not in calls


def test_up_to_date_with_a_missing_mirror_builds_and_restarts() -> None:
    """The bug: no new commit does not mean nothing to do."""
    rc, out, calls = _run_fetch(
        r"""
rm -f "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=0
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "but the street mirror is missing" in out
    assert "locatron build streets" in calls


def test_up_to_date_with_a_stale_mirror_builds() -> None:
    """A NORM_VERSION mismatch is a rebuild reason with no commit involved."""
    rc, out, calls = _run_fetch(
        r"""
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=1 DIGEST_RC=0
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "stale or unreadable" in out
    assert "locatron build streets" in calls


def test_up_to_date_with_a_changed_source_digest_builds() -> None:
    """locatron_street rebuilt in MySQL, no commit. This is the case the
    shortcut used to hide."""
    rc, out, calls = _run_fetch(
        r"""
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=1
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "source digest changed" in out
    assert "locatron build streets" in calls


def test_an_unreachable_database_does_not_force_a_rebuild() -> None:
    """Exit 2 means could-not-tell. Treating it as changed would rebuild and
    restart services over a network blip."""
    rc, out, calls = _run_fetch(
        r"""
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=2
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "could not compare the source digest" in out
    assert "nothing to do" in out
    assert "locatron build streets" not in calls


def test_no_mirror_flag_keeps_the_old_shortcut() -> None:
    rc, out, calls = _run_fetch(
        r"""
rm -f "$MIRROR_PATH"
rc=0; run_deploy --no-mirror || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "nothing to do" in out
    assert "locatron build streets" not in calls


def test_the_digest_is_computed_once_not_twice() -> None:
    """Both the shortcut and the mirror step ask. The answer is cached, because
    the digest is a full scan of 532k rows."""
    rc, out, calls = _run_fetch(
        r"""
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=1
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert calls.count("python digest") == 1, calls
