#!/usr/bin/env bash
# Run playbooks/core/discover_vlan.yml end to end against canned CLI output.
#
# A test-only shadow of ansible.netcommon (tests/mock_cli/ansible_collections)
# answers cli_command from tests/mock_cli/devices/<host>/<command>.txt, so every
# task, filter, and report template runs without devices. VLAN 300 is probed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${MOCK_OUT:-$(mktemp -d)}"
[ -n "${MOCK_OUT:-}" ] || trap 'rm -rf "$OUT"' EXIT
rc=0

ANSIBLE_COLLECTIONS_PATH="$ROOT/tests/mock_cli:$ROOT/collections" \
ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=300 \
  -e discovery_allow_probe=true \
  -e artifacts_dir="$OUT" \
  > "$OUT/run.log" 2>&1 || rc=$?
# rc 4 means some hosts were unreachable: nxos-mls-99 is unreachable on purpose.
if [ "${rc:-0}" -ne 0 ] && [ "${rc:-0}" -ne 4 ]; then
  cat "$OUT/run.log"
  echo "FAIL: playbook run failed (rc=$rc)" >&2
  exit 1
fi

REPORT="$(ls "$OUT"/reports/lisle/*/*_discovery_*.md)"
ENDPOINTS="$(ls "$OUT"/reports/lisle/*/*_endpoints_*.csv)"
fail=0
expect() {
  if ! grep -qF -- "$2" "$1"; then
    echo "FAIL: $(basename "$1") is missing: $2" >&2
    fail=1
  fi
}
expect "$REPORT" "Legacy MLS: nxos-mls-01, nxos-mls-02 (HSRP 10.30.0.1)"
expect "$REPORT" "**L3** - discovery:"
expect "$REPORT" "nxos-mls-99 was not discovered"
expect "$REPORT" "nxos-mls-02: \`show mac address-table dynamic vlan 300\` failed"
expect "$REPORT" "nxos-mls-01 Ethernet1/20 faces a firewall/router (lis-asa01 TenGigabitEthernet0/8)"
expect "$REPORT" "| eos-leaf-01 | fabric | participating | Ethernet3, Ethernet4 [2 MAC]"
expect "$REPORT" "endpoint-facing interfaces: Ethernet3, Ethernet4; local MAC"
expect "$REPORT" "| nxos-mls-02 | legacy | review | port-channel20 (switch)"
expect "$REPORT" "1 active IP(s) are in ARP but not in any MAC table"
expect "$REPORT" "1. eos-leaf-02"
expect "$REPORT" "- DHCP relay: 10.99.0.10"
expect "$REPORT" "- ACL: ACL_WEB_IN in"
expect "$REPORT" "| 10.40.0.0/16 via 10.30.0.254 name fw_dmz |"
expect "$REPORT" "| bgp peer on VLAN | 10.30.0.254 AS 65099 (FW-DMZ)"
expect "$REPORT" "10.30.0.0/24 advertised to 10.30.0.254"
expect "$REPORT" "| STP root | nxos-mls-01"
expect "$REPORT" "| Fabric VTEPs | eos-leaf-01 Loopback1 10.1.1.11, headend/EVPN, L3 VNI 50001 |"
expect "$ENDPOINTS" "300,10.30.0.10,00:50:56:ab:30:10,edge,eos-leaf-01,Et3,\"esx-web-01\",compute"
expect "$ENDPOINTS" "300,10.30.0.254,00:a0:c9:aa:02:54,edge,nxos-mls-01,Eth1/20"
expect "$ENDPOINTS" "300,10.30.0.50,00:50:56:ab:30:50,not_in_mac_table"

if [ "$fail" -ne 0 ]; then
  echo "--- report ---"; cat "$REPORT"; echo "--- endpoints ---"; cat "$ENDPOINTS"
  exit 1
fi
echo "OK: mock discovery run produced the expected report ($(basename "$REPORT"))"
