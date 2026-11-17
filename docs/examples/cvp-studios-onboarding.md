# Onboard switches to Static Configuration Studio (Studios deploy)

`cvp_deploy_method: studios` deploys the AVD config with Arista's
`arista.avd.cv_deploy` role: a workspace in Static Configuration Studio,
submitted to create a change control that is left pending approval. Switches
do not need a Network Provisioning container (Undefined is fine).

## How the Studios deploy differs

`cv_deploy` keeps **one configlet per switch**, `VXLAN-MIGRATION_<leaf>`
(`cvp_studio_configlet_name_template`), and **replaces** it on every run. So
each run rebuilds the leaf's configlet from every VLAN record in
`vars/vlans/<dc>/` that places config on that leaf and has `deployed: true`,
plus the VLAN being migrated. A rollback rebuilds it without that VLAN.

- **Set `deployed: true` in the record and commit it** once a VLAN's change
  control has run. A migrated VLAN without it is **removed** from its leaves on
  the next Studios run for any other VLAN on those leaves.
- A deployed record must carry its L2 VNI on disk (`evpn.l2.vni`); the run
  stops otherwise rather than drop the VLAN.
- The run prints, per leaf, the VLANs its configlet will hold. Check it, and
  check the change control diff in CloudVision before approving.

## One-time onboarding, per switch

1. **Inventory & Topology Studio:** the switch is streaming and onboarded, and
   its hostname matches `inventory/hosts.yml` (cv_deploy finds switches by hostname).
2. **Static Configuration Studio:** the switch's full running config is owned by
   CloudVision (reconcile it into a per-device configlet). Without that, the
   designed config is only the migration configlet and the change control
   would remove everything else. cv_deploy puts its "AVD Configurations"
   container first, so the reconciled config is applied after it.
3. Submit the onboarding workspace and confirm its change control has an
   **empty diff** before running it. Do one MLAG pair at a time.
4. Do not re-reconcile a switch after a migration without checking the result:
   reconciling copies the migration lines into the reconcile configlet, and a
   later rollback would no longer remove them.

When every target switch is onboarded, set `cvp_studios_onboarded: true` in
`inventory/group_vars/CVAAS/` (the push refuses to run before that).

## Run

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_deploy.yml \
  -e manual_vlan_id=<vlan> -e manual_data_center=omaha \
  -e cvp_deploy_method=studios -e cvp_apply_configlets=false \
  --limit dc_omaha --ask-vault-pass
```

That renders `reports/cvp_configlets/omaha/vlan<id>/avd_studio/configs/<leaf>.cfg`
without touching CloudVision. Add `-e cvp_apply_configlets=true` to push. Set
`cvp_deploy_method: studios` in `inventory/group_vars/all/main.yml` to make it the default.

Rollback (`roles/cvp_rollback`, also the auto-rollback in `workflow_verify.yml`)
follows `cvp_deploy_method`: with `studios` it re-deploys each leaf without the VLAN.

Back to [examples](README.md).
