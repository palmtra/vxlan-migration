# Classic VLAN to VXLAN EVPN Migration

Ansible automation to **migrate legacy VLANs to VXLAN/EVPN** on Arista EOS via CloudVision (CVP).

- **Core workflow:** discover → local VLAN SSOT → AVD/Jinja config → CVP (pending change control). No ServiceNow.
- **Advanced workflow:** Core + ServiceNow intake and ticket closure.
- **Discovery** is read-only across EOS, NXOS, and IOS; **deploy** targets Arista EOS only.
- **Cleanup / trunk pruning** is reported in discovery and handled by `decomm-vlan`, not this repo.
- **NetBox export** is a standalone utility, not part of the migration run.

## Documentation

**Index:** [docs/README.md](docs/README.md) — architecture, usage guides, and runnable examples.

| Doc | Purpose |
|---|---|
| [docs/WORKFLOWS.md](docs/WORKFLOWS.md) | Core vs Advanced architecture (mermaid diagrams) |
| [docs/USAGE_GUIDE.md](docs/USAGE_GUIDE.md) | Flags, options, and CLI reference |
| [docs/examples/](docs/examples/) | Short task-focused examples (discover, generate-only CVP, NetBox export) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Open features |
| [docs/AGENTS.md](docs/AGENTS.md) | Instructions for maintaining this documentation tree |

## Quick start (Core)

```bash
# 1. Discover a VLAN (reports under reports/<dc>/<vlan_name>/)
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle -e manual_vlan_id=100 --limit dc_lisle

# 2. Add/update vars/vlans/lisle/0100_legacy_web.yml from the report snippet, then deploy
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e cvp_apply_configlets=false \
  --limit dc_lisle

# 3. After approving the CVP change control — optional verify
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=100 --limit dc_lisle
```

Per-DC VLAN databases: `vars/vlans/<data_center>/*.yml` — one file per VLAN (see `vars/vlans/README.md`).

Generate config without CVP push: [docs/examples/generate-config-without-cvp-push.md](docs/examples/generate-config-without-cvp-push.md).

## Standalone utilities

```bash
# Export VLAN attributes to NetBox (dry-run by default)
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle -e manual_vlan_id=100
```

## Repository layout

```
playbooks/core/          # Discovery, deploy, verify (no ServiceNow)
playbooks/advanced/      # Core wrappers + ServiceNow
vars/vlans/              # Per-DC VLAN DB — one YAML file per VLAN (primary SSOT)
roles/vlan_discovery/    # Read-only discovery + VLAN-centric reports
roles/netbox_export/     # Standalone NetBox sync (not in workflow)
docs/WORKFLOWS.md        # Architecture
docs/USAGE_GUIDE.md      # Operator reference
```

See the full README sections below for install steps, data model, and AAP integration.

---

## Install

```bash
ansible-galaxy collection install -r collections/requirements.yml -p collections/
```

Provide credentials via `inventory/group_vars/all/vault.yml` or AAP credentials. See [docs/USAGE_GUIDE.md](docs/USAGE_GUIDE.md).

## Data model

Each data center has its own VLAN database directory under `vars/vlans/<dc>/` (one file per VLAN).
Filename: `{vid:04d}_{slug}.yml` — see `vars/vlans/README.md`.

```yaml
# vars/vlans/lisle/0100_legacy_web.yml
id: 100
name: legacy_web
action: migrate
service_type: l3          # l2 | l3 | l2_l3
vni: 50100
vrf: default
target_switches:          # CVP configlet targets
  - eos-leaf-lis-01
  - eos-leaf-lis-02
discovery_switches:       # optional; defaults to target_switches
  - eos-leaf-lis-01
  - nxos-spine-lis-01
```

## Safety controls

- `--check` / `--diff` supported end-to-end.
- `cvp_apply_configlets=false` by default (generate files only).
- CVP change controls are **pending** by default; manual approval in CloudVision.
- Discovery and trunk analysis are **read-only** — no device or trunk config changes.

## AAP integration

- Job Templates: see [docs/USAGE_GUIDE.md § AAP](docs/USAGE_GUIDE.md#7-aap-job-template-mapping).
- Inventory groups: `dc_lisle`, `dc_omaha`.
- Separate Job Templates for Core vs Advanced tiers.
