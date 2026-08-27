# Per-DC service migration bundles

A **service** groups related VLANs, VRFs, build patterns, and prune/retain rules for
one tenant cutover (for example Aspira `0000822`). Per-VLAN files under
`vars/vlans/<dc>/` remain the discovery and NetBox SSOT; service files are the
**cutover orchestrator**.

```text
vars/services/
  lisle/
    _example.yml              # schema template (not loaded)
    aspira_0000822.yml        # real service (example naming)
  omaha/
    _example.yml
```

## Filename convention

```text
{service_slug}.yml
```

Use lowercase snake_case (`aspira_0000822`, `customer_acme_dr`). Files starting
with `_` are ignored by the loader.

## When to use a service file

| Use case | SSOT |
|---|---|
| Single VLAN → one VNI | `vars/vlans/<dc>/{vid}_*.yml` only |
| Multi-VLAN / multi-VRF tenant with custom BGP, statics, retain | Service file + linked VLAN files |
| Prune-first DR cutover with keep-lists (AWS DX, shared VRFs) | Service `prune.retain` |

## Loading a service during discovery

Pass the service id with the data center:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_service_id=aspira_0000822 \
  --limit dc_lisle
```

Discovery stays **read-only**. The service `prune.retain` list is applied when
building the candidate `prune_plan` so retained VRFs/neighbors/interfaces are
marked `blocked_by_retain` instead of delete candidates.

## Related

- Schema template: [`lisle/_example.yml`](lisle/_example.yml)
- Per-VLAN DB: [`../vlans/README.md`](../vlans/README.md)
- Discovery prune plans: [`../../docs/examples/discover-vlan.md`](../../docs/examples/discover-vlan.md)
