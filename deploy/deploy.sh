#!/usr/bin/env bash
#
# deploy/deploy.sh — update Locatron on the container.
#
#   sudo -u locatron /opt/locatron/app/deploy/deploy.sh
#   /opt/locatron/app/deploy/deploy.sh --branch dev
#   /opt/locatron/app/deploy/deploy.sh --skip-tests
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
# Roll back: the previous commit is printed at the end.
#     git -C /opt/locatron/app reset --hard <sha>
#     /opt/locatron/app/deploy/deploy.sh --force

set -euo pipefail

APP_USER="${LOCATRON_USER:-locatron}"
APP_DIR="${LOCATRON_APP_DIR:-/opt/locatron/app}"
VENV="${LOCATRON_VENV:-/opt/locatron/venv}"
ENV_FILE="${LOCATRON_ENV_FILE:-/opt/locatron/.env}"
BRANCH="${LOCATRON_BRANCH:-main}"

SKIP_TESTS=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)     BRANCH="$2"; shift 2 ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --force)      FORCE=1; shift ;;
        -h|--help)    sed -n '3,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

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
