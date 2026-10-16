# Prune eligible switches

Remove a VLAN from switches that carry it and have no local endpoints. Source gateways are included and come first: they are the root of the legacy L2. Participating leaves and engineer-named gateway leaves are not touched. A source gateway that still has local endpoints stays with the participating leaves.

The playbook discovers first, then prunes from that classification. It does not read an old report file.

## Link prunes (non-VTEP switches)

A switch that is not a VTEP (an access switch, or an EOS switch without Vxlan1) keeps the VLAN as plain L2 and must reach it through a VTEP only. If it is linked to a VTEP **and** to the MLS, it bridges the EVPN and legacy L2 domains for that VLAN. That is a loop. Discovery turns each of those MLS links into a **link prune**: `switchport trunk allowed vlan remove <id>` on the non-VTEP switch's port and on the MLS port (the MLS side is skipped when the whole MLS is prune-eligible). The switch itself keeps the VLAN.

A link prune is applied only when it is ready:

| Status | Meaning |
|---|---|
| `ready` | The VLAN is VNI-mapped on every VTEP the switch uplinks to, and the gateway is reachable from the fabric |
| `blocked_until_vni_live` | Deploy the VNI on those VTEPs first, then re-run. Pruning now would cut the endpoints off |
| `blocked_until_gateway_in_fabric` | The gateway (MLS SVI or firewall) is still only on the legacy side and no fabric-legacy interconnect carries the VLAN |

Blocked link prunes are listed in the prune report with their CLI under "do not paste" and are never sent. A non-VTEP switch with endpoints and **no** VTEP uplink is an action item; its MLS links are kept and the MLS is `legacy_retained`.

Typical cutover order: deploy the VNI (and the anycast gateway for L3) → re-run discovery → link prunes become `ready` → apply → move or retire the MLS.

## Dry-run (default)

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/prune_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle
```

This writes `reports/<dc>/_prune/prune_dry_run_<timestamp>.md` and prints the CLI. No switch is changed.

`--check` also stays a dry-run, even if the approval vars are set.

## Apply

Both extra vars are required. `prune_apply=true` alone is refused.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/prune_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  -e prune_apply=true \
  -e prune_approval=approve \
  --limit dc_lisle
```

| Platform | What apply does | What it does not do |
|---|---|---|
| EOS | `configure session`, then `commit timer 00:10:00` | `configure confirm`. Confirm on the switch, or wait and the timer rolls back |
| NXOS | Checkpoint, then the trunk/VLAN removal | `copy running-config startup-config`. Roll back with the checkpoint command in the dry-run |
| IOS | Nothing | IOS prune stays manual |

## Related variables

| Variable | Default | Description |
|---|---|---|
| `prune_apply` | `false` | `true` sends the CLI |
| `prune_approval` | empty | Must be the exact word `approve` when `prune_apply` is true |
| `prune_commit_timer` | `00:10:00` | EOS rollback timer written into the session |

Discovery and the prune report stay read-only. This playbook is the only apply path.
