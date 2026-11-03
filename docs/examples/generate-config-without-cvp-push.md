# Generate CVP config without pushing

Review VXLAN migration configlets locally before any CloudVision API call.

## When to use this

- After discovery of a VLAN (report section 4) — generate overlay config from the SSOT snippet once `vni` is assigned.
- First migration for a VLAN — validate generated EOS snippets against design standards.
- Lab or change-window prep — produce artifacts for peer review without creating a CVP change control.
- CI / preflight — confirm AVD or Jinja output renders without CVP credentials.

## How it works

`cvp_apply_configlets` defaults to **`false`** in `roles/cvp_deploy` and `roles/avd_vxlan_config`.

| `cvp_apply_configlets` | Behaviour |
|---|---|
| `false` (default) | Render config to local files; **no** CVP upload, device assignment, or change control |
| `true` | Upload configlets to CloudVision, add them to their switches, create a change control for the resulting tasks, and wait for it to run |

CloudVision credentials (`vault_cvp_token`) are **not required** for generate-only runs.

---

## Discover once, generate as often as you like

The deploy re-runs discovery by default, to read the switches as they are right before the change. To iterate on the config without that wait, discover once and generate from the VLAN record:

1. Discover:

   ```bash
   ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
     -e manual_vlan_id=3000 -e manual_data_center=omaha --limit dc_omaha
   ```

2. Review the record discovery wrote, `reports/<dc>/<vlan_name>/<vid>_<VLAN_NAME>.yml` (the deployable model): VNI, VRF, gateway, prefixes, and under `placement` each leaf's `asn`, `router_id` and `rd`. Copy it to `vars/vlans/<dc>/` (the file says where).
3. Generate, with no switch contacted:

   ```bash
   ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
     -e manual_vlan_id=3000 -e manual_data_center=omaha -e manual_target_vrf=<vrf> \
     -e cvp_apply_configlets=false -e deploy_discovery=false --limit dc_omaha
   ```

   The configlets land in `reports/cvp_configlets/`. Edit the record and run step 3 again as often as needed; it takes seconds. The NetBox VNI / route-target lookup still runs when NetBox is configured.

4. Push when the config is right: the same command with `-e cvp_apply_configlets=true` (with or without `deploy_discovery=false`).

With `deploy_discovery=false`, each leaf's ASN and router ID come from its `placement` entry in the record, else from inventory `bgp_as` / `router_id`. A leaf with neither stops the run and says which. The configlets match what a run with discovery renders, as long as nothing changed on the switches in between. Re-run discovery (step 1) when it might have.

---

## Prerequisites

1. VLAN record exists in the local SSOT: `vars/vlans/<data_center>/{vid}_{slug}.yml`
2. Required fields populated: `id`, `name`, `action`, `service_type`, `vni`, `vrf`, `target_switches`
3. Collections installed: `ansible-galaxy collection install -r collections/requirements.yml -p collections/`

Example VLAN record (`vars/vlans/lisle/0100_legacy_web.yml`):

```yaml
id: 100
name: legacy_web
action: migrate
service_type: l3
vlan_name: WEB_VXLAN
vni: 50100
vrf: default
target_switches:
  - chi01-blf01
  - chi01-blf02
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
  -e cvp_apply_configlets=false \
  --limit dc_lisle
```

Reads the record from `vars/vlans/<dc>/` and renders only its `target_switches`; no switch login.
`target_vrf` defaults to the record's `vrf`. Per-leaf ASN and RD come from the discovery model, else
inventory `bgp_as` / `router_id`.

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

Requires `vault_cvp_token` in vault or AAP credentials, and the CloudVision host in `inventory/group_vars/CVAAS/`.

This creates a **pending** change control (`cvp_change_control_state: set`) and waits up to `cvp_change_control_wait_minutes` (240) for someone to approve and execute it in CloudVision. The run ends when it completes; a failed change control fails the run. If the wait times out, resume with `playbooks/core/cvp_change_control_wait.yml` (the run prints the command). `-e cvp_change_control_wait=false` returns as soon as the change control exists.

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
| `cvp_build_borderleaf_config` | `false` | Jinja border-leaf VRF import. Off unless the VRF itself must be created |
| `cvp_configlet_build_dir` | `reports/cvp_configlets` | Jinja output directory |
| `avd_output_dir` | `cvp_configlets/avd` | AVD output directory |
| `cvp_change_control_state` | `set` | Never auto-execute without explicit override |

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) for the full variable reference.

---

## See also

- [WORKFLOWS.md](../WORKFLOWS.md) — Core deploy flow and where CVP fits
- [discover-vlan.md](discover-vlan.md) — discover before adding a VLAN to SSOT
