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
- [x] P0 hardening: `no_log` on secrets, safer rollback defaults, GitHub CI. The direct-device migrate path was removed; deploy is CVP only
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

- [x] LLDP/CDP neighbor classification (switch in inventory, switch outside scope, firewall/LB/router, server) with port-channel members
- [x] Firewall/router ports are local endpoints, never switch uplinks (no prune of the switch holding the external gateway)
- [x] Gateway assessment: legacy MLS / fabric / mixed / external, VIPs per subnet, redundancy, objects that move with it (DHCP relay, ACL, PBR, IPv6, OSPF, multicast, NAT, uRPF, unrecognised SVI lines)
- [x] `gateway_mode` (`distributed_anycast` / `centralized_anycast`) and `gateway_location` in the VLAN model
- [x] Route-map / prefix-list evaluation for redistribution and peer export; live `show ip bgp <prefix>`; OSPF
- [x] Active endpoint baseline (ARP joined to edge port) in the report and `<vlan>_endpoints_<ts>.csv`
- [x] Unreachable switches and failed commands reported as action items
- [x] STP root resolution; VXLAN VLAN-to-VNI collisions
- [x] VLAN ranges (`100-110`) and multi-VLAN probing
- [x] End-to-end mock run of `discover_vlan.yml` in CI (`make mock`)
- [x] Fabric VTEP readiness from `show interfaces Vxlan1` (headend + EVPN flood list on Loopback1, gateway VRF L3 VNI)
- [x] Head-end replication only: multicast BUM groups removed from config generation and rejected in the VLAN schema
- [x] VTEP source defaults to Loopback1 (AVD); sample inventory names the NXOS hosts as legacy MLS
- [x] Non-VTEP switches: `l2_attached` (keep VLAN, no VNI), VTEP attachment points, MLS link prunes gated on VNI live and gateway reachable, EVPN-legacy bridge and interconnect loop warnings
- [x] Only VTEPs are VNI targets; an MLS with endpoints is `legacy_retained`
- [x] EOS `show interfaces trunk`: physical `Et` trunks recognised; allowed list from the `Vlans allowed` section
- [x] Inventory by role per DC (`vtep_leaves`, `non_vtep_switches`, `legacy_mls`); Lisle legacy MLS is Arista
- [x] Light discovery profile for non-VTEP switches
- [x] UCS fabric interconnects keep the VLAN on their leaf
- [x] Prune phases: MLS link prune first, non-VTEP side second, teardown blocked while the SVI is the active gateway
- [x] VNI proposal from per-DC internal/customer ranges (ranges still placeholders)
- [x] Legacy SVI MTU carried (capped at 9214); secondary subnets flagged, not migrated; DHCP relay not migrated
- [x] Verify pings every baseline endpoint from a gateway leaf
- [x] AVD services repo (palmtra/avd-vxlan-repo): services-only, per-DC tenants, rendered with pyavd, CVaaS Static Configuration Studio manifest with change control left pending approval
- [x] `export_avd_services.yml`: VLAN record to AVD SVI / l2vlan (distributed and centralized anycast, external gateway), router-id/ASN drift check against the AVD repo

### Pending decisions (see the open-questions doc)

- [x] Lisle production inventory (VTEP / non-VTEP / legacy pairs) and AVD fabric facts; sw05/06 BGP in asdot
- [x] EVPN VLAN scheme, both DCs: RD `<router-id>:<vlan>`, RT `<l2 vni>:<vlan>` (VLAN 966 is the reference)
- [x] VNI standard: Lisle internal 20000 / external 22000, Omaha 10000 / 11000 (`customer_type`)
- [x] Subnets advertised with `network` under the VRF (no redistribute connected); gateway in VRF default refused
- [x] External gateway VLANs placed on the DC's border pair; `external_gateway_leafs` as the manual override
- [x] Omaha production inventory and AVD fabric facts (sw14 Loopback0 to confirm)
- [x] VNI range end values confirmed
- [x] Routing discovery matches the VLAN prefix or a more-specific only (no covering /16, no next hop outside the subnet)
- [x] Border-leaf BGP view of each legacy subnet (same AS as the MLS): advertised over iBGP from the MLS, by a fabric leaf, or not at all
- [ ] NetBox as the VNI allocator: next free per DC and customer_type
- [ ] CVaaS: fabric config in Static Configuration Studio? `hostname:` tag query, or serial numbers per leaf
- [ ] Open the AVD repo PR from the migration automatically (today: export + manual commit/PR)
- [ ] Retire the legacy `cv_configlet_v3` deploy path once the AVD repo path is proven
- [ ] Sample outputs from the N7K and an EOS leaf (B9) for parser fixtures
- [ ] Teams / email notification on change control (J4)

### Parked / hold (do not implement until there is a use-case)

- [ ] Default-VRF static route + BGP neighbor **prune candidates** (shown as L3 review only; not actions)

Discovery never deletes. Prune apply is `playbooks/core/prune_vlan.yml`: dry-run unless `prune_apply=true` and `prune_approval=approve`.

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
