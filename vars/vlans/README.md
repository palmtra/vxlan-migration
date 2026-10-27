# Per-DC VLAN database (local SSOT)

Each data center has its own directory of **one YAML file per VLAN**.

```text
vars/vlans/
  lisle/
    0810_legacy_web.yml
  omaha/
    3014_network_mgmt_servers.yml
```

Only real VLAN records belong here. Sample and test records live on the `test` branch.

## Filename convention

```text
{vid:04d}_{slug}.yml
```

| VLAN ID | name | Filename |
|---|---|---|
| 100 | legacy_web | `0100_legacy_web.yml` |
| 2911 | customer_acme | `2911_customer_acme.yml` |

Rules:

- **vid**: zero-padded to at least 4 digits
- **slug**: lowercase snake_case derived from `name`
- Files starting with `_` are ignored

## Record format

Each file is a **single VLAN record** at the top level:

```yaml
id: 100
name: legacy_web
action: migrate
service_type: l3          # l2 | l3 - see docs/VXLAN_SERVICE_TYPES.md
gateway_mode: distributed_anycast  # L3: default; centralized_anycast needs gateway_leafs
gateway_leafs: []         # L3 centralized_anycast: the leaf pair (never inferred)
vni: 50100
vrf: default
gateway: 10.10.100.1      # optional; discovery fills from VR / HSRP
prefixes:                 # optional; derived from SVI CIDRs
  - 10.10.100.0/24
static_route_tags: []     # optional; case-insensitive match against `ip route ... name`
target_switches:
  - chi01-blf01
discovery_switches:
  - chi01-blf01
  - fnts-chi01-aggpe-ml01
```

Records are validated against [`schemas/vlan_record.schema.json`](../../schemas/vlan_record.schema.json) on load (`jsonschema` required). Discovery writes a deployable model (routing, EVPN, participating leaves, prune-eligible leaves). **Confirm the proposed VNI** before migrate. L3 defaults to `distributed_anycast`; name `gateway_leafs` only for `centralized_anycast`. A discovered SVI is a source gateway, not a fabric gateway leaf. L3 objects in a shared VRF are in the JSON analysis payload but are not unique to this VLAN.

| Field | Required | Notes |
|---|---|---|
| `vrf` | yes | Target VRF (discovery fills from SVI) |
| `gateway` | no | Anycast / HSRP / virtual-router address |
| `prefixes` | no | Subnets from SVI CIDRs |
| `static_route_tags` | no | Name tags. A static is associated when a tag equals `ip route ... name`, ignoring case |
| `gateway_mode` | L3 | `distributed_anycast` (default, SVI on every participating leaf) or `centralized_anycast` |
| `gateway_leafs` | centralized L3 | The leaf pair hosting the gateway. Engineer-supplied |
| `gateway_location` | no | Filled by discovery: `legacy`, `fabric`, `mixed`, `external` |
| `customer_type` | no | `internal` or `external`. Picks the DC's VNI range when discovery proposes a VNI: Lisle 20000 / 22000, Omaha 10000 / 11000 (default `vni_default_customer_type`) |
| `external_gateway_leafs` | no | L2 VLANs with an external gateway (firewall) only. Leaves the gateway attaches to when it is not the DC's border leaf pair. Manual override; without it the VLAN also goes on the border pair |
| `ownership.tenant` | no | Numeric tenant id, quoted (`"0000964"`). The NetBox route target's tenant (`export_vlan_to_netbox.yml`). Discovery leaves it empty |
| `ownership.owner` | no | Customer name. The NetBox route target's owner. Discovery leaves it empty |
| `svi_mtu` | no | Fabric SVI MTU. Discovery copies the legacy SVI MTU, capped at 9214 (EOS maximum) |
| `secondary_prefixes` | no | Secondary subnets on the legacy SVI. Not migrated; flagged for review |
| `target_switches` | yes (migrate) | EOS hosts for CVP. A nested discovery model uses participating leaves |
| `mcast_group` | never | Rejected by the schema and by deploy. BUM is head-end replication with EVPN flood lists |

Choosing `l2` vs `l3`: [docs/VXLAN_SERVICE_TYPES.md](../docs/VXLAN_SERVICE_TYPES.md). A nested discovery model (`site` / `placement`) loads as this flat record.

## Targeting VLANs in playbooks

```bash
# Single VLAN
-e manual_vlan_id=100

# Multiple VLANs
-e 'target_vlan_ids=[100,200]'
-e target_vlan_ids=100,200

# All VLANs in the DC directory (omit both)
ansible-playbook ... -e manual_data_center=lisle
```

When `target_vlan_ids` is set, only matching `{vid}_*.yml` files are loaded (fast path).
