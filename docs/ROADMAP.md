# Roadmap - vxlan-migration

Open features and planned work. Completed items are kept for context.

## Completed (this iteration)

- [x] Split **Core** (no ServiceNow) and **Advanced** (ServiceNow plugin) workflows
- [x] Per-DC local VLAN databases under `vars/vlans/<dc>/` (one YAML file per VLAN)
- [x] Local VLAN DB as **primary SSOT**; NetBox demoted to secondary sync check
- [x] VLAN record fields: `service_type`, `target_switches`, `discovery_switches`
- [x] CVP deploy/rollback driven by `target_switches` from VLAN DB (not discovery heuristics)
- [x] VLAN-centric discovery reports (`<vlan_name>_discovery_<timestamp>.*`)
- [x] Verify stage scoped to `target_switches` (EOS only)
- [x] Targeted per-VLAN CLI (`show vlan id`, `show run interface VlanX`)
- [x] MAC learning, VRF-aware ARP, and trunk VLAN carriage reporting (read-only)
- [x] Trunk cleanup **recommendations** in discovery reports (no config command generation)
- [x] Standalone NetBox export playbook (`playbooks/export_vlan_to_netbox.yml`)
- [x] Workflow docs with mermaid diagrams (`docs/WORKFLOWS.md`, `docs/USAGE_GUIDE.md`)
- [x] Service bundles + retain-aware discovery prune plans
- [x] Platform policy: `main` = NXOS→EOS; IOS parked (`docs/OS_SUPPORT.md`, archive tag/branch)
- [x] P0 hardening: `no_log` on secrets, gate direct-device migrate, safer rollback defaults, GitHub CI
- [x] Split `vlan_filters` into `plugins/vlan_lib/` + thin `plugins/filter/vlan_filters.py`
- [x] JSON Schema validation for VLAN/service SSOT (`schemas/*.schema.json`)
- [x] Molecule localhost scenarios for `vlan_db` and `service_db`
- [x] Placement model: participating leaves vs prune-eligible leaves; migration type is only `l2` or `l3`
- [x] Discovery outputs: Markdown report, YAML deploy model, JSON analysis payload
- [x] Prune apply playbook: dry-run by default, explicit approval, prune-eligible switches only

---

## In progress / next up

### Discovery quality

- [x] Structured SVI parse (description, MTU, IP, virtual-router address, VRF)
- [x] EOS static route + BGP neighbor collection for **L3 review** (not prune actions)
- [x] NXOS L3 discovery (VRF-context statics, split-stanza BGP, HSRP groups)
- [x] First-class `l3_discovery` on VLAN reports (shared-VRF warning; SVI/HSRP/BGP/statics)
- [x] App-owner L2 inventory (MACs, access ports, trunks) plus prune-where and SSOT next-step sections
- [x] Retain-aware `prune_plan` candidates in discovery reports (no deletes applied)
- [x] Structured MAC address table + ARP entry parsing (MAC→port / IP→MAC) in discovery reports
- [x] Trunk cleanup intent kept simple: list VLAN-carrying trunks + `switchport trunk allowed vlan remove <id>` (no per-interface config dump)
- [x] NXOS cumulative allow-list parsing for `add`/`remove` trunk lines
- [x] CLI output fixture tests (EOS/NXOS sample files under `tests/fixtures/cli/`)
- [x] Split discovery (compute MACs / endpoints) from prune (EOS session + commit timer)
- [x] Human-only prune report: trunk / SVI / VLAN candidates with compute-endpoint blockers
- [x] Discovery SSOT snippet fills VRF, gateway, prefixes, `service_type`, switch lists from facts

### Parked / hold (do not implement until there is a use-case)

- [ ] Default-VRF static route + BGP neighbor **prune candidates** (shown as L3 review only; not actions)
- [ ] In-repo prune apply role (session diffs + commit timer + retain fail-closed)

Human operators prune from the **prune report** (EOS configure session + timer). Automated apply is reserved until that report is proven; discovery never deletes.

### VLAN database

- [x] Optional service bundles under `vars/services/<dc>/` (multi-VLAN orchestrator + retain)
- [x] JSON Schema validation for `vars/vlans/<dc>/*.yml` and `vars/services/<dc>/*.yml`
- [x] Document optional SSOT fields filled by discovery (`gateway`, `prefixes`)
- [ ] Document and enforce additional fields (customer ID, old VRF, etc.)
- [ ] Support VLANs shared across DCs vs DC-exclusive IDs (collision policy)

### Core workflow hardening

- [ ] Dry-run / preflight playbook (validate VLAN DB + inventory + CVP connectivity only)
- [ ] Idempotent CVP configlet naming and change-control reuse policy
- [ ] Service-type-aware configlet templates (L2-only vs L3 SVI/VRF handling)
- [ ] Border-leaf selection from VLAN DB instead of hostname regex

### Advanced workflow (ServiceNow)

- [ ] Live ServiceNow webhook + work-note field mapping
- [ ] Map SNOW payload fields (`customer_id`, `old_vrf`, `new_vrf`) into VLAN DB lookups
- [ ] CVP approval detector (poll or native webhook → EDA `/cvp_approval`)
- [ ] Optional `netbox_ssot_required: true` gate once NetBox is populated

### NetBox

- [x] Standalone export role/playbook (dry-run default)
- [ ] VRF and tenant mapping in NetBox export
- [ ] Drift report comparing local DB vs NetBox before sync
- [ ] Mark local record `synced_to_netbox: true` with timestamp

### Verification

- [ ] Verification report per VLAN (mirror discovery report layout)
- [ ] Pass/fail artifact written to `reports/<dc>/<vlan_name>/`
- [ ] Partial-failure policy across multiple `target_switches`

### Operations

- [ ] AAP Job Templates for Core playbooks (separate from Advanced)

---

## Ideas / backlog

- Multi-VLAN batch migration from a single Core job
- Integration test harness with mocked EOS/NXOS (`ansible.netcommon` + text fixtures)
- Post-migration handoff to `decomm-vlan` with structured cleanup payload (human prune report is the payload today)
- Git-backed VLAN DB change review (MR per VLAN addition)
- Prometheus / AWX artifact upload for report URLs on ticket

---

## How to add items

Add new bullets under the relevant section. Move items to **Completed** when merged.
Link GitLab issues with `#<issue-number>` when available.
