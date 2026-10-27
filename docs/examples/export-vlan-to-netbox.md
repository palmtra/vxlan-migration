# Export VLAN attributes to NetBox

Standalone utility — **not** part of the Core or Advanced migration workflow.

## Dry-run (default — no NetBox writes)

```bash
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100
```

## Apply export

```bash
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  -e netbox_export_dry_run=false
```

Requires `netbox_url` and `netbox_token` in vault or AAP credentials.

## Route targets

The same run writes one route target per migrated VLAN (IPAM > Route Targets): name
`<vni>:<vlan id>`, the record's tenant and owner, and the description
`<vlan name> - Created by Ansible VXLAN Automation`. An existing route target with that name is
updated. The VLAN record needs its ownership filled in, with the tenant id quoted:

```yaml
ownership:
  tenant: "0000964"     # NetBox tenant name (numeric tenant id)
  owner: Rewards Trust  # NetBox owner name (customer)
```

A VLAN whose tenant or owner is missing, or not in NetBox, is not written and fails the run. The
dry run looks them up too, so it shows these failures before anything is written. Pass
`-e netbox_export_route_targets=false` to export VLANs only.

Artifact: `reports/netbox_export/netbox_export_<dc>_vlan<id>_<timestamp>.json`

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) § NetBox export for all flags.
