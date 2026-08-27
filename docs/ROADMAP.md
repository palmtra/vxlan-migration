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

---

## In progress / next up

### Discovery quality

- [x] Structured SVI parse (description, MTU, IP, virtual-router address, VRF)
- [x] EOS static route + BGP neighbor collection for prune plan candidates
- [x] Retain-aware `prune_plan` candidates in discovery reports (no deletes applied)
- [ ] Structured parsing of trunk/access interface names tied to VLAN port lists
- [ ] NXOS cumulative allow-list parsing for `add`/`remove` trunk lines
- [ ] CLI output fixture tests (EOS/NXOS/IOS sample files)
- [ ] In-repo prune apply role (session diffs + commit timer + retain fail-closed)

### VLAN database

- [x] Optional service bundles under `vars/services/<dc>/` (multi-VLAN orchestrator + retain)
- [x] JSON Schema validation for `vars/vlans/<dc>/*.yml` and `vars/services/<dc>/*.yml`
- [ ] Document and enforce additional fields (customer ID, old VRF, gateway, subnet, etc.)
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
- Post-migration handoff to `decomm-vlan` with structured cleanup payload
- Git-backed VLAN DB change review (MR per VLAN addition)
- Prometheus / AWX artifact upload for report URLs on ticket

---

## How to add items

Add new bullets under the relevant section. Move items to **Completed** when merged.
Link GitLab issues with `#<issue-number>` when available.
