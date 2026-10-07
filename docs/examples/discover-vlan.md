# Discover a VLAN (read-only)

Run targeted discovery for one VLAN in a data center before adding it to the local SSOT.

On **`main`**, discovery is first-class for **NXOS** and **EOS**. IOS hosts are skipped unless
`-e discovery_enable_ios=true`. Platform policy: [OS_SUPPORT.md](../OS_SUPPORT.md).

## Before you run — preflight

Discovery talks to network devices over SSH. **Ansible does not prompt for a password unless you ask it to.**

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

Requires at least one of: `paramiko` or `ansible-pylibssh` (both are listed in `requirements.txt`).

### 2. Provide device credentials

**Option A — vault file (recommended)**

```bash
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
# Edit with real ansible_user / ansible_password, then optionally encrypt:
ansible-vault encrypt inventory/group_vars/all/vault.yml
```

Run with vault:

```bash
ansible-playbook ... --ask-vault-pass
# or: --vault-password-file ~/.vault_pass
```

**Option B — prompt for SSH password**

```bash
ansible-playbook ... --ask-pass -e ansible_user=admin
```

**Option C — extra vars (ad hoc only)**

```bash
ansible-playbook ... -e ansible_user=admin -e ansible_password='secret'
```

### 3. Confirm inventory targets are reachable

Update `inventory/hosts.yml` so `ansible_host` values point at real devices (the sample `10.1.0.x` addresses are placeholders).

Preflight one switch:

```bash
ansible eos-leaf-lis-01 -i inventory/hosts.yml -m ansible.netcommon.cli_command \
  -a "command='show version'" --ask-pass -e ansible_user=admin
```

Expect `ok` with version output — not `FAILED` / `paramiko is not installed` / timeout.

### 4. Collections installed

```bash
ansible-galaxy collection install -r collections/requirements.yml -p collections/
```

---

## Probe an unknown VLAN

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=2911 \
  -e discovery_allow_probe=true \
  --limit dc_lisle
```

## Discover multiple VLANs

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e 'target_vlan_ids=[100,200]' \
  --limit dc_lisle
```

Comma-separated form:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e target_vlan_ids=100,200 \
  --limit dc_lisle
```

## Discover via a service bundle (multi-VLAN + prune retain)

Multi-VLAN / multi-VRF cutovers can use a service file under `vars/services/<dc>/`.
Copy `vars/services/lisle/_example.yml` to a real id (without `_meta.example`) first.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_service_id=aspira_0000822 \
  --limit dc_lisle
```

Effects:

- Seeds `target_vlan_ids` from the service `vlans:` list (when you did not pass VLAN ids).
- Applies `prune.retain` when tagging L3 review objects (`blocked_by_retain`).
- Still **read-only** — no deletes are applied. A human uses the prune report.

Without a service file you can pass retain inline:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=890 \
  -e '{"discovery_prune_retain":{"vrfs":["v0000822a"],"bgp_neighbors":[{"vrf":"v0000822a","neighbor":"169.254.255.29"}]}}' \
  --limit dc_lisle
```

## Discover a VLAN already in the DB

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle
```

With vault:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  --ask-vault-pass \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle
```

---

## How to know it worked

### Successful PLAY RECAP

Look for:

- `failed=0` on targeted switches (not `localhost` only)
- Tasks such as `EOS — show vlan id 100` → **ok** (not `unreachable` / `failed`)
- Final task: **Display VLAN report output paths** → lists files under `reports/`

Example:

```text
PLAY RECAP
eos-leaf-lis-01   : ok=25  failed=0  unreachable=0
...
TASK [Display VLAN report output paths]
ok: [eos-leaf-lis-01 -> localhost] => {
    "msg": "Wrote VLAN discovery reports under reports/lisle/legacy_web/ ..."
}
```

### Ad-hoc commands do not create reports

`ansible ... -m ansible.netcommon.cli_command -a "command='show version'"` only tests connectivity.
The `reports/` tree is created by **`playbooks/core/discover_vlan.yml`** when it reaches the
report-writing tasks at the end of `roles/vlan_discovery` — and only if the playbook gets
that far without failing on earlier hosts.

### Where reports land

```text
<repo-root>/reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.md
<repo-root>/reports/<dc>/<vlan_name>/<vlan_name>_prune_<timestamp>.md
<repo-root>/reports/<dc>/_prune_plans/<hostname>_prune_plan_<timestamp>.json
```

Two reports:

1. **Discovery** — MACs learned on compute links (servers, IBM Z, Nutanix, UCS, HCI), plus those endpoint configs. Switch-to-switch uplinks are listed for context. No prune CLI.
2. **Prune** — review `*_prune_*.md`, then apply with [prune-vlan.md](prune-vlan.md) if the switch is prune-eligible. Dry-run is the default. Compute endpoints, gateway leaves, and source gateways are not pruned.

SSOT snippet → fill VNI → generate VXLAN config (`workflow_deploy.yml`, `cvp_apply_configlets=false` by default).

Device-level JSON plans under `_prune_plans/` are candidates only (`destructive: false`, `apply_automated: false`).

The directory is **auto-created** (`ansible.builtin.file` with `state: directory`) during discovery.
It is listed in `.gitignore`, so it will not appear in `git status` even when present.

### Common failure signatures

| Symptom | Cause | Fix |
|---|---|---|
| Only a callback plugin error, no PLAY output | Outdated `stdout_callback` in `ansible.cfg` | Use repo `ansible.cfg` (fixed to `ansible.builtin.default`) |
| `A data center must be provided` right after vlan_db starts | Empty `target_data_center` passed via role vars; Ansible `default()` ignores `""` | Pull latest (uses `coalesce_trimmed` + single bootstrap block) |
| `'dict object' has no attribute 'vni'` during probe | SSOT task ran for synthetic probe record | Pull latest (probe mode skips SSOT) |
| `build_vlan_discovery_reports expects a list` | `_effective_vlans` not on localhost | Pull latest (bootstrap + report fallback) |
| `paramiko is not installed` | Missing Python deps | `pip install -r requirements.txt` |
| No password prompt, immediate SSH/auth failure | No credentials configured | Create `vault.yml` or use `--ask-pass` |
| `UNREACHABLE` / timeout | Wrong IP or network path | Fix `ansible_host` in inventory |
| `skipping: no hosts matched` on first play | Legacy playbook with `--limit dc_*` only | Update playbook (bootstrap now runs on first network host) |
| Play succeeds but empty reports | All hosts `meta: end_host` (not in `discovery_switches`) | Use `discovery_allow_probe=true` or add hosts to VLAN DB |

---

## Report location

```text
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.md    # short report
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.yml   # deployable model
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.json  # LLM analysis payload
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.csv
reports/<dc>/<vlan_name>/<vlan_name>_prune_<timestamp>.{md,json}
```

Use the `{vid}_{slug}.yml` snippet written under the report directory to bootstrap
`vars/vlans/<dc>/` (see `vars/vlans/README.md` and `vars/vlans/<dc>/_example.yml`).

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) § Discovery for all flags.
