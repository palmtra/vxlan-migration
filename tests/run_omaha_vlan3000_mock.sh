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
# VLAN 3000 is not in the VLAN DB yet: a blocker, and the VNI is only a proposal.
check(mop["vlan"] == {"id": 3000, "name": "ACME_APP", "data_center": "omaha", "migration_type": "l3",
                      "vrf": "v0000822a", "l2_vni": 11002, "l2_vni_source": "proposed", "l3_vni": 822, "status": "blocked",
                      "l3_vni_source": "fabric: VRF v0000822a maps to it on fntc-aggacc-sw01, fntc-aggacc-sw02 (show interfaces Vxlan1)"},
      "vlan: %s" % mop["vlan"])
check(mop["findings"][0]["text"].startswith("VLAN 3000 is not in the VLAN DB (vars/vlans/omaha/). Copy the starter record "
                                            "discovery wrote (reports/omaha/ACME_APP/3000_ACME_APP.yml)"), "not-in-DB blocker first")
eos = [s for s in mop["steps"] if s["switch"] == "fntc-aggacc-sw05"][0]
check(eos["confirm"] == "configure session prune_v3000 commit" and "configure session prune_v3000 abort" in eos["rollback"], "EOS teardown confirm/rollback")
phases = [(s["phase"], s["switch"]) for s in mop["steps"]]
check(phases == [
    ("0-prechecks", ""), ("0-prechecks", ""), ("0-prechecks", ""), ("0-prechecks", ""),
    ("1-gateway-shutdown", "fnts-fntc03-aggpe-mls01"), ("1-gateway-shutdown", "fnts-fntc03-aggpe-mls02"),
    ("2-deploy", "fntc-aggacc-sw01"), ("2-deploy", "fntc-aggacc-sw02"),
    ("5-validate", ""),
    ("7-teardown", "fntc-aggacc-sw05"), ("7-teardown", "fntc-aggacc-sw06"),
    ("8-source-gateway-teardown", "fnts-fntc03-aggpe-mls01"), ("8-source-gateway-teardown", "fnts-fntc03-aggpe-mls01"),
    ("8-source-gateway-teardown", "fnts-fntc03-aggpe-mls02"), ("8-source-gateway-teardown", "fnts-fntc03-aggpe-mls02"),
    ("9-verify", ""),
], "steps: %s" % phases)
cut = [s for s in mop["steps"] if s["phase"] == "1-gateway-shutdown"][0]
check(cut["manual"] is True and cut["cli"] == "interface Vlan3000\n  shutdown" and cut["rollback"] == "interface Vlan3000\n  no shutdown", "shutdown CLI")
svi = [s for s in mop["steps"] if s["phase"] == "8-source-gateway-teardown"][0]
check(svi["cli"] == "checkpoint prune_v3000_svi\nconfigure terminal\nno interface Vlan3000\nend\n", "source gateway SVI step: %r" % svi["cli"])
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
# 3n. Generate again from the record alone (deploy_discovery=false): no switch
#     is contacted (all unreachable here), and the configlets are the same.
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=false -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/deploy_nodisc" \
  -e deploy_discovery=false -e mock_unreachable=true \
  --limit dc_omaha > "$OUT/deploy_nodisc.log" 2>&1 \
  || { cat "$OUT/deploy_nodisc.log"; echo "FAIL: generate without discovery failed" >&2; exit 1; }
expect "$OUT/deploy_nodisc.log" "deploy_discovery=false: discovery skipped, no switch contacted"
if grep -q "UNREACHABLE\|unreachable=[1-9]" "$OUT/deploy_nodisc.log"; then
  echo "FAIL: deploy_discovery=false contacted a switch" >&2; fail=1
fi
diff -r "$OUT/deploy/reports/cvp_configlets" "$OUT/deploy_nodisc/reports/cvp_configlets" >/dev/null \
  || { echo "FAIL: configlets differ without discovery" >&2; fail=1; }

