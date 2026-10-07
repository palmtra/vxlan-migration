# Workflows — Core vs Advanced

This repository automates **legacy VLAN → VXLAN/EVPN migration** on Arista EOS via CloudVision (CVP).
On **`main`**, discovery targets **NXOS** (legacy) and **EOS** (fabric); deploy/verify are **EOS only**.
IOS discovery is parked — see [OS_SUPPORT.md](OS_SUPPORT.md).

Two workflow tiers share the same roles. **Build and harden Core first**, then enable Advanced when ServiceNow integration is ready.

---

## Scope

| In scope (this repo) | Out of scope |
|---|---|
| Read-only VLAN discovery (NXOS + EOS; optional IOS) | Greenfield / new VLAN deployments (separate app) |
| Local per-DC VLAN DB as primary SSOT | Automatic trunk/SVI cleanup **apply** on devices (candidates only today) |
| Optional service bundles (`vars/services/`) for multi-VLAN cutovers | Executing prune/delete sessions (parked; human prune from the report) |
| AVD or Jinja → CVP configlet generation | NetBox export as part of the migration run |
| CVP change control creation (pending approval) | Executing CVP change control automatically |
| NXOS→EOS as the supported migration path on `main` | Expanding IOS as a first-class platform on `main` |

Legacy VLAN cleanup and trunk pruning are **reported** during discovery as a
human-review `prune_plan` (trunk / SVI / VLAN only). Application is not automated
and is parked until there is a use-case. Default-VRF statics and BGP are L3 review
only, never prune actions.

---

## Core workflow

**Input:** VLAN ID + data center (+ VLAN record in local SSOT for deploy).

**Output:** Discovery reports; CVP configlets in a **pending** change control.

**End state:** Operator approves and executes the change control in CloudVision. The Ansible run stops there.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#e8f4fd', 'primaryTextColor': '#111827', 'primaryBorderColor': '#0366d6', 'lineColor': '#374151', 'secondaryColor': '#f3f4f6', 'tertiaryColor': '#ffffff' }}}%%
flowchart TD
    Start([Operator: VLAN ID + DC]) --> Intake[1. Manual intake<br/>roles/workflow_input]

    Intake --> Discover{Run discovery?}
    Discover -- Standalone --> DiscOnly[playbooks/core/discover_vlan.yml]
    Discover -- Deploy path --> VlanDB[2. Load local VLAN DB<br/>vars/vlans/&lt;dc&gt;.yml]

    DiscOnly --> DiscRole[roles/vlan_discovery<br/>read-only CLI]
    DiscRole --> Reports[(reports/&lt;dc&gt;/&lt;vlan_name&gt;/)]
    Reports --> Plan[Operator updates VLAN DB<br/>from report snippet]

    VlanDB --> DiscRole2[3. Discovery on discovery_switches]
    DiscRole2 --> Handoff[4. Cleanup hand-off artifact<br/>roles/external_cleanup]
    Handoff --> NetBoxCheck[5. NetBox drift check<br/>roles/netbox_check — warn only]
    NetBoxCheck --> ConfigGen{use_avd?}

    ConfigGen -- false --> Jinja[6a. roles/cvp_deploy]
    ConfigGen -- true --> AVD[6b. roles/avd_vxlan_config]

    Jinja --> CVP[7. Push configlets to CVP<br/>arista.cvp — pending change control]
    AVD --> CVP

    CVP --> End([Stop — operator approves<br/>change control in CVP])

    Plan --> Deploy[playbooks/core/workflow_deploy.yml]
    Deploy --> VlanDB
```

### Core playbooks

| Playbook | Purpose |
|---|---|
| `playbooks/core/discover_vlan.yml` | Read-only discovery; writes VLAN-centric reports |
| `playbooks/core/workflow_deploy.yml` | Intake → discovery → CVP configlet push (ends at pending CC) |
| `playbooks/core/workflow_verify.yml` | Optional post-approval verification (separate run) |
| `playbooks/core/workflow.yml` | Deploy + verify chained (demo/lab only) |

### Primary SSOT

Per-DC local VLAN databases (one YAML file per VLAN):

- `vars/vlans/lisle/0100_legacy_web.yml`
- `vars/vlans/omaha/0100_legacy_web.yml`

See `vars/vlans/README.md` and `vars/vlans/<dc>/_example.yml` for naming rules.

NetBox is **secondary**. A missing NetBox record logs a warning; the run continues unless `netbox_ssot_required: true`.

### Standalone utilities (not in workflow)

| Playbook | Purpose |
|---|---|
| `playbooks/export_vlan_to_netbox.yml` | Export local VLAN DB attributes to NetBox (dry-run by default) |
| `playbooks/check_netbox.yml` | One-off NetBox VLAN lookup |

---

## Advanced workflow

Wraps Core with ServiceNow at **start** (intake) and **end** (status callbacks).

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#fef3c7', 'primaryTextColor': '#111827', 'primaryBorderColor': '#d97706', 'lineColor': '#374151' }}}%%
flowchart TD
    SNOWStart([ServiceNow change ticket<br/>or manual SNOW fields]) --> SNOWIn[roles/servicenow_input]

    SNOWIn --> CoreDeploy[playbooks/advanced/workflow_deploy.yml<br/>Core deploy + SNOW callbacks]

    CoreDeploy --> WaitCC[Change control pending in CVP]
    WaitCC --> Operator[Operator approves CC in CVP]

    Operator --> Verify[playbooks/advanced/workflow_verify.yml<br/>Core verify + SNOW close]

    Verify --> SNOWEnd([ServiceNow ticket updated<br/>complete / escalated / rolled_back])
```

