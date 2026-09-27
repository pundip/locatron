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
cat > "$STUBS/systemctl" <<'SCSTUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
for arg in "$@"; do
    case "$arg" in
        is-active) echo "${STUB_SERVICE_STATE:-active}"; exit 0 ;;
        show)      echo "${STUB_STARTED_UTC:-}"; exit 0 ;;
    esac
done
exit 0
SCSTUB
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

# Outside the checkout, as it is on the container.
DSHA="$WORK/.deployed-sha"

head_sha() { git -C "$APP" rev-parse HEAD; }

# Record a completed deploy, the way a successful run would.
mark_deployed() { printf '%s\n' "${1:-$(head_sha)}" > "$DSHA"; }

# The restart step needs a unit installed and the module it points at to exist.
# Without both it skips the service, and nothing is ever recorded as deployed.
enable_services() {
    mkdir -p "$APP/locatron/api" "$APP/locatron/bulk"
    : > "$APP/locatron/api/app.py"
    : > "$APP/locatron/bulk/app.py"
    cp "$APP"/deploy/systemd/locatron-*.service "$SYSD/"
    git -C "$APP" add -A >/dev/null 2>&1
    git -C "$APP" commit -qm services >/dev/null 2>&1
    git -C "$APP" push -q origin master >/dev/null 2>&1 || true
    git -C "$APP" fetch -q origin 2>/dev/null
}

