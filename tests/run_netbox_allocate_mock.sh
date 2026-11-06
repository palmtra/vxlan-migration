#!/usr/bin/env bash
# playbooks/allocate_vni.yml against a mock NetBox (tests/mock_netbox/route_targets.json):
# reuse of a VLAN's route target per DC, the next free number per range and
# tenant (0000001 internal, anyone else external), paging, and the approval
# word before a route target is written.
#
#   tests/run_netbox_allocate_mock.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${MOCK_OUT:-$(mktemp -d)}"
cleanup() {
  kill "${NB_PID:-}" 2>/dev/null || true
  [ -n "${MOCK_OUT:-}" ] || rm -rf "$OUT"
}
trap cleanup EXIT
mkdir -p "$OUT"
export ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false
NB_PORT="${NB_PORT:-18093}"
python3 tests/mock_netbox/server.py "$NB_PORT" tests/mock_netbox/route_targets.json "$OUT/writes.jsonl" >/dev/null 2>&1 &
NB_PID=$!
for _ in $(seq 50); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $NB_PORT), 0.2)" 2>/dev/null && break
  sleep 0.1
done
fail=0
VLANS="$OUT/vlans"
mkdir -p "$VLANS/omaha" "$VLANS/lisle"

run() {  # run <name> <dc> <vlan> [extra args...]
  local name="$1" dc="$2" vid="$3"; shift 3
  ansible-playbook -i inventory/hosts.yml playbooks/allocate_vni.yml \
    -e manual_data_center="$dc" -e manual_vlan_id="$vid" -e vlan_db_dir="$VLANS" \
    -e netbox_url="http://127.0.0.1:$NB_PORT" -e netbox_token=test "$@" > "$OUT/$name.log" 2>&1
}
expect() {  # expect <name> <text>: whitespace-insensitive, the YAML callback wraps and quotes long lines
  if ! python3 - "$OUT/$1.log" "$2" <<'PY'
import re, sys
text = re.sub(r"\s+", " ", open(sys.argv[1], encoding="utf-8").read()).replace("''", "'")
sys.exit(0 if re.sub(r"\s+", " ", sys.argv[2]) in text else 1)
PY
  then
    echo "FAIL: $1 is missing: $2" >&2; fail=1
  fi
}

run reuse_omaha omaha 3052 || { cat "$OUT/reuse_omaha.log"; fail=1; }
expect reuse_omaha "VLAN 3052 (omaha): VNI 10010, route target 10010:3052 - in NetBox"
expect reuse_omaha "Range: internal 10000-10999 (tenant 0000001)"
expect reuse_omaha "Route targets for VLAN 3052 in other data centers' ranges: 22027:3052."

run reuse_lisle lisle 3052 || { cat "$OUT/reuse_lisle.log"; fail=1; }
expect reuse_lisle "VLAN 3052 (lisle): VNI 22027, route target 22027:3052 - in NetBox"
expect reuse_lisle "Range: external 22000-23999 (tenant 0000910)"

# FNTS: 10000-10011 and 10013 are route targets, 10012 an L2VPN identifier.
run fnts omaha 3060 || { cat "$OUT/fnts.log"; fail=1; }
expect fnts "VLAN 3060 (omaha): VNI 10014, route target 10014:3060 - lowest free number in omaha internal 10000-10999 (14 taken)"
expect fnts 'would write {"name": "10014:3060", "description": "FNTS_NEW - Created by Ansible VXLAN Automation", "tenant": 1}'

# Another customer: the external range, whatever the tenant.
run customer omaha 3061 || { cat "$OUT/customer.log"; fail=1; }
expect customer "VLAN 3061 (omaha): VNI 11003, route target 11003:3061"
expect customer "Range: external 11000-11999 (tenant 0000874)"

# No tenant in NetBox: the record's tenant decides.
cat > "$VLANS/omaha/3063_no_tenant.yml" <<'YAML'
id: 3063
name: no_tenant
action: migrate
service_type: l2
vrf: default
target_switches: [leaf1]
tenant: "0000001"
YAML
run record_tenant omaha 3063 || { cat "$OUT/record_tenant.log"; fail=1; }
expect record_tenant "VLAN 3063 (omaha): VNI 10014, route target 10014:3063"
expect record_tenant "Range: internal 10000-10999 (tenant 0000001)"

# A VNI the record already gives another VLAN's route target stops the run.
cat > "$VLANS/omaha/3064_clash.yml" <<'YAML'
id: 3064
name: clash
action: migrate
service_type: l2
vrf: default
vni: 10011
target_switches: [leaf1]
YAML
if run clash omaha 3064; then echo "FAIL: a VNI used by another VLAN was accepted" >&2; fail=1; fi
expect clash "VNI 10011 of VLAN 3064 is already in NetBox for another VLAN: 10011:2011."

# Writing needs the approval word.
if run no_approval omaha 3061 -e netbox_reserve_rt=true; then
  echo "FAIL: reserve without approval ran" >&2; fail=1
fi
expect no_approval "Refusing to write a route target to NetBox."
run reserve omaha 3061 -e netbox_reserve_rt=true -e netbox_reserve_approval=approve || { cat "$OUT/reserve.log"; fail=1; }
expect reserve "Written to NetBox (id"
python3 - "$OUT/writes.jsonl" <<'PY' || fail=1
import json, sys
writes = [json.loads(line) for line in open(sys.argv[1])]
expected = [{"method": "POST", "path": "/api/ipam/route-targets/",
             "body": {"name": "11003:3061", "description": "CUST_NEW - Created by Ansible VXLAN Automation", "tenant": 2}}]
if writes != expected:
    print("FAIL: NetBox writes: %s" % writes, file=sys.stderr)
    sys.exit(1)
PY
# The next run reuses what was written.
run reuse_written omaha 3061 || { cat "$OUT/reuse_written.log"; fail=1; }
expect reuse_written "VLAN 3061 (omaha): VNI 11003, route target 11003:3061 - in NetBox"

[ "$fail" -eq 0 ] || exit 1
echo "OK: NetBox VNI / route target allocation"
