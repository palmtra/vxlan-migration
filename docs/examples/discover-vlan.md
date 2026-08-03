# Discover a VLAN (read-only)

Run targeted discovery for one VLAN in a data center before adding it to the local SSOT.

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

### Report files on disk

```text
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.md
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.json
```

Open the `.md` file — it should contain per-switch CLI output, not empty sections.

### Common failure signatures

| Symptom | Cause | Fix |
|---|---|---|
| Only a callback plugin error, no PLAY output | Outdated `stdout_callback` in `ansible.cfg` | Use repo `ansible.cfg` (fixed to `ansible.builtin.default`) |
| `_effective_vlans is not set on localhost` | Old playbook + `--limit` excluding bootstrap | Update `discover_vlan.yml` or pull latest |
| `paramiko is not installed` | Missing Python deps | `pip install -r requirements.txt` |
| No password prompt, immediate SSH/auth failure | No credentials configured | Create `vault.yml` or use `--ask-pass` |
| `UNREACHABLE` / timeout | Wrong IP or network path | Fix `ansible_host` in inventory |
| `skipping: no hosts matched` on first play | Legacy playbook with `--limit dc_*` only | Update playbook (bootstrap now runs on first network host) |
| Play succeeds but empty reports | All hosts `meta: end_host` (not in `discovery_switches`) | Use `discovery_allow_probe=true` or add hosts to VLAN DB |

---

## Report location

```text
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.{md,csv,json,yml}
```

Use the `*_vlan_db_snippet_*.yml` file to bootstrap `vars/vlans/<dc>.yml`.

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) § Discovery for all flags.