run_deploy() {
    LOCATRON_APP_DIR="$APP" \
    LOCATRON_VENV="$WORK/venv" \
    LOCATRON_ENV_FILE="$ENVF" \
    LOCATRON_SYSTEMD_DIR="$SYSD" \
    LOCATRON_NGINX_SITE="$NGX" \
    LOCATRON_DEPLOYED_SHA_FILE="$DSHA" \
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
    """The shortcut still exists, for the one case that earns it: origin, the
    checkout and the deployed record all agree, and the mirror is current."""
    rc, out, calls = _run_fetch(
        r"""
mark_deployed
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
mark_deployed
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
mark_deployed
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
mark_deployed
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
mark_deployed
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


# ---------------------------------------------------------------------------
# What is deployed, as opposed to what has been fetched
# ---------------------------------------------------------------------------


def test_a_fetch_that_never_restarted_does_not_count_as_deployed() -> None:
    """The bug that left production a day behind.

    A deploy fetched the commit and then failed before the restart. The checkout
    matched origin, so the shortcut said "nothing to do" on every later run while
    the services kept serving the previous commit -- and nothing in the repo could
    tell you, because the checkout looked perfect.

    Here the deployed record names an older commit, which is exactly the state
    that failed deploy leaves behind. The run must not shortcut.
    """
    rc, out, calls = _run_fetch(
        r"""
enable_services
mark_deployed 0000000000000000000000000000000000000000
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=0
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "nothing to do" not in out
    assert "the last completed deploy was 0000000" in out
    assert "fetched but never restarted into service" in out
    assert "systemctl restart locatron-api" in calls


def test_no_deployed_record_at_all_means_deploy() -> None:
    """A missing file is not evidence of anything. It is the state on the first
    run after this record was introduced, and also what a deploy that died early
    leaves behind."""
    rc, out, calls = _run_fetch(
        r"""
enable_services
rm -f "$DSHA"
: > "$MIRROR_PATH"
export CHECK_MIRROR_RC=0 DIGEST_RC=0
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "nothing to do" not in out
    assert "no record of a completed deploy" in out
    assert "systemctl restart locatron-api" in calls


def test_a_successful_restart_records_the_sha_outside_the_checkout() -> None:
    rc, out, _ = _run_fetch(
        r"""
enable_services
rm -f "$DSHA"
: > "$MIRROR_PATH"
rc=0; run_deploy || rc=$?
echo "recorded=$(cat "$DSHA" 2>/dev/null)"
echo "head=$(head_sha)"
echo "inside_checkout=$(git -C "$APP" status --porcelain --ignored | grep -c deployed-sha)"
report "$rc"
"""
    )
    assert rc == 0, out
    recorded = next(x for x in out.splitlines() if x.startswith("recorded="))[len("recorded=") :]
    head = next(x for x in out.splitlines() if x.startswith("head="))[len("head=") :]
    assert recorded == head, out
    assert "inside_checkout=0" in out, "the record must live outside the checkout"


def test_the_record_is_written_only_after_the_services_are_verified_up() -> None:
    """A unit that restarts and then dies has not deployed anything. The deploy
    fails, and the record keeps naming the commit that is actually in service.

    `journalctl` is deliberately not stubbed here, so this also covers the deploy
    reaching its own error message when the journal cannot be read: under pipefail
    a failing `journalctl | sed` used to abort the script first, losing the only
    line that said which service died.
    """
    rc, out, _ = _run_fetch(
        r"""
enable_services
mark_deployed 1111111111111111111111111111111111111111
: > "$MIRROR_PATH"
export STUB_SERVICE_STATE=failed
rc=0; run_deploy || rc=$?
echo "still=$(cat "$DSHA" 2>/dev/null)"
report "$rc"
"""
    )
    assert rc != 0, out
    assert "did not come up" in out
    assert "still=1111111111111111111111111111111111111111" in out, (
        "a failed restart must not overwrite the record"
    )


def test_a_second_run_after_a_real_deploy_does_nothing() -> None:
    """The shortcut has to still work, or every deploy does the full job forever.
    Two runs back to back: the first deploys and records, the second stops."""
    rc, out, calls = _run_fetch(
        r"""
enable_services
rm -f "$DSHA"
: > "$MIRROR_PATH"
run_deploy > "$WORK/first.log" 2>&1; echo "first=$?"
grep -c 'systemctl restart' "$WORK/first.log" > /dev/null
: > "$STUB_LOG"
rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "first=0" in out
    assert "deployed, and the mirror is current, nothing to do" in out
    assert "systemctl restart" not in calls


def test_status_reports_both_shas_without_deploying() -> None:
    rc, out, calls = _run_fetch(
        r"""
enable_services
mark_deployed 2222222222222222222222222222222222222222
rc=0; run_deploy --status || rc=$?
report "$rc"
"""
    )
    assert rc != 0, "drift must be reported through the exit code too"
    assert "checkout  " in out
    assert "deployed  2222222" in out
    assert "the checkout is ahead of what was deployed" in out
    assert "Stale:" in out
    assert "systemctl restart" not in calls
    assert "uv pip install" not in calls


def test_status_warns_when_the_service_predates_the_deployed_commit() -> None:
    """The other half of the same failure: the SHA can be right while the process
    running it is older than the commit. A service that started in 2020 is not
    running a commit made today, whatever the record says."""
    rc, out, _ = _run_fetch(
        r"""
enable_services
mark_deployed
export STUB_STARTED_UTC="2020-01-01 00:00:00 UTC"
rc=0; run_deploy --status || rc=$?
report "$rc"
"""
    )
    assert rc != 0, out
    assert "started 2020-01-01" in out
    assert "was committed, so it is running older code" in out


def test_status_is_quiet_when_everything_agrees() -> None:
    rc, out, _ = _run_fetch(
        r"""
enable_services
mark_deployed
export STUB_STARTED_UTC="2099-01-01 00:00:00 UTC"
rc=0; run_deploy --status || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "Current" in out
    assert "WARNING" not in out


# ---------------------------------------------------------------------------
# deploy.sh updating itself mid-run
# ---------------------------------------------------------------------------


def test_a_fetch_that_changes_deploy_sh_hands_over_to_the_new_copy() -> None:
    """bash reads a script incrementally, so a fetch that rewrites deploy.sh
    underneath a running deploy leaves the rest of the run a mix of old and new
    lines.

    That is not hypothetical: the first live deploy of the .deployed-sha change
    fetched the version that records the SHA and then finished on the tail bash
    had already buffered, so no record was written and the next run reported
    nothing to do.

    Here origin carries a deploy.sh with an extra marker line. The running copy
    must hand over rather than finish itself.
    """
    rc, out, _ = _run_fetch(
        r"""
enable_services
rm -f "$DSHA"
: > "$MIRROR_PATH"

# A commit on origin that changes deploy.sh, exactly as a real deploy would.
ORIGIN="$WORK/origin"
git clone -q --bare "$APP" "$ORIGIN"
git -C "$APP" remote set-url origin "$ORIGIN"
CLONE="$WORK/clone"
git clone -q "$ORIGIN" "$CLONE"
git -C "$CLONE" config user.email t@t
git -C "$CLONE" config user.name t
printf '\ninfo "MARKER from the new copy"\n' >> "$CLONE/deploy/deploy.sh"
git -C "$CLONE" commit -qam "change deploy.sh"
git -C "$CLONE" push -q origin master

rc=0; run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "deploy.sh changed in this fetch, re-executing the new version" in out
    # Proof the new copy ran: the marker only exists in the fetched version.
    assert "MARKER from the new copy" in out
    # And the run it handed over to completed, which the broken case never did.
    assert "recorded" in out


def test_the_hand_over_passes_the_original_arguments_on() -> None:
    """--branch, --skip-tests and the rest have to survive the hand-over, or the
    new copy deploys something other than what was asked for.

    The marker reads ORIGINAL_ARGS rather than $*, because by the time any line
    near the end of the script runs, the parse loop has shifted $@ away -- which
    is exactly why the arguments have to be saved up front to be passed on.
    """
    rc, out, calls = _run_fetch(
        r"""
enable_services
rm -f "$DSHA"
: > "$MIRROR_PATH"
ORIGIN="$WORK/origin"
git clone -q --bare "$APP" "$ORIGIN"
git -C "$APP" remote set-url origin "$ORIGIN"
CLONE="$WORK/clone"
git clone -q "$ORIGIN" "$CLONE"
git -C "$CLONE" config user.email t@t
git -C "$CLONE" config user.name t
printf '\ninfo "MARKER args=${ORIGINAL_ARGS[*]}"\n' >> "$CLONE/deploy/deploy.sh"
git -C "$CLONE" commit -qam "change deploy.sh"
git -C "$CLONE" push -q origin master

rc=0; run_deploy --no-mirror --branch master || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "MARKER args=--no-mirror --branch master" in out


def test_the_hand_over_does_not_loop() -> None:
    """A guard against the obvious way to get this wrong. With the marker already
    set, a changed deploy.sh must carry on rather than exec itself forever."""
    rc, out, _ = _run_fetch(
        r"""
enable_services
mark_deployed
: > "$MIRROR_PATH"
# Pretend a hand-over already happened, and make the copy differ from disk by
# editing the file after the run has started reading it is not possible here --
# so drive the guard directly with a checkout whose deploy.sh differs from HEAD.
ORIGIN="$WORK/origin"
git clone -q --bare "$APP" "$ORIGIN"
git -C "$APP" remote set-url origin "$ORIGIN"
CLONE="$WORK/clone"
git clone -q "$ORIGIN" "$CLONE"
git -C "$CLONE" config user.email t@t
git -C "$CLONE" config user.name t
printf '\ninfo "MARKER second copy"\n' >> "$CLONE/deploy/deploy.sh"
git -C "$CLONE" commit -qam "change deploy.sh"
git -C "$CLONE" push -q origin master

rc=0; LOCATRON_DEPLOY_REEXECED=1 run_deploy || rc=$?
report "$rc"
"""
    )
    assert rc == 0, out
    assert "changed again after the hand-over, continuing with this copy" in out
    assert "re-executing the new version" not in out
    assert "MARKER second copy" not in out, "the old copy must finish the run"
