#!/usr/bin/env bash
# Run playbooks/export_vlan_to_netbox.yml against a mock NetBox
# (tests/mock_netbox/server.py) and check the route targets it writes.
#
#   1. dry run, VLANs 951, 1521, 960: nothing written
#   2. apply, same VLANs: 22010:1521 updated, 20951:951 created, 960 ignored;
#      VLAN 951 created in Lisle's VLAN group with its tenant (Omaha's VLAN 951
#      untouched) and its prefix in VRF v0000001a, assigned to it
#   3. apply, VLANs 951, 952: fails on 952's unknown owner, nothing written for it
#   4. apply with a token NetBox refuses: fails with HTTP 403 and the reason
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${MOCK_OUT:-$(mktemp -d)}"
cleanup() {
  kill "${SERVER_PID:-}" 2>/dev/null || true
  [ -n "${MOCK_OUT:-}" ] || rm -rf "$OUT"
}
trap cleanup EXIT
PORT="${MOCK_NETBOX_PORT:-18080}"
WRITES="$OUT/writes.jsonl"
: > "$WRITES"

python3 tests/mock_netbox/server.py "$PORT" tests/mock_netbox/seed.json "$WRITES" &
SERVER_PID=$!
for _ in $(seq 50); do
  python3 -c "import socket,sys; socket.create_connection(('127.0.0.1', $PORT), 0.2)" 2>/dev/null && break
  sleep 0.1
done

export_run() {
  local name="$1" vlans="$2" dry_run="$3" token="${4:-test}"
  local rc=0
  ansible-playbook -i tests/mock_cli/inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
    -e manual_data_center=lisle \
    -e target_vlan_ids="$vlans" \
    -e netbox_export_dry_run="$dry_run" \
    -e netbox_url="http://127.0.0.1:$PORT" \
    -e netbox_token="$token" \
    -e netbox_validate_certs=false \
    -e vlan_db_dir="$ROOT/tests/mock_netbox/vlans" \
    -e reports_dir="$OUT/$name" \
    > "$OUT/$name.log" 2>&1 || rc=$?
  echo "$rc"
}

fail=0
check() {
  if ! python3 - "$WRITES" "$@"; then
    fail=1
  fi
}

rc="$(export_run dry 951,1521,960 true)"
if [ "$rc" -ne 0 ]; then cat "$OUT/dry.log"; echo "FAIL: dry run failed (rc=$rc)" >&2; exit 1; fi
check <<'EOF'
import sys
writes = [line for line in open(sys.argv[1]) if line.strip()]
if writes:
    sys.exit("FAIL: dry run wrote to NetBox: %s" % writes)
EOF
grep -qF '"netbox_status": "dry_run_would_update"' "$OUT"/dry/netbox_export/*.json \
  || { echo "FAIL: dry run did not plan to update 22010:1521" >&2; fail=1; }
grep -qF '"netbox_status": "dry_run_would_create"' "$OUT"/dry/netbox_export/*.json \
  || { echo "FAIL: dry run did not plan to create 20951:951" >&2; fail=1; }

rc="$(export_run apply 951,1521,960 false)"
if [ "$rc" -ne 0 ]; then cat "$OUT/apply.log"; echo "FAIL: apply failed (rc=$rc)" >&2; exit 1; fi
check <<'EOF'
import json, sys
writes = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
rts = [w for w in writes if w["path"].startswith("/api/ipam/route-targets/")]
expected = [
    ("PATCH", "/api/ipam/route-targets/31/",
     {"name": "22010:1521", "tenant": "11", "owner": "21",
      "description": "REWARDS_APP - Created by Ansible VXLAN Automation"}),
    ("POST", "/api/ipam/route-targets/",
     {"name": "20951:951", "tenant": "12", "owner": "22",
      "description": "WEB - Created by Ansible VXLAN Automation"}),
]
got = sorted((w["method"], w["path"], {k: str(v) for k, v in w["body"].items()}) for w in rts)
want = sorted(expected)
if got != want:
    sys.exit("FAIL: route target writes\n  got:  %s\n  want: %s" % (got, want))
EOF
check <<'EOF'
import json, sys
writes = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
if any(w["path"] == "/api/ipam/vlans/400/" for w in writes):
    sys.exit("FAIL: the Lisle export wrote Omaha's VLAN 951 (another data center)")
vlans = [w for w in writes if w["method"] == "POST" and w["path"] == "/api/ipam/vlans/"]
web = [w["body"] for w in vlans if w["body"].get("vid") == 951]
if len(web) != 1 or web[0].get("group") != 8 or web[0].get("tenant") != 12 or "site" in web[0]:
    sys.exit("FAIL: VLAN 951 not created in Lisle's group with tenant 0000001: %s" % web)
rewards = [w["body"] for w in vlans if w["body"].get("vid") == 1521]
if len(rewards) != 1 or rewards[0].get("tenant") != 11:
    sys.exit("FAIL: VLAN 1521 not created with tenant 0000964: %s" % rewards)
prefixes = [w["body"] for w in writes if w["path"] == "/api/ipam/prefixes/"]
if (len(prefixes) != 1 or prefixes[0].get("prefix") != "10.95.1.0/24" or prefixes[0].get("vrf") != 3
        or not isinstance(prefixes[0].get("vlan"), int) or prefixes[0].get("tenant") != 12):
    sys.exit("FAIL: prefix 10.95.1.0/24 not created in VRF v0000001a for VLAN 951: %s" % prefixes)
EOF

: > "$WRITES"
rc="$(export_run unknown_owner 951,952 false)"
if [ "$rc" -eq 0 ]; then echo "FAIL: run with an unknown owner succeeded" >&2; fail=1; fi
grep -qF "VLAN 952 owner Unknown Co is not in NetBox" "$OUT/unknown_owner.log" \
  || { cat "$OUT/unknown_owner.log"; echo "FAIL: unknown owner not reported" >&2; fail=1; }
check <<'EOF'
import json, sys
writes = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
if any(w["body"].get("name") == "20952:952" for w in writes):
    sys.exit("FAIL: route target 20952:952 was written despite the unknown owner")
EOF

: > "$WRITES"
rc="$(export_run bad_token 951 false refused-token-7731)"
if [ "$rc" -eq 0 ]; then echo "FAIL: run with a refused token succeeded" >&2; fail=1; fi
for want in "NetBox VLAN lookup for VID 951 failed: HTTP 403" "NetBox site lookup failed: HTTP 403" "Invalid token"; do
  grep -qF "$want" "$OUT/bad_token.log" \
    || { cat "$OUT/bad_token.log"; echo "FAIL: refused token not reported: $want" >&2; fail=1; }
done
if grep -qF "refused-token-7731" "$OUT/bad_token.log"; then echo "FAIL: the token appears in the output" >&2; fail=1; fi
if [ -s "$WRITES" ]; then echo "FAIL: run with a refused token wrote to NetBox" >&2; fail=1; fi

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "OK: NetBox route-target export against the mock NetBox"
