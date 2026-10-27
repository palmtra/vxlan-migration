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

Hosts are defined once, in a role-per-DC group (`<dc>_vteps`, `<dc>_non_vtep`, `<dc>_mls`). Every other group is built from those.

| Group | Purpose |
|---|---|
| `dc_lisle`, `dc_omaha` | Limit discovery/deploy to one data center |
| `vtep_leaves` | Arista VXLAN/EVPN leaves. `migration_tier: fabric`, full discovery |
| `non_vtep_switches` | L2 extensions hanging off a leaf. `migration_tier: access`, `discovery_profile: light` (VLAN, ports, MACs, neighbors, port-channels, STP; no routing, policy, SVI, or VXLAN commands) |
| `legacy_mls` | Legacy MLS being retired: Omaha Nexus 7010 vPC pair (NX-OS 8.4), Lisle Arista MLAG pair. `migration_tier: legacy`, full discovery |
| `network_devices` | All of the above |
| `eos_devices`, `nxos_devices` | OS groups (CLI modules, `network_os_family`). IOS is out of scope |

Always pass `--limit dc_<name>` (or a host list) to control blast radius.

---

## VLAN service types (`l2`, `l3`)

Each VLAN record requires `service_type`. The type says where the gateway will live after migration. `l2_l3` is no longer a type. Extra leaves that host endpoints are participating leaves, not a stretch type.

| Value | Use when |
|---|---|
| `l3` | The gateway moves into the VXLAN fabric (on every participating leaf by default). |
| `l2` | The gateway stays outside the fabric. The fabric provides Layer-2 transport only. |

Discovery always queries SVI; ARP runs when an SVI is present. A discovered SVI is recorded as a source gateway. It is not copied into `gateway_leafs`. For a probe (a VLAN not yet in the DB), discovery picks the type: an active SVI on a discovered switch means `l3`, no SVI means `l2` (external gateway). Pass `-e discovery_probe_service_type=l2|l3` to force it. An explicit `service_type` on a DB record always wins; when it disagrees with discovery, the report lists it under **Action items**.

For `l3`, `gateway_mode` defaults to `distributed_anycast` (every participating leaf hosts the anycast gateway). Set `centralized_anycast` and name the leaf pair in `gateway_leafs` to host it on two leaves only.

Full guide: **[VXLAN_SERVICE_TYPES.md](VXLAN_SERVICE_TYPES.md)**.

---

## 1. Discovery (read-only)

**Playbook:** `playbooks/core/discover_vlan.yml`

