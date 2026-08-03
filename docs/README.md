# vxlan-migration documentation

Documentation hub for the Classic VLAN → VXLAN EVPN migration automation.

Start here for architecture and operator guides. Use **Examples** for short, task-focused commands.

---

## Architecture

| Document | Description |
|---|---|
| [WORKFLOWS.md](WORKFLOWS.md) | **Primary reference** - Core vs Advanced tiers, mermaid diagrams, discovery scope, SSOT model |
| [VXLAN_SERVICE_TYPES.md](VXLAN_SERVICE_TYPES.md) | **L2 vs L3 vs l2_l3** - choosing `service_type` for migration |
| [WORKFLOW_V2.md](WORKFLOW_V2.md) | Historical target-state design (ServiceNow + EDA); some details superseded by Core/Advanced split |
| [ROADMAP.md](ROADMAP.md) | Completed work and planned features |

---

## Usage and administration

| Document | Description |
|---|---|
| [USAGE_GUIDE.md](USAGE_GUIDE.md) | **Operator reference** — all playbooks, extra vars, flags, AAP job templates, troubleshooting |
| [OPERATING_GUIDE.md](OPERATING_GUIDE.md) | Redirect stub — points to WORKFLOWS and USAGE_GUIDE |

---

## Examples

Short, runnable task guides in [examples/](examples/):

| Example | Description |
|---|---|
| [examples/discover-vlan.md](examples/discover-vlan.md) | Read-only VLAN discovery |
| [examples/generate-config-without-cvp-push.md](examples/generate-config-without-cvp-push.md) | Generate CVP configlets locally without push |
| [examples/export-vlan-to-netbox.md](examples/export-vlan-to-netbox.md) | Export local VLAN DB to NetBox |

Full examples index: [examples/README.md](examples/README.md)

---

## Repository entry points

| Path | Purpose |
|---|---|
| [../README.md](../README.md) | Project overview and quick start |
| [../playbooks/core/](../playbooks/core/) | Core workflow playbooks (no ServiceNow) |
| [../playbooks/advanced/](../playbooks/advanced/) | Advanced workflow (Core + ServiceNow) |
| [../vars/vlans/](../vars/vlans/) | Per-DC local VLAN database (primary SSOT) |

---

## For AI agents

See [AGENTS.md](AGENTS.md) for rules on maintaining this documentation tree.
