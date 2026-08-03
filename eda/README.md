# Event-Driven Ansible: VLAN → VXLAN migration

This directory contains the EDA rulebook that wires the ServiceNow-triggered
VLAN → VXLAN migration workflow (`docs/WORKFLOW_V2.md`) together as two
event-driven stages instead of one long-running, polling playbook:

1. **`vlan_to_vxlan_migration.yml` / ruleset "ServiceNow intake"** — listens
   for a webhook from ServiceNow and launches
   `playbooks/advanced/workflow_deploy.yml` (Steps 1-5: intake,
   discovery, cleanup hand-off, NetBox validation, CVP configlet push). That
   playbook stops after creating a **pending** CVP change control and
   persists the workflow's context to `reports/workflow_state/vlan_<id>.yml`.
2. **`vlan_to_vxlan_migration.yml` / ruleset "CVP change control approval
   resume"** — listens for a second webhook (fired when the change control
   is approved/executed in CVP) and launches
   `playbooks/advanced/workflow_verify.yml` (Step 6: post-change
   verification, with rollback + re-verification on failure), passing only
   the VLAN ID. That playbook loads the rest of the context back from the
   persisted state file.

This means the automation never blocks or polls waiting for a human to
approve the change control — the deploy stage returns immediately, and the
verify stage only runs once the approval event actually arrives.

## Prerequisites

`ansible-rulebook` needs a JVM. Install:

```bash
sudo apt-get install default-jre-headless   # or any JRE/JDK >= 11
uv tool install ansible-rulebook            # or: pip install ansible-rulebook
ansible-galaxy collection install ansible.eda
```

## Running

```bash
ANSIBLE_COLLECTIONS_PATH=./collections ansible-rulebook \
  --rulebook eda/rulebooks/vlan_to_vxlan_migration.yml \
  --inventory inventory \
  --vars eda/extra_vars.yml   # optional: credentials not carried by the webhook payload
```

Notes on the flags above, both required for this repo's layout specifically:
- `ANSIBLE_COLLECTIONS_PATH=./collections`: `ansible-rulebook` doesn't read
  this project's `ansible.cfg`, so it needs the collections path (for
  `ansible.eda.webhook`) set explicitly.
- `--inventory inventory` (the **directory**, not `inventory/hosts.yml`):
  `ansible-rulebook`'s `run_playbook` action copies whatever inventory path
  you give it into an isolated temp dir before invoking `ansible-playbook`.
  If you point it at the `hosts.yml` **file**, only that file is copied and
  the sibling `inventory/group_vars/` is silently dropped (losing
  `ansible_connection: network_cli` and everything else). Pointing it at the
  `inventory/` **directory** copies the whole thing, group_vars included.

This starts two webhook listeners:

| Purpose | Port | Endpoint | Payload |
|---|---|---|---|
| ServiceNow intake | 5000 | `/vlan_migration` | `{"snow_vlan_id": 100, "snow_data_center": "lisle", "snow_target_vrf": "default", "snow_ticket_sys_id": "<sys_id>"}` |
| CVP change control approval | 5001 | `/cvp_approval` | `{"vlan_id": 100, "change_control_status": "approved"}` |

In ServiceNow, configure the change-ticket workflow to POST to the first
webhook on ticket creation/approval. The second webhook is meant to be
called by whatever is watching CVP change control state — either a native
CVP webhook/notification integration if available, or a small scheduled job
(e.g. an AAP job template on a schedule, or a second EDA rulebook using an
`ansible.eda.range`/interval-based source) that polls
`arista.cvp.cv_change_control_v3` for the change controls this workflow
created and POSTs to `/cvp_approval` once one flips to `approved`/`executed`.
That polling piece is intentionally not included here since it depends on
how change control approvals are tracked in your CVP/CVaaS deployment.

## Why eda_deploy_entrypoint.yml / eda_verify_entrypoint.yml exist

`run_playbook`'s directory-copy behavior described above applies to the
playbook itself too, not just the inventory: it copies the **parent
directory** of whatever playbook `name:` you give it. If the rulebook
referenced a playbook under `playbooks/` directly, only that directory
would get copied — `roles/`, `vars/`, `plugins/`, `ansible.cfg`, etc. would
be missing, and role resolution would fail. `eda_deploy_entrypoint.yml` and
`eda_verify_entrypoint.yml` are one-line `import_playbook` shims that live
at the **repo root** for exactly this reason: their parent directory is the
whole repo, so the copy picks up everything the real playbooks need. Under
AAP (see below) this doesn't matter, since AAP syncs a full project instead.

## Live-tested status

This rulebook and both entrypoint shims have been run live end-to-end in
this workspace (`ansible-rulebook`/`ansible.eda` installed locally, no AAP)
against curl-simulated webhooks, with all four rules exercised: valid
ServiceNow intake (successfully launched the deploy stage, which correctly
ran through role/group_vars resolution and failed cleanly at device
connectivity since there are no real switches here), an incomplete intake
payload (correctly rejected without launching anything), a non-approved CVP
event (correctly logged without resuming), and an approved CVP event
(successfully launched the verify stage, which correctly attempted to load
persisted state and failed cleanly since no prior deploy run had reached the
point of persisting state). Fixed along the way: `roles/servicenow_input`
assumed `snow_vlan_id` would always be string-typed (webhook JSON delivers
it as a native int); the rulebook's ruleset-level `hosts:` was `localhost`,
which `run_playbook` uses to derive `--limit`, incorrectly restricting the
launched sub-playbooks to `localhost` only (changed to `all`); and
`inventory/hosts.yml` had no explicit `localhost` entry, so Ansible's
*implicit* localhost did not reliably survive `--limit all` for the
`hosts: localhost` plays (added an explicit entry).

## Running under AAP / Controller

In an AAP Event-Driven Ansible controller, replace the `run_playbook` action
in `vlan_to_vxlan_migration.yml` with `run_job_template`, referencing Job
Templates for `playbooks/advanced/workflow_deploy.yml` and
`playbooks/advanced/workflow_verify.yml`, and map
`event.payload.*` fields to the Job Template's survey/extra vars the same
way this rulebook maps them to `extra_vars` here. Store `netbox_url`,
`cvp_url`, `snow_url`, and their credentials in AAP Credentials rather than
in `eda/extra_vars.yml`.

## Local testing without a live CVP/ServiceNow

You can exercise both playbook stages directly, without EDA:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/advanced/workflow_deploy.yml \
  -e manual_vlan_id=100 -e manual_data_center=lisle -e manual_target_vrf=default

ansible-playbook -i inventory/hosts.yml playbooks/advanced/workflow_verify.yml \
  -e resume_vlan_id=100
```