# 3r. Configlets live in reports/cvp_configlets/<dc>/vlan<id>/. A rerun replaces
#     this VLAN's files (a leaf no longer in the record disappears) and never
#     touches another VLAN's directory.
RR="$OUT/deploy_rerun/reports/cvp_configlets/omaha"
mkdir -p "$RR/vlan3000/configs" "$RR/vlan3001/configs"
echo stale > "$RR/vlan3000/configs/vxlan_vlan3000_removed-leaf.cfg"
echo other > "$RR/vlan3001/configs/vxlan_vlan3001_fntc-aggacc-sw01.cfg"
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=false -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/deploy_rerun" \
  -e deploy_discovery=false -e mock_unreachable=true \
  --limit dc_omaha > "$OUT/deploy_rerun.log" 2>&1 \
  || { cat "$OUT/deploy_rerun.log"; echo "FAIL: rerun of the generate-only deploy failed" >&2; exit 1; }
if [ -e "$RR/vlan3000/configs/vxlan_vlan3000_removed-leaf.cfg" ]; then
  echo "FAIL: a rerun kept a configlet for a leaf that is no longer in the record" >&2; fail=1
fi
if [ "$(cat "$RR/vlan3001/configs/vxlan_vlan3001_fntc-aggacc-sw01.cfg" 2>/dev/null)" != "other" ]; then
  echo "FAIL: deploying VLAN 3000 changed VLAN 3001's configlets" >&2; fail=1
fi
diff -r "$OUT/deploy/reports/cvp_configlets/omaha/vlan3000" "$RR/vlan3000" >/dev/null \
  || { echo "FAIL: the rerun's VLAN 3000 configlets differ from the first run" >&2; fail=1; }


# 3o. Rollback covers every switch the deploy changed. A centralized_anycast
#     gateway on the border pair, which is not in target_switches (Omaha VLAN
#     3014's shape): the deploy configures the SVI there, so the rollback must
#     render a configlet for both border leaves and remove the SVI and network.
mkdir -p "$OUT/vlans_central/omaha"
python3 - "$OUT/vlans/omaha/3000_acme_app.yml" "$OUT/vlans_central/omaha/3000_acme_app.yml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
doc["gateway"]["mode"] = "centralized_anycast"
doc["placement"]["gateway_leafs"] = ["oma01-blf01", "oma01-blf02"]
yaml.safe_dump(doc, open(sys.argv[2], "w", encoding="utf-8"), sort_keys=False)
PY
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=false -e deploy_discovery=false \
  -e vlan_db_dir="$OUT/vlans_central" -e artifacts_dir="$OUT/deploy_central" \
  --limit dc_omaha > "$OUT/deploy_central.log" 2>&1 \
  || { cat "$OUT/deploy_central.log"; echo "FAIL: centralized-gateway deploy failed" >&2; exit 1; }
ansible-playbook -i "$MOCK_INV" tests/playbooks/cvp_rollback_render.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha \
  -e vlan_db_dir="$OUT/vlans_central" -e artifacts_dir="$OUT/rollback_central" \
  --limit dc_omaha > "$OUT/rollback_central.log" 2>&1 \
  || { cat "$OUT/rollback_central.log"; echo "FAIL: centralized-gateway rollback render failed" >&2; exit 1; }
CENTRAL_CFG="$OUT/deploy_central/reports/cvp_configlets/omaha/vlan3000/configs"
CENTRAL_RB="$OUT/rollback_central/reports/cvp_configlets/omaha/vlan3000/rollback"
# ls || true: a missing directory is reported below, not an exit under set -e.
DEPLOYED="$(ls "$CENTRAL_CFG" 2>/dev/null || true)"
ROLLED_BACK="$(ls "$CENTRAL_RB" 2>/dev/null || true)"
if [ -z "$DEPLOYED" ]; then
  echo "FAIL: the centralized-gateway deploy rendered no configlets in $CENTRAL_CFG" >&2; fail=1
elif [ "$DEPLOYED" != "$ROLLED_BACK" ]; then
  echo "FAIL: rollback configlets ($(echo $ROLLED_BACK)) differ from the deploy's ($(echo $DEPLOYED))" >&2; fail=1
