# Per-DC VLAN database (local SSOT)

Each data center has its own directory of **one YAML file per VLAN**.

```text
vars/vlans/
  lisle/
    _example.yml          # template + naming docs (not loaded)
    0100_legacy_web.yml
    0200_legacy_app.yml
  omaha/
    _example.yml
    0100_legacy_web.yml
```

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
- Files starting with `_` are ignored (including `_example.yml`)

## Record format

Each file is a **single VLAN record** at the top level:

```yaml
id: 100
name: legacy_web
action: migrate
service_type: l3          # l2 | l3 - see docs/VXLAN_SERVICE_TYPES.md
gateway_leafs: []         # L3: fabric leaves that will host the gateway (never inferred)
vni: 50100
vrf: default
gateway: 10.10.100.1      # optional; discovery fills from VR / HSRP
prefixes:                 # optional; derived from SVI CIDRs
  - 10.10.100.0/24
static_route_tags: []     # optional; case-insensitive match against `ip route ... name`
target_switches:
  - eos-leaf-lis-01
discovery_switches:
  - eos-leaf-lis-01
  - nxos-spine-lis-01
```

Records are validated against [`schemas/vlan_record.schema.json`](../../schemas/vlan_record.schema.json) on load (`jsonschema` required). Discovery writes a deployable model (routing, EVPN, participating leaves, prune-eligible leaves). **VNI must still be assigned** before migrate, and **gateway_leafs must be named** for an L3 cutover. A discovered SVI is a source gateway, not a fabric gateway leaf. L3 objects in a shared VRF are in the JSON analysis payload but are not unique to this VLAN.

| Field | Required | Notes |
|---|---|---|
| `vrf` | yes | Target VRF (discovery fills from SVI) |
| `gateway` | no | Anycast / HSRP / virtual-router address |
| `prefixes` | no | Subnets from SVI CIDRs |
| `static_route_tags` | no | Name tags. A static is associated when a tag equals `ip route ... name`, ignoring case |
| `gateway_leafs` | L3 | Fabric gateway leaves. Engineer-supplied |
| `target_switches` | yes (migrate) | EOS hosts for CVP. A nested discovery model uses participating leaves |
| `mcast_group` | no | Omit. BUM is head-end replication already on the fabric |

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
