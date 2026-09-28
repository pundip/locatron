#!/usr/bin/env bash
#
# deploy/lc.sh — run the locatron CLI on the container, as the service user.
#
#   /opt/locatron/app/deploy/lc.sh check
#   /opt/locatron/app/deploy/lc.sh check --deep
#   /opt/locatron/app/deploy/lc.sh build streets
#   /opt/locatron/app/deploy/lc.sh parse "65 clifton park drive 3201 carrum downs"
#   /opt/locatron/app/deploy/lc.sh resolve "Greater Melbourne"
#
# Why this exists. Running `uv run locatron ...` as root in /opt/locatron/app
# leaves root-owned files behind — a root-owned .venv is the one that bit us, and
# uv will happily create one next to the project when it cannot find the
# configured interpreter. The next deploy then runs as locatron, cannot write
# them, and fails somewhere unrelated to the actual cause.
#
# So: root never runs uv or git in the app directory. Everything goes through
# here, which reproduces exactly the environment deploy.sh uses — same venv, same
# env file, same user, same working directory — so a command you run by hand
# behaves the way the same command behaves during a deploy.
#
# Deliberately does not use `uv run`. uv resolves and may install into a project
# environment; the deploy's venv is already built and is the thing we want to
# exercise. This calls its console script directly.

set -euo pipefail

APP_USER="${LOCATRON_USER:-locatron}"
APP_DIR="${LOCATRON_APP_DIR:-/opt/locatron/app}"
VENV="${LOCATRON_VENV:-/opt/locatron/venv}"
ENV_FILE="${LOCATRON_ENV_FILE:-/opt/locatron/.env}"

die() { printf '\nFAILED: %s\n\n' "$1" >&2; exit 1; }

if [[ $# -eq 0 ]]; then
    sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

[[ -x "$VENV/bin/locatron" ]] || die "$VENV/bin/locatron missing. Run deploy/deploy.sh first."
[[ -f "$ENV_FILE" ]]          || die "$ENV_FILE missing. Run deploy/install.sh first."
[[ -d "$APP_DIR" ]]           || die "$APP_DIR missing. Run deploy/install.sh first."

# The env file holds the MySQL password, so it is sourced rather than passed on a
# command line where it would show up in ps output. Exported for the child only.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# cd first so a relative sqlite_path resolves the same way it does under the
# service, and so nothing is created in root's home.
cd "$APP_DIR"

if [[ $EUID -eq 0 ]]; then
    # runuser rather than su: no PAM session, no login shell, and the already
    # exported environment carries through.
    exec runuser -u "$APP_USER" -- "$VENV/bin/locatron" "$@"
fi

if [[ "$(id -un)" != "$APP_USER" ]]; then
    die "run this as root or as $APP_USER, not as $(id -un)"
fi

exec "$VENV/bin/locatron" "$@"
