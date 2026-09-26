#!/usr/bin/env bash
#
# deploy/deploy.sh — update Locatron on the container.
#
#   sudo -u locatron /opt/locatron/app/deploy/deploy.sh
#   /opt/locatron/app/deploy/deploy.sh --branch dev
#   /opt/locatron/app/deploy/deploy.sh --skip-tests
#   /opt/locatron/app/deploy/deploy.sh --no-config
#   /opt/locatron/app/deploy/deploy.sh --no-mirror
#   /opt/locatron/app/deploy/deploy.sh --config-only
#
# Assumes deploy/install.sh has already run. If the checkout or venv does not
# exist yet, run install.sh instead.
#
# Works as root or as the locatron user. As root it runs git and uv through
# runuser so the checkout does not end up with root-owned files, which would
# break the next run as locatron.
#
# Fully non-interactive. Nothing here prompts.
#
# Installs deploy/systemd/locatron-*.service and deploy/nginx.conf whenever they
# differ from what is live, before anything is restarted. --no-config skips that
# step; --config-only runs only that step and touches no code.
#
# Rebuilds the SQLite street mirror when it is missing, stale or out of step
# with locatron_street, before services restart. --no-mirror skips it.
#
# Roll back: the previous commit is printed at the end.
#     git -C /opt/locatron/app reset --hard <sha>
#     /opt/locatron/app/deploy/deploy.sh --force

set -euo pipefail

APP_USER="${LOCATRON_USER:-locatron}"
APP_DIR="${LOCATRON_APP_DIR:-/opt/locatron/app}"
VENV="${LOCATRON_VENV:-/opt/locatron/venv}"
ENV_FILE="${LOCATRON_ENV_FILE:-/opt/locatron/.env}"
BRANCH="${LOCATRON_BRANCH:-main}"

# Where the config files go. Overridable so the test suite can point them at a
# scratch directory instead of /etc.
SYSTEMD_DIR="${LOCATRON_SYSTEMD_DIR:-/etc/systemd/system}"
NGINX_SITE="${LOCATRON_NGINX_SITE:-/etc/nginx/sites-available/locatron}"

SKIP_TESTS=0
FORCE=0
NO_CONFIG=0
CONFIG_ONLY=0
NO_MIRROR=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)      BRANCH="$2"; shift 2 ;;
        --skip-tests)  SKIP_TESTS=1; shift ;;
        --force)       FORCE=1; shift ;;
        --no-config)   NO_CONFIG=1; shift ;;
        --no-mirror)   NO_MIRROR=1; shift ;;
        --config-only) CONFIG_ONLY=1; shift ;;
        -h|--help)     sed -n '3,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

if (( NO_CONFIG && CONFIG_ONLY )); then
    echo "--no-config and --config-only are mutually exclusive" >&2
    exit 1
fi

