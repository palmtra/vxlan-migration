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

Artifact: `reports/netbox_export/netbox_export_<dc>_vlan<id>_<timestamp>.json`

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) § NetBox export for all flags.
