#!/usr/bin/env bash
# Provision a fresh Debian 12 LXC for Locatron. Idempotent.
set -euo pipefail

APP_USER=locatron
APP_HOME=/opt/locatron
DATA_DIR=/var/lib/locatron

if [[ $EUID -ne 0 ]]; then
    echo "Run as root inside the container." >&2
    exit 1
fi

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl git nginx redis-server \
    build-essential pkg-config tzdata

timedatectl set-timezone Australia/Melbourne || true

echo "==> Service account"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --home-dir "$APP_HOME" --shell /bin/bash "$APP_USER"
mkdir -p "$APP_HOME" "$DATA_DIR/exports"
chown -R "$APP_USER:$APP_USER" "$APP_HOME" "$DATA_DIR"

echo "==> uv"
# uv manages the Python version, so we do not depend on what the distro ships.
if [[ ! -x "$APP_HOME/.local/bin/uv" ]]; then
    su - "$APP_USER" -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
ln -sf "$APP_HOME/.local/bin/uv" /usr/local/bin/uv
su - "$APP_USER" -c 'uv python install 3.12'

echo "==> Redis"
# Cache only. Losing it must cost latency, never correctness, so no persistence.
if ! grep -q '# locatron' /etc/redis/redis.conf; then
    cat >> /etc/redis/redis.conf <<'REDIS'

# locatron
bind 127.0.0.1 -::1
maxmemory 1gb
maxmemory-policy allkeys-lru
save ""
appendonly no
REDIS
fi
systemctl enable redis-server
systemctl restart redis-server

echo "==> nginx"
install -m 0644 "$(dirname "$0")/nginx.conf" /etc/nginx/sites-available/locatron
ln -sf /etc/nginx/sites-available/locatron /etc/nginx/sites-enabled/locatron
rm -f /etc/nginx/sites-enabled/default

if grep -q REPLACE_ME /etc/nginx/sites-available/locatron; then
    echo
    echo "  !! Set the shared secret before starting nginx:"
    echo "     openssl rand -hex 32"
    echo "     then replace REPLACE_ME in /etc/nginx/sites-available/locatron"
    echo
fi

echo "==> systemd units"
install -m 0644 "$(dirname "$0")"/systemd/*.service "$(dirname "$0")"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

echo
echo "Done. Next:"
echo "  1. Set the shared secret in /etc/nginx/sites-available/locatron"
echo "  2. Clone the repo to $APP_HOME/app and create $APP_HOME/.env"
echo "  3. /opt/locatron/venv/bin/locatron check"
echo "  4. systemctl enable --now nginx locatron-api locatron-bulk"
