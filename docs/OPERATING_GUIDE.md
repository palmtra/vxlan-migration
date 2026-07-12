# Operating Guide — cvg-vxlan

## Design goals

- **AAP-first** but runnable from Ansible CLI/AWX.
- **Primary platforms**: Arista EOS and Cisco NXOS. Cisco IOS/IOS-XE is also supported.
- **Single source of truth** for VLAN intent: `vars/vlan_registry.yml`.
- **Safe defaults**: destructive decommission requires an explicit flag.
- **Platform abstraction**: EOS, NXOS, and IOS logic isolated in per-platform task files.
- **Data center scoping**: inventory groups `dc_lisle` and `dc_omaha`.

## Data model

Edit `vars/vlan_registry.yml` once per VLAN:

| Field | Required | Notes |
|-------|----------|-------|
| `id` | yes | 802.1Q VLAN ID |
| `name` | yes | Human-readable label |
| `action` | yes | `migrate` or `decommission` |
| `vni` | for migrate | VXLAN Network Identifier |
| `vlan_name` | no | Desired VLAN name after migration |
| `mcast_group` | no | Per-VLAN BUM group; falls back to `vxlan_default_mcast_group` |
| `explicit_trunk_interfaces` | no | For decommission, prune only these trunks |

## CLI usage

### 1. Install collections

```bash
ansible-galaxy collection install -r collections/requirements.yml -p collections/
```

### 2. Provide credentials

Set environment variables or create an encrypted vault:

```bash
export ANSIBLE_NET_USER=admin
export ANSIBLE_NET_PASS=changeme
```

For production, replace the env lookups in `inventory/hosts.yml` with an
Ansible Vault file such as `inventory/group_vars/vault.yml`.

### 3. Discover current state (per data center)

```bash
ansible-playbook -i inventory/hosts.yml playbooks/discover_vlan_state.yml --limit dc_lisle
```

Reports land in `reports/`:

- `<hostname>_vlan_state.json` — raw command output per host
- `combined_vlan_state.csv` — summary across the data center

### 4. Dry-run decommission plan

```bash
ansible-playbook -i inventory/hosts.yml playbooks/dry_run_decommission.yml \
  --limit dc_lisle -e target_vlan_ids='[900,910]'
```

This writes `reports/dry_run_decommission.csv` listing, per switch, the SVIs,
trunk interfaces, and VLANs that would be removed. No changes are made.

### 5. Run migration stream only

```bash
# Scope to a data center
ansible-playbook -i inventory/hosts.yml playbooks/migrate_vlans_to_vxlan.yml --limit dc_lisle

# Scope to specific VLANs
ansible-playbook -i inventory/hosts.yml playbooks/migrate_vlans_to_vxlan.yml \
  --limit dc_lisle -e target_vlan_ids='[100,200]'
```

### 6. Run decommission stream only

```bash
# Review first (dry-run report)
ansible-playbook -i inventory/hosts.yml playbooks/dry_run_decommission.yml \
  --limit dc_lisle -e target_vlan_ids='[900,910]'

# Execute after review
ansible-playbook -i inventory/hosts.yml playbooks/decommission_vlans.yml \
  --limit dc_lisle -e vlan_change_dangerous=true -e target_vlan_ids='[900,910]'
```

## AAP usage

1. **Execution Environment**: build `execution-environment/execution-environment.yml`
   and push to your AAP registry.
2. **Project**: point AAP at this Git repository.
3. **Inventories**: import `inventory/hosts.yml` or configure a dynamic inventory.
4. **Credentials**: create Machine/Network credentials and link them to Job Templates.
5. **Job Templates**:
   - `playbooks/discover_vlan_state.yml`
   - `playbooks/dry_run_decommission.yml`
   - `playbooks/migrate_vlans_to_vxlan.yml`
   - `playbooks/decommission_vlans.yml`
   - `playbooks/validate_network_state.yml`
6. **Surveys** (optional): expose `target_vlan_ids` and `target_actions` as survey
   questions so operators can scope a Job run without editing files.
7. **Workflows** (example):
   - Discovery: `discover_vlan_state`
   - Decommission planning: `dry_run_decommission`
   - Approval node
   - Decommission: `decommission_vlans` (with `vlan_change_dangerous=true`)
   - Migration: `migrate_vlans_to_vxlan`
   - Validation: `validate_network_state`

## Safety controls

- `--check` and `--diff` are supported end-to-end.
- `vlan_change_dangerous` must be `true` for decommission to make changes.
- A pre-change backup is saved per host under `backups/<hostname>/`.
- Migration and decommission playbooks are separate; `site.yml` filters by
  `target_actions` so a VLAN cannot be migrated and deleted in the same run
  unless explicitly requested.

## Extending to other platforms

EOS, NXOS, and IOS are already implemented. To add another platform:

1. Add `network_os_family: <new>` in `inventory/group_vars/<new>.yml`.
2. Add platform-specific task files in `roles/migrate_to_vxlan/tasks/`,
   `roles/decommission_vlan/tasks/`, and `roles/vlan_discovery/tasks/`.
3. Add platform templates in `roles/migrate_to_vxlan/templates/` and
   `roles/decommission_vlan/templates/`.
4. Update the platform dispatcher in each role's `tasks/main.yml`.

## Validation

Use the standalone validation playbook after any change:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/validate_network_state.yml
```
