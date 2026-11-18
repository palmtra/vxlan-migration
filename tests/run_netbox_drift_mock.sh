#!/usr/bin/env bash
# Run playbooks/netbox_drift.yml against a mock NetBox (read-only).
#
#   VLAN 3000  in sync (Lisle also has a VLAN 3000: the Omaha scope picks ours)
#   VLAN 3002  drift: VNI 11009 in the record, 11004 in NetBox; prefix in the
#              global table instead of v0000822a
#   VLAN 3003  missing from NetBox
# Then: nothing written; netbox_drift_fail_on_drift=true fails the run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${MOCK_OUT:-$(mktemp -d)}"
mkdir -p "$OUT"
cleanup() {
  kill "${SERVER_PID:-}" 2>/dev/null || true
  [ -n "${MOCK_OUT:-}" ] || rm -rf "$OUT"
}
trap cleanup EXIT
PORT="${MOCK_NETBOX_PORT:-18081}"
WRITES="$OUT/writes.jsonl"
: > "$WRITES"
python3 tests/mock_netbox/server.py "$PORT" tests/mock_netbox/drift_omaha.json "$WRITES" &
SERVER_PID=$!
for _ in $(seq 50); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 0.2)" 2>/dev/null && break
  sleep 0.1
done

drift_run() {
  local name="$1"; shift
  local rc=0
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml playbooks/netbox_drift.yml \
    -e manual_data_center=omaha \
    -e netbox_url="http://127.0.0.1:$PORT" -e netbox_token=test \
    -e vlan_db_dir="$ROOT/tests/mock_netbox/vlans" \
    -e '{"netbox_vlan_query_by_dc": {"omaha": {"group": "omaha-datacenter"}}}' \
    -e reports_dir="$OUT/$name" "$@" > "$OUT/$name.log" 2>&1 || rc=$?
  echo "$rc"
}

fail=0
rc="$(drift_run report)"
if [ "$rc" -ne 0 ]; then cat "$OUT/report.log"; echo "FAIL: drift run failed (rc=$rc)" >&2; exit 1; fi
python3 - "$OUT"/report/netbox_drift/omaha_*.json <<'PY' || fail=1
import json, sys
result = json.load(open(sys.argv[1]))
by_id = {r["vlan_id"]: r for r in result["reports"]}
want = {3000: "in_sync", 3002: "drift", 3003: "missing"}
got = {vid: by_id.get(vid, {}).get("status") for vid in want}
if got != want:
    sys.exit("FAIL: drift status %s, want %s" % (got, want))
fields = {i["field"]: i["status"] for i in by_id[3002]["items"]}
for field, status in (("vni", "differs"), ("prefix 10.232.0.0/24 vrf", "differs"),
                      ("ipam_route_target", "missing_in_netbox")):
    if fields.get(field) != status:
        sys.exit("FAIL: VLAN 3002 %s is %s, want %s" % (field, fields.get(field), status))
if by_id[3000]["netbox_vlan_id"] != 300:
    sys.exit("FAIL: VLAN 3000 matched NetBox VLAN %s, not Omaha's 300" % by_id[3000]["netbox_vlan_id"])
PY
grep -qF "| vni | 11009 | 11004 | differs |" "$OUT"/report/netbox_drift/omaha_*.md \
  || { echo "FAIL: the Markdown report does not show the VNI difference" >&2; fail=1; }
if [ -s "$WRITES" ]; then echo "FAIL: the drift check wrote to NetBox: $(cat "$WRITES")" >&2; fail=1; fi

rc="$(drift_run strict -e netbox_drift_fail_on_drift=true)"
if [ "$rc" -eq 0 ]; then echo "FAIL: netbox_drift_fail_on_drift=true did not fail on drift" >&2; fail=1; fi

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "OK: NetBox drift report against the mock NetBox"