fi
for gw in oma01-blf01 oma01-blf02; do
  expect "$CENTRAL_CFG/vxlan_vlan3000_$gw.cfg" " ip address virtual 10.230.0.1/24"
  RB="$CENTRAL_RB/vxlan_vlan3000_$gw.cfg"
  [ -f "$RB" ] || { echo "FAIL: no rollback configlet for gateway leaf $gw" >&2; fail=1; continue; }
  expect "$RB" "no interface Vlan3000"
  expect "$RB" " no vxlan vlan 3000 vni 11002"
  expect "$RB" " no vlan 3000"
  expect "$RB" "  no network 10.230.0.0/24"
done
RB_LEAF="$CENTRAL_RB/vxlan_vlan3000_fntc-aggacc-sw01.cfg"
expect "$RB_LEAF" " no vxlan vlan 3000 vni 11002"
if grep -q "interface Vlan3000\|network 10.230.0.0/24" "$RB_LEAF"; then
  echo "FAIL: rollback touches an SVI on a non-gateway leaf (centralized)" >&2; fail=1
fi

# 3p. Resume verification on its own (workflow_verify.yml -e resume_vlan_id),
#     as after a CloudVision approval. The mock CLI still shows the switches
#     before the change, so verification fails and the auto-rollback renders
#     (cvp_apply_configlets=false) the same configlets as 3o; the run fails.
mkdir -p "$OUT/verify_resume/reports"
cp -r "$OUT/deploy_central/reports/workflow_state" "$OUT/verify_resume/reports/"
vr_rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=3000 -e manual_data_center=omaha \
  -e auto_rollback_on_verification_failure=true -e cvp_apply_configlets=false \
  -e vlan_db_dir="$OUT/vlans_central" -e artifacts_dir="$OUT/verify_resume" \
  --limit dc_omaha > "$OUT/verify_resume.log" 2>&1 || vr_rc=$?
if [ "$vr_rc" -eq 0 ]; then echo "FAIL: a failed verification did not fail the resumed run" >&2; fail=1; fi
if grep -q "is undefined" "$OUT/verify_resume.log"; then
  grep -m1 -B2 "is undefined" "$OUT/verify_resume.log" >&2
  echo "FAIL: resumed verification hit an undefined variable" >&2; fail=1
fi
expect "$OUT/verify_resume.log" "Post-change verification failed on oma01-blf01."
expect "$OUT/verify_resume.log" "TASK [cvp_rollback : Report generated rollback configlets]"
expect "$OUT/verify_resume.log" "Rollback re-verification failed on"
RESUMED="$(ls "$OUT/verify_resume/reports/cvp_configlets/omaha/vlan3000/rollback" 2>/dev/null || true)"
if [ -z "$DEPLOYED" ] || [ "$DEPLOYED" != "$RESUMED" ]; then
  echo "FAIL: auto-rollback configlets ($(echo $RESUMED)) differ from the deploy's ($(echo $DEPLOYED))" >&2; fail=1
fi

CFG="$OUT/deploy/reports/cvp_configlets/omaha/vlan3000/configs/vxlan_vlan3000_fntc-aggacc-sw01.cfg"
expect "$CFG" " ip address virtual 10.230.0.1/24"
expect "$CFG" " vxlan vlan 3000 vni 11002"
expect "$CFG" "router bgp 4200000102"
expect "$CFG" "  rd 66.180.0.10:3000"
expect "$CFG" "  route-target both 11002:3000"
expect "$CFG" "  network 10.230.0.0/24"

# 3c. Push to CloudVision (mock arista.cvp): configlets, device assignment in
#     each switch's current container, one change control for the tasks, and
#     the wait until it has run.
export MOCK_CVP_LOG="$OUT/cvp_calls.jsonl"
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=true -e cvp_change_control_poll_seconds=1 \
  -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/push" -e reports_dir="$OUT/push/reports" \
  --limit dc_omaha > "$OUT/push.log" 2>&1 \
  || { cat "$OUT/push.log"; echo "FAIL: CloudVision push failed" >&2; exit 1; }
python3 - "$MOCK_CVP_LOG" "$OUT/push/reports/cvp_change_controls" <<'PY' || fail=1
import glob, json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
problems = []
def check(ok, text):
    if not ok:
        problems.append(text)
modules = [c["module"] for c in calls]
check(modules == ["cv_facts_v3", "cv_configlet_v3", "cv_device_v3", "cv_change_control_v3", "cv_change_control_v3"],
      "call order: %s" % modules)
