#!/usr/bin/env python3
"""End-to-end discovery: raw CLI text -> per-switch records -> VLAN report.

Topology for VLAN 100 (TENANT1, 10.10.100.0/24):

* nxos-mls-lis-01 / -02: legacy MLS pair, HSRP VIP .1, vPC peer-link Po1.
  -01 trunks Po10 to eos-leaf-lis-01 and Po11 to eos-leaf-lis-02.
  -02 trunks Po20 to acc-sw-12, a switch outside the inventory with no MACs.
  -01 redistributes the SVI into BGP and peers with the firewall 10.10.1.1.
* eos-leaf-lis-01: server on Et3 (LLDP: ESXi), uplink Po1 to the MLS.
* eos-leaf-lis-02: VLAN on its uplink only (transit).
* nxos-mls-lis-99: in the play, never reported back (unreachable).
"""

import os
import sys
import types
import unittest

if "ansible" not in sys.modules:
    ansible = types.ModuleType("ansible")
    ansible_errors = types.ModuleType("ansible.errors")

    class AnsibleFilterError(Exception):
        pass

    ansible_errors.AnsibleFilterError = AnsibleFilterError
    ansible.errors = ansible_errors
    sys.modules["ansible"] = ansible
    sys.modules["ansible.errors"] = ansible_errors

_REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REPO, "plugins"))

from vlan_lib.discovery import extract_vlan_targeted_discovery  # noqa: E402
from vlan_lib.db import probe_vlan_records  # noqa: E402
from vlan_lib.placement import flatten_deployment_model  # noqa: E402
from vlan_lib.parsers import (  # noqa: E402
    interface_config_entries_from_cli_results,
    parse_bgp_context,
    parse_ip_route_statics,
)
from vlan_lib.policy import parse_prefix_lists, parse_route_maps  # noqa: E402
from vlan_lib.reports import build_vlan_discovery_reports  # noqa: E402
from vlan_lib.topology import parse_lldp_neighbors, parse_vxlan_interface  # noqa: E402

INVENTORY = [
    "nxos-mls-lis-01",
    "nxos-mls-lis-02",
    "eos-leaf-lis-01",
    "eos-leaf-lis-02",
    "nxos-mls-lis-99",
]

MLS_SVI = """interface Vlan100
  description legacy_web
  no shutdown
  vrf member TENANT1
  ip address {ip}/24
  hsrp 1
    ip 10.10.100.1
  ip dhcp relay address 10.99.0.10
"""

NXOS_LLDP = """Chassis id: 001c.7300.0011
Port id: Ethernet1
Local Port id: Eth1/10
System Name: {name}
System Description: Arista Networks EOS version 4.28.3M
Enabled Capabilities: B, R
"""

MLS1_RPM = """ip prefix-list PL_TENANT1 seq 5 permit 10.10.0.0/16 le 24
route-map RM_CONN permit 10
  match ip address prefix-list PL_TENANT1
"""

MLS1_BGP = """router bgp 65001
  router-id 10.1.0.21
  vrf TENANT1
    address-family ipv4 unicast
      redistribute direct route-map RM_CONN
    neighbor 10.10.1.1
      remote-as 65099
      description FW
"""

MLS1_ROUTES = """vrf context TENANT1
  ip route 10.30.0.0/16 10.10.100.5 name app_lb
"""

MLS1_BGP_PREFIX = """BGP routing table entry for 10.10.100.0/24, version 5
  Path type: redist, path is valid, is best path
  AS-Path: NONE, path locally originated

  Path-id 1 advertised to peers:
    10.10.1.1
"""


def _mls(hostname, ip, trunks, lldp_name, arp, extra_ports=""):
    ports = "Po1, " + ", ".join(trunks) + extra_ports
    return {
        "platform": "nxos",
        "vlan_id_output": (
            "VLAN Name                             Status    Ports\n"
            "---- -------------------------------- --------- -----\n"
            "100  WEB_VXLAN                        active    %s\n" % ports
        ),
        "svi_output": "!Command: show running-config interface Vlan100\n" + MLS_SVI.format(ip=ip),
        "mac_output": "",
        "arp_output": arp,
        "trunk_interface_names": ["Po1"] + trunks,
        "exempt_trunk_interface_names": ["Po1"],
        "interface_configs": interface_config_entries_from_cli_results(
            [
                {"item": "Po1", "stdout": "interface port-channel1\n  vpc peer-link\n"},
            ],
            "nxos",
        ),
        "switch_hostnames": INVENTORY,
        "inventory_hostnames": INVENTORY,
        "neighbors": parse_lldp_neighbors(NXOS_LLDP.format(name=lldp_name), "nxos"),
        "port_channels": {trunks[0].lower(): ["Ethernet1/10"]},
        "stp_output": "",
        "errors": [],
    }


class DiscoveryEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vlan = probe_vlan_records([100], [], "lisle")[0]
        mls1_arp = (
            "IP ARP Table for context TENANT1\n"
            "10.10.100.10    00:00:12  0050.56ab.1001  Vlan100\n"
            "10.10.100.3     00:00:02  0023.04ee.be02  Vlan100\n"
        )
        mls1 = extract_vlan_targeted_discovery(
            vlan,
            dict(
                _mls("nxos-mls-lis-01", "10.10.100.2", ["Po10", "Po11"], "eos-leaf-lis-01", mls1_arp),
                mac_output=(
                    "* 100    0050.56ab.1001   dynamic   0   F   F   Po10\n"
                ),
                bgp_prefix_outputs=[{"item": "10.10.100.0/24", "stdout": MLS1_BGP_PREFIX}],
            ),
        )
        mls2 = extract_vlan_targeted_discovery(
            vlan,
            _mls(
                "nxos-mls-lis-02",
                "10.10.100.3",
                ["Po20"],
                "acc-sw-12",
                "",
            ),
        )
        leaf1 = extract_vlan_targeted_discovery(
            vlan,
            {
                "platform": "eos",
                "vlan_id_output": (
                    "VLAN  Name      Status    Ports\n"
                    "----- --------- --------- -------\n"
                    "100   WEB_VXLAN active    Et3, Po1\n"
                ),
                "svi_output": "",
                "mac_output": (
                    "100    0050.56ab.1001    DYNAMIC     Et3\n"
                    "100    0023.04ee.be02    DYNAMIC     Po1\n"
                ),
                "trunk_interface_names": ["Port-Channel1"],
                "switch_hostnames": INVENTORY,
                "inventory_hostnames": INVENTORY,
                "neighbors": parse_lldp_neighbors(
                    "Interface Ethernet3 detected 1 LLDP neighbors:\n\n"
                    "  Neighbor 0050.56ab.1001/vmnic2, age 3 seconds\n"
                    '  - System Name: "esx-web-01"\n'
                    '  - System Description: "VMware ESX Releasebuild-21495797"\n'
                    "Interface Ethernet49 detected 1 LLDP neighbors:\n\n"
                    "  Neighbor 0023.04ee.be01/Ethernet1/10, age 3 seconds\n"
                    '  - System Name: "nxos-mls-lis-01"\n',
                    "eos",
                ),
                "port_channels": {"po1": ["Ethernet49"]},
            },
        )
        leaf2 = extract_vlan_targeted_discovery(
            vlan,
            {
                "platform": "eos",
                "vlan_id_output": "100   WEB_VXLAN active    Po1\n",
                "trunk_interface_names": ["Port-Channel1"],
                "interface_configs": [
                    {"interface": "Port-Channel1", "description": "uplink to nxos-mls-lis-01", "raw": "x"}
                ],
                "switch_hostnames": INVENTORY,
                "inventory_hostnames": INVENTORY,
            },
        )
        bgp = parse_bgp_context(MLS1_BGP)
        hostvars = {
            "nxos-mls-lis-01": {
                "_vlan_discovery": {
                    "hostname": "nxos-mls-lis-01",
                    "os_family": "nxos",
                    "per_vlan": [mls1],
                    "bgp_as": bgp["bgp_as"],
                    "bgp_vrfs": bgp["vrfs"],
                    "bgp_neighbors": bgp["neighbors"],
                    "static_routes": parse_ip_route_statics(MLS1_ROUTES),
                    "prefix_lists": parse_prefix_lists(MLS1_RPM),
                    "route_maps": parse_route_maps(MLS1_RPM),
                }
            },
            "nxos-mls-lis-02": {
                "_vlan_discovery": {"hostname": "nxos-mls-lis-02", "os_family": "nxos", "per_vlan": [mls2]}
            },
            "eos-leaf-lis-01": {
                "_vlan_discovery": {
                    "hostname": "eos-leaf-lis-01",
                    "os_family": "eos",
                    "per_vlan": [leaf1],
                    "expected_vtep_source": "Loopback1",
                    "vxlan_interface": parse_vxlan_interface(
                        "Vxlan1 is up, line protocol is up (connected)\n"
                        "  Source interface is Loopback0 and is active with 10.1.0.11\n"
                        "  Replication/Flood Mode is headend with Flood List Source: CLI\n"
                        "  Static VRF to VNI mapping is\n"
                        "   [TENANT2, 50002]\n"
                    ),
                }
            },
            "eos-leaf-lis-02": {
                "_vlan_discovery": {"hostname": "eos-leaf-lis-02", "os_family": "eos", "per_vlan": [leaf2]}
            },
            "nxos-mls-lis-99": {},
            "ios-leaf-lis-01": {"_discovery_skipped": True},
        }
        play_hosts = INVENTORY[:4]
        cls.report = build_vlan_discovery_reports(
            [vlan], play_hosts, hostvars, play_hosts + ["nxos-mls-lis-99", "ios-leaf-lis-01"]
        )[0]

    def test_probe_record_takes_l3_from_the_legacy_gateway(self):
        gateway = self.report["gateway_assessment"]
        self.assertEqual(gateway["location"], "legacy")
        self.assertEqual(gateway["source_devices"], ["nxos-mls-lis-01", "nxos-mls-lis-02"])
        self.assertEqual(gateway["virtual_ip"], "10.10.100.1")
        self.assertEqual(gateway["services"]["dhcp_relay"], [{"server": "10.99.0.10", "vrf": ""}])
        self.assertEqual(self.report["migration_type"], "l3")
        self.assertEqual(self.report["deployment_model"]["gateway"]["location"], "legacy")

    def test_placement(self):
        placement = self.report["deployment_model"]["placement"]
        self.assertEqual(
            [leaf["hostname"] for leaf in placement["participating_leafs"]], ["eos-leaf-lis-01"]
        )
        self.assertEqual(
            [leaf["hostname"] for leaf in placement["prune_eligible_leafs"]],
            ["nxos-mls-lis-01", "eos-leaf-lis-02"],
        )
        roles = {item["hostname"]: item["role"] for item in self.report["placement_analysis"]["switches"]}
        self.assertEqual(roles["nxos-mls-lis-02"], "review")

    def test_lldp_overrides_missing_descriptions(self):
        leaf1 = next(d for d in self.report["devices"] if d["hostname"] == "eos-leaf-lis-01")
        roles = {item["interface"]: item["role"] for item in leaf1["port_attachments"]}
        self.assertEqual(roles["Et3"], "compute")
        self.assertEqual(roles["Po1"], "switch_uplink")

    def test_endpoints_are_located_at_the_edge(self):
        rows = {row["ip"]: row for row in self.report["endpoint_table"]["endpoints"]}
        server = rows["10.10.100.10"]
        self.assertEqual((server["hostname"], server["interface"]), ("eos-leaf-lis-01", "Et3"))
        self.assertIn("esx-web-01", server["port_label"])
        self.assertEqual(server["flags"], [])
        self.assertEqual(rows["10.10.100.3"]["flags"], ["gateway"])
        self.assertEqual(self.report["endpoint_table"]["summary"]["active_ips"], 1)

    def test_routing_objects_and_advertisement(self):
        assoc = self.report["deployment_model"]["routing"]["associated"]
        statics = [(s["prefix"], s["next_hop"]) for s in assoc["static_routes"]]
        self.assertEqual(statics, [("10.30.0.0/16", "10.10.100.5")])
        redist = [n for n in assoc["bgp_networks"] if n["kind"] == "redistribute"][0]
        self.assertEqual(redist["route_map"], "RM_CONN")
        self.assertEqual(redist["verdicts"][0]["verdict"], "permit")
        exports = [(e["neighbor"], e["verdict"]) for e in assoc["bgp_exports"]]
        self.assertEqual(exports, [("10.10.1.1", "permit")])
        self.assertEqual(assoc["bgp_live"][0]["advertised_to"], ["10.10.1.1"])

    def test_model_round_trips_with_gateway_mode(self):
        import copy

        model = copy.deepcopy(self.report["deployment_model"])
        model["gateway"]["mode"] = "distributed_anycast"
        record = flatten_deployment_model(model)
        self.assertEqual(record["service_type"], "l3")
        self.assertEqual(record["gateway"], "10.10.100.1")
        self.assertEqual(record["gateway_mode"], "distributed_anycast")
        self.assertEqual(record["gateway_location"], "legacy")
        self.assertEqual(record["vrf"], "TENANT1")

    def test_fabric_readiness_flags_a_leaf_that_is_not_her_evpn(self):
        readiness = self.report["fabric_readiness"]
        self.assertEqual([row["hostname"] for row in readiness["checked"]], ["eos-leaf-lis-01"])
        issues = " | ".join(readiness["issues"])
        self.assertIn("eos-leaf-lis-01 builds its head-end flood list from CLI, not EVPN", issues)
        self.assertIn("eos-leaf-lis-01 VTEP source is Loopback0, expected Loopback1", issues)
        self.assertIn("VRF TENANT1 has no L3 VNI on eos-leaf-lis-01", issues)
        self.assertNotIn("floods BUM with", issues)
        self.assertIn(readiness["issues"][0], self.report["action_items"])

    def test_unreachable_and_action_items(self):
        self.assertEqual(self.report["not_discovered"], ["nxos-mls-lis-99"])
        items = " | ".join(self.report["action_items"])
        self.assertIn("nxos-mls-lis-99 was not discovered", items)
        self.assertIn("nxos-mls-lis-02 needs review", items)
        self.assertIn("Choose gateway_mode", items)
        self.assertIn("Assign evpn.l2.vni", items)


if __name__ == "__main__":
    unittest.main()
