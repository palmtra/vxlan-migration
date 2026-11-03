# Collect validation data

The parsers, the NetBox L2VPN lookup and the CloudVision calls are tested against mock switches, a mock NetBox and a mock CloudVision. This playbook collects the same data from your real systems, read-only and with secrets removed, so it can be checked against what the code expects.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/collect_validation_data.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha --limit dc_omaha \
  -e vxlan_capture_dir=captures/omaha_vlan3000
```

Nothing is changed on a switch, in NetBox or in CloudVision. The result is `captures/omaha_vlan3000.tar.gz`.

## What it collects

| Folder | What | How |
|---|---|---|
| `devices/<host>/` | Every command preflight and discovery send, one file per command (`show_mac_address_table_vlan_3000.txt`; `.error` when the command failed) | The `vxlan_capture` callback records each `cli_command` result |
| `netbox/` | API status, the VLAN by VID, its L2VPN terminations and L2VPN objects, from `/api/vpn/` and `/api/ipam/` | Read-only GETs; the answer body only, never the request |
| `cloudvision/` | `cv_facts_v3` devices (hostname, container, serial); with `-e cvp_change_control_id=<id>`, that change control as `cv_change_control_v3 state=show` returns it | The callback records each `arista.cvp` result |
| `reports/` | The preflight result and the discovery reports of this run | |
| top level | `README.txt`, `capture_index.json` (hosts, inventory groups, command per file), `manifest.json` (SHA-256 of every file) | |

Pick a VLAN with endpoints on both the legacy switches and the fabric leaves if there is one. For `cvp_change_control_id`, any existing change control works (a completed one is best). The call only reads it.

## Secrets

Removed before anything is written:

- In switch outputs, everything after `password`, `secret`, `key`, `key-string`, `md5`, `authentication-key`, `passphrase`, `token`, and SNMP `community` on a line: `neighbor 10.0.0.1 password <removed>`.
- In JSON, the value of any key that names a secret: `password`, `token`, `authorization`, `api_key`, `community`, `private_key`.

Kept, because the checks need them: hostnames, interface names and descriptions, IP addresses, MAC addresses, VLAN names, BGP communities, route maps. **Review the bundle before you share it.**

## Capture any other run

The callback works with every playbook here. Add `-e vxlan_capture_dir=<dir>` to:

- `migrate_vlan.yml` with `-e migrate_push_to_cvp=false`: everything up to the configlets.
- `migrate_vlan.yml` with the push: also CloudVision's answers to `cv_configlet_v3`, `cv_device_v3` (the task IDs) and `cv_change_control_v3` (create, then the wait).
- `workflow_verify.yml` after a change: the switches as they are after it.

Use a new directory per run. A command whose output changes within a run is kept as `<command>~2.txt`.

## How the capture is used

The `devices/` folder has the same layout as the mock switches in `tests/mock_cli`. Discovery runs against it unchanged (`-e mock_cli_root=<capture>` with the mock collections), so a parser that misreads your switches shows up as a wrong report, and the fix is tested on your outputs.

Back to [examples](README.md).
