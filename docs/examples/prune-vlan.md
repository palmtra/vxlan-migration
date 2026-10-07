# Prune eligible switches

Remove a VLAN from switches that carry it and have no local endpoints. Source gateways are included and come first: they are the root of the legacy L2. Participating leaves and engineer-named gateway leaves are not touched. A source gateway that still has local endpoints stays with the participating leaves.

The playbook discovers first, then prunes from that classification. It does not read an old report file.

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