Collects VLAN, MAC, SVI, ARP, trunks, LLDP/CDP neighbors, spanning tree, and (on L3-capable devices) VRF, HSRP/VRRP/VARP/anycast, DHCP relay, ACL, PBR, BGP, statics, prefix-lists, and route-maps. Writes reports under `reports/<dc>/<vlan_name>/`. What each command feeds is in [WORKFLOWS.md § Discovery](WORKFLOWS.md#discovery--what-it-collects).

### Required extra vars

| Variable | Description |
|---|---|
| `manual_data_center` | Data center key matching `vars/vlans/<dc>/` (e.g. `lisle`, `omaha`) |

### Common optional vars

| Variable | Default | Description |
|---|---|---|
| `manual_vlan_id` | all VLANs in DB | Discover a single VLAN ID (a range such as `100-110` also works) |
| `target_vlan_ids` | — | Discover multiple VLANs: `[100,200]`, `100,200`, or ranges `100-110,200` |
| `manual_service_id` | — | Load `vars/services/<dc>/<id>.yml` (multi-VLAN seed + `prune.retain`) |
| `discovery_prune_retain` | `{}` | Inline retain keep-list when no service file is used |
| `discovery_allow_probe` | `true` in discover playbook | Allow discovering a VLAN **not yet** in the DC database |
| `discovery_probe_service_type` | empty | Empty lets discovery choose `l2`/`l3` for probe records. Set it to force a type |
| `vlan_discovery_backup_enabled` | `false` | Capture config backup before discovery |
| `discovery_collect_prune_context` | `true` | Collect static routes, BGP, prefix-lists, and route-maps (EOS and NXOS) as **L3 discovery** (not prune actions). OSPF is not collected |
| `discovery_model_routing_detail` | `false` | The model YAML and VLAN DB snippet carry condensed routing (`advertised` prefix / VRF / ASN / how / to whom, `static_routes`, `bgp_peers`). `true` adds the full per-switch `routing.associated` detail the reports show |
| `discovery_keep_idle_endpoint_ports` | `false` | A switch keeps the VLAN only when MACs are learned on its endpoint-facing ports. Idle endpoint, fabric-interconnect, firewall / load balancer, unclassified and outside-switch ports make it prune-eligible, with an action item naming them. `true` keeps such switches (applies to migration placement and prune alike) |
| `discovery_scan_all_switches` | `false` | With a VLAN record, discovery covers the record's switches plus the DC's border leaves. `true` scans every switch |
| `migration_tier` (inventory group/host var) | `eos_devices`: `fabric`; `nxos_devices`: `legacy`; `ios_devices`: `access` | `fabric`: Arista leaf (a VTEP when Vxlan1 exists, otherwise a non-VTEP switch). `legacy`: the MLS / core being retired. `access`: a non-VTEP L2 switch that stays and must attach through a VTEP. Set `access` per host for any switch in the discovery list that is not a VTEP |
| `vni_ranges` / `vni_default_customer_type` | Lisle 20000 / 22000, Omaha 10000 / 11000 / `external` | Per-DC VNI ranges for `internal` and `external` customers (`inventory/group_vars/all/vni_ranges.yml`). A VLAN record without `vni` gets the next free VNI in its DC and `customer_type`, skipping VNIs in the DC's VLAN DB and VNIs (L2 and L3) already mapped on discovered switches. Proposed, never written: confirm it into the SSOT |
| `vtep_source_interface` | `Loopback1` | Expected VTEP source on fabric leaves (AVD default). Also the source for underlay pings in verify |
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

# Probe a range; VLANs not in the DB become probe records
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e target_vlan_ids=300-310 \
  -e discovery_allow_probe=true \
  --limit dc_lisle

# Discover every VLAN declared under vars/vlans/lisle/
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  --limit dc_lisle
```

### Report outputs

| File | Contents |
|---|---|
| `<vlan>_discovery_<ts>.md` | Short report: summary, action items, switch table, gateway objects to rebuild, routing objects, active endpoints, prune order |
| `<vlan>_endpoints_<ts>.csv` | Endpoint baseline: one row per active IP (ARP) or MAC-only endpoint, with its edge switch and port. Use it for pre/post-migration tests |
| `<vlan>_discovery_<ts>.csv` | One row per switch, including `placement` |
| `<vlan>_discovery_<ts>.yml` | Deployable VLAN model (condensed routing, EVPN, placement) |
| `<vlan>_discovery_<ts>.json` | LLM analysis payload: the model, placement evidence, and discovery facts |
| `<vlan>_prune_<ts>.md` | Human prune report: EOS session + commit timer, NXOS checkpoint |
| `<vlan>_prune_<ts>.json` | Execution-shaped prune payload (`apply_automated: false`) |
| `{vid}_{slug}.yml` | Starter deploy model for `vars/vlans/<dc>/` (confirm the proposed VNI; `gateway_leafs` only for `centralized_anycast`) |

Key **discovery** fields:

- `action_items` — what to do, one imperative line each: fix reachability, create a missing VRF / L3 VNI on named leaves, rebuild DHCP relay / ACL / PBR on the fabric SVI, recreate statics and BGP peers on the VLAN, re-home firewall ports, review switches, STP root outside scope, VNI collisions, endpoints that could not be located
- `gateway_assessment` — where the gateway lives today (`legacy`, `fabric`, `mixed`, `external`), subnets and VIPs, redundancy (HSRP / VRRP / VARP / anycast), what moves with it (DHCP relay, ACL, PBR, IPv6, multicast, NAT, uRPF, and any SVI line not understood), firewall/router ports (`external_exits`, report and JSON only), and the recommended migration type
- `endpoint_table` — active IPs from ARP joined to the MAC table edge port (`edge`, `uplink_only`, `not_in_mac_table`), plus MAC-only endpoints. Gateway and FHRP addresses are flagged
- `spanning_tree` — root bridge resolved to a discovered switch, or flagged as outside discovery scope
- `fabric_readiness` — `show interfaces Vxlan1` on each fabric leaf that will carry the VLAN: VTEP source, replication mode, flood list source, and the gateway VRF's L3 VNI. The fabric uses head-end replication with EVPN flood lists only; anything else is an action item
- `not_discovered` — switches in the play that never reported back
- `l2_attachment` — switch roles (`vtep`, `mls`, `l2_switch`), non-VTEP groups with their VTEP uplinks and MLS links (`vtep_only`, `dual_attached`, `no_vtep_uplink`, `no_endpoints`), link prunes, and fabric-legacy interconnects
- `link_prune_plans` — per switch, the MLS-facing trunk to drop the VLAN from. `ready`, `blocked_until_vni_live`, or `blocked_until_gateway_in_fabric`. Ready plans are applied by `prune_vlan.yml` like prune-eligible plans

- `endpoint_inventory` / `compute_inventory` — MACs and ports on compute links (servers, IBM Z, Nutanix, UCS, HCI). Switch-to-switch MAC learning is omitted.
- `uplink_inventory` — trunks toward other switches (context only; does not make a leaf participating)
- `unknown_inventory` — ports that could not be classified. With MACs on them the switch is participating; without MACs it is prune-eligible and the ports are listed as idle (`review` only with `discovery_keep_idle_endpoint_ports=true` or when the MAC table could not be read)
- Port roles come from LLDP/CDP first (`switch_uplink` for an inventory switch, `downstream_switch` for a switch outside the inventory, `network_service` for a firewall / load balancer / router, `compute`), then from the description. A port that faces an endpoint (`compute`, `network_service`, `downstream_switch`, UCS fabric interconnect) keeps the switch only while MACs are learned on it; an idle one is pruned with a warning (`discovery_keep_idle_endpoint_ports=true` keeps it)
- `deployment_model.placement` — `participating_leafs` (VTEPs only; these become `target_switches`), `gateway_leafs`, `l2_attached_switches` (non-VTEP switches that keep the VLAN, with their VTEP uplinks; never `target_switches`), `prune_eligible_leafs`, `source_gateway_devices`
- `routing_detail` (report and JSON; in the model only with `discovery_model_routing_detail=true`) — statics whose next hop is in the VLAN prefix, that exit via `Vlan<id>`, whose prefix is the VLAN prefix, Null0 discards that cover it, or whose `name` equals an optional `static_route_tags` entry (case-insensitive). Also BGP networks or aggregates of that prefix, `redistribute connected`/`direct`/`static` on the source gateway with the route-map verdict for the VLAN prefix, BGP peers that sit on the VLAN, `bgp_exports` (each peer's outbound route-map verdict), `bgp_live` (`show ip bgp <prefix>`: in the table, and advertised to which peers), and `bgp_fabric` (border leaves). Review items, not prune actions
- `deployment_model.routing` — the condensed form: `advertised` (prefix, VRF, ASN, `network` or `redistribute ... route-map`, peers it is sent to), `static_routes`, `bgp_peers`, deduplicated across the gateway pair
- Route-map verdicts walk prefix-lists in sequence order: `permit`, `deny`, `unknown` (a clause matches on something discovery cannot evaluate, such as a community), or `missing` (route-map not defined)

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
See `vars/vlans/README.md`.

### Config generation

| Variable | Default | Description |
|---|---|---|
| `use_avd` | `false` | `true` → `roles/avd_vxlan_config` (Arista AVD); `false` → legacy Jinja |
| `cvp_apply_configlets` | `false` | `true` → push to CVP; `false` → render files only under `reports/cvp_configlets/` |

Detailed generate-only walkthrough: [examples/generate-config-without-cvp-push.md](examples/generate-config-without-cvp-push.md).
| `cvp_change_control_state` | `set` | CVP change control action (`set` = pending approval) |
| `cvp_change_control_auto_execute_allowed` | `false` | Must stay `false` unless explicitly overriding after review |
| `cvp_build_borderleaf_config` | `false` | Border-leaf VRF/L3 VNI configlet. Off: do not create or rebind an existing VRF |
| `avd_default_bgp_as` | `""` | Inventory fallback ASN when the VLAN record has no per-leaf `asn` |
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
| `verify_endpoint_reachability` | `true` | Ping every host IP in the discovery endpoint baseline from one gateway leaf, in the VLAN's VRF. Pass = every baseline host answers; skipped when no baseline exists |
| `verify_endpoint_baseline_csv` | newest `<vlan>_endpoints_*.csv` | Baseline to test against |
| `verify_ping_host` | first gateway leaf | Leaf that sends the pings |
| `verify_ping_options` | `repeat 2 timeout 1` | EOS ping options per endpoint |

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

**Not part of Core or Advanced migration.** Run when you want to push local VLAN DB attributes and route targets to NetBox.

**Playbook:** `playbooks/export_vlan_to_netbox.yml`

| Variable | Default | Description |
|---|---|---|
| `manual_data_center` | — | Required |
| `manual_vlan_id` | all VLANs in DC DB | Export one VLAN |
| `netbox_export_dry_run` | `true` | `false` to write to NetBox |
| `netbox_export_create_missing` | `true` | POST new VLAN records |
| `netbox_export_update_existing` | `true` | PATCH existing records |
| `netbox_export_site_map` | `{lisle: lisle, omaha: omaha}` | DC → NetBox site slug |
| `netbox_validate_certs` | `false` | Verify the NetBox TLS certificate. Off because NetBox uses a self-signed certificate |
| `netbox_export_route_targets` | `true` | Export a route target for every `action: migrate` VLAN |
| `netbox_export_rt_description_marker` | `Created by Ansible VXLAN Automation` | Text after the VLAN name in the route target description |

### Route targets

Each migrated VLAN gets one route target in NetBox (IPAM > Route Targets):

| NetBox field | Value |
|---|---|
| Name | `<vni>:<vlan id>` (or the record's `l2_rt`), the route target the configlets use |
| Tenant | `ownership.tenant`, the numeric tenant id, matched to the NetBox tenant of that name |
| Owner | `ownership.owner`, the customer name, matched to the NetBox owner of that name |
| Description | `<vlan_name or name> - Created by Ansible VXLAN Automation` |

NetBox route target names are unique, so a route target that already exists is updated in place:
its tenant, owner and description are replaced. A VLAN with no VNI, no tenant or no owner, or whose
tenant or owner is not in NetBox, is not written, and the run fails after the export with every such
VLAN listed. Tenants and owners are never created by the export. Quote the tenant id in the record
(`tenant: "0000001"`): unquoted, YAML reads an id made only of the digits 0-7 as an octal number.

The NetBox token needs view permission on tenants and owners and add/change permission on route
targets. Owners need NetBox 4.5 or later. The `netbox.netbox` collection has no owner option, so the
role uses the REST API directly.

### When a NetBox request fails

The requests carry the token, so their own output is hidden (`no_log`). A failed request is
reported by the next task instead, with the HTTP status and NetBox's reason:

| Message | Fix |
|---|---|
| `the NetBox TLS certificate is not trusted` | Only with `netbox_validate_certs: true`. Install the CA that signed it on the control node, or set it back to `false` |
| `HTTP 401` / `HTTP 403` | Check `netbox_token` and its permissions |
| `HTTP 404` | `netbox_url` is the NetBox address without `/api` |
| `no HTTP answer from NetBox` | DNS, firewall or proxy between the control node and NetBox |

To test the connection by hand (`curl -k` skips the certificate check, as the playbooks do):

```bash
curl -sS -o /dev/null -w '%{http_code}
' -H "Authorization: Token <token>" "<netbox_url>/api/ipam/vlans/?vid=100"
```

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
| Action item "was not discovered" | Host unreachable or failed before discovery finished | Fix reachability and re-run; placement and prune lists are incomplete until then |
| Action item "`<command>` failed" | Command timed out or is not supported on that platform/version | Re-run; if it persists, check the command on the device |
| Ports classified by description only | LLDP/CDP disabled or the neighbor does not advertise | Enable LLDP, or fix the port description |
| Deploy fails “VLAN not in DB” | Record missing from `vars/vlans/<dc>/` | Run discovery; copy snippet to `{vid}_{slug}.yml`; fill VNI/VRF |
| CVP push skipped | `cvp_apply_configlets=false` | Set to `true` after reviewing generated files |
| Empty trunk section | Trunk parse mismatch | Check raw output in `.md` report; file an issue with sample CLI |
| NetBox warning only | Expected in Core | Set `netbox_ssot_required=true` only when ready |
