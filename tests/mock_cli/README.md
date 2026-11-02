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

`acc-sw-01` is a non-VTEP EOS switch with `discovery_profile: light`, dual-homed to
`eos-leaf-01` and `nxos-mls-02`; its link prunes come out blocked because VLAN 300 is
not VNI-mapped yet. `verify_mock.yml` then runs `post_change_verification` against the
baseline CSV; `10.30.0.50` does not answer, so the endpoint check fails by design.

Never put this directory on a production collections path.

## Omaha VLAN 3000 (`make mock-omaha`)

`tests/run_omaha_vlan3000_mock.sh` runs the whole Core path for one VLAN in Omaha:
discovery, the VLAN record an engineer writes from the snippet, and the generated
configs. `omaha/` is a second scenario with its own `inventory/` and `devices/`,
named after real Omaha switches so the router-ids and ASNs match the AVD repo.

- `fnts-fntc03-aggpe-mls01/02` (NXOS vPC) own the gateway: Vlan3000 in VRF
  `v0000822a`, HSRP 10.230.0.1/24, MTU 9216 (capped to 9214 on the fabric).
- `fntc-aggacc-sw01/02` have the endpoints and are the VNI targets.
  `fntc-aggacc-sw03` is a non-VTEP behind them.
- `fntc-aggacc-sw05/06` carry the VLAN with no endpoints, so they are prune-ready.
- `oma01-blf01/02` do not have the VLAN. They learn 10.230.0.0/24 over iBGP from the MLS pair.
- External VNIs 11000 and 11001 are already mapped, so discovery proposes 11002.

The record is written to a temp VLAN DB, not `vars/vlans/`. With
`AVD_REPO_DIR=<clone of palmtra/avd-vxlan-repo>`, the script also exports the
record into a copy of that clone and runs `python -m avd_services build --dc omaha`
there (needs `pyavd`). The generate-only deploy needs the `arista.cvp` collection
installed, even though nothing is sent to CloudVision.
