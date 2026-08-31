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
service_type: l3          # l2 | l3 | l2_l3 - see docs/VXLAN_SERVICE_TYPES.md
vni: 50100
vrf: default
gateway: 10.10.100.1      # optional; discovery fills from VR / HSRP
prefixes:                 # optional; derived from SVI CIDRs
  - 10.10.100.0/24
target_switches:
  - eos-leaf-lis-01
discovery_switches:
  - eos-leaf-lis-01
  - nxos-spine-lis-01
```

Records are validated against [`schemas/vlan_record.schema.json`](../../schemas/vlan_record.schema.json) on load (`jsonschema` required). Discovery writes a paste-ready snippet with VRF, gateway (anycast/HSRP), prefixes, `service_type`, and switch lists filled in; **VNI must still be assigned** before migrate. L3 objects in a shared VRF are shown on the discovery report but are not unique to this VLAN.

| Field | Required | Notes |
|---|---|---|
| `vrf` | yes | Target VRF (discovery fills from SVI) |
| `gateway` | no | Anycast / HSRP / virtual-router address |
| `prefixes` | no | Subnets from SVI CIDRs |
| `target_switches` | yes (migrate) | EOS hosts for CVP |

Choosing `l2` vs `l3` vs `l2_l3`: [docs/VXLAN_SERVICE_TYPES.md](../docs/VXLAN_SERVICE_TYPES.md).

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