| Playbook | Purpose |
|---|---|
| `playbooks/advanced/workflow_deploy.yml` | Core deploy + SNOW “pending approval” callback |
| `playbooks/advanced/workflow_verify.yml` | Core verify + SNOW “complete” / escalation |
| `playbooks/advanced/workflow.yml` | Chained deploy + verify (no CVP wait) |

---

## Discovery — what it collects

Per switch, per VLAN (read-only):

| Attribute | Source |
|---|---|
| VLAN + L2 ports | `show vlan id <id>` (Ports column; empty = no access/trunk) |
| Compute vs switch uplink | Interface `description` (z14/Nutanix/UCS vs `to ...-sw`) plus access-vs-trunk fallback |
| Learned MACs | `show mac address-table dynamic vlan <id>` (discovery shows compute/unknown links only) |
| SVI / L3 | `show run interface Vlan<id>` (parsed: VRF, IP, VR/anycast, HSRP, MTU, description) |
| SVI VRF + ARP | VRF from SVI config; ARP whenever an SVI exists (not gated on `service_type`) |
| Static routes (EOS + NXOS) | EOS `section ip route`; NXOS `vrf context` + global `ip route` → **L3 discovery** (shared VRF OK; not prune actions) |
| BGP (EOS + NXOS) | Neighbors (incl. NXOS split stanzas), VLAN EVPN blocks, VRF RD/RT → **L3 discovery** |
| Retain keep-list | Service `prune.retain` or `discovery_prune_retain` → `blocked_by_retain` |

**Port logic:** Empty **Ports** on `show vlan id` means no L2 attachment. Remaining ports are classified from interface descriptions plus access-vs-trunk:

- **Compute** — servers, IBM Z, Nutanix, UCS, HCI (including compute trunks such as z14 OSA)
- **Switch uplink** — links to other switches (prune candidates)
- **Peer-link** — EOS MLAG peer (`switchport trunk group mlagpeer`) and NXOS vPC peer-link (exempt)

Discovery reports MACs learned on compute / unknown links only. Prune CLI is a **separate** report: EOS `configure session` + `commit timer` + `configure confirm`; NXOS checkpoint + rollback. Nothing is applied.

**Human prune plan:** only prune-eligible switches (VLAN present, no local endpoints, not protected) get trunk / SVI / VLAN CLI. Participating leaves, gateway leaves, and source gateways are withheld. Source gateways stay until the gateway move finishes. Default-VRF statics and BGP, and objects in a **shared VRF**, are L3 review only — never prune candidates. Applying that CLI is `playbooks/core/prune_vlan.yml`: dry-run unless `-e prune_apply=true -e prune_approval=approve`. See [examples/prune-vlan.md](examples/prune-vlan.md).

Reports land under:

```text
reports/<dc>/<vlan_slug>/<vlan_slug>_discovery_<timestamp>.md    # short operator report
reports/<dc>/<vlan_slug>/<vlan_slug>_discovery_<timestamp>.yml   # deployable VLAN model
reports/<dc>/<vlan_slug>/<vlan_slug>_discovery_<timestamp>.json  # LLM analysis payload
reports/<dc>/<vlan_slug>/<vlan_slug>_discovery_<timestamp>.csv
reports/<dc>/<vlan_slug>/<vlan_slug>_prune_<timestamp>.{md,json}
reports/<dc>/_prune_plans/<hostname>_prune_plan_<timestamp>.json
```

### Service bundles (optional)

Multi-VLAN / multi-VRF tenant cutovers can declare a service under
`vars/services/<dc>/<service_id>.yml`. See [`vars/services/README.md`](../vars/services/README.md).

Pass `-e manual_service_id=<id>` on discovery to seed VLAN IDs and apply `prune.retain`.

---

## VLAN database schema (per record)

| Field | Required | Notes |
|---|---|---|
| `id` | yes | 802.1Q VLAN ID |
| `name` | yes | Human label; used in report filenames |
| `action` | yes | `migrate` |
| `service_type` | yes | `l2` or `l3` - see [VXLAN_SERVICE_TYPES.md](VXLAN_SERVICE_TYPES.md) |
| `gateway_leafs` | L3 | Fabric leaves that host the gateway. Engineer-supplied; discovery does not infer them |
| `protected_vlan` | no | When true, no switch is prune-eligible |
| `vni` | yes for migrate | VXLAN network identifier (not inferred by discovery) |
| `vrf` | yes | Target VRF (discovery fills from SVI when present) |
| `target_switches` | yes | Inventory hostnames for CVP config push |
| `discovery_switches` | no | Switches to query; defaults to `target_switches` |
| `vlan_name` | no | On-box / post-migration VLAN name |
| `gateway` | no | Anycast / HSRP / VR address discovered on the SVI |
| `prefixes` | no | Subnets derived from SVI CIDRs |
| `mcast_group` | no | BUM group override |

---

## Typical operator journey (Core)

1. **Discover** an unknown VLAN → share the compute MAC / endpoint view → review the **prune report** (EOS session + timer) → review **L3** before removing the SVI.
2. **Copy the snippet** to `vars/vlans/<dc>/{vid}_{slug}.yml` and complete the VLAN record (VNI is required; confirm VRF, `target_switches`, `service_type`).
3. **Generate overlay config** from that SSOT (`workflow_deploy.yml` with `cvp_apply_configlets=false`), then push when ready.
4. **Approve and execute** the change control in CloudVision.
5. *(Optional)* **Verify** with `workflow_verify.yml` after the change is live.
6. *(Optional)* **Export to NetBox** with `export_vlan_to_netbox.yml` when inventory should reflect the migrated VLAN.

Detailed flags and examples: **[USAGE_GUIDE.md](USAGE_GUIDE.md)**.
