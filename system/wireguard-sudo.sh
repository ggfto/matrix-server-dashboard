#!/usr/bin/env bash
# Lets the dashboard user read WireGuard peers: only `wg show all <field>` for the fields
# below, none of which prints a private key. Nothing else runs as root.
#   sudo ./system/wireguard-sudo.sh [user]
# Undo with: sudo rm /etc/sudoers.d/matrix-dashboard-wireguard
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Rode com: sudo $0"; exit 1; }
RUN_USER="${1:-${SUDO_USER:-}}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = root ] || ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "Informe o usuário do painel: sudo $0 <usuario>"; exit 1
fi
WG=/usr/bin/wg
[ -x "$WG" ] || { echo "wg não encontrado em $WG (apt install wireguard-tools)"; exit 1; }
TARGET=/etc/sudoers.d/matrix-dashboard-wireguard
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
CMDS=""
for field in allowed-ips endpoints latest-handshakes transfer persistent-keepalive; do
  CMDS="${CMDS:+$CMDS, }$WG show all $field"
done
cat > "$TMP" <<RULE
# Matrix dashboard: read-only WireGuard peer status (no private keys).
$RUN_USER ALL=(root) NOPASSWD: $CMDS
RULE
visudo -cf "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" "$TARGET"
echo "OK: $TARGET instalado para $RUN_USER. Teste com: sudo -n $WG show all latest-handshakes"
