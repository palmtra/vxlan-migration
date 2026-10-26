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
expect "$REPORT" "Fix reachability to nxos-mls-99 and re-run discovery: it was not discovered"
expect "$REPORT" "nxos-mls-02: \`show mac address-table dynamic vlan 300\` failed"
expect "$REPORT" "nxos-mls-01 Ethernet1/20 faces a firewall/router (lis-asa01 TenGigabitEthernet0/8)"
expect "$REPORT" "| eos-leaf-01 | VTEP | participating | Ethernet3, Ethernet4 [2 MAC]"
expect "$REPORT" "endpoint-facing interfaces: Ethernet3, Ethernet4; local MAC"
# nxos-mls-02's MAC-table query failed, so its silent trunk is not provably idle: kept for review.
expect "$REPORT" "| nxos-mls-02 | legacy | review | port-channel20 (switch)"
expect "$REPORT" "MAC table not read, so idle ports cannot be told apart: kept as before"
expect "$REPORT" "1 active IP(s) are in ARP but not in any MAC table"
expect "$REPORT" "| 1 | nxos-mls-02 | port-channel30 toward acc-sw-01 | blocked_until_vni_live"
expect "$REPORT" "| 2 | acc-sw-01 | Ethernet48 toward nxos-mls-02 | blocked_until_vni_live"
expect "$REPORT" "| 3 | eos-leaf-02 | trunks, VLAN 300 | ready |"
expect "$REPORT" "- DHCP relay (not migrated): 10.99.0.10"
expect "$REPORT" "- ACL: ACL_WEB_IN in"
expect "$REPORT" "| 10.40.0.0/16 via 10.30.0.254 name fw_dmz |"
expect "$REPORT" "| bgp peer on VLAN | 10.30.0.254 AS 65099 (FW-DMZ)"
expect "$REPORT" "10.30.0.0/24 advertised to 10.30.0.254"
expect "$REPORT" "| STP root | nxos-mls-01"
expect "$REPORT" "VNI 20000 proposed (next free in lisle external 20000-29999)"
expect "$REPORT" "| Fabric VTEPs | eos-leaf-01 Loopback1 10.1.1.11, headend/EVPN, L3 VNI 50001 |"
expect "$ENDPOINTS" "300,10.30.0.10,00:50:56:ab:30:10,edge,eos-leaf-01,Et3,\"esx-web-01\",compute"
expect "$ENDPOINTS" "300,10.30.0.254,00:a0:c9:aa:02:54,edge,nxos-mls-01,Eth1/20"
expect "$ENDPOINTS" "300,10.30.0.50,00:50:56:ab:30:50,not_in_mac_table"
expect "$ENDPOINTS" "300,10.30.0.77,00:50:56:ab:30:77,edge,acc-sw-01,Et5,\"app-db-07\",compute"
expect "$REPORT" "acc-sw-01 bridges EVPN and legacy L2 for VLAN 300 (VTEP: eos-leaf-01 via Port-Channel1; MLS: nxos-mls-02 via Ethernet48)"
expect "$REPORT" "Link prune on acc-sw-01 is blocked: VLAN 300 is not VNI-mapped yet on eos-leaf-01"
expect "$REPORT" "| acc-sw-01 | non-VTEP | l2_attached |"
expect "$REPORT" "| nxos-mls-01 | legacy | legacy_retained |"
expect "$REPORT" "attachment point for non-VTEP switch acc-sw-01"
expect "$REPORT" '| eos-leaf-02 | TENANT1 | bgp fabric view | 10.30.0.0/24 eBGP from nxos-mls-01 (AS path 65001), best | border leaf AS 65101, live `show ip bgp`, exact prefix only |'
PRUNE="$(ls "$OUT"/reports/lisle/*/*_prune_*.md)"
expect "$PRUNE" "## acc-sw-01 (eos) - link prune"
expect "$PRUNE" "## nxos-mls-02 (nxos) - link prune"
expect "$PRUNE" "Status: \`blocked_until_vni_live\`"

