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
NetBox's certificate is self-signed, so it is not verified (`netbox_validate_certs: false` in
`inventory/group_vars/all/main.yml`). A failed NetBox request reports its HTTP status and reason; see
[USAGE_GUIDE.md](../USAGE_GUIDE.md#when-a-netbox-request-fails).

## What is written

Every lookup runs first, including in the dry run; a VLAN with a problem gets nothing written and
fails the run.

- **VLAN.** Looked up in this data center's scope (`netbox_vlan_query_by_dc`, the VLAN group), the
  same as the deploy's NetBox check, so a VID another data center also uses is never touched. A new
  VLAN is created in that VLAN group (on the `netbox_export_site_map` site when there is no group);
  an existing one keeps its group and site. The group must exist in NetBox.
- **Tenant.** The record's numeric tenant id (`ownership.tenant`), set on the VLAN. It must exist in
  NetBox (Tenancy > Tenants). A record without a tenant leaves the VLAN's tenant as it is.
- **Prefixes and VRF.** Each record prefix in the record's VRF (`default` is the global table),
  assigned to the VLAN and its tenant. The VRF must exist in NetBox (IPAM > VRFs); VRFs are never
  created here. An existing prefix in that VRF is updated; one in another VRF is left alone and the
  prefix is created in the right VRF. `-e netbox_export_prefixes=false` skips prefixes.

Check what differs before and after with the read-only [drift report](netbox-drift.md).

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
