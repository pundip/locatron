# Container provisioning

## 1. Create the LXC (run on the Proxmox host)

```bash
# Adjust CTID, storage names, and bridge to match your setup.
pct create 110 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname locatron \
  --cores 4 \
  --memory 8192 \
  --swap 2048 \
  --rootfs local-lvm:60 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 \
  --features nesting=0 \
  --onboot 1 \
  --start 1
```

Sizing notes:

- **8 GB RAM.** Small gazetteers sit in each gunicorn worker's memory. The
  street gazetteer deliberately does not — it lives in SQLite so workers share
  pages. See CLAUDE.md.
- **60 GB disk.** The SQLite mirror plus Parquet export snapshots. Grow this
  before you start writing exports, not after.
- **nesting=0.** There is no Docker inside. Keeping it off is one less
  privilege.

Give it a static lease on your router, or swap `ip=dhcp` for a static address.

## 2. Bootstrap inside the container

```bash
pct enter 110
# then, inside:
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/YOU/locatron/main/deploy/bootstrap.sh -o /tmp/bootstrap.sh
# or just scp deploy/bootstrap.sh in
bash /tmp/bootstrap.sh
```

## 3. Deploy the code

```bash
su - locatron
git clone git@github.com:YOU/locatron.git /opt/locatron/app
cd /opt/locatron/app
uv venv --python 3.12 /opt/locatron/venv
uv pip install --python /opt/locatron/venv/bin/python -e ".[export]"
cp .env.example /opt/locatron/.env
# fill in LOCATRON_MYSQL_PASSWORD, then:
chmod 600 /opt/locatron/.env
/opt/locatron/venv/bin/locatron check
```

Do not start the services until `locatron check` passes. A green check means
credentials work, the derived tables exist, and norm_key is populated.

## 4. Enable services

```bash
systemctl enable --now redis-server nginx locatron-api locatron-bulk
systemctl status locatron-api --no-pager
curl -H 'X-Locatron-Edge: YOUR_SHARED_SECRET' http://127.0.0.1:8000/locatron/healthz
```

## 5. Expose it

**On the Proxmox host**, if the container is NAT'd:

```bash
iptables -t nat -A PREROUTING -p tcp --dport 18000 \
  -j DNAT --to-destination <CT_IP>:8000
iptables -A FORWARD -p tcp -d <CT_IP> --dport 8000 -j ACCEPT
```

Persist with `iptables-persistent`. If the container has its own LAN address,
forward at the router instead and skip this.

**On the external nginx**, in front of Cloudflare:

```nginx
location /locatron/ {
    proxy_pass http://pundip.com:18000/locatron/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Locatron-Edge   "YOUR_SHARED_SECRET";
    proxy_read_timeout 30s;
}

location /locatron/v1/export/ {
    proxy_pass http://pundip.com:18000/locatron/v1/export/;
    proxy_set_header X-Locatron-Edge "YOUR_SHARED_SECRET";
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

The shared secret matters. The forwarded port is reachable directly on the
public internet, bypassing Cloudflare entirely. The container's nginx returns
444 to anything missing the header, so a direct hit on `pundip.com:18000`
gets nothing. Generate one with `openssl rand -hex 32` and keep it out of git.

Optionally tighten further by restricting the forwarded port to your external
nginx's source IP at the Proxmox host firewall.

## 6. Verify end to end

```bash
curl https://urlloom.com/locatron/healthz
curl 'https://urlloom.com/locatron/v1/resolve?text=Greater+Melbourne'
curl http://pundip.com:18000/locatron/healthz    # must fail with an empty reply
```