check(all(c["host"] for c in calls), "every call ran with an inventory host")
configlets = calls[1]["args"]["configlets"]
check(sorted(configlets) == ["vxlan_vlan3000_fntc-aggacc-sw01", "vxlan_vlan3000_fntc-aggacc-sw02"],
      "configlets pushed: %s" % sorted(configlets))
check(" vxlan vlan 3000 vni 11002" in configlets["vxlan_vlan3000_fntc-aggacc-sw01"], "configlet content")
devices = calls[2]["args"]
check(devices["apply_mode"] == "loose" and devices["search_key"] == "hostname", "device apply mode")
check([(d["hostname"], d["parentContainerName"], d["configlets"]) for d in devices["devices"]] == [
    ("fntc-aggacc-sw01", "Leaves", ["vxlan_vlan3000_fntc-aggacc-sw01"]),
    ("fntc-aggacc-sw02", "Leaves", ["vxlan_vlan3000_fntc-aggacc-sw02"])], "device assignments: %s" % devices["devices"])
change = calls[3]["args"]
check(change["state"] == "set", "change control created pending (state set)")
check([a["task_id"] for a in change["change"]["activities"]] == ["101", "102"], "change control tasks")
check(change["change"]["name"] == "VXLAN migration - VLAN 3000", "change control name")
check(calls[4]["args"] == {"state": "show", "change_id": ["mock-cc-vlan"]}, "waited on the change control")
records = glob.glob(sys.argv[2] + "/vlan3000_cvp_*.json")
record = json.load(open(records[0])) if records else {}
check(record.get("status") == "completed" and record.get("change_control_id") == "mock-cc-vlan"
      and record.get("task_ids") == ["101", "102"], "change-control record: %s" % record)
