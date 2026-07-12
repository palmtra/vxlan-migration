# Classic VLAN to VXLAN EVPN Migration

Ansible Automation Platform (AAP) driven network automation for two VLAN lifecycle streams:

1. **Migrate legacy VLANs to VXLAN**
2. **Decommission VLANs** (remove SVIs, prune trunks, delete VLANs)

Built to mature quickly for AAP but remains runnable from Ansible CLI / AWX.
Primary platforms are **Arista EOS** and **Cisco NXOS**; **Cisco IOS/IOS-XE** is also supported.

## Quick start

```bash
# Install collections
ansible-galaxy collection install -r collections/requirements.yml -p collections/

# Discover current state for a data center
ansible-playbook -i inventory/hosts.yml playbooks/discover_vlan_state.yml --limit dc_lisle

# Dry-run decommission for specific VLANs
ansible-playbook -i inventory/hosts.yml playbooks/dry_run_decommission.yml \
  --limit dc_lisle -e target_vlan_ids='[900,910]'

# Run migration stream only
ansible-playbook -i inventory/hosts.yml playbooks/migrate_vlans_to_vxlan.yml --limit dc_lisle

# Run decommission stream only (requires vlan_change_dangerous=true)
ansible-playbook -i inventory/hosts.yml playbooks/decommission_vlans.yml \
  --limit dc_lisle -e vlan_change_dangerous=true
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
│   ├── site.yml                   # Runs both streams in one workflow
│   ├── migrate_vlans_to_vxlan.yml
│   ├── decommission_vlans.yml
│   ├── dry_run_decommission.yml   # CSV plan before any changes
│   ├── discover_vlan_state.yml    # Current VLAN/SVI/trunk/VXLAN reports
│   └── validate_network_state.yml
├── roles/
│   ├── network_common/            # Facts, backup, pre-validation
│   ├── vlan_discovery/            # State discovery and reporting
│   ├── migrate_to_vxlan/        # VXLAN migration logic
│   └── decommission_vlan/       # VLAN removal logic
└── vars/
    └── vlan_registry.yml          # Source-of-truth VLAN actions
```

## Data model

Every VLAN is declared once in `vars/vlan_registry.yml` with an `action`:

```yaml
vlans:
  - id: 100
    name: legacy_app_a
    action: migrate            # migrate | decommission
    vrf: prod                  # used when building VNI/VRF bindings
    vni: 10100                 # required for migrate
    vlan_name: APP_A_VXLAN
  - id: 200
    name: legacy_app_b
    action: decommission
```

Playbooks filter by `action` so a VLAN is never both migrated and decommissioned in the same run.

## Safety controls

- `--check` / `--diff` supported end-to-end.
- `vlan_change_dangerous: false` global default. Set to `true` to allow VLAN deletion / SVI removal.
- Backups captured per host before any change.
- Pre- and post-validation tasks in each role.
- Migration and decommission playbooks are separate so streams cannot cross-contaminate.

## Workflow

1. **Discover**: run `playbooks/discover_vlan_state.yml` against a DC to capture current state.
2. **Plan** (decommission only): run `playbooks/dry_run_decommission.yml` to produce a CSV plan.
3. **Review**: inspect `reports/` output and the registry.
4. **Execute**: run migration or decommission playbooks.
5. **Validate**: run `playbooks/validate_network_state.yml` or re-run discovery.

## AAP integration

- The `execution-environment/` directory defines the container image; it now includes `arista.eos`, `cisco.nxos`, and `cisco.ios`.
- Job Templates in AAP should point to the playbooks in `playbooks/`.
- Use inventory groups `dc_lisle` / `dc_omaha` to scope Job Templates by data center.
- Surveys can override `target_vlan_ids` or `target_actions` to scope a Job run.
