# Migrate a VLAN end to end

One command takes a VLAN from the legacy MLS network into the VXLAN fabric:

1. Check that it can reach the switches, CloudVision and NetBox.
2. Discover it.
3. Check that it is ready.
4. Read its VNI and route targets from NetBox.
5. Generate the configlets.
6. Push them to CloudVision with a change control and wait for it to run.
7. Verify the switches and compare every endpoint before and after.
8. Check what can be pruned on the legacy side.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/migrate_vlan.yml \
  -e manual_vlan_id=3000 \
  -e manual_data_center=omaha \
  --limit dc_omaha
```

Nothing is pruned. Pruning stays a separate step that needs approval (see [Prune](#after-the-run-prune)).

## Before the first run

| What | Where |
|---|---|
| Switch credentials | `inventory/group_vars/all/vault.yml` (from `vault.yml.example`) or AAP credentials |
| CloudVision token | `vault_cvp_token`. The `CVAAS` host and its connection settings are in `inventory/group_vars/CVAAS/cvaas_auth.yml` (CVaaS by default; the on-prem settings are noted there) |
| NetBox | `netbox_url` and `netbox_token` (vault). Only read access is needed |
| NetBox L2VPN | A VXLAN-EVPN L2VPN whose **identifier is the VNI**, with one import and one export target, and a termination on the VLAN. Without it, the VNI must be in the VLAN record |
| VLAN record | `vars/vlans/<dc>/<vid>_<slug>.yml` with `target_switches` and the gateway settings. See [vars/vlans/README.md](../../vars/vlans/README.md) |

No VLAN record yet? Run the command anyway. Discovery writes a starter record (`<vid>_<slug>.yml`) next to its report, and the readiness check stops with `VLAN <id> is not in the VLAN DB`. Review the starter record, decide what it leaves open (gateway mode and leaves), copy it to `vars/vlans/<dc>/`, and run again.

VLAN IDs repeat across NetBox sites. Scope the lookup per data center in group vars:

```yaml
netbox_vlan_query_by_dc:
  omaha: {group: omaha-vlans}
  lisle: {site: lis01}
```

## What the run does

| Step | What happens | Stops the run when |
|---|---|---|
| 0. Arguments | Checks `manual_vlan_id` and `manual_data_center` | Either is missing, or `prune_apply=true` is passed |
| 0. Preflight | Logs in to every switch in scope (`show version`, and the OS matches the inventory), reads CloudVision's devices (token, every target switch in a container), reads NetBox (API, the VLAN, its L2VPN). Writes `reports/<dc>/_runs/preflight_vlan<id>_<ts>.json` | Any check fails: a switch that does not answer or runs another OS, a target switch CloudVision does not manage, NetBox not answering or disagreeing with the record, no VNI source at all |
| 1. Discovery | Reads every switch in scope. Writes the report, endpoint baseline CSV, prune plan, MOP input and run index under `reports/<dc>/` | — (a switch that stops answering becomes a blocker in step 2) |
| 2. Readiness check | Reads the VNI and route targets from NetBox, then checks the MOP input's findings | Any blocker; the VLAN is not in the VLAN DB; open decisions (see below); NetBox and the record disagree on the VNI or route targets |
| 3. Deploy | Re-discovers, renders one configlet per target leaf, pushes them to CloudVision, creates one pending change control, waits for it | No L2 VNI; a switch CloudVision does not manage; the change control fails |
| 4. Verification | Checks the VNI mapping and MAC/ARP learning on the target leaves, and pings every host in the endpoint baseline in the VLAN's VRF | Runs only after the change control has completed |
| 5. Prune readiness | Discovers again with the VNI live and writes a prune dry run: which legacy switches can be pruned now, and which wait, by phase | Never changes a switch |
| 6. Summary | `reports/<dc>/_runs/migrate_vlan<id>_<ts>.json`, the endpoint comparison `migrate_vlan<id>_endpoints_<ts>.csv`, and a short result on screen | — |

### Open decisions

Discovery records what a person has to decide as `decision` findings in the MOP input (`<vlan>_mop_input_<ts>.json`): the VNI when NetBox has none, the gateway mode, naming. The run stops on them:

```text
NOT READY: VLAN 3000
1 decision(s) are open. Answer them in the VLAN record, then run again with -e migrate_decisions_reviewed=true.
VLAN 3000 [D-01] ...
```

Settle each one in the VLAN record (or in NetBox for the VNI), then run again with `-e migrate_decisions_reviewed=true`. A VNI that NetBox supplies is not a decision. **Blockers** always stop the run, even with that flag. High risks are listed and do not stop it. Put them in the MOP.

### The change control

The run creates the change control **pending** and waits for someone to approve and execute it in CloudVision. By default it checks every 60 seconds for up to 4 hours (`cvp_change_control_poll_seconds`, `cvp_change_control_wait_minutes`).

- **Completed:** verification and the prune check run.
- **Failed:** the run fails with CloudVision's error.
- **Still pending at the timeout:** the run ends without failing, with the outcome `waiting_for_change_control` and the resume commands. Once it has been executed in CloudVision:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/cvp_change_control_wait.yml \
  -e cvp_change_control_id=<id> -e target_vlan_id=3000
```

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/workflow_verify.yml \
  -e resume_vlan_id=3000 --limit dc_omaha
