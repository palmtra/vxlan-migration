# Discover a VLAN (read-only)

Run targeted discovery for one VLAN in a data center before adding it to the local SSOT.

## Probe an unknown VLAN

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=2911 \
  -e discovery_allow_probe=true \
  --limit dc_lisle
```

## Discover a VLAN already in the DB

```bash
ansible-playbook -i inventory/hosts.yml playbooks/core/discover_vlan.yml \
  -e manual_data_center=lisle \
  -e manual_vlan_id=100 \
  --limit dc_lisle
```

## Report location

```text
reports/<dc>/<vlan_name>/<vlan_name>_discovery_<timestamp>.{md,csv,json,yml}
```

Use the `*_vlan_db_snippet_*.yml` file to bootstrap `vars/vlans/<dc>.yml`.

See [USAGE_GUIDE.md](../USAGE_GUIDE.md) § Discovery for all flags.
