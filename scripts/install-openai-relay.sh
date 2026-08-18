#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 64
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run this installer as root"

APP_SERVER_IP="${APP_SERVER_IP:-}"
RELAY_HOSTNAME="${RELAY_HOSTNAME:-}"

[[ "$APP_SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
  || fail "APP_SERVER_IP must contain one IPv4 address"
[[ "$RELAY_HOSTNAME" =~ ^[a-z0-9]([a-z0-9.-]{1,251}[a-z0-9])?$ ]] \
  || fail "RELAY_HOSTNAME must be a lowercase DNS hostname"

python3 - "$APP_SERVER_IP" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
if address.version != 4 or address.is_unspecified or address.is_multicast:
    raise SystemExit(64)
PY

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes \
  apt-transport-https \
  ca-certificates \
  curl \
  debian-archive-keyring \
  debian-keyring \
  fail2ban \
  gnupg \
  ufw

install -d -m 0755 /usr/share/keyrings
curl --fail --silent --show-error --location \
  https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor --yes --output /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl --fail --silent --show-error --location \
  https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  --output /etc/apt/sources.list.d/caddy-stable.list
chmod 0644 \
  /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
  /etc/apt/sources.list.d/caddy-stable.list

apt-get update
apt-get install --yes caddy

install -d -o caddy -g caddy -m 0750 /var/log/caddy
install -m 0644 /dev/stdin /etc/caddy/Caddyfile <<EOF
{
  admin 127.0.0.1:2019
}

${RELAY_HOSTNAME} {
  @allowed remote_ip ${APP_SERVER_IP}/32

  handle @allowed {
    reverse_proxy https://api.openai.com {
      header_up Host api.openai.com
    }
  }

  respond 403

  log {
    output file /var/log/caddy/access.log {
      roll_size 25MiB
      roll_keep 4
    }
    format json
  }
}
EOF

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy
systemctl enable --now fail2ban

ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

printf 'Relay installed.\n'
printf 'RELAY_BASE_URL=https://%s/v1\n' "$RELAY_HOSTNAME"
printf 'Only %s is allowed to proxy application requests.\n' "$APP_SERVER_IP"
