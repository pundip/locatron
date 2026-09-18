#!/usr/bin/env bash
#
# deploy/install.sh — first-time setup, and safe to re-run to repair.
#
# Run as root on the container:
#     LOCATRON_REPO_URL=https://github.com/YOU/locatron.git bash install.sh
#
# Once this has succeeded, use deploy/deploy.sh for ordinary code updates.
# This script only needs running again if something is broken or missing.
#
# Fully non-interactive. Every step is a no-op when already done, so re-running
# is cheap and safe. It never prompts, never clones over an existing checkout,
# and never recreates a working virtualenv.

set -euo pipefail

REPO_URL="${LOCATRON_REPO_URL:-}"
BRANCH="${LOCATRON_BRANCH:-main}"

APP_USER=locatron
APP_HOME=/opt/locatron
APP_DIR="$APP_HOME/app"
VENV="$APP_HOME/venv"
ENV_FILE="$APP_HOME/.env"
DATA_DIR=/var/lib/locatron
UV="$APP_HOME/.local/bin/uv"

say()  { printf '\n==> %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    WARN  %s\n' "$1"; }
die()  { printf '\nFAILED: %s\n\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"

# runuser, not 'su -'. 'su - user' with no command opens an interactive shell
# and blocks the script forever. runuser -u takes a command and returns.
as_app() { runuser -u "$APP_USER" -- "$@"; }

# ---------------------------------------------------------------------------

say "Packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git nginx redis-server \
    build-essential pkg-config tzdata >/dev/null
timedatectl set-timezone Australia/Melbourne 2>/dev/null || true
info "done"

# ---------------------------------------------------------------------------

say "Service account and directories"

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$APP_HOME" --shell /bin/bash "$APP_USER"
    info "created user $APP_USER"
else
    info "user $APP_USER exists"
fi

mkdir -p "$APP_HOME" "$DATA_DIR/exports"
chown -R "$APP_USER:$APP_USER" "$APP_HOME" "$DATA_DIR"

# ---------------------------------------------------------------------------

say "uv"

if [[ ! -x "$UV" ]]; then
    as_app bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
    info "installed"
else
    info "already installed"
fi

ln -sf "$UV" /usr/local/bin/uv
as_app "$UV" python install 3.12 >/dev/null 2>&1 || true
info "python 3.12 available"

# ---------------------------------------------------------------------------

say "Repository"

if [[ -d "$APP_DIR/.git" ]]; then
    # Already cloned. Update rather than failing on a non-empty directory.
    info "existing checkout, updating"
    as_app git -C "$APP_DIR" fetch --prune origin
    as_app git -C "$APP_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
    as_app git -C "$APP_DIR" reset --hard "origin/$BRANCH"
elif [[ -e "$APP_DIR" ]] && [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
    die "$APP_DIR exists but is not a git checkout. Move it aside and re-run."
else
    [[ -n "$REPO_URL" ]] || die "set LOCATRON_REPO_URL to clone, e.g. https://github.com/YOU/locatron.git"
    info "cloning $REPO_URL"
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    as_app git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
info "at $(as_app git -C "$APP_DIR" rev-parse --short HEAD)"

# Shell scripts committed from Windows can arrive with CRLF endings, which
# makes bash reject the shebang. .gitattributes prevents this, but repair any
# that predate it.
if grep -qlr $'\r$' "$APP_DIR"/deploy/*.sh 2>/dev/null; then
    warn "CRLF line endings found in deploy scripts, fixing"
    sed -i 's/\r$//' "$APP_DIR"/deploy/*.sh
fi
chmod +x "$APP_DIR"/deploy/*.sh 2>/dev/null || true

# ---------------------------------------------------------------------------

say "Virtualenv"

if [[ -x "$VENV/bin/python" ]]; then
    info "exists, keeping it"
else
    # --clear makes this non-interactive if a broken venv is already there.
    as_app "$UV" venv --python 3.12 --clear "$VENV"
    info "created"
fi

as_app "$UV" pip install --python "$VENV/bin/python" --quiet -e "$APP_DIR[export]"
info "dependencies installed"

# ---------------------------------------------------------------------------

say "Environment file"

NEEDS_ENV=0
if [[ ! -f "$ENV_FILE" ]]; then
    install -o "$APP_USER" -g "$APP_USER" -m 600 "$APP_DIR/.env.example" "$ENV_FILE"
    warn "created $ENV_FILE from the example, you must fill in the password"
    NEEDS_ENV=1
else
    info "exists"
fi
chmod 600 "$ENV_FILE"
chown "$APP_USER:$APP_USER" "$ENV_FILE"

if grep -qE '^LOCATRON_MYSQL_PASSWORD=$' "$ENV_FILE"; then
    warn "LOCATRON_MYSQL_PASSWORD is empty in $ENV_FILE"
    NEEDS_ENV=1
fi

# ---------------------------------------------------------------------------

say "Redis"

if ! grep -q '# locatron' /etc/redis/redis.conf; then
    cat >> /etc/redis/redis.conf <<'REDIS'

# locatron: cache only, no persistence
bind 127.0.0.1 -::1
maxmemory 1gb
maxmemory-policy allkeys-lru
save ""
appendonly no
REDIS
    info "configured"
else
    info "already configured"
fi
systemctl enable --now redis-server >/dev/null 2>&1
info "running"

# ---------------------------------------------------------------------------

say "nginx"

install -m 0644 "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/locatron
ln -sf /etc/nginx/sites-available/locatron /etc/nginx/sites-enabled/locatron
rm -f /etc/nginx/sites-enabled/default

if grep -q REPLACE_ME /etc/nginx/sites-available/locatron; then
    warn "shared secret not set in /etc/nginx/sites-available/locatron"
    warn "generate one with: openssl rand -hex 32"
    NEEDS_SECRET=1
else
    nginx -t >/dev/null 2>&1 && systemctl reload nginx && info "reloaded"
    NEEDS_SECRET=0
fi

# ---------------------------------------------------------------------------

say "systemd units"

install -m 0644 "$APP_DIR"/deploy/systemd/locatron-*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/locatron-*.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
info "installed"

if [[ -f "$APP_DIR/deploy/sudoers.locatron" ]]; then
    if visudo -c -q -f "$APP_DIR/deploy/sudoers.locatron" 2>/dev/null; then
        install -m 0440 "$APP_DIR/deploy/sudoers.locatron" /etc/sudoers.d/locatron
        info "sudoers installed, deploy.sh can restart services without a password"
    else
        warn "deploy/sudoers.locatron failed validation, skipped"
    fi
fi

# ---------------------------------------------------------------------------

say "Check"

if (( NEEDS_ENV )); then
    warn "skipped, fill in $ENV_FILE first"
else
    set -a; . "$ENV_FILE"; set +a
    if as_app "$VENV/bin/locatron" check; then
        info "passed"
    else
        warn "locatron check failed, see above"
    fi
fi

# ---------------------------------------------------------------------------

printf '\n%s\n' "-------------------------------------------------------------"
printf 'Install complete.\n\n'

(( NEEDS_ENV )) && printf '  1. Fill in %s (the MySQL password)\n' "$ENV_FILE"
(( ${NEEDS_SECRET:-0} )) && printf '  2. Set the shared secret in /etc/nginx/sites-available/locatron\n       openssl rand -hex 32\n     then: nginx -t && systemctl reload nginx\n'

cat <<EOF

  Verify:   sudo -u $APP_USER $VENV/bin/locatron check
  Deploy:   sudo -u $APP_USER $APP_DIR/deploy/deploy.sh

  Services stay stopped until locatron/api/app.py exists. That is expected
  for now.

EOF
