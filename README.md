# Classic VLAN to VXLAN EVPN Migration

Ansible automation to **migrate legacy VLANs to VXLAN/EVPN** on Arista EOS via CloudVision (CVP).

- **Core workflow:** discover → local VLAN SSOT → AVD/Jinja config → CVP (pending change control). No ServiceNow.
- **Advanced workflow:** Core + ServiceNow intake and ticket closure.
- **Platforms on `main`:** **NXOS** (legacy source discovery) + **EOS** (target deploy/verify). IOS discovery is parked — see [docs/OS_SUPPORT.md](docs/OS_SUPPORT.md).
- **Cleanup / trunk pruning** is reported as retain-aware `prune_plan` candidates (optional service `prune.retain`). Apply is not automated yet.
- **NetBox export** is a standalone utility, not part of the migration run.
- **Safety:** CVP generate-only by default; direct-device migrate path gated (`allow_direct_device_push`); auto-rollback off; secrets tasks use `no_log`.
- **CI:** `.github/workflows/ci.yml` runs yamllint, unit tests, Molecule (`vlan_db`/`service_db`), syntax-check, ansible-lint (`make ci`).

## Documentation

**Index:** [docs/README.md](docs/README.md) - architecture, usage guides, and runnable examples.

| Doc | Purpose |
|---|---|
| [docs/WORKFLOWS.md](docs/WORKFLOWS.md) | Core vs Advanced architecture (mermaid diagrams) |
| [docs/OS_SUPPORT.md](docs/OS_SUPPORT.md) | NXOS+EOS on main; IOS archive tag/branch |
| [docs/VXLAN_SERVICE_TYPES.md](docs/VXLAN_SERVICE_TYPES.md) | L2 vs L3 vs l2_l3 (`service_type`) |
| [docs/USAGE_GUIDE.md](docs/USAGE_GUIDE.md) | Flags, options, and CLI reference |
| [docs/examples/](docs/examples/) | Short task-focused examples (discover, generate-only CVP, NetBox export) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Open features |
| [docs/AGENTS.md](docs/AGENTS.md) | Instructions for maintaining this documentation tree |

## Install

```bash
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml -p collections/
```

## Credentials and vault

Device SSH credentials live in `inventory/group_vars/all/vault.yml` (see `vault.yml.example`).
When that file is encrypted, append **`--ask-vault-pass`** to every playbook that touches network devices
(or use `--vault-password-file ~/.vault_pass` in automation).

```bash
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
# Edit ansible_user / ansible_password, then optionally:
ansible-vault encrypt inventory/group_vars/all/vault.yml
```

CVP push (`cvp_apply_configlets=true`) also needs `cvp_url` and `cvp_token` in vault.
Generate-only runs (`cvp_apply_configlets=false`) do not call CVP.

Full credential options: [docs/examples/discover-vlan.md](docs/examples/discover-vlan.md) (preflight section).

## Quick start (Core)

Run playbooks from the **repository root** so `ansible.cfg` and `collections/` resolve correctly.

### 1. Discover a VLAN

Probe an unknown VLAN (not yet in the VLAN DB):

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=810 \
  -e discovery_allow_probe=true \
  --limit dc_lisle \
  --ask-vault-pass
```

Discover a VLAN already declared under `vars/vlans/lisle/`:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle \
  --ask-vault-pass
```

Reports are written under `reports/<dc>/<vlan_name>/` (auto-created, gitignored).
Copy the generated snippet into `vars/vlans/<dc>/{vid}_{slug}.yml` and fill in `vni`, `vrf`, and `target_switches`.

More discovery examples: [docs/examples/discover-vlan.md](docs/examples/discover-vlan.md)

### 2. Deploy (generate config only)

After the VLAN record exists in the local SSOT, generate configlets locally (no CVP API calls):

**Legacy Jinja:**

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=810 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e cvp_apply_configlets=false \
  --limit dc_lisle \
  --ask-vault-pass
```

**Arista AVD:**

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=810 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=false \
  --limit dc_lisle \
  --ask-vault-pass
```

Output: `reports/cvp_configlets/` (Jinja) or `reports/cvp_configlets/avd/` (AVD structured + configs).

More generate-only options: [docs/examples/generate-config-without-cvp-push.md](docs/examples/generate-config-without-cvp-push.md)

### 3. Deploy (push to CVP)

After reviewing generated files, push to CloudVision (creates a **pending** change control):

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=810 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=true \
  --limit dc_lisle \
  --ask-vault-pass
```

Approve and execute the change control in CloudVision manually.

### 4. Verify (optional, post-CVP)

Run after the change control has been executed on devices:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=810 \
  --limit dc_lisle \
  --ask-vault-pass
```

Per-DC VLAN databases: `vars/vlans/<data_center>/*.yml` - one file per VLAN (see `vars/vlans/README.md`).

## Standalone utilities

```bash
# Export VLAN attributes to NetBox (dry-run by default)
ansible-playbook -i inventory/hosts.yml playbooks/export_vlan_to_netbox.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --ask-vault-pass
```

NetBox export details: [docs/examples/export-vlan-to-netbox.md](docs/examples/export-vlan-to-netbox.md)

## Repository layout

```
playbooks/core/          # Discovery, deploy, verify (no ServiceNow)
playbooks/advanced/      # Core wrappers + ServiceNow
vars/vlans/              # Per-DC VLAN DB - one YAML file per VLAN (primary SSOT)
vars/services/           # Optional multi-VLAN service bundles + prune.retain
roles/vlan_discovery/    # Read-only discovery + VLAN-centric reports
roles/service_db/        # Load service bundles / prune.retain
roles/netbox_export/     # Standalone NetBox sync (not in workflow)
docs/WORKFLOWS.md        # Architecture
docs/USAGE_GUIDE.md      # Operator reference
```

## Data model

Each data center has its own VLAN database directory under `vars/vlans/<dc>/` (one file per VLAN).
Filename: `{vid:04d}_{slug}.yml` - see `vars/vlans/README.md`.

```yaml
# vars/vlans/lisle/0100_legacy_web.yml
id: 100
name: legacy_web
action: migrate
service_type: l3          # l2 | l3 | l2_l3 - see docs/VXLAN_SERVICE_TYPES.md
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
- Discovery and trunk analysis are **read-only** - no device or trunk config changes.
- On `main`, discovery runs for **NXOS + EOS** by default; IOS requires `discovery_enable_ios=true` ([OS_SUPPORT.md](docs/OS_SUPPORT.md)).

## AAP integration

- Job Templates: see [docs/USAGE_GUIDE.md § AAP](docs/USAGE_GUIDE.md#7-aap-job-template-mapping).
- Inventory groups: `dc_lisle`, `dc_omaha`.
- Separate Job Templates for Core vs Advanced tiers.
