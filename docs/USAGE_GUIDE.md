# Usage Guide - vxlan-migration

Administrative reference for operators running Core and Advanced migration workflows from CLI or Ansible Automation Platform (AAP).

---

## Prerequisites

```bash
ansible-galaxy collection install -r collections/requirements.yml -p collections/
```

Credentials via vault (`inventory/group_vars/all/vault.yml`) or AAP custom credentials:

| Variable | Used by |
|---|---|
| `ansible_user`, `ansible_password` | Device CLI |
| `cvp_url`, `cvp_token` | CVP configlet push |
| `netbox_url`, `netbox_token` | NetBox check / export |
| `snow_url`, `snow_username`, `snow_password` | Advanced workflow only |

---

## Inventory scoping

| Group | Purpose |
|---|---|
| `dc_lisle`, `dc_omaha` | Limit discovery/deploy to one data center |
| `network_devices` | All EOS + NXOS + IOS targets |
| `eos_devices` | CVP deploy and verify targets |

Always pass `--limit dc_<name>` (or a host list) to control blast radius.

---

## 1. Discovery (read-only)

**Playbook:** `playbooks/core/discover_vlan.yml`

Collects VLAN, MAC, SVI, ARP, and trunk carriage. Writes reports under `reports/<dc>/<vlan_name>/`.

### Required extra vars

| Variable | Description |
|---|---|
| `manual_data_center` | Data center key matching `vars/vlans/<dc>/` (e.g. `lisle`, `omaha`) |

### Common optional vars

| Variable | Default | Description |
|---|---|---|
| `manual_vlan_id` | all VLANs in DB | Discover a single VLAN ID |
| `target_vlan_ids` | — | Discover multiple VLANs: `[100,200]` or `100,200` |
| `discovery_allow_probe` | `true` in discover playbook | Allow discovering a VLAN **not yet** in the DC database |
| `discovery_probe_service_type` | `l3` | `service_type` assigned to synthetic probe records |
| `vlan_discovery_backup_enabled` | `false` | Capture config backup before discovery |
| `discovery_write_markdown` | `true` | Write `.md` report |
| `discovery_write_csv` | `true` | Write `.csv` report |
| `discovery_write_json` | `true` | Write `.json` report |
| `discovery_write_yaml` | `true` | Write `.yml` structured report (same data as JSON) |
| `discovery_write_vlan_db_snippet` | `true` | Write YAML snippet for pasting into VLAN DB |

### Examples

```bash
# Discover one VLAN already in the VLAN DB (scoped hosts from discovery_switches)
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle

# Probe an unknown VLAN across all switches in the DC
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=2911 \
  -e discovery_allow_probe=true \
  --limit dc_lisle

# Discover multiple VLANs from the DC directory
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e 'target_vlan_ids=[100,200]' \
  --limit dc_lisle

# Discover every VLAN declared under vars/vlans/lisle/
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  --limit dc_lisle
```

### Report outputs

| File | Contents |
|---|---|
| `<vlan>_discovery_<ts>.md` | Human-readable summary + raw CLI |
| `<vlan>_discovery_<ts>.csv` | One row per switch |
| `<vlan>_discovery_<ts>.json` | Full structured report |
| `<vlan>_discovery_<ts>.yml` | Full structured report (YAML, same data as JSON) |
| `0100_legacy_web.yml` | Starter per-VLAN record for `vars/vlans/<dc>/` |

Key report fields:

- `switches_found` — VLAN L2 present
- `switches_with_mac_learning` — active MAC learning
- `switches_with_svi` / `switches_with_arp` — L3 presence
- `trunk_cleanup_candidates` — trunks where cleanup should be planned (informational only)

---

## 2. Core deploy (migration config → CVP)

**Playbook:** `playbooks/core/workflow_deploy.yml`

Loads the VLAN from local SSOT, optionally re-discovers, generates configlets, pushes to CVP. Creates a **pending** change control and **stops**.

