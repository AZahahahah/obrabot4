#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_COMPOSE="$SCRIPT_DIR/../gpu/compose.yaml"
readonly INSTALL_DIR="/opt/kakpeople-model"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  printf 'Run this installer as root.\n' >&2
  exit 77
fi
if [[ ! -f "$SOURCE_COMPOSE" ]]; then
  printf 'GPU compose file is missing.\n' >&2
  exit 66
fi
if [[ -z "${APP_SERVER_IP:-}" ]]; then
  printf 'APP_SERVER_IP is required.\n' >&2
  exit 64
fi
if ! python3 - "$APP_SERVER_IP" <<'PY'
from ipaddress import IPv4Address
import sys

try:
    IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
then
  printf 'APP_SERVER_IP must be one IPv4 address.\n' >&2
  exit 64
fi

if [[ -z "${MODEL_API_KEY:-}" ]]; then
  read -r -s -p 'Create a private model API key (32-128 letters/digits/_/-): ' MODEL_API_KEY
  printf '\n'
fi
if [[ ! "$MODEL_API_KEY" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
  printf 'MODEL_API_KEY has an unsafe format.\n' >&2
  exit 64
fi

for command in curl docker iptables nvidia-smi python3; do
  if ! command -v "$command" >/dev/null; then
    printf 'Required command is missing: %s\n' "$command" >&2
    exit 69
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  printf 'Docker Compose plugin is required.\n' >&2
  exit 69
fi
if ! nvidia-smi >/dev/null 2>&1; then
  printf 'NVIDIA driver is unavailable. Use the Ubuntu GPU image.\n' >&2
  exit 69
fi

install -d -m 700 "$INSTALL_DIR"
install -m 600 "$SOURCE_COMPOSE" "$INSTALL_DIR/compose.yaml"
umask 077
printf 'MODEL_API_KEY=%s\n' "$MODEL_API_KEY" >"$INSTALL_DIR/model.env"

cat >"$INSTALL_DIR/apply-firewall.sh" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \\
  || iptables -I DOCKER-USER 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -C DOCKER-USER -p tcp -s ${APP_SERVER_IP}/32 --dport 8000 -j ACCEPT 2>/dev/null \\
  || iptables -I DOCKER-USER 2 -p tcp -s ${APP_SERVER_IP}/32 --dport 8000 -j ACCEPT
iptables -C DOCKER-USER -p tcp --dport 8000 -j DROP 2>/dev/null \\
  || iptables -A DOCKER-USER -p tcp --dport 8000 -j DROP
EOF
chmod 700 "$INSTALL_DIR/apply-firewall.sh"

cat >/etc/systemd/system/kakpeople-model-firewall.service <<EOF
[Unit]
Description=Restrict Kakpeople model API to the application server
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/apply-firewall.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kakpeople-model-firewall.service
docker compose --project-directory "$INSTALL_DIR" pull
docker compose --project-directory "$INSTALL_DIR" up --detach

for _attempt in $(seq 1 180); do
  if curl --fail --silent --show-error \
    --header "Authorization: Bearer $MODEL_API_KEY" \
    http://127.0.0.1:8000/v1/models >/dev/null; then
    unset MODEL_API_KEY
    printf 'MODEL_READY\n'
    exit 0
  fi
  sleep 10
done

unset MODEL_API_KEY
docker compose --project-directory "$INSTALL_DIR" logs --tail 80 model >&2
printf 'Model did not become ready in 30 minutes.\n' >&2
exit 70