if problems:
    print("FAIL: CloudVision push: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
PY

# 3d. A change control that fails in CloudVision fails the run, with the error.
rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=true -e cvp_change_control_poll_seconds=1 \
  -e "mock_cvp_cc_error='Task 101 failed: config session error'" \
  -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/push_fail" -e reports_dir="$OUT/push_fail/reports" \
  --limit dc_omaha > "$OUT/push_fail.log" 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then echo "FAIL: a failed change control did not fail the run" >&2; fail=1; fi
expect "$OUT/push_fail.log" "failed in CloudVision: Task 101 failed"

# 3e. Nobody approves in time: the run ends without failing, says how to resume,
#     and the resume playbook waits for it (fails while pending, passes once run).
ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
  -e cvp_apply_configlets=true -e cvp_change_control_poll_seconds=1 -e cvp_change_control_wait_minutes=0 \
  -e mock_cvp_cc_status=CHANGE_CONTROL_STATUS_NOT_STARTED \
  -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/push_wait" -e reports_dir="$OUT/push_wait/reports" \
  --limit dc_omaha > "$OUT/push_wait.log" 2>&1 \
  || { cat "$OUT/push_wait.log"; echo "FAIL: a pending change control failed the run" >&2; exit 1; }
expect "$OUT/push_wait.log" "cvp_change_control_id=mock-cc-vlan -e target_vlan_id=3000"
expect "$(ls "$OUT"/push_wait/reports/cvp_change_controls/vlan3000_cvp_*.json)" '"status": "timed_out"'
rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/cvp_change_control_wait.yml \
  -e cvp_change_control_id=mock-cc-vlan -e target_vlan_id=3000 \
  -e cvp_change_control_wait_minutes=0 -e cvp_change_control_poll_seconds=1 \
  -e mock_cvp_cc_status=CHANGE_CONTROL_STATUS_NOT_STARTED > "$OUT/resume_pending.log" 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then echo "FAIL: resume passed while the change control was pending" >&2; fail=1; fi
ansible-playbook -i "$MOCK_INV" playbooks/core/cvp_change_control_wait.yml \
  -e cvp_change_control_id=mock-cc-vlan -e target_vlan_id=3000 -e cvp_change_control_poll_seconds=1 \
  > "$OUT/resume_done.log" 2>&1 \
  || { cat "$OUT/resume_done.log"; echo "FAIL: resume failed on a completed change control" >&2; fail=1; }
expect "$OUT/resume_done.log" "Change control mock-cc-vlan: completed"
unset MOCK_CVP_LOG

# 3f. VNI and route targets from NetBox: the VXLAN-EVPN L2VPN terminated on the
#     VLAN (mock NetBox). 4.x API with matching values; 3.x API (no /api/vpn/)
#     with different import and export targets; a VNI that conflicts stops the run.
NB_PORT="${MOCK_NETBOX_PORT:-18081}"
netbox_deploy() {
  local name="$1" seed="$2" pid
  python3 tests/mock_netbox/server.py "$NB_PORT" "tests/mock_netbox/$seed" "$OUT/$name.writes" &
  pid=$!
  for _ in $(seq 50); do
    python3 -c "import socket; socket.create_connection(('127.0.0.1', $NB_PORT), 0.2)" 2>/dev/null && break
    sleep 0.1
  done
  nb_rc=0
  ansible-playbook -i "$MOCK_INV" playbooks/core/workflow_deploy.yml \
    -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=v0000822a \
    -e cvp_apply_configlets=false -e netbox_url="http://127.0.0.1:$NB_PORT" -e netbox_token=test \
    -e vlan_db_dir="$OUT/vlans" -e artifacts_dir="$OUT/$name" \
    --limit dc_omaha > "$OUT/$name.log" 2>&1 || nb_rc=$?
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
netbox_deploy nb_v4 omaha_evpn_v4.json
if [ "$nb_rc" -ne 0 ]; then cat "$OUT/nb_v4.log"; echo "FAIL: deploy with NetBox L2VPN failed" >&2; fail=1; fi
expect "$OUT/nb_v4.log" "NetBox L2VPN ACME_APP: VNI 11002, import 11002:3000, export 11002:3000"
expect "$OUT/nb_v4/reports/cvp_configlets/omaha/vlan3000/configs/vxlan_vlan3000_fntc-aggacc-sw01.cfg" "  route-target both 11002:3000"
netbox_deploy nb_v3 omaha_evpn_v3_split_rt.json
if [ "$nb_rc" -ne 0 ]; then cat "$OUT/nb_v3.log"; echo "FAIL: deploy with NetBox 3.x L2VPN failed" >&2; fail=1; fi
NB3_CFG="$OUT/nb_v3/reports/cvp_configlets/omaha/vlan3000/configs/vxlan_vlan3000_fntc-aggacc-sw01.cfg"
expect "$NB3_CFG" "  route-target import 65000:3000"
expect "$NB3_CFG" "  route-target export 65000:3001"
if grep -qF "route-target both" "$NB3_CFG"; then echo "FAIL: split NetBox route targets rendered as both" >&2; fail=1; fi
netbox_deploy nb_conflict omaha_evpn_conflict.json
if [ "$nb_rc" -eq 0 ]; then echo "FAIL: a VNI conflict with NetBox did not stop the deploy" >&2; fail=1; fi
expect "$OUT/nb_conflict.log" "VNI conflict for VLAN 3000: the record says 11002, NetBox L2VPN ACME_APP"
if [ -n "$(find "$OUT/nb_conflict/reports/cvp_configlets" -name '*.cfg' 2>/dev/null)" ]; then
  echo "FAIL: configlets were rendered despite the NetBox conflict" >&2; fail=1
fi

# 3g. One command, end to end (playbooks/core/migrate_vlan.yml): the VLAN record
#     has no VNI, NetBox's L2VPN supplies it; discovery, readiness gate, deploy,
#     CloudVision change control, verification against the switches as they are
#     after the change (the mock change control flips the mock CLI to post/).
mkdir -p "$OUT/vlans_novni/omaha"
python3 - "$OUT/vlans/omaha/3000_acme_app.yml" "$OUT/vlans_novni/omaha/3000_acme_app.yml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
doc["evpn"]["l2"].pop("vni", None)
doc["evpn"]["l2"].pop("vni_proposed", None)
yaml.safe_dump(doc, open(sys.argv[2], "w", encoding="utf-8"), sort_keys=False)
PY
migrate_run() {
  local name="$1"; shift
  mig_rc=0
  ansible-playbook -i "$MOCK_INV" playbooks/core/migrate_vlan.yml \
    -e manual_vlan_id=3000 -e manual_data_center=omaha -e cvp_change_control_poll_seconds=1 \
    -e artifacts_dir="$OUT/$name" "$@" --limit dc_omaha > "$OUT/$name.log" 2>&1 || mig_rc=$?
}
python3 tests/mock_netbox/server.py "$NB_PORT" tests/mock_netbox/omaha_evpn_v4.json "$OUT/migrate.writes" &
NB_PID=$!
for _ in $(seq 50); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $NB_PORT), 0.2)" 2>/dev/null && break
  sleep 0.1
