# Per-DC service migration bundles

A **service** groups related VLANs, VRFs, build patterns, and prune/retain rules for
one tenant cutover (for example Aspira `0000822`). Per-VLAN files under
`vars/vlans/<dc>/` remain the discovery and NetBox SSOT; service files are the
**cutover orchestrator**.

```text
vars/services/
  lisle/
    aspira_0000822.yml        # real service (example naming)
  omaha/
```

Only real service files belong here. The schema template and test fixtures live on the `test` branch.

Loaded bundles are validated against [`schemas/service_bundle.schema.json`](../../schemas/service_bundle.schema.json).

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

Discovery stays **read-only**. The service `prune.retain` list tags L3 review
objects (`blocked_by_retain`). Prune **actions** remain trunk / SVI / VLAN only;
statics and BGP are never auto-candidates.

## Related

- Schema: [`../../schemas/service_bundle.schema.json`](../../schemas/service_bundle.schema.json)
- Per-VLAN DB: [`../vlans/README.md`](../vlans/README.md)
- Discovery prune plans: [`../../docs/examples/discover-vlan.md`](../../docs/examples/discover-vlan.md)
