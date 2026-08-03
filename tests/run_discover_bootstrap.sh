#!/usr/bin/env bash
# Verify discover_vlan bootstrap reaches vlan_db without templating errors.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=810 \
  -e discovery_allow_probe=true \
  --limit dc_lisle 2>&1 | tee /tmp/discover_vlan_bootstrap.log

if grep -q "target_data_center.*is undefined" /tmp/discover_vlan_bootstrap.log; then
  echo "FAIL: bootstrap still hits undefined target_data_center" >&2
  exit 1
fi

if grep -q "TASK \[vlan_db : Display loaded VLAN database summary\]" /tmp/discover_vlan_bootstrap.log; then
  echo "OK: vlan_db bootstrap completed"
  exit 0
fi

echo "FAIL: vlan_db summary task not reached" >&2
exit 1
