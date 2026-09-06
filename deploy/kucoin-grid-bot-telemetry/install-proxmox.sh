#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 root@PROXMOX_HOST [VMID]" >&2
    exit 2
fi

PVE_HOST=$1
VMID=${2:-109}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REMOTE_DIR="/tmp/kucoin-grid-bot-telemetry-install-$$"

for file in telemetry.py kucoin-grid-bot-telemetry.service; do
    if [[ ! -f "$HERE/$file" ]]; then
        echo "Missing deployment file: $HERE/$file" >&2
        exit 1
    fi
done

cleanup() {
    ssh "$PVE_HOST" "rm -rf '$REMOTE_DIR'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ssh "$PVE_HOST" "install -d -m 0700 '$REMOTE_DIR'"
scp -q \
    "$HERE/telemetry.py" \
    "$HERE/kucoin-grid-bot-telemetry.service" \
    "$PVE_HOST:$REMOTE_DIR/"

ssh "$PVE_HOST" bash -s -- "$VMID" "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
VMID=$1
REMOTE_DIR=$2

pct status "$VMID" >/dev/null
pct exec "$VMID" -- id gridbot >/dev/null

pct exec "$VMID" -- install -d -o root -g root -m 0755 /opt/kucoin-grid-bot-telemetry
pct exec "$VMID" -- install -d -o gridbot -g gridbot -m 0750 /var/lib/kucoin-grid-bot-telemetry

pct push "$VMID" "$REMOTE_DIR/telemetry.py" /opt/kucoin-grid-bot-telemetry/telemetry.py
pct push "$VMID" "$REMOTE_DIR/kucoin-grid-bot-telemetry.service" /etc/systemd/system/kucoin-grid-bot-telemetry.service
pct exec "$VMID" -- chown root:root /opt/kucoin-grid-bot-telemetry/telemetry.py /etc/systemd/system/kucoin-grid-bot-telemetry.service
pct exec "$VMID" -- chmod 0755 /opt/kucoin-grid-bot-telemetry/telemetry.py
pct exec "$VMID" -- chmod 0644 /etc/systemd/system/kucoin-grid-bot-telemetry.service

pct exec "$VMID" -- sh -lc '
set -eu
TOKEN=/etc/kucoin-grid-bot/telemetry.token
if [ ! -s "$TOKEN" ]; then
    umask 027
    /opt/kucoin-grid-bot/.venv/bin/python -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN"
fi
chown root:gridbot "$TOKEN"
chmod 0640 "$TOKEN"
'

pct exec "$VMID" -- systemctl daemon-reload
pct exec "$VMID" -- systemctl enable --now kucoin-grid-bot-telemetry.service
pct exec "$VMID" -- systemctl is-active --quiet kucoin-grid-bot-telemetry.service

IP=$(pct exec "$VMID" -- hostname -I | awk '{print $1}')
echo
echo "KuCoin telemetry sidecar is active."
echo "LXC:      $VMID"
echo "Base URL: http://${IP}:8766"
echo "Preset:   KuCoin Grid Bot Telemetry"
echo
echo "The bearer token was created/preserved inside the LXC and was not printed."
echo "Copy it directly into the Odysseus integration API-key field with:"
echo "  pct exec $VMID -- cat /etc/kucoin-grid-bot/telemetry.token"
echo "Use the integration value: Bearer <copied-token>"
REMOTE
