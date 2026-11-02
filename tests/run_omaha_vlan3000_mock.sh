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
expect "$REPORT" "legacy SVI MTU 9216 is above the EOS maximum, so the fabric SVI is generated with 9214."
expect "$REPORT" "| oma01-blf01 | v0000822a | bgp fabric view | 10.230.0.0/24 iBGP from fnts-fntc03-aggpe-mls01, best"
expect "$REPORT" "| 3 | fntc-aggacc-sw05 | trunks, VLAN 3000 | ready |"
expect "$REPORT" "Not present on: oma01-blf01, oma01-blf02."
expect "$ENDPOINTS" "3000,10.230.0.20,00:50:56:b3:00:20,edge,fntc-aggacc-sw03,Et10,\"acme-db-01\",compute"
expect "$ENDPOINTS" "3000,10.230.0.40,00:50:56:b3:00:40,not_in_mac_table"
# Gateway MAC: HSRP v2 group 3000 today, the leaves' ip virtual-router mac-address after.
expect "$REPORT" "| Gateway MAC | today 00:00:0c:9f:fb:b8 (HSRP); fabric 00:1c:73:00:00:99: **changes at cutover** |"
expect "$REPORT" "The gateway MAC changes at cutover: hosts cache 10.230.0.1 as 00:00:0c:9f:fb:b8"

# MOP input and run index: valid against the schema, steps in change order.
MOP="$(ls "$OUT"/reports/omaha/ACME_APP/ACME_APP_mop_input_*.json)"
RUNIDX="$(ls "$OUT"/reports/omaha/_runs/run_*.json)"
python3 - "$MOP" "$RUNIDX" "$ROOT/schemas/mop_input.schema.json" <<'PY' || fail=1
import json, sys
import jsonschema
mop, index, schema = (json.load(open(path)) for path in sys.argv[1:4])
jsonschema.validate(mop, schema)
problems = []
def check(ok, text):
    if not ok:
        problems.append(text)
check(mop["vlan"] == {"id": 3000, "name": "ACME_APP", "data_center": "omaha", "migration_type": "l3",
                      "vrf": "v0000822a", "l2_vni": 11002, "l3_vni": 822, "status": "needs_decisions",
                      "l3_vni_source": "fabric: VRF v0000822a maps to it on fntc-aggacc-sw01, fntc-aggacc-sw02 (show interfaces Vxlan1)"},
      "vlan: %s" % mop["vlan"])
eos = [s for s in mop["steps"] if s["switch"] == "fntc-aggacc-sw05"][0]
check(eos["confirm"] == "configure confirm" and "configure session prune_v3000 abort" in eos["rollback"], "EOS teardown confirm/rollback")
phases = [(s["phase"], s["switch"]) for s in mop["steps"]]
check(phases == [
    ("0-prechecks", ""), ("0-prechecks", ""), ("0-prechecks", ""),
    ("1-deploy", "fntc-aggacc-sw01"), ("1-deploy", "fntc-aggacc-sw02"),
    ("4-gateway-cutover", "fnts-fntc03-aggpe-mls01"), ("4-gateway-cutover", "fnts-fntc03-aggpe-mls02"),
    ("5-teardown", "fnts-fntc03-aggpe-mls01"), ("5-teardown", "fnts-fntc03-aggpe-mls02"),
    ("5-teardown", "fntc-aggacc-sw05"), ("5-teardown", "fntc-aggacc-sw06"), ("6-verify", ""),
], "steps: %s" % phases)
cut = [s for s in mop["steps"] if s["phase"] == "4-gateway-cutover"][0]
check(cut["cli"] == "interface Vlan3000\n  shutdown" and cut["rollback"] == "interface Vlan3000\n  no shutdown", "cutover CLI")
check(mop["gateway"]["mac_change"]["changes"] is True, "gateway MAC change")
check([f["id"] for f in mop["findings"] if f["kind"] == "risk" and f["severity"] == "high"] == ["R-01"], "high risks")
check(mop["coverage"]["complete"] is True, "coverage")
check(index["order"] == [3000] and index["totals"]["switches_changed"] == 6, "run index: %s" % index["totals"])
paths = {item["path"] for item in index["files"]}
check("ACME_APP/" + mop["files"]["mop_input"] in paths and "ACME_APP/3000_ACME_APP.yml" in paths, "manifest")
if problems:
    print("FAIL: MOP input: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
PY

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