say()  { printf '\n==> %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die()  { printf '\nFAILED: %s\n\n' "$1" >&2; exit 1; }

# Run a command as the app user when we are root, directly otherwise.
if [[ $EUID -eq 0 ]]; then
    as_app() { runuser -u "$APP_USER" -- "$@"; }
    SUDO=""
    CTX="running as root, git and uv run as $APP_USER"
else
    as_app() { "$@"; }
    SUDO="sudo"
    CTX="running as $(id -un)"
fi

# ---------------------------------------------------------------------------
# Config file installation.
#
# deploy.sh used to deploy Python code only, so the unit files and nginx.conf
# were installed by hand and drifted from the repo. The ordering was the
# expensive part: restart a service while the old unit is still in place and a
# bug you have already fixed looks like it persisted.
#
# Every install here is conditional on the file actually differing, so a
# code-only deploy costs a couple of cmp calls and reloads nothing.
#
# Staging happens under TMPDIR (/tmp), never anywhere nginx reads. Putting a
# work file in sites-available/ risks it being mistaken for the real config,
# and one in sites-enabled/ would actually be loaded, since the stock
# nginx.conf includes that directory by glob. The staged copy also carries the
# shared secret in the clear, so it is 0600 by virtue of mktemp and removed on
# the way out however the script exits.

TMP_FILES=()

cleanup_tmp() {
    (( ${#TMP_FILES[@]} )) || return 0
    rm -f "${TMP_FILES[@]}"
}

# EXIT alone would cover a die(), but not a Ctrl-C or a SIGTERM from a deploy
# that gets killed part-way through.
trap cleanup_tmp EXIT INT TERM

# mktemp, registered for cleanup. Two statements rather than a function that
# echoes the path, because $(...) would append to the array inside a subshell
# and the trap would never learn about the file.
new_tmp() {
    mktemp "${TMPDIR:-/tmp}/locatron-deploy.XXXXXX"
}

# Recover the live shared secret from an installed nginx config.
#
# The repo copy carries REPLACE_ME where the installed one carries a real
# value, so installing the repo copy verbatim would make nginx reject every
# request the edge forwards. Echoes nothing when there is no secret to recover.
recover_secret() {
    local file="$1" found=""
    [[ -f "$file" ]] || return 0
    found=$(grep -oP 'http_x_locatron_edge != "\K[^"]+' "$file" 2>/dev/null | head -n1) || true
    if [[ "$found" == "REPLACE_ME" ]]; then
        found=""
    fi
    printf '%s' "$found"
}

install_units() {
    local changed=0 src name dest
    for src in "$APP_DIR"/deploy/systemd/locatron-*.service; do
        [[ -f "$src" ]] || continue
        name=$(basename "$src")
        dest="$SYSTEMD_DIR/$name"

        # cmp is non-zero when dest is missing too, which is the right answer.
        if cmp -s "$src" "$dest"; then
            info "$name unchanged"
            continue
        fi
        $SUDO install -m 0644 "$src" "$dest"
        info "$name installed"
        changed=1
    done

    # One reload covers every unit, and only if something actually changed.
    if (( changed )); then
        $SUDO systemctl daemon-reload
        info "systemctl daemon-reload"
    fi
}

install_nginx() {
    local src="$APP_DIR/deploy/nginx.conf"
    local staged backup="" recovered test_out rc=0

    if [[ ! -f "$src" ]]; then
        info "deploy/nginx.conf missing from the repo, skipped"
        return 0
    fi

    staged=$(new_tmp)
    TMP_FILES+=("$staged")
    recovered=$(recover_secret "$NGINX_SITE")

    if [[ -n "$recovered" ]]; then
        # Validated before it reaches sed: a secret containing & or / would
        # otherwise corrupt the substitution rather than fail it.
        if [[ ! "$recovered" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
            die "the shared secret in $NGINX_SITE contains unexpected characters, refusing to substitute it"
        fi
        sed "s/REPLACE_ME/$recovered/g" "$src" > "$staged"
    else
        cat "$src" > "$staged"
    fi

    # Writing the placeholder into the live config would reject all edge
    # traffic with a 444, which looks exactly like an application outage.
    if grep -q REPLACE_ME "$staged"; then
        die "no shared secret to recover from $NGINX_SITE, and deploy/nginx.conf still has REPLACE_ME.
    Set a secret in the installed config first:
        openssl rand -hex 32
    Refusing to write REPLACE_ME into the live config."
    fi

    if cmp -s "$staged" "$NGINX_SITE"; then
        info "nginx.conf unchanged"
        return 0
    fi

    if [[ -f "$NGINX_SITE" ]]; then
        backup=$(new_tmp)
        TMP_FILES+=("$backup")
        cat "$NGINX_SITE" > "$backup"
    fi

    $SUDO install -m 0644 "$staged" "$NGINX_SITE"
    info "nginx.conf installed"

    # Capture the output before restoring, or the failure we print is the test
    # of the config we just put back rather than the one that broke.
    test_out=$($SUDO nginx -t 2>&1) || rc=$?
    if (( rc == 0 )); then
        $SUDO systemctl reload nginx
        info "nginx reloaded"
        return 0
    fi

    printf '%s\n' "$test_out" | sed 's/^/      /'
    info "nginx -t failed, restoring the previous config"

    if [[ -n "$backup" ]]; then
        $SUDO install -m 0644 "$backup" "$NGINX_SITE"
    else
        # Nothing was there before, so removing it is the restore.
        $SUDO rm -f "$NGINX_SITE"
    fi
    $SUDO systemctl reload nginx || true

    die "deploy/nginx.conf failed nginx -t. The previous config is back in place and nginx was reloaded"
}

install_config() {
    install_units
    install_nginx
}

if (( CONFIG_ONLY )); then
    say "Installing config files"
    info "$CTX"
    install_config
    printf '\nConfig step complete, no code deployed\n\n'
    exit 0
fi

# ---------------------------------------------------------------------------

say "Checking environment"

[[ -d "$APP_DIR/.git" ]]    || die "$APP_DIR is not a git checkout. Run deploy/install.sh first"
[[ -f "$ENV_FILE" ]]        || die "$ENV_FILE missing. Run deploy/install.sh first"
[[ -x "$VENV/bin/python" ]] || die "$VENV is not a virtualenv. Run deploy/install.sh first"
command -v uv >/dev/null    || die "uv not on PATH. Run deploy/install.sh first"

cd "$APP_DIR"
info "$CTX"
info "app     $APP_DIR"
info "branch  $BRANCH"

# ---------------------------------------------------------------------------

say "Fetching"

OLD_SHA=$(as_app git -C "$APP_DIR" rev-parse HEAD)

as_app git -C "$APP_DIR" fetch --prune origin

as_app git -C "$APP_DIR" rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
    || die "origin/$BRANCH does not exist. Try --branch master"

# checkout -B recovers from a detached HEAD. reset --hard discards local edits
# on the container, which is what you want on a deploy target.
as_app git -C "$APP_DIR" checkout -B "$BRANCH" --quiet "origin/$BRANCH"
as_app git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"

NEW_SHA=$(as_app git -C "$APP_DIR" rev-parse HEAD)

if [[ "$OLD_SHA" == "$NEW_SHA" ]] && (( ! FORCE )); then
    info "already at ${NEW_SHA:0:7}, nothing to do (use --force to redeploy)"
    exit 0
fi

info "${OLD_SHA:0:7} -> ${NEW_SHA:0:7}  $(as_app git -C "$APP_DIR" log -1 --format=%s | cut -c1-55)"

# Repair CRLF endings on scripts committed from Windows before .gitattributes
# landed. Left alone, bash rejects the shebang on the next run.
if grep -qlr $'\r$' "$APP_DIR"/deploy/*.sh 2>/dev/null; then
    info "fixing CRLF line endings in deploy scripts"
    as_app sed -i 's/\r$//' "$APP_DIR"/deploy/*.sh
fi
as_app chmod +x "$APP_DIR"/deploy/*.sh 2>/dev/null || true

# ---------------------------------------------------------------------------

say "Installing dependencies"

as_app uv pip install --python "$VENV/bin/python" --quiet -e "$APP_DIR[export]"
info "done"

# ---------------------------------------------------------------------------

say "Running tests"

if (( SKIP_TESTS )); then
    info "skipped"
elif as_app "$VENV/bin/python" -c 'import pytest' 2>/dev/null; then
    as_app "$VENV/bin/python" -m pytest "$APP_DIR/tests" -q \
        || die "tests failed (override with --skip-tests)"
else
    info "pytest not installed, skipped"
fi

# ---------------------------------------------------------------------------

say "Checking config and database"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [[ -x "$VENV/bin/locatron" ]]; then
    as_app "$VENV/bin/locatron" check || die "locatron check failed, see above"
else
    info "locatron entry point not installed, skipped"
fi

# ---------------------------------------------------------------------------
#
# Before the restart, because a worker refuses to start without a current
# mirror. Last in the rebuild order, after normalize_pass and dedupe_locality.

say "Building the street mirror"

if (( NO_MIRROR )); then
    info "skipped (--no-mirror)"
elif [[ ! -x "$VENV/bin/locatron" ]]; then
    info "locatron entry point not installed, skipped"
else
    MIRROR=$(as_app "$VENV/bin/python" -c 'from locatron.config import get_settings; print(get_settings().sqlite_path)')
    info "mirror  $MIRROR"

    # Cheap checks first: the digest is a full scan of 532k rows and costs about
    # 1.4s, so it only runs once the mirror exists and matches on version.
    NEED_BUILD=0
    REASON=""
    if [[ ! -f "$MIRROR" ]]; then
        NEED_BUILD=1; REASON="missing"
    elif ! as_app "$VENV/bin/python" -c 'from locatron.db import local; local.check_mirror()' 2>/dev/null; then
        NEED_BUILD=1; REASON="stale or unreadable"
    elif ! as_app "$VENV/bin/python" - <<'PYCHECK' 2>/dev/null
import sys
from locatron.build import streets
from locatron.db import local, mysql
meta = local.read_meta()
with mysql.session_scope() as s:
    s.execute(__import__("sqlalchemy").text("SET SESSION TRANSACTION READ ONLY"))
    digest = streets.source_digest(s)
sys.exit(0 if digest.digest == meta.source_digest else 1)
PYCHECK
    then
        NEED_BUILD=1; REASON="source digest changed"
    fi

    if (( NEED_BUILD )); then
        info "rebuilding: $REASON"
        as_app "$VENV/bin/locatron" build streets --quiet             || die "street mirror build failed"
    else
        info "current, not rebuilt"
    fi
fi

# ---------------------------------------------------------------------------
#
# Before the restart, not after. A restart that picks up the old unit file
# makes an already-fixed bug look like it is still there.

say "Installing config files"

if (( NO_CONFIG )); then
    info "skipped (--no-config)"
else
    install_config
fi

# ---------------------------------------------------------------------------

say "Restarting services"

RESTARTED=0
for svc in locatron-api locatron-bulk; do
    unit="/etc/systemd/system/$svc.service"
    module="$APP_DIR/locatron/${svc#locatron-}/app.py"

    if [[ ! -f "$unit" ]]; then
        info "$svc not installed"
        continue
    fi
    if [[ ! -f "$module" ]]; then
        info "$svc skipped, ${module#"$APP_DIR"/} does not exist yet"
        continue
    fi

    $SUDO systemctl restart "$svc"
    info "$svc restarted"
    RESTARTED=1
done

if (( RESTARTED )); then
    sleep 2
    for svc in locatron-api locatron-bulk; do
        [[ -f "/etc/systemd/system/$svc.service" ]] || continue
        state=$($SUDO systemctl is-active "$svc" 2>/dev/null || true)
        [[ "$state" == "inactive" ]] && continue
        info "$svc is $state"
        if [[ "$state" != "active" ]]; then
            $SUDO journalctl -u "$svc" -n 20 --no-pager | sed 's/^/      /'
            die "$svc did not come up. Previous commit was ${OLD_SHA:0:7}"
        fi
    done
fi

# ---------------------------------------------------------------------------

printf '\nDeployed %s\n' "${NEW_SHA:0:7}"
printf 'Previous %s\n' "${OLD_SHA:0:7}"
printf 'Roll back: git -C %s reset --hard %s && %s/deploy/deploy.sh --force\n\n' \
    "$APP_DIR" "${OLD_SHA:0:7}" "$APP_DIR"
