# Get a VLAN's VNI and route target from NetBox

NetBox IPAM route targets (`/ipam/route-targets/`) are the register of L2 VNIs. Each VLAN has one route target, named `<vni>:<vlan id>`. The number comes from the data center's ranges in `inventory/group_vars/all/vni_ranges.yml`:

| Data center | Tenant `0000001` (FNTS) | Any other tenant |
|---|---|---|
| Omaha | internal 10000-10999 | external 11000-11999 |
| Lisle | internal 20000-21999 | external 22000-23999 |

Within a range, numbers are handed out in order, whatever the customer. A VLAN ID can exist in both data centers; the range tells their route targets apart (`10010:3052` is Omaha's VLAN 3052, `22027:3052` is Lisle's).

## What the playbook decides

1. **The VLAN already has a route target** in one of its data center's ranges: that is its VNI. Nothing is allocated.
2. **The VLAN record has a VNI** (`evpn.l2.vni` in `vars/vlans/<dc>/`): that VNI is used. The run stops if NetBox gives that number to another VLAN.
3. **Neither**: the VLAN gets the **lowest free number** of its range. Taken numbers are route targets in the range, VXLAN-EVPN L2VPN identifiers, and the VNIs of the other records in the data center's VLAN DB. Gaps are filled first.

The range comes from the tenant. The run reads the tenant from the NetBox VLAN in the data center's VLAN group (`omaha-datacenter`, `lisle-datacenter`: `netbox_vlan_query_by_dc` in `inventory/group_vars/all/main.yml`). Without that scope, a VID in several groups is narrowed to the one whose group or site names the data center. Without a tenant in NetBox, it uses the record's `ownership.tenant`, then its `customer_type`, then `vni_default_customer_type` (external).

## Prerequisites

`netbox_url` and `netbox_token` in `inventory/group_vars/all/vault.yml`. The token needs view permission on VLANs, route targets, L2VPNs and tenants. To write route targets, it also needs add permission on route targets.

## Report only (default)

```bash
ansible-playbook -i inventory/hosts.yml playbooks/allocate_vni.yml -e manual_data_center=lisle -e manual_vlan_id=3052 --ask-vault-pass
```

The output names the VNI, the route target, the range and why that range was chosen, and what would be written:

```text
VLAN 3052 (lisle): VNI 22030, route target 22030:3052 - lowest free number in lisle external 22000-23999 (37 taken)
Range: external 22000-23999 (tenant 0000910)
Not in NetBox yet; would write {"name": "22030:3052", "description": "CUST_3052 - Created by Ansible VXLAN Automation", "tenant": 3}
```

## Write the route target to NetBox

Writing holds the number, so that a second run cannot pick it too. Both extra vars are required:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/allocate_vni.yml -e manual_data_center=lisle -e manual_vlan_id=3052 -e netbox_reserve_rt=true -e netbox_reserve_approval=approve --ask-vault-pass
```

The route target is created with its name, the VLAN's tenant (when NetBox has that tenant) and the description `<vlan name> - Created by Ansible VXLAN Automation`. The owner is added later by `playbooks/export_vlan_to_netbox.yml`. A run after that reuses the route target.

Then set `evpn.l2.vni` in `vars/vlans/<dc>/<vid>_<name>.yml` to the VNI shown.

## During a migration

`roles/netbox_check` runs the same step in `migrate_vlan.yml`, `workflow_deploy.yml` and `workflow_verify.yml`, after the L2VPN lookup:

- A VNI from a route target fills a record without one, like a VNI from an L2VPN. The migration's readiness check accepts it in place of discovery's proposal.
- A route target whose number differs from the record's VNI stops the run (`netbox_evpn_conflict: warn` goes on from the record).
- A CloudVision push (`cvp_apply_configlets=true`) stops when the VNI was only allocated by this run and not written to NetBox. Run this playbook with the write first, or pass the same two extra vars to the migration.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `netbox_rt_lookup` | `true` | Read or allocate the route target in `roles/netbox_check` |
| `netbox_internal_tenants` | `["0000001"]` | Tenants that use the internal range |
| `netbox_reserve_rt` / `netbox_reserve_approval` | `false` / empty | Write the route target; needs `approve` |
| `netbox_rt_require_reserved` | `false` (`true` in a CloudVision push) | Stop when the VNI is allocated but not written |
| `netbox_vlan_query_by_dc` | `{omaha: {group: omaha-datacenter}, lisle: {group: lisle-datacenter}}` (inventory) | NetBox VLAN filters per data center: each DC's VLAN group slug |
