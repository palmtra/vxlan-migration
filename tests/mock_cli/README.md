# Mock CLI for discovery tests

`tests/run_discovery_mock.sh` (`make mock`) runs `playbooks/core/discover_vlan.yml`
end to end without devices.

- `ansible_collections/ansible/netcommon/` is a test-only shadow of `ansible.netcommon`.
  Its `cli_command` action reads `devices/<inventory_hostname>/<slug>.txt`, where `slug`
  is the command in lowercase with every run of non-alphanumerics replaced by `_`
  (`show ip arp vlan 300 vrf TENANT1` -> `show_ip_arp_vlan_300_vrf_tenant1.txt`).
- A `<slug>.error` file makes that command fail with the file content as the message.
- A missing file returns empty output, like a command that prints nothing.
- `mock_unreachable: true` on a host makes it unreachable.
- `inventory/` runs every host locally and reuses `inventory/group_vars/all` from the repo.

The canned topology probes VLAN 300: an NXOS MLS pair with HSRP, a firewall on the
first MLS (CDP), a trunk from the second MLS to an access switch outside the inventory,
an EOS leaf with servers, an EOS leaf with the VLAN on its uplink only, one failing
command, and one unreachable switch.

Never put this directory on a production collections path.
