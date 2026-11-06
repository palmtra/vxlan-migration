# Examples

Focused, copy-paste runnable examples. Each file covers one operator task.

| Example | Description |
|---|---|
| [migrate-vlan.md](migrate-vlan.md) | One command, end to end: discovery, readiness check, NetBox VNI / route targets, configlets, CloudVision change control, verification, prune dry run |
| [collect-validation-data.md](collect-validation-data.md) | Read-only capture of switch outputs, NetBox L2VPN answers and CloudVision results (secrets removed) to check the code against real data |
| [discover-vlan.md](discover-vlan.md) | Read-only VLAN discovery plus a separate prune report (EOS sessions) |
| [prune-vlan.md](prune-vlan.md) | Prune eligible switches: dry-run by default, apply only with approval |
| [generate-config-without-cvp-push.md](generate-config-without-cvp-push.md) | Generate migration configlets locally without CVP push |
| [export-vlan-to-netbox.md](export-vlan-to-netbox.md) | Standalone NetBox export (dry-run default) |
| [allocate-vni.md](allocate-vni.md) | A VLAN's VNI and route target from NetBox IPAM route targets: reuse, or the next free number per DC and tenant |

For the full flag reference, see [USAGE_GUIDE.md](../USAGE_GUIDE.md).

Back to [documentation index](../README.md).
