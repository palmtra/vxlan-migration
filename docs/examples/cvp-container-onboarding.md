# Place switches in CloudVision containers (Undefined / Unassigned)

The deploy adds configlets to each target switch and keeps it in its current
container. CloudVision cannot apply a configlet to a switch that is streaming
but sits in **Undefined** (some CVaaS views show **Unassigned**), so preflight
marks it `fail` and the deploy stops before it pushes anything:

```text
in CloudVision but not in a container (Undefined / Unassigned): move it in Network Provisioning
```

This is a one-time CloudVision change, done by a person. The playbooks never
move a switch between containers.

This page is for `cvp_deploy_method: configlets` (the default). With
`cvp_deploy_method: studios` no container is needed; see
[cvp-studios-onboarding.md](cvp-studios-onboarding.md) instead.

## Before you start

1. **Check who owns the switch's config.** If the switch is managed by
   Studios (Static Configuration Studio, or AVD through Studios), do not move
   it into a Network Provisioning container: that would give it two config
   sources. Settle that first. The configlet deploy in this repo assumes
   Network Provisioning.
2. **Use the same tree for every site.** Copy the container layout the
   working DC already uses (for example `Tenant > <DC> > Border-Leaf / Leaf`)
   rather than building a different one for Omaha.
3. **Leave the parent containers empty.** Put configlets on devices, not on
   the parent containers, unless every switch below must get that exact
   config. A configlet on a container goes to each switch moved into it.
4. **Make sure the hostnames match.** The CloudVision hostname (or the first
   label of its FQDN) must match the inventory name in `inventory/hosts.yml`.

## Move and reconcile, one MLAG pair at a time

1. Provisioning > Network Provisioning: select the switch under Undefined,
   then **Move** it to its DC container.
2. Before you save, open **Manage > Configlets** on the switch, then
   **Validate**. Every line of the running config that is not in a configlet
   shows as a difference.
3. **Reconcile**: CloudVision writes those lines into a per-device reconcile
   configlet, so the configuration the switch should have (designed config)
   matches what it runs now. Validate again and confirm there are **no
   differences**.
4. **Save**. CloudVision creates one task per switch.
5. Put the pair's tasks in one change control. Open the review and check that
   the diff is empty (or contains only lines you expect, such as the
   `daemon TerminAttr` block). **If the diff removes any config, do not
   run it.** Go back and reconcile the switch.
6. Approve and run the change control in a maintenance window, even when the
   diff is empty. Then do the second switch of the pair.

## Check

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/preflight.yml \
  -e manual_vlan_id=<vlan> -e manual_data_center=omaha --limit dc_omaha
```

Each target switch should report `ok` and `container <name>`. When a switch
later drifts from its designed config (someone changes it on the CLI),
reconcile it again before the next migration: otherwise the migration's
change control also reverts that change.

Back to [examples](README.md).
