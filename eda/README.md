# Event-Driven Ansible: VLAN → VXLAN migration

This directory contains the EDA rulebook that wires the ServiceNow-triggered
VLAN → VXLAN migration workflow (`docs/WORKFLOW_V2.md`) together as two
event-driven stages instead of one long-running, polling playbook:

1. **`vlan_to_vxlan_migration.yml` / ruleset "ServiceNow intake"** — listens
   for a webhook from ServiceNow and launches
   `playbooks/workflow_vlan_to_vxlan_deploy.yml` (Steps 1-5: intake,
   discovery, cleanup hand-off, NetBox validation, CVP configlet push). That
   playbook stops after creating a **pending** CVP change control and
   persists the workflow's context to `reports/workflow_state/vlan_<id>.yml`.
2. **`vlan_to_vxlan_migration.yml` / ruleset "CVP change control approval
   resume"** — listens for a second webhook (fired when the change control
   is approved/executed in CVP) and launches
   `playbooks/workflow_vlan_to_vxlan_verify.yml` (Step 6: post-change
   verification, with rollback + re-verification on failure), passing only
   the VLAN ID. That playbook loads the rest of the context back from the
   persisted state file.

This means the automation never blocks or polls waiting for a human to
approve the change control — the deploy stage returns immediately, and the
verify stage only runs once the approval event actually arrives.

## Prerequisites

This repo does not vendor `ansible-rulebook` or the `ansible.eda` collection
(neither is installed in this workspace). To run the rulebook for real:

```bash
pip install ansible-rulebook
ansible-galaxy collection install ansible.eda
```

## Running

```bash
ansible-rulebook \
  --rulebook eda/rulebooks/vlan_to_vxlan_migration.yml \
  --inventory inventory/hosts.yml \
  --vars eda/extra_vars.yml   # optional: credentials not carried by the webhook payload
```

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

## Running under AAP / Controller

In an AAP Event-Driven Ansible controller, replace the `run_playbook` action
in `vlan_to_vxlan_migration.yml` with `run_job_template`, referencing Job
Templates for `playbooks/workflow_vlan_to_vxlan_deploy.yml` and
`playbooks/workflow_vlan_to_vxlan_verify.yml`, and map
`event.payload.*` fields to the Job Template's survey/extra vars the same
way this rulebook maps them to `extra_vars` here. Store `netbox_url`,
`cvp_url`, `snow_url`, and their credentials in AAP Credentials rather than
in `eda/extra_vars.yml`.

## Local testing without a live CVP/ServiceNow

You can exercise both playbook stages directly, without EDA, exactly as
`playbooks/workflow_vlan_to_vxlan.yml` already does for CLI testing:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/workflow_vlan_to_vxlan_deploy.yml \
  -e manual_vlan_id=100 -e manual_data_center=lisle -e manual_target_vrf=default

ansible-playbook -i inventory/hosts.yml playbooks/workflow_vlan_to_vxlan_verify.yml \
  -e resume_vlan_id=100
```
