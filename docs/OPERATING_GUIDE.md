# Operating Guide — cvg-vxlan

> **This guide has been superseded by:**
>
> - **[WORKFLOWS.md](WORKFLOWS.md)** — Core vs Advanced architecture and mermaid diagrams
> - **[USAGE_GUIDE.md](USAGE_GUIDE.md)** — Detailed flags, options, and CLI examples
> - **[ROADMAP.md](ROADMAP.md)** — Planned work

## Quick pointers

| Task | Playbook |
|---|---|
| Discover a VLAN | `playbooks/core/discover_vlan.yml` |
| Migrate (config → CVP) | `playbooks/core/workflow_deploy.yml` |
| Verify after CVP approval | `playbooks/core/workflow_verify.yml` |
| ServiceNow-driven deploy | `playbooks/advanced/workflow_deploy.yml` |
| Export VLAN to NetBox | `playbooks/export_vlan_to_netbox.yml` |

Primary SSOT: `vars/vlans/<data_center>.yml` (not `vars/vlan_registry.yml`).

Discovery is read-only. Trunk cleanup is reported as recommendations only; execute via `cvg-decomm-vlan`.

For install, credentials, and safety controls, see [USAGE_GUIDE.md](USAGE_GUIDE.md).