```

The change control ID is printed by the run and saved in `reports/cvp_change_controls/vlan<id>_cvp_<ts>.json`.

## Outcomes

The summary's `outcome` and the first line printed at the end:

| Outcome | Meaning |
|---|---|
| `deployed_and_verified` | Change control completed and every target leaf passed verification |
| `verification_failed` | Change control completed; at least one leaf failed (`verification.failed_switches`) |
| `waiting_for_change_control` | Change control created, not executed before the wait ended (`created`, `timed_out`) |
| `configlets_generated` | Run with `-e migrate_push_to_cvp=false`: configlets under `reports/cvp_configlets/<dc>/vlan<id>/`, nothing pushed |
| `no_change` | CloudVision created no task: the configlets were already on the switches |

The summary also holds the readiness result, the MOP input paths, the NetBox source of the VNI and route targets, the change control record, the endpoint comparison counts, and the prune readiness per switch.

### Endpoints before and after

Every endpoint discovery found before the change (step 1) is looked up again in the discovery after it (step 5), with the verification pings. One row per endpoint in `migrate_vlan<id>_endpoints_<ts>.csv`:

| Status | Meaning |
|---|---|
| `same_port` | Learned on the same switch port |
| `moved` | Learned on another edge port (expected for hosts re-cabled to fabric leaves) |
| `not_located` | Still in ARP or the MAC tables, but only on trunks or in no MAC table (`after_location`) |
| `mac_only` | No ARP entry any more; the MAC is still learned |
| `reachable_not_learned` | In no table, but answered the ping |
| `mac_changed` | **Check:** the IP now answers from another MAC |
| `ping_failed` | **Check:** in the tables, but did not answer the ping |
| `missing` | **Check:** in no table and no ping answer (`note` says when it was already intermittent before) |

The last three are counted as problems in the summary and on screen (`Endpoints: 40 same_port, 1 missing; check 1 in ...csv`). Quiet hosts age out of the tables, so look at the CSV before you prune. Endpoints seen only after the change are counted as `new`. Without step 5 (`migrate_prune_readiness=false`) there is nothing to compare against.

## After the run: prune

The last lines say which legacy switches are ready to prune and which are waiting:

```text
Prune after the deploy: ready mls-01; waiting acc-01, mls-02
Next: prune with playbooks/core/prune_vlan.yml -e prune_apply=true -e prune_approval=approve (dry run under reports/omaha/_prune/).
```

Read the dry run (`reports/<dc>/_prune/prune_dry_run_<ts>.md`) and follow the phase order in [prune-vlan.md](prune-vlan.md): MLS side of the non-VTEP links, then their non-VTEP side, then move the gateway, then the teardown. Switches wait with a status that says why: `blocked_until_vni_live`, `blocked_until_gateway_in_fabric`, `blocked_until_gateway_moved`. Re-run `prune_vlan.yml` (dry run) after each step.

## Options

| Variable | Default | Effect |
|---|---|---|
| `migrate_push_to_cvp` | `true` | `false` stops after generating the configlets |
| `migrate_decisions_reviewed` | `false` | `true` once every open decision is settled in the record |
| `migrate_preflight` | `true` | `false` skips step 0's preflight |
| `migrate_prune_readiness` | `true` | `false` skips step 5 (and its extra discovery, so no endpoint comparison) |
| `use_avd` | `false` | `true` renders the configlets with Arista AVD |
| `manual_target_vrf` | the record's VRF | VRF for the deploy and the endpoint pings |
| `cvp_change_control_wait_minutes` / `cvp_change_control_poll_seconds` | `240` / `60` | How long to wait for the change control |
| `netbox_evpn_conflict` | `fail` | `warn` deploys from the VLAN record when NetBox disagrees |

The other deploy and verification variables in [USAGE_GUIDE.md](../USAGE_GUIDE.md) apply too.

## Generate first, push later

To review the configlets before anything reaches CloudVision:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/migrate_vlan.yml \
  -e manual_vlan_id=3000 -e manual_data_center=omaha \
  -e migrate_push_to_cvp=false --limit dc_omaha
```

Then run the command again without `migrate_push_to_cvp=false`.

## Run it once against a real VLAN, read-only

Before the first real push, collect the data this was built against mocks for
(switch outputs, NetBox L2VPN answers, CloudVision device list): see
[collect-validation-data.md](collect-validation-data.md).

Back to [examples](README.md).
