# NetBox drift report

Read-only: compares the VLAN records in `vars/vlans/<dc>/` with NetBox and writes nothing to NetBox.
Run it before an export to see what the export would change, after one to confirm it, or on a
schedule to catch edits made in NetBox by hand.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/netbox_drift.yml \
  -e manual_data_center=omaha \
  --ask-vault-pass
```

Add `-e manual_vlan_id=3000` (or `-e target_vlan_ids=3000-3010`) for some VLANs only. With
`-e netbox_drift_fail_on_drift=true` a VLAN that drifted or is missing from NetBox fails the run.

## What is compared

| Field | Record | NetBox |
|---|---|---|
| `vlan` | the VLAN | VLAN with that VID in the data center's scope (`netbox_vlan_query_by_dc`) |
| `name` | `vlan_name` or `name` | VLAN name |
| `tenant` | `ownership.tenant` | VLAN tenant |
| `vni` | `vni` | identifier of the VXLAN-EVPN L2VPN terminated on the VLAN |
| `l2vpn_import_target` / `l2vpn_export_target` | `<vni>:<vlan id>` (or `l2_rt`) | the L2VPN's import / export targets |
| `ipam_route_target`, `route_target_tenant` | `<vni>:<vlan id>`, tenant | IPAM route target with that name, its tenant |
| `prefix <p> vrf`, `prefix <p> vlan` | each prefix, `vrf` | the prefix in that VRF (`default` = global), its VLAN |

Each field is `ok`, `differs`, `missing_in_netbox`, `netbox_only` (NetBox has a value the record
leaves empty, such as a VNI the deploy reads from NetBox: not drift) or `not_checked`. A VLAN is
`in_sync`, `drift`, `missing` (not in NetBox) or `error` (several matches, or a failed request).

## Output

`reports/netbox_drift/<dc>_<timestamp>.md` and `.json`: counts per status, one row per VLAN, and
for each VLAN that is not in sync the fields that differ.

To fix drift, change whichever side is wrong: the record (and commit it), or NetBox, by hand or with
[export-vlan-to-netbox.md](export-vlan-to-netbox.md) (dry run first).

Back to [examples](README.md).
