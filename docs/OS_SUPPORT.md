# Network OS support policy

This repository migrates **legacy Cisco NX-OS → Arista EOS (VXLAN/EVPN)**.

## Active on `main` (first-class)

| OS | Role |
|---|---|
| **NXOS** | Legacy source — discovery (VLAN/SVI/HSRP/trunk/MAC/ARP/VRF/BGP) |
| **EOS** | Target fabric — discovery, AVD/Jinja configlets, CVP, verify |

New features (service bundles, prune plans, AVD patterns) are designed for this pair.

## IOS — parked (reuse, not active development)

IOS discovery tasks remain in the tree (`roles/vlan_discovery/tasks/gather_ios*.yml`)
but are **disabled by default** on `main` (`discovery_enable_ios: false`).

| Goal | How |
|---|---|
| Run IOS discovery on current `main` | `-e discovery_enable_ios=true` |
| Restore the pre-park three-OS snapshot | Tag `archive/multi-os-ios-nxos-eos-2026-08` or branch `archive/ios-discovery` |
| Contribute IOS hardening | Prefer a branch from the archive tag; do not expand IOS on `main` unless product scope changes |

Deploy, AVD, CVP, and post-change verification remain **EOS-only** regardless of discovery OS.

## Safety gates

Deploy is `playbooks/core/workflow_deploy.yml`. The old direct-device playbooks and `roles/migrate_to_vxlan` have been removed.

| Control | Default | Purpose |
|---|---|---|
| `auto_rollback_on_verification_failure` | `false` | No auto CVP rollback on verify failure |
| `cvp_rollback_allow_destructive_no_vlan` | `false` | Rollback only removes VXLAN maps, not `no vlan` |
| `allow_placeholder_fabric_defaults` | `false` | Rejects mcast `239.1.1.1` / empty BGP ASN placeholders |
| `cvp_apply_configlets` | `false` | Generate-only until explicitly enabled |

Prefer `playbooks/core/workflow_deploy.yml` for all production migrations.

## Snapshot refs (created 2026-08)

```text
tag:    archive/multi-os-ios-nxos-eos-2026-08   # annotated freeze at 2dd4687
branch: archive/ios-discovery                   # same commit; easy checkout
```

```bash
# Inspect the freeze point
git show archive/multi-os-ios-nxos-eos-2026-08

# Work from the archive branch (read-only reuse / cherry-pick source)
git switch archive/ios-discovery
```

Push tag + archive branch when publishing:

```bash
git push origin archive/ios-discovery
git push origin archive/multi-os-ios-nxos-eos-2026-08
```

## Why not a separate repo?

IOS was only a discovery adapter. Splitting would duplicate VLAN DB, services, CVP/AVD,
and workflow glue. One repo + archive tag/branch is enough for reuse.

## Related

- [WORKFLOWS.md](WORKFLOWS.md) — Core vs Advanced architecture
- [USAGE_GUIDE.md](USAGE_GUIDE.md) — `discovery_enable_ios` and other flags
- Inventory groups: `eos_devices`, `nxos_devices` (active); `ios_devices` (optional / parked)
