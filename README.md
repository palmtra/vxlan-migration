# Classic VLAN to VXLAN EVPN Migration

Ansible Automation Platform (AAP) driven network automation to **migrate legacy VLANs to VXLAN**.

VLAN decommission discovery and cleanup planning are maintained separately in `cvg-decomm-vlan`.

Built to mature quickly for AAP but remains runnable from Ansible CLI / AWX.
Primary platforms are **Arista EOS** and **Cisco NXOS**; **Cisco IOS/IOS-XE** is also supported.

## Quick start

```bash
# Install collections
ansible-galaxy collection install -r collections/requirements.yml -p collections/

# Discover current state for a data center
ansible-playbook -i inventory/hosts.yml playbooks/discover_vlan_state.yml --limit dc_lisle

# Run migration stream only
ansible-playbook -i inventory/hosts.yml playbooks/migrate_vlans_to_vxlan.yml --limit dc_lisle

```

## Repository layout

```
.
├── ansible.cfg
├── collections/requirements.yml   # Execution-env collection dependencies (EOS, IOS, NXOS)
├── execution-environment/         # AAP Execution Environment definition
├── inventory/
│   ├── hosts.yml                  # Devices grouped by OS family and data center
│   └── group_vars/
│       ├── all.yml                # Global knobs (safety flags, VTEP defaults)
│       ├── eos.yml                # Arista EOS defaults
│       ├── ios.yml                # Cisco IOS defaults
│       └── nxos.yml               # Cisco NXOS defaults
├── playbooks/
│   ├── site.yml                   # Runs the migration workflow
│   ├── migrate_vlans_to_vxlan.yml
│   ├── discover_vlan_state.yml    # Current VLAN/SVI/trunk/VXLAN reports
│   └── validate_network_state.yml
├── roles/
│   ├── network_common/            # Facts, backup, pre-validation
│   ├── vlan_discovery/            # State discovery and reporting
│   └── migrate_to_vxlan/          # VXLAN migration logic
└── vars/
    └── vlan_registry.yml          # Source-of-truth VLAN actions
```

## Data model

Every migration VLAN is declared in `vars/vlan_registry.yml` with `action: migrate`:

```yaml
vlans:
  - id: 100
    name: legacy_app_a
    action: migrate
    vrf: prod # used when building VNI/VRF bindings
    vni: 10100 # required for migrate
    vlan_name: APP_A_VXLAN
```

The migration playbooks filter by `action: migrate`.

## Safety controls

- `--check` / `--diff` supported end-to-end.
- Backups captured per host before any change.
- Pre- and post-validation tasks in each role.

## Workflow

1. **Discover**: run `playbooks/discover_vlan_state.yml` against a DC to capture current state.
2. **Review**: inspect `reports/` output and the registry.
3. **Execute**: run the migration playbook.
4. **Validate**: run `playbooks/validate_network_state.yml` or re-run discovery.

## AAP integration

- The `execution-environment/` directory defines the container image; it now includes `arista.eos`, `cisco.nxos`, and `cisco.ios`.
- Job Templates in AAP should point to the playbooks in `playbooks/`.
- Use inventory groups `dc_lisle` / `dc_omaha` to scope Job Templates by data center.
- Surveys can override `target_vlan_ids` to scope a Job run.
