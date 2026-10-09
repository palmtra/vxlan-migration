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

## VLAN service types (`l2`, `l3`)

Each VLAN record requires `service_type`. The type says where the gateway will live after migration. `l2_l3` is no longer a type. Extra leaves that host endpoints are participating leaves, not a stretch type.

| Value | Use when |
|---|---|
| `l3` | The gateway moves into the VXLAN fabric. Name those leaves in `gateway_leafs`. |
| `l2` | The gateway stays outside the fabric. The fabric provides Layer-2 transport only. |

Discovery always queries SVI; ARP runs when an SVI is present. A discovered SVI is recorded as a source gateway. It is not copied into `gateway_leafs`. Probe mode defaults to `l3`; pass `-e discovery_probe_service_type=l2` for an L2 probe.

Full guide: **[VXLAN_SERVICE_TYPES.md](VXLAN_SERVICE_TYPES.md)**.

---

## 1. Discovery (read-only)

**Playbook:** `playbooks/core/discover_vlan.yml`

Collects VLAN, MAC, SVI, ARP, trunks, and (on L3-capable devices) VRF/HSRP/BGP/statics. Writes reports under `reports/<dc>/<vlan_name>/`.

### Required extra vars

| Variable | Description |
|---|---|
| `manual_data_center` | Data center key matching `vars/vlans/<dc>/` (e.g. `lisle`, `omaha`) |

### Common optional vars

| Variable | Default | Description |
|---|---|---|
| `manual_vlan_id` | all VLANs in DB | Discover a single VLAN ID |
| `target_vlan_ids` | — | Discover multiple VLANs: `[100,200]` or `100,200` |
| `manual_service_id` | — | Load `vars/services/<dc>/<id>.yml` (multi-VLAN seed + `prune.retain`) |
| `discovery_prune_retain` | `{}` | Inline retain keep-list when no service file is used |
| `discovery_allow_probe` | `true` in discover playbook | Allow discovering a VLAN **not yet** in the DC database |
| `discovery_probe_service_type` | `l3` | `service_type` assigned to synthetic probe records |
| `vlan_discovery_backup_enabled` | `false` | Capture config backup before discovery |
| `discovery_collect_prune_context` | `true` | Collect static routes + BGP (EOS and NXOS) as **L3 discovery** (not prune actions) |
| `discovery_write_prune_plan` | `true` | Write the separate prune report (trunk/SVI/VLAN + EOS session text; never applies deletes) |
| `prune_commit_timer` | `00:10:00` | EOS `commit timer` value printed in the prune report and used when prune apply is approved |
| `prune_apply` | `false` | `playbooks/core/prune_vlan.yml` only. `true` sends CLI to prune-eligible switches |
| `prune_approval` | empty | Must be `approve` together with `prune_apply=true`. See [examples/prune-vlan.md](examples/prune-vlan.md) |
| `discovery_write_device_prune_plans` | `true` | Write `reports/<dc>/_prune_plans/*.json` |
| `discovery_enable_ios` | `false` | Parked IOS discovery; set `true` to run `gather_ios*` (see [OS_SUPPORT.md](OS_SUPPORT.md)) |
| `discovery_write_markdown` | `true` | Write `.md` report |
| `discovery_write_csv` | `true` | Write `.csv` report |
| `discovery_write_json` | `true` | Write `.json` report |
| `discovery_write_yaml` | `true` | Write the deployable YAML model |
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
| `<vlan>_discovery_<ts>.md` | Short report: migration type, participating leaves, prune-eligible leaves, gateway leaves, source gateways |
| `<vlan>_discovery_<ts>.csv` | One row per switch, including `placement` |
| `<vlan>_discovery_<ts>.yml` | Deployable VLAN model (routing, EVPN, placement) |
| `<vlan>_discovery_<ts>.json` | LLM analysis payload: the model, placement evidence, and discovery facts |
| `<vlan>_prune_<ts>.md` | Human prune report: EOS session + commit timer, NXOS checkpoint |
| `<vlan>_prune_<ts>.json` | Execution-shaped prune payload (`apply_automated: false`) |
| `0100_legacy_web.yml` | Starter deploy model for `vars/vlans/<dc>/` (VNI and `gateway_leafs` still need an engineer) |

Key **discovery** fields:

- `endpoint_inventory` / `compute_inventory` — MACs and ports on compute links (servers, IBM Z, Nutanix, UCS, HCI). Switch-to-switch MAC learning is omitted.
- `uplink_inventory` — trunks toward other switches (context only; does not make a leaf participating)
- `unknown_inventory` — ports that could not be classified. A non-gateway switch with those ports is `review`. A source gateway stays prune-eligible; the unclassified ports are called out on the dry-run
- `deployment_model.placement` — `participating_leafs`, `gateway_leafs`, `prune_eligible_leafs`, `source_gateway_devices`
- `routing.associated` — statics whose next hop is in the VLAN prefix, whose prefix is the VLAN prefix, or whose `name` equals an optional `static_route_tags` entry (case-insensitive). Also BGP networks or aggregates of that prefix, `redistribute connected`/`static` on the source gateway, and BGP peers that sit on the VLAN. Review items, not prune actions

Key **prune** fields (separate files):

- `prune_plans[].execution` — EOS `configure session` / `commit timer` / `configure confirm` / abort; NXOS checkpoint + rollback
- Participating leaves are `retained_participating`. Source gateways with no local endpoints are prune-eligible and listed first. Only that set keeps trunk / SVI / VLAN CLI
- Static routes, BGP, and shared-VRF objects stay in `l3_review`, never actions
- Device JSON: `reports/<dc>/_prune_plans/<host>_prune_plan_<ts>.json`
- `prune_commit_timer` default `00:10:00` (report text only; nothing is applied)

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
| `cvp_build_borderleaf_config` | `false` | Border-leaf VRF/L3 VNI configlet. Off: do not create or rebind an existing VRF |
| `avd_default_bgp_as` | `""` | Default BGP ASN for AVD when not set per host (required if `evpn_enabled`) |
| `allow_placeholder_fabric_defaults` | `false` | Allow lab placeholder mcast `239.1.1.1` / empty ASN |
| `auto_rollback_on_verification_failure` | `false` | Attempt CVP rollback on verify failure (off by default) |
| `cvp_rollback_allow_destructive_no_vlan` | `false` | Allow rollback to emit `no vlan` / VRF teardown |
| `artifacts_dir` / `ARTIFACTS_DIR` | project root | Root for `reports/` and `backups/` (AAP EE mount) |

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
| `auto_rollback_on_verification_failure` | `false` | Attempt CVP rollback on verify failure (off by default) |
| `cvp_rollback_allow_destructive_no_vlan` | `false` | Allow rollback `no vlan` / VRF teardown |

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
| `prune_apply=false` | Prune playbook dry-run. Apply needs `prune_apply=true` and `prune_approval=approve` |
| `cvp_change_control_state=set` | Never auto-execute CVP changes |
| `network_backup_enabled` | Per-host backup before workflow deploy (default `true` in group_vars) |

---

## 7. AAP job template mapping

| Job Template | Playbook | Survey fields |
|---|---|---|
| VLAN Discovery | `playbooks/core/discover_vlan.yml` | `manual_data_center`, `manual_vlan_id`, `discovery_allow_probe` |
| VLAN Prune | `playbooks/core/prune_vlan.yml` | `manual_data_center`, `manual_vlan_id`, `prune_apply`, `prune_approval` |
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