done
export MOCK_CVP_LOG="$OUT/migrate_cvp.jsonl" MOCK_CLI_STATE_FILE="$OUT/migrate_cli_state"
migrate_run migrate -e vlan_db_dir="$OUT/vlans_novni" -e netbox_url="http://127.0.0.1:$NB_PORT" -e netbox_token=test
unset MOCK_CVP_LOG MOCK_CLI_STATE_FILE
kill "$NB_PID" 2>/dev/null || true
wait "$NB_PID" 2>/dev/null || true
if [ "$mig_rc" -ne 0 ]; then cat "$OUT/migrate.log"; echo "FAIL: migrate_vlan.yml failed" >&2; fail=1; fi
python3 - "$(ls "$OUT"/migrate/reports/omaha/_runs/migrate_vlan3000_*.json 2>/dev/null | head -1)" <<'PY' || fail=1
import json, sys
summary = json.load(open(sys.argv[1])) if sys.argv[1] else {}
problems = []
def check(ok, text):
    if not ok:
        problems.append(text)
check(summary.get("outcome") == "deployed_and_verified", "outcome: %s" % summary.get("outcome"))
check(summary.get("vni") == 11002, "VNI from NetBox: %s" % summary.get("vni"))
check(summary.get("netbox", {}).get("sources", {}).get("vni") in ("netbox", "record and netbox"), "NetBox VNI source")
check(summary.get("readiness", {}).get("ready") is True and summary["readiness"]["decisions"] == [], "gate passed with no decision")
cv = summary.get("cloudvision", {})
check(cv.get("status") == "completed" and cv.get("vlan_id") == 3000, "change control: %s" % cv)
check(summary.get("verification", {}).get("ran") and summary["verification"]["failed_switches"] == [], "verification")
prune = summary.get("prune", {})
check(prune.get("checked_after_deploy") is True and isinstance(prune.get("plans"), list), "prune readiness: %s" % prune)
ep = summary.get("endpoints", {})
check(ep.get("compared") is True and ep.get("total", 0) > 0 and ep.get("csv", "").endswith(".csv"), "endpoint comparison: %s" % ep)
check("rows" not in ep and isinstance(ep.get("new"), int), "endpoint rows stay in the CSV, not the summary")
if problems:
    print("FAIL: migrate summary: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
PY
expect "$OUT/migrate.log" "VLAN 3000: deployed_and_verified"
expect "$OUT/migrate.log" "Prune after the deploy:"
if ! ls "$OUT"/migrate/reports/omaha/_prune/prune_dry_run_*.md >/dev/null 2>&1; then
  echo "FAIL: migrate wrote no prune dry run" >&2; fail=1
fi
expect "$OUT/migrate.log" "Mode: dry-run."
expect "$OUT/migrate.log" "Preflight: OK"
expect "$OUT/migrate.log" "deploy_discovery=false: discovery skipped"
expect "$OUT/migrate.log" "Endpoints: "
if ! ls "$OUT"/migrate/reports/omaha/_runs/migrate_vlan3000_endpoints_*.csv >/dev/null 2>&1; then
  echo "FAIL: migrate wrote no endpoint comparison CSV" >&2; fail=1
fi
if ! ls "$OUT"/migrate/reports/omaha/_runs/preflight_vlan3000_*.json >/dev/null 2>&1; then
  echo "FAIL: migrate wrote no preflight result" >&2; fail=1
fi

# 3h. The same record without NetBox: no VNI source, so the preflight stops
#     the run before discovery. With the preflight off, discovery proposes a
#     VNI, an open decision, so the gate stops before anything is deployed.
migrate_run migrate_novni -e vlan_db_dir="$OUT/vlans_novni"
if [ "$mig_rc" -eq 0 ]; then echo "FAIL: migrate ran without a VNI source" >&2; fail=1; fi
expect "$(ls "$OUT"/migrate_novni/reports/omaha/_runs/preflight_vlan3000_*.json | head -1)" "no VNI in the record and NetBox is not configured"
expect "$OUT/migrate_novni.log" "Preflight failed (1 check(s)):"
if grep -q "TASK \[vlan_discovery" "$OUT/migrate_novni.log"; then echo "FAIL: discovery ran after a failed preflight" >&2; fail=1; fi
migrate_run migrate_decision -e vlan_db_dir="$OUT/vlans_novni" -e migrate_preflight=false
if [ "$mig_rc" -eq 0 ]; then echo "FAIL: migrate deployed with an open decision" >&2; fail=1; fi
expect "$OUT/migrate_decision.log" "decision(s) are open"
if [ -n "$(find "$OUT/migrate_decision/reports/cvp_configlets" -name '*.cfg' 2>/dev/null)" ]; then
  echo "FAIL: configlets rendered past a closed gate" >&2; fail=1
fi

# 3i. A VLAN that is not in the VLAN DB stops at the gate: discovery only.
mkdir -p "$OUT/vlans_empty/omaha"
migrate_run migrate_probe -e vlan_db_dir="$OUT/vlans_empty"
if [ "$mig_rc" -eq 0 ]; then echo "FAIL: migrate deployed a VLAN that is not in the VLAN DB" >&2; fail=1; fi
expect "$OUT/migrate_probe.log" "VLAN 3000 is not in the VLAN DB"

# 3j. migrate never prunes: prune_apply is refused before discovery starts.
migrate_run migrate_prune -e prune_apply=true -e prune_approval=approve
if [ "$mig_rc" -eq 0 ]; then echo "FAIL: migrate accepted prune_apply" >&2; fail=1; fi
expect "$OUT/migrate_prune.log" "migrate_vlan.yml never prunes"
if grep -q "TASK \[vlan_discovery" "$OUT/migrate_prune.log"; then echo "FAIL: discovery ran before the prune refusal" >&2; fail=1; fi

# 3k. Preflight finds what would break the run, without changing anything:
#     a target switch CloudVision does not manage, a VNI NetBox disagrees with.
start_netbox() {
  python3 tests/mock_netbox/server.py "$NB_PORT" "tests/mock_netbox/$1" "$OUT/$2.writes" &
  NB_PID=$!
  for _ in $(seq 50); do
    python3 -c "import socket; socket.create_connection(('127.0.0.1', $NB_PORT), 0.2)" 2>/dev/null && break
    sleep 0.1
  done
}
stop_netbox() { kill "$NB_PID" 2>/dev/null || true; wait "$NB_PID" 2>/dev/null || true; }
start_netbox omaha_evpn_conflict.json preflight_fail
pf_rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/preflight.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e vlan_db_dir="$OUT/vlans" \
  -e netbox_url="http://127.0.0.1:$NB_PORT" -e netbox_token=test -e artifacts_dir="$OUT/preflight_fail" \
  -e '{"mock_cvp_devices": [{"hostname": "fntc-aggacc-sw01", "parentContainerName": "Leaves"}]}' \
  --limit dc_omaha > "$OUT/preflight_fail.log" 2>&1 || pf_rc=$?
stop_netbox
if [ "$pf_rc" -eq 0 ]; then echo "FAIL: preflight passed with a missing CloudVision switch and a VNI conflict" >&2; fail=1; fi
expect "$OUT/preflight_fail.log" "Preflight: FAIL"
expect "$OUT/preflight_fail.log" "Preflight failed (2 check(s)):"
expect "$OUT/preflight_fail.log" "cloudvision | fntc-aggacc-sw02 | not in CloudVision"
expect "$OUT/preflight_fail.log" "the record says 11002, NetBox L2VPN"
python3 - "$(ls "$OUT"/preflight_fail/reports/omaha/_runs/preflight_vlan3000_*.json 2>/dev/null | head -1)" <<'PY' || fail=1
import json, sys
result = json.load(open(sys.argv[1])) if sys.argv[1] else {}
switches = [c for c in result.get("checks", []) if c["check"] == "switch"]
problems = []
if result.get("status") != "fail" or result.get("failed") != 2:
    problems.append("status %s, failed %s" % (result.get("status"), result.get("failed")))
if len(switches) != 9 or any(c["status"] != "ok" for c in switches):
    problems.append("switch checks: %s" % switches)
nxos = [c for c in switches if c["target"] == "fnts-fntc03-aggpe-mls01"]
if not nxos or nxos[0]["detail"] != "NXOS Nexus7000 C7010 8.4(2)":
    problems.append("NX-OS identity: %s" % nxos)
if problems:
    print("FAIL: preflight result: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
PY

# 3l. Validation data capture: switch outputs (preflight + discovery), NetBox
#     answers and CloudVision results, secrets removed, packed as .tar.gz.
CAP="$OUT/capture/omaha_vlan3000"
start_netbox omaha_evpn_v4.json capture
cap_rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/collect_validation_data.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e vlan_db_dir="$OUT/vlans_novni" \
  -e netbox_url="http://127.0.0.1:$NB_PORT" -e netbox_token=test -e cvp_change_control_id=mock-cc-1 \
  -e vxlan_capture_dir="$CAP" --limit dc_omaha > "$OUT/capture.log" 2>&1 || cap_rc=$?
stop_netbox
if [ "$cap_rc" -ne 0 ]; then cat "$OUT/capture.log"; echo "FAIL: collect_validation_data.yml failed" >&2; fail=1; fi
for f in README.txt capture_index.json manifest.json netbox/status.json netbox/vpn_l2vpns_5.json \
         cloudvision/001_cv_facts_v3.json devices/fntc-aggacc-sw01/show_version.txt \
         devices/fntc-aggacc-sw01/show_vxlan_vlan_to_vni.txt devices/fnts-fntc03-aggpe-mls01/show_version.txt; do
  [ -f "$CAP/$f" ] || { echo "FAIL: capture has no $f" >&2; fail=1; }
done
[ -f "$CAP.tar.gz" ] || { echo "FAIL: capture was not packed" >&2; fail=1; }
if grep -rq "Token test" "$CAP"; then echo "FAIL: the NetBox token is in the capture" >&2; fail=1; fi
ls "$CAP"/cloudvision/*_cv_change_control_v3.json >/dev/null 2>&1 || { echo "FAIL: no change control in the capture" >&2; fail=1; }
expect "$CAP/netbox/vpn_l2vpns_5.json" '"identifier": 11002'

# 3m. A capture replays through the mock CLI as is: discovery from the
#     captured outputs finds the same endpoints as discovery from the fixtures.
rp_rc=0
ansible-playbook -i "$MOCK_INV" playbooks/core/discover_vlan.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha -e vlan_db_dir="$OUT/vlans_novni" \
  -e mock_cli_root="$CAP" -e reports_dir="$OUT/replay/reports" --limit dc_omaha > "$OUT/replay.log" 2>&1 || rp_rc=$?
if [ "$rp_rc" -ne 0 ]; then cat "$OUT/replay.log"; echo "FAIL: discovery from the capture failed" >&2; fail=1; fi
python3 - "$CAP/reports" "$OUT/replay/reports" <<'PY' || fail=1
import glob, json, sys
def endpoints(root):
    files = sorted(glob.glob(root + "/omaha/*/*_discovery_*.json"))
    report = json.load(open(files[-1])) if files else {}
    table = (report.get("discovery") or {}).get("endpoint_table") or {}
    return sorted((r.get("ip"), r.get("mac"), r.get("hostname"), r.get("interface")) for r in table.get("endpoints") or [])
original, replayed = endpoints(sys.argv[1]), endpoints(sys.argv[2])
if not original or original != replayed:
    print("FAIL: replayed endpoints differ: %d vs %d" % (len(original), len(replayed)), file=sys.stderr)
    sys.exit(1)
PY

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
