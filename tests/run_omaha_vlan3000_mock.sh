#!/usr/bin/env bash
# Omaha VLAN 3000 end to end against canned CLI: discover, write the VLAN
# record, generate the configs. No device, CloudVision or NetBox is touched.
#
# tests/mock_cli/omaha/ is a slice of the real Omaha inventory: the N7K vPC
# pair owns the HSRP gateway, AGGACC01_02 has the endpoints and the non-VTEP
# fntc-aggacc-sw03 behind it, AGGACC05_06 carries the VLAN with no endpoints,
# and the border pair learns the subnet over iBGP. The VLAN record goes to a
# temp VLAN DB (vars/vlans/ is not changed) and the AVD repo is copied.
#
#   tests/run_omaha_vlan3000_mock.sh                                 # discovery + Jinja configlets
#   AVD_REPO_DIR=../avd-vxlan-repo tests/run_omaha_vlan3000_mock.sh  # + AVD export and build (needs pyavd)
#   MOCK_OUT=/tmp/omaha tests/run_omaha_vlan3000_mock.sh             # keep the output
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${MOCK_OUT:-$(mktemp -d)}"
[ -n "${MOCK_OUT:-}" ] || trap 'rm -rf "$OUT"' EXIT
mkdir -p "$OUT"
export ANSIBLE_COLLECTIONS_PATH="$ROOT/tests/mock_cli:$ROOT/collections"
export ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false
MOCK_INV=tests/mock_cli/omaha/inventory/hosts.yml
fail=0
expect() {
  if ! grep -qF -- "$2" "$1"; then
    echo "FAIL: $(basename "$1") is missing: $2" >&2
    fail=1
  fi
}

# 1. Discover VLAN 3000 (not in the VLAN DB yet, so probe).
ansible-playbook -i "$MOCK_INV" playbooks/core/discover_vlan.yml \
  -e manual_data_center=omaha -e manual_vlan_id=3000 -e discovery_allow_probe=true \
  -e artifacts_dir="$OUT" --limit dc_omaha > "$OUT/discover.log" 2>&1 \
  || { cat "$OUT/discover.log"; echo "FAIL: discovery run failed" >&2; exit 1; }
REPORT="$(ls "$OUT"/reports/omaha/ACME_APP/ACME_APP_discovery_*.md)"
ENDPOINTS="$(ls "$OUT"/reports/omaha/ACME_APP/ACME_APP_endpoints_*.csv)"
SNIPPET="$OUT/reports/omaha/ACME_APP/3000_ACME_APP.yml"
expect "$REPORT" "Legacy MLS: fnts-fntc03-aggpe-mls01, fnts-fntc03-aggpe-mls02 (HSRP 10.230.0.1)"
expect "$REPORT" "| VRF | v0000822a |"
expect "$REPORT" "| VNI targets (VTEPs) | fntc-aggacc-sw01, fntc-aggacc-sw02 |"
expect "$REPORT" "| Keep VLAN, no VNI | fntc-aggacc-sw03 (non-VTEP) |"
expect "$REPORT" "VNI 11002 proposed (next free in omaha external 11000-11999)"
expect "$REPORT" "Legacy SVI MTU 9216 is above the EOS maximum; the fabric SVI uses 9214."
expect "$REPORT" "| oma01-blf01 | v0000822a | bgp fabric view | 10.230.0.0/24 iBGP from fnts-fntc03-aggpe-mls01, best"
expect "$REPORT" "| 3 | fntc-aggacc-sw05 | trunks, VLAN 3000 | ready |"
expect "$REPORT" "Not present on: oma01-blf01, oma01-blf02."
expect "$ENDPOINTS" "3000,10.230.0.20,00:50:56:b3:00:20,edge,fntc-aggacc-sw03,Et10,\"acme-db-01\",compute"
expect "$ENDPOINTS" "3000,10.230.0.40,00:50:56:b3:00:40,not_in_mac_table"

# 2. The engineer's part: paste the snippet as the VLAN record and decide
#    what discovery leaves open (gateway mode and leaves, the VRF's L3 VNI).
mkdir -p "$OUT/vlans/omaha"
python3 - "$SNIPPET" "$OUT/vlans/omaha/3000_acme_app.yml" <<'EOF'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
doc["gateway"]["mode"] = "distributed_anycast"
doc["placement"]["gateway_leafs"] = ["fntc-aggacc-sw01", "fntc-aggacc-sw02"]
doc["evpn"]["l3"]["vni"] = 822  # VRF v0000822a, from show interfaces Vxlan1
yaml.safe_dump(doc, open(sys.argv[2], "w", encoding="utf-8"), sort_keys=False)
EOF

# 3a. Legacy Jinja configlets, generate only (cvp_apply_configlets=false).
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=false -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/deploy" \
  --limit dc_omaha > "$OUT/deploy.log" 2>&1 \
  || { cat "$OUT/deploy.log"; echo "FAIL: generate-only deploy failed" >&2; exit 1; }
CFG="$OUT/deploy/reports/cvp_configlets/vxlan_vlan3000_fntc-aggacc-sw01.cfg"
expect "$CFG" " ip address virtual 10.230.0.1/24"
expect "$CFG" " vxlan vlan 3000 vni 11002"
expect "$CFG" "router bgp 4200000102"
expect "$CFG" "  rd 66.180.0.10:3000"
expect "$CFG" "  route-target both 11002:3000"
expect "$CFG" "  network 10.230.0.0/24"

# 3b. AVD services repo: export the record, then build with pyavd there.
if [ -n "${AVD_REPO_DIR:-}" ]; then
  cp -r "$AVD_REPO_DIR" "$OUT/avd-vxlan-repo"
  ansible-playbook -i inventory/hosts.yml playbooks/core/export_avd_services.yml \
    -e manual_data_center=omaha -e manual_vlan_id=3000 \
    -e vlan_db_dir="$OUT/vlans" -e avd_repo_dir="$OUT/avd-vxlan-repo" > "$OUT/export.log" 2>&1 \
    || { cat "$OUT/export.log"; echo "FAIL: AVD export failed" >&2; exit 1; }
  (cd "$OUT/avd-vxlan-repo" && python -m avd_services build --dc omaha) > "$OUT/avd_build.log" 2>&1 \
    || { cat "$OUT/avd_build.log"; echo "FAIL: AVD build failed" >&2; exit 1; }
  AVD_CFG="$OUT/avd-vxlan-repo/intended/configs/fntc-aggacc-sw02.cfg"
  expect "$OUT/avd-vxlan-repo/group_vars/OMAHA_SERVICES.yml" "- AGGACC01_02"
  expect "$AVD_CFG" "   ip address virtual 10.230.0.1/24"
  expect "$AVD_CFG" "   vxlan vlan 3000 vni 11002"
  expect "$AVD_CFG" "      rd 66.180.0.11:3000"
fi

if [ "$fail" -ne 0 ]; then
  echo "--- report ---"; cat "$REPORT"
  exit 1
fi
echo "OK: Omaha VLAN 3000 discovered and configs generated (output: $OUT)"