# Verify: ping the baseline endpoints from the gateway leaf (10.30.0.50 is down).
ANSIBLE_COLLECTIONS_PATH="$ROOT/tests/mock_cli:$ROOT/collections" \
ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
ANSIBLE_ROLES_PATH="$ROOT/roles" \
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml tests/mock_cli/verify_mock.yml \
  -e artifacts_dir="$OUT" > "$OUT/verify.log" 2>&1 || { cat "$OUT/verify.log"; echo "FAIL: verify run failed" >&2; exit 1; }
VERIFY="$OUT/reports/verify_endpoint_reachability.json"
expect "$VERIFY" '"passed": false'
expect "$VERIFY" "4/5 baseline endpoints answer from eos-leaf-01; no reply: 10.30.0.50"

# Record mode: VLAN 300 is in a VLAN DB whose record names no border leaf. The
# border leaf (eos-leaf-02) must still be discovered, the model must stay
# condensed, and the L3 gateway mode must default to distributed_anycast.
REC="$OUT/record"
mkdir -p "$REC"
ANSIBLE_COLLECTIONS_PATH="$ROOT/tests/mock_cli:$ROOT/collections" \
ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=300 \
  -e vlan_db_dir="$ROOT/tests/mock_cli/vlans" \
  -e artifacts_dir="$REC" > "$REC/run.log" 2>&1 || rc=$?
if [ "${rc:-0}" -ne 0 ] && [ "${rc:-0}" -ne 4 ]; then
  cat "$REC/run.log"; echo "FAIL: record-mode discovery failed (rc=$rc)" >&2; exit 1
fi
MODEL="$(ls "$REC"/reports/lisle/*/*_discovery_*.yml)"
RECREPORT="$(ls "$REC"/reports/lisle/*/*_discovery_*.md)"
expect "$RECREPORT" "| eos-leaf-02 | non-VTEP | prune_eligible |"
expect "$RECREPORT" "| eos-leaf-02 | TENANT1 | bgp fabric view | 10.30.0.0/24 eBGP from nxos-mls-01"
expect "$MODEL" "mode: distributed_anycast"
expect "$MODEL" "via: redistribute connected route-map RM_CONN"
for absent in "external_exits" "associated:" "bgp_fabric" "ospf"; do
  if grep -qF -- "$absent" "$MODEL"; then echo "FAIL: model still carries $absent" >&2; fail=1; fi
done
if grep -qiF "gateway_mode" "$RECREPORT"; then
  echo "FAIL: report still asks for gateway_mode" >&2; fail=1
fi

# Config generation from the record: distributed_anycast puts the SVI on the participating leaf.
ANSIBLE_COLLECTIONS_PATH="$ROOT/tests/mock_cli:$ROOT/collections" \
ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml playbooks/deploy_to_cvp_avd.yml \
  --limit dc_lisle \
  -e target_vlan_id=300 \
  -e target_data_center=lisle \
  -e vlan_db_dir="$ROOT/tests/mock_cli/vlans" \
  -e reports_dir="$REC/reports" > "$REC/avd.log" 2>&1 || { cat "$REC/avd.log"; echo "FAIL: deploy_to_cvp_avd failed" >&2; exit 1; }
CFG="$REC/reports/cvp_configlets/avd/configs/eos-leaf-01.cfg"
expect "$CFG" "interface Vlan300"
expect "$CFG" "ip address virtual 10.30.0.1/24"
expect "$CFG" "vxlan vlan 300 vni 20000"
if [ "$(grep -c '^vlan 300$' "$CFG")" -ne 1 ]; then echo "FAIL: $CFG defines vlan 300 more than once" >&2; fail=1; fi

if [ "$fail" -ne 0 ]; then
  echo "--- report ---"; cat "$REPORT"; echo "--- endpoints ---"; cat "$ENDPOINTS"
  exit 1
fi
echo "OK: mock discovery run produced the expected report ($(basename "$REPORT"))"
