# Classic VLAN to VXLAN EVPN Migration

Ansible Automation Platform (AAP) driven network automation to **migrate legacy VLANs to VXLAN**.

VLAN decommission discovery and cleanup planning are maintained separately in `cvg-decomm-vlan`.

Built to mature quickly for AAP but remains runnable from Ansible CLI / AWX.
**VXLAN migration configlets** target **Arista EOS** via CloudVision (CVP).
Discovery and decommission support **Arista EOS**, **Cisco NXOS**, and **Cisco IOS/IOS-XE**.

## Quick start

```bash
# Option A: install ansible-core as a uv tool and inject project dependencies
uv tool install ansible-core
uv pip install -r requirements.txt --python ~/.local/share/uv/tools/ansible-core/bin/python

# Option B: create a local uv virtualenv so ansible* commands run from the project
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install ansible-core

# Install collections
ansible-galaxy collection install -r collections/requirements.yml -p collections/

# Discover current state for a data center
ansible-playbook -i inventory/hosts.yml playbooks/discover_vlan_state.yml --limit dc_lisle

# Run the full ServiceNow-driven workflow (manual override)
ansible-playbook -i inventory/hosts.yml playbooks/workflow_vlan_to_vxlan.yml \
  --limit dc_lisle \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default

# Same workflow using Arista AVD for EOS configlet generation
ansible-playbook -i inventory/hosts.yml playbooks/workflow_vlan_to_vxlan.yml \
  --limit dc_lisle \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=false

# Run migration stream only
ansible-playbook -i inventory/hosts.yml playbooks/migrate_vlans_to_vxlan.yml --limit dc_lisle

# AVD-based CVP configlet generation (generate-only)
ansible-playbook -i inventory/hosts.yml playbooks/deploy_to_cvp_avd.yml \
  --limit dc_lisle \
  -e target_vlan_id=100 \
  -e target_data_center=lisle \
  -e target_vrf=default \
  -e cvp_apply_configlets=false

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
│       ├── eos_devices.yml        # Arista EOS defaults
│       ├── ios_devices.yml        # Cisco IOS defaults
│       └── nxos_devices.yml       # Cisco NXOS defaults
├── playbooks/
│   ├── site.yml                   # Runs the migration workflow
│   ├── workflow_vlan_to_vxlan.yml # ServiceNow-driven end-to-end workflow
│   ├── check_netbox.yml           # Standalone NetBox VLAN existence check
│   ├── deploy_to_cvp.yml          # Generate/apply CVP configlets (legacy Jinja)
│   ├── deploy_to_cvp_avd.yml      # Generate/apply CVP configlets (Arista AVD)
│   ├── migrate_vlans_to_vxlan.yml
│   ├── discover_vlan_state.yml    # Current VLAN/SVI/trunk/VXLAN reports
│   └── validate_network_state.yml
├── roles/
│   ├── network_common/            # Facts, backup, pre-validation
│   ├── vlan_discovery/            # State discovery and reporting
│   ├── migrate_to_vxlan/          # VXLAN migration logic
│   ├── servicenow_input/          # Normalize SNOW / manual input
│   ├── external_cleanup/          # Hand off discovered VLAN for cleanup
│   ├── netbox_check/              # Verify VLAN exists in NetBox
│   ├── cvp_deploy/                # Build/apply CVP configlets (legacy Jinja)
│   └── avd_vxlan_config/          # Build/apply CVP configlets (Arista AVD)
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

### Full ServiceNow-driven workflow

The `playbooks/workflow_vlan_to_vxlan.yml` playbook orchestrates the complete
migration triggered by a ServiceNow change ticket:

1. **Input normalization** (`roles/servicenow_input`) — accepts `snow_vlan_id`,
   `snow_data_center`, `snow_target_vrf` or manual overrides.
2. **Discovery** (`roles/vlan_discovery`) — gathers VLAN, MAC table, STP,
   trunk/access interfaces, SVI, and ARP data per VRF; writes `combined_vlan_state.csv`
   and `discovery_report.md`.
3. **External cleanup hand-off** (`roles/external_cleanup`) — marks discovered
   VLANs as `pending` for an external decommission tool; no device changes.
4. **NetBox check** (`roles/netbox_check`) — verifies the VLAN exists in NetBox.
5. **CVP deployment** — generates leaf and border-leaf configlets for Arista CVP.
   - By default `roles/cvp_deploy` uses custom Jinja templates.
   - Set `use_avd: true` to use `roles/avd_vxlan_config` and
     `arista.avd.eos_cli_config_gen` for schema-validated EOS configuration.
6. **Validation** — runs post-migration show commands.

### AVD-based CVP configlet generation

For large, continuous migrations, use Arista AVD to generate the EOS snippets
that are pushed to CVP:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy_to_cvp_avd.yml \
  --limit dc_lisle \
  -e target_vlan_id=100 \
  -e target_data_center=lisle \
  -e target_vrf=default \
  -e cvp_apply_configlets=false
```

To push generated configlets to CVP, add:

```bash
  -e cvp_apply_configlets=true \
  -e cvp_server=https://cvp.example.com \
  -e cvp_token=<token>
```

By default this creates a **pending** CVP change control (`cvp_change_control_state: set`)
and does **not** approve or execute it automatically. Approval and execution must be done
in CloudVision. Only override with `-e cvp_change_control_state=approve_and_execute -e
cvp_change_control_auto_execute_allowed=true` after explicit review.

Per-device BGP parameters may be supplied as hostvars (`bgp_as`, `router_id`) or
via `avd_default_bgp_as` in group variables.

### Legacy granular workflow

1. **Discover**: run `playbooks/discover_vlan_state.yml` against a DC to capture current state.
2. **Review**: inspect `reports/` output and the registry.
3. **Execute**: run the migration playbook.
4. **Validate**: run `playbooks/validate_network_state.yml` or re-run discovery.

## AAP integration

- The `execution-environment/` directory defines the container image; it now includes `arista.eos`, `arista.avd`, `arista.cvp`, `cisco.nxos`, and `cisco.ios`.
- Job Templates in AAP should point to the playbooks in `playbooks/`.
  - `playbooks/workflow_vlan_to_vxlan.yml` for the full ServiceNow-driven workflow.
  - `playbooks/discover_vlan_state.yml` for read-only discovery.
- Use inventory groups `dc_lisle` / `dc_omaha` to scope Job Templates by data center.
- Surveys can override `target_vlan_ids` to scope a Job run, or pass
  `manual_vlan_id`, `manual_data_center`, and `manual_target_vrf` for ad-hoc runs.
- Configure NetBox and CVP credentials via environment variables
  (`NETBOX_API`, `NETBOX_TOKEN`, `CVP_SERVER`, `CVP_TOKEN`) or encrypted extra vars.
