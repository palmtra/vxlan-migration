# Generate CVP config without pushing

Review VXLAN migration configlets locally before any CloudVision API call.

## When to use this

- First migration for a VLAN — validate generated EOS snippets against design standards.
- Lab or change-window prep — produce artifacts for peer review without creating a CVP change control.
- CI / preflight — confirm AVD or Jinja output renders without CVP credentials.

## How it works

`cvp_apply_configlets` defaults to **`false`** in `roles/cvp_deploy` and `roles/avd_vxlan_config`.

| `cvp_apply_configlets` | Behaviour |
|---|---|
| `false` (default) | Render config to local files; **no** CVP upload, device assignment, or change control |
| `true` | Upload configlets to CVP and create a pending change control |

CVP credentials (`cvp_url`, `cvp_token`) are **not required** for generate-only runs.

---

## Prerequisites

1. VLAN record exists in the local SSOT: `vars/vlans/<data_center>.yml`
2. Required fields populated: `id`, `name`, `action`, `service_type`, `vni`, `vrf`, `target_switches`
3. Collections installed: `ansible-galaxy collection install -r collections/requirements.yml -p collections/`

Example VLAN record (`vars/vlans/lisle.yml`):

```yaml
vlans:
  - id: 100
    name: legacy_web
    action: migrate
    service_type: l3
    vlan_name: WEB_VXLAN
    vni: 50100
    vrf: default
    target_switches:
      - eos-leaf-lis-01
      - eos-leaf-lis-02
```

---

## Option A — full Core deploy path (generate only)

Runs intake, optional discovery, NetBox drift check, then config generation. Skips CVP push.

### Legacy Jinja configlets

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e cvp_apply_configlets=false \
  --limit dc_lisle
```

**Output directory:** `reports/cvp_configlets/`

| File | Description |
|---|---|
| `vxlan_vlan100_<hostname>.cfg` | Per-leaf VXLAN configlet |
| `vxlan_vlan100_borderleaf_import.cfg` | Border-leaf VRF import (when `cvp_build_borderleaf_config=true`) |

### Arista AVD configlets

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=false \
  --limit dc_lisle
```

**Output directories:**

| Path | Description |
|---|---|
| `cvp_configlets/avd/structured_configs/<hostname>.yml` | AVD structured input per device |
| `cvp_configlets/avd/configs/<hostname>.cfg` | Rendered EOS CLI |

---

## Option B — standalone deploy playbooks (no discovery)

Use when the VLAN DB is complete and you only need config generation.

### Legacy Jinja

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy_to_cvp.yml \
  -e target_vlan_id=100 \
  -e target_data_center=lisle \
  -e target_vrf=default \
  -e cvp_apply_configlets=false \
  --limit dc_lisle
```

### AVD

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy_to_cvp_avd.yml \
  -e target_vlan_id=100 \
  -e target_data_center=lisle \
  -e target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=false \
  --limit dc_lisle
```

---

## Review checklist

Before setting `cvp_apply_configlets=true`:

- [ ] VNI and VRF match the fabric design
- [ ] `target_switches` list is correct for this VLAN
- [ ] Generated snippet includes expected VXLAN/VNI/VRF bindings
- [ ] Border-leaf import section present when L3/VRF migration requires it
- [ ] No unintended diffs on unrelated sections

---

## Push to CVP (after review)

Re-run the same command with push enabled:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=100 \
  -e manual_data_center=lisle \
  -e manual_target_vrf=default \
  -e use_avd=true \
  -e cvp_apply_configlets=true \
  --limit dc_lisle
```

Requires `cvp_url` and `cvp_token` in vault or AAP credentials.

This creates a **pending** CVP change control (`cvp_change_control_state: set`). Approve and execute manually in CloudVision.

Optional verify after approval:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=100 \
  --limit dc_lisle
```

---

## Related variables

| Variable | Default | Notes |
|---|---|---|
| `cvp_apply_configlets` | `false` | Master switch for CVP API calls |
| `use_avd` | `false` | `true` → AVD path; `false` → Jinja templates |
| `cvp_build_borderleaf_config` | `true` | Border-leaf import configlet (Jinja path) |
| `cvp_configlet_build_dir` | `reports/cvp_configlets` | Jinja output directory |
| `avd_output_dir` | `cvp_configlets/avd` | AVD output directory |
| `cvp_change_control_state` | `set` | Never auto-execute without explicit override |

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) for the full variable reference.

---

## See also

- [WORKFLOWS.md](../WORKFLOWS.md) — Core deploy flow and where CVP fits
- [discover-vlan.md](discover-vlan.md) — discover before adding a VLAN to SSOT