### Required extra vars

| Variable | Description |
|---|---|
| `manual_vlan_id` | VLAN ID to migrate |
| `manual_data_center` | Data center (`lisle`, `omaha`) |
| `manual_target_vrf` | Target VRF (usually `default`) |

The VLAN must exist in `vars/vlans/<dc>/{vid}_{slug}.yml` with `target_switches`, `vni`, `service_type`, etc.
See `vars/vlans/README.md` and `vars/vlans/<dc>/_example.yml`.

### Config generation

| Variable | Default | Description |
|---|---|---|
| `use_avd` | `false` | `true` → `roles/avd_vxlan_config` (Arista AVD); `false` → legacy Jinja |
| `cvp_apply_configlets` | `false` | `true` → push to CVP; `false` → render files only under `reports/cvp_configlets/` |

Detailed generate-only walkthrough: [examples/generate-config-without-cvp-push.md](examples/generate-config-without-cvp-push.md).
| `cvp_change_control_state` | `set` | CVP change control action (`set` = pending approval) |
| `cvp_change_control_auto_execute_allowed` | `false` | Must stay `false` unless explicitly overriding after review |
| `cvp_build_borderleaf_config` | `true` | Generate border-leaf import configlet |
| `avd_default_bgp_as` | `""` | Default BGP ASN for AVD when not set per host |

### Discovery during deploy

Deploy re-runs discovery on `discovery_switches` (or `target_switches`). Scope with the VLAN DB — not with extra vars.

### NetBox during deploy

| Variable | Default | Description |
|---|---|---|
| `netbox_ssot_required` | `false` | `true` → fail if VLAN missing in NetBox |
| `netbox_warn_on_missing_record` | `true` | Log warning when NetBox has no record |

### External cleanup hand-off

| Variable | Default | Description |
|---|---|---|
| `external_cleanup_write_handoff` | `true` | Write JSON hand-off under `reports/cleanup_handoff/` |
| `external_cleanup_api_url` | `""` | Optional HTTP POST to external orchestrator |

### Examples

```bash
# Generate configlets locally — no CVP push (safest first run)
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e cvp_apply_configlets=false \
  --limit dc_lisle

# Push to CVP — creates pending change control
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e cvp_apply_configlets=true \
  -e use_avd=true \
  --limit dc_lisle
```

After deploy, approve and execute the change control in CloudVision. Workflow state is saved to:

```text
reports/workflow_state/vlan_<id>.yml
```

---

## 3. Core verify (optional, post-CVP)

**Playbook:** `playbooks/core/workflow_verify.yml`

Run **after** the CVP change control has been executed. Checks VXLAN mapping and learning on `target_switches`.

### Extra vars

| Variable | Description |
|---|---|
| `resume_vlan_id` | Load state from `reports/workflow_state/vlan_<id>.yml` |
| `target_vlan_id` | Alternative when chaining immediately after deploy |

| Variable | Default | Description |
|---|---|---|
| `auto_rollback_on_verification_failure` | `true` | Attempt CVP rollback on verify failure |

### Example

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=100 \
  --limit dc_lisle
```

---

## 4. Advanced workflow (ServiceNow)

Same as Core, plus ServiceNow intake and callbacks.

**Playbook:** `playbooks/advanced/workflow_deploy.yml`

### ServiceNow extra vars

| Variable | Description |
|---|---|
| `snow_vlan_id` | VLAN from ticket (or use `manual_vlan_id`) |
| `snow_data_center` | DC from ticket |
| `snow_target_vrf` | VRF from ticket |
| `snow_ticket_sys_id` | ServiceNow change record sys_id |
| `snow_enable_callbacks` | `false` to disable all SNOW API calls during testing |

### SNOW callback points

| Stage | Ticket state |
|---|---|
| Deploy success | `wait_for_approval` |
| Deploy failure | `escalated` |
| Verify success | `complete` |
| Verify failure + rollback | `rolled_back` or `escalated` |

### Examples

```bash
# Local test with SNOW fields but no live callbacks
ansible-playbook -i inventory/hosts.yml playbooks/advanced/workflow_deploy.yml \
  -e snow_vlan_id=100 \
  -e snow_data_center=lisle \
  -e snow_target_vrf=default \
  -e snow_enable_callbacks=false \
  -e cvp_apply_configlets=false \
  --limit dc_lisle

# Verify after CVP approval (closes ticket when callbacks enabled)
ansible-playbook -i inventory/hosts.yml playbooks/advanced/workflow_verify.yml \
  -e resume_vlan_id=100 \
  -e snow_enable_callbacks=true \
  --limit dc_lisle
```

---

## 5. NetBox export (standalone)

**Not part of Core or Advanced migration.** Run when you want to push local VLAN DB attributes to NetBox.

**Playbook:** `playbooks/export_vlan_to_netbox.yml`

| Variable | Default | Description |
|---|---|---|
| `manual_data_center` | — | Required |
| `manual_vlan_id` | all VLANs in DC DB | Export one VLAN |
| `netbox_export_dry_run` | `true` | `false` to write to NetBox |
| `netbox_export_create_missing` | `true` | POST new VLAN records |
| `netbox_export_update_existing` | `true` | PATCH existing records |
| `netbox_export_site_map` | `{lisle: lisle, omaha: omaha}` | DC → NetBox site slug |

### Examples

```bash
# Preview — no NetBox writes
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100

# Apply export
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  -e netbox_export_dry_run=false
```

Artifact: `reports/netbox_export/netbox_export_<dc>_vlan<id>_<timestamp>.json`

---

## 6. Safety and check mode

| Control | Description |
|---|---|
| `--check` | Supported on discovery and deploy (CVP push skipped in check mode when role respects it) |
| `--diff` | Show template diffs |
| `cvp_apply_configlets=false` | Default safe mode — files only |
| `cvp_change_control_state=set` | Never auto-execute CVP changes |
| `network_backup_enabled` | Per-host backup before workflow deploy (default `true` in group_vars) |

---

## 7. AAP job template mapping

| Job Template | Playbook | Survey fields |
|---|---|---|
| VLAN Discovery | `playbooks/core/discover_vlan.yml` | `manual_data_center`, `manual_vlan_id`, `discovery_allow_probe` |
| Core Deploy | `playbooks/core/workflow_deploy.yml` | `manual_vlan_id`, `manual_data_center`, `manual_target_vrf`, `use_avd`, `cvp_apply_configlets` |
| Core Verify | `playbooks/core/workflow_verify.yml` | `resume_vlan_id` |
| Advanced Deploy | `playbooks/advanced/workflow_deploy.yml` | SNOW fields + deploy vars |
| Advanced Verify | `playbooks/advanced/workflow_verify.yml` | `resume_vlan_id`, `snow_enable_callbacks` |
| NetBox Export | `playbooks/export_vlan_to_netbox.yml` | `manual_data_center`, `manual_vlan_id`, `netbox_export_dry_run` |

Limit each template to the appropriate inventory group (`dc_lisle`, `dc_omaha`).

---

## 8. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Wrong VLANs in report | Missing `manual_vlan_id` filter | Pass `-e manual_vlan_id=<id>` |
| No switches in report | Host not in `discovery_switches` / inventory | Use `discovery_allow_probe=true` or update VLAN DB |
| Deploy fails “VLAN not in DB” | Record missing from `vars/vlans/<dc>/` | Run discovery; copy snippet to `{vid}_{slug}.yml`; fill VNI/VRF |
| CVP push skipped | `cvp_apply_configlets=false` | Set to `true` after reviewing generated files |
| Empty trunk section | Trunk parse mismatch | Check raw output in `.md` report; file an issue with sample CLI |
| NetBox warning only | Expected in Core | Set `netbox_ssot_required=true` only when ready |
