#!/usr/bin/env python3
"""Topology (LLDP/CDP/STP/port-channel) and routing-policy parser tests."""

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
_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "cli")
sys.path.insert(0, os.path.join(_REPO, "plugins"))

from vlan_lib.discovery import build_vlan_verification  # noqa: E402
from vlan_lib.parsers import (  # noqa: E402
    parse_arp_entries,
    parse_ip_route_statics,
    parse_svi_details,
    parse_vlan_id_ports,
    svi_command_indicates_present,
)
from vlan_lib.policy import (  # noqa: E402
    collect_command_errors,
    evaluate_route_map,
    parse_bgp_prefix_detail,
    parse_prefix_lists,
    parse_route_maps,
)
from vlan_lib.topology import (  # noqa: E402
    classify_neighbor,
    neighbors_for_interface,
    parse_cdp_neighbors,
    parse_lldp_neighbors,
    parse_port_channel_members,
    parse_spanning_tree_vlan,
    parse_vxlan_interface,
    parse_vxlan_vlan_to_vni,
)

INVENTORY = ["eos-leaf-lis-01", "eos-leaf-lis-02", "nxos-mls-lis-01", "nxos-mls-lis-02"]


def _read(*parts):
    with open(os.path.join(_FIXTURES, *parts), encoding="utf-8") as handle:
        return handle.read()


class ParserBugFixTests(unittest.TestCase):
    def test_nxos_command_echo_is_not_an_svi(self):
        text = _read("nxos", "show_run_interface_vlan100_absent.txt")
        self.assertFalse(svi_command_indicates_present(text, 100))
        self.assertTrue(
            svi_command_indicates_present(_read("nxos", "show_run_interface_vlan100.txt"), 100)
        )

    def test_svi_presence_does_not_match_a_longer_vlan(self):
        self.assertFalse(svi_command_indicates_present("interface Vlan1000\n", 100))

    def test_nxos_vlan_table_stops_at_blank_line(self):
        result = parse_vlan_id_ports(
            _read("nxos", "show_vlan_id_100_full.txt"), 100, "nxos", ["Po1", "Po10"], ["Po1"]
        )
        self.assertEqual(result["access_ports"], ["Eth1/5", "Eth1/6"])
        self.assertEqual(result["trunk_ports"], ["Po10"])
        self.assertEqual(result["exempt_trunk_ports"], ["Po1"])

    def test_eos_mlag_peer_ports_are_not_local(self):
        result = parse_vlan_id_ports(
            _read("eos", "show_vlan_id_100_mlag.txt"), 100, "eos", ["Port-Channel1"], []
        )
        self.assertEqual(result["all_ports"], ["Et3", "Po1"])
        self.assertEqual(result["mlag_peer_ports"], ["PEt10", "PPo20"])
        self.assertTrue(result["vxlan_mapped"])

    def test_eos_arp_include_drops_vlan1000(self):
        entries = parse_arp_entries(_read("eos", "show_ip_arp_vlan100_include.txt"), 100)
        self.assertEqual([e["ip"] for e in entries], ["10.10.100.10", "10.10.100.11", "10.10.100.3"])
        self.assertEqual(entries[0]["interface"], "Vlan100")
        self.assertEqual(entries[0]["port"], "Ethernet3")
        self.assertEqual(entries[1]["age"], "0:01:40")

    def test_static_route_with_exit_interface_and_options(self):
        routes = parse_ip_route_statics(_read("eos", "show_run_ip_route.txt"))
        by_prefix = {(r["vrf"], r["prefix"]): r for r in routes}
        lb = by_prefix[("TENANT1", "10.30.0.0/16")]
        self.assertEqual(lb["next_hop"], "10.10.100.5")
        self.assertEqual(lb["interface"], "Vlan100")
        self.assertEqual(lb["distance"], "200")
        self.assertEqual(lb["tag"], "50")
        self.assertEqual(lb["name"], "app_lb")
        null = by_prefix[("TENANT1", "10.10.0.0/16")]
        self.assertEqual(null["next_hop"], "Null0")
        self.assertEqual(null["distance"], "254")
        self.assertEqual(by_prefix[("TENANT1", "10.40.0.0/16")]["next_hop"], "10.10.100.6")

    def test_rich_nxos_svi_keeps_every_migration_object(self):
        details = parse_svi_details(_read("nxos", "show_run_interface_vlan200_rich.txt"), "nxos")
        self.assertEqual(details["vrf"], "TENANT1")
        self.assertEqual(details["primary_ip"], "10.20.0.2/24")
        self.assertEqual(details["secondary_ips"], ["10.20.1.2/24"])
        self.assertEqual(details["gateway_addresses"], ["10.20.0.1", "10.20.1.1"])
        self.assertEqual(details["redundancy"], "hsrp")
        group = details["hsrp_groups"][0]
        self.assertEqual((group["group"], group["priority"], group["preempt"]), (200, 110, True))
        self.assertEqual(
            details["dhcp_relay"],
            [{"server": "10.99.0.10", "vrf": ""}, {"server": "10.99.0.11", "vrf": "SHARED"}],
        )
        self.assertEqual(details["acls"], [{"name": "ACL_APP_IN", "direction": "in"}])
        self.assertEqual(details["pbr"], ["PBR_APP"])
        self.assertNotIn("ospf", details)
        self.assertEqual(details["multicast"], ["ip pim sparse-mode"])
        self.assertEqual(details["ipv6_addresses"], ["2001:db8:200::2/64"])
        self.assertEqual(details["mtu"], 9000)
        self.assertFalse(details["shutdown"])
        self.assertEqual(details["other_lines"], ["ip arp timeout 300"])

    def test_eos_anycast_and_vrrp_svis(self):
        anycast = parse_svi_details(
            "interface Vlan300\n   vrf TENANT1\n   ip address virtual 10.30.0.1/24\n", "eos"
        )
        self.assertEqual(anycast["redundancy"], "anycast")
        self.assertEqual(anycast["ip_addresses"], ["10.30.0.1/24"])
        self.assertEqual(anycast["gateway_addresses"], ["10.30.0.1"])
        vrrp = parse_svi_details(
            "interface Vlan301\n   ip address 10.31.0.2/24\n"
            "   vrrp 5 priority-level 120\n   vrrp 5 ipv4 10.31.0.1\n   shutdown\n",
            "eos",
        )
        self.assertEqual(vrrp["redundancy"], "vrrp")
        self.assertEqual(vrrp["vrrp_addresses"], ["10.31.0.1"])
        self.assertTrue(vrrp["shutdown"])


class TopologyTests(unittest.TestCase):
    def test_eos_lldp_detail(self):
        neighbors = parse_lldp_neighbors(_read("eos", "show_lldp_neighbors_detail.txt"), "eos")
        self.assertEqual([n["local_interface"] for n in neighbors], ["Ethernet1", "Ethernet3", "Ethernet7"])
        self.assertEqual(neighbors[0]["neighbor_name"], "nxos-mls-lis-01.example.net")
        self.assertEqual(neighbors[0]["neighbor_port"], "Ethernet1/49")
        self.assertEqual(neighbors[0]["capabilities"], ["bridge", "router"])
        roles = [classify_neighbor(n, INVENTORY) for n in neighbors]
        self.assertEqual(roles, ["switch", "compute", "network_service"])

    def test_nxos_lldp_and_cdp_detail(self):
        lldp = parse_lldp_neighbors(_read("nxos", "show_lldp_neighbors_detail.txt"), "nxos")
        self.assertEqual([n["local_interface"] for n in lldp], ["Eth1/1", "Eth1/10"])
        self.assertEqual(
            [classify_neighbor(n, INVENTORY) for n in lldp], ["switch", "downstream_switch"]
        )
        cdp = parse_cdp_neighbors(_read("nxos", "show_cdp_neighbors_detail.txt"))
        self.assertEqual([n["local_interface"] for n in cdp], ["Ethernet1/53", "Ethernet1/20"])
        self.assertEqual(cdp[0]["neighbor_name"], "nxos-mls-lis-02")
        self.assertEqual(
            [classify_neighbor(n, INVENTORY) for n in cdp], ["switch", "network_service"]
        )

    def test_port_channel_neighbors_come_from_members(self):
        members = parse_port_channel_members(
            _read("nxos", "show_run_section_channel_group.txt"), "nxos"
        )
        self.assertEqual(members["po10"], ["Ethernet1/10"])
        lldp = parse_lldp_neighbors(_read("nxos", "show_lldp_neighbors_detail.txt"), "nxos")
        found = neighbors_for_interface("Po10", lldp, members, "nxos")
        self.assertEqual([n["neighbor_name"] for n in found], ["acc-sw-12.example.net"])

    def test_spanning_tree_root_and_blocked_ports(self):
        root = parse_spanning_tree_vlan(_read("nxos", "show_spanning_tree_vlan_100.txt"))
        self.assertTrue(root["is_root"])
        self.assertEqual(root["root_address"], "00:23:04:ee:be:01")
        leaf = parse_spanning_tree_vlan(_read("eos", "show_spanning_tree_vlan_100.txt"))
        self.assertFalse(leaf["is_root"])
        self.assertEqual(leaf["root_port"], "Port-Channel1")
        self.assertEqual(leaf["bridge_address"], "00:1c:73:00:00:11")
        self.assertEqual(leaf["blocked_ports"], ["Po3"])

    def test_verification_reads_the_vni_column(self):
        outputs = {
            "vni_output": _read("eos", "show_vxlan_vlan_to_vni.txt"),
            "registry": [{"id": 200, "vni": 10200}, {"id": 300, "vni": 99999}],
        }
        self.assertTrue(build_vlan_verification(200, outputs)["vni_mapped"])
        self.assertFalse(build_vlan_verification(300, outputs)["vni_mapped"])

    def test_vxlan_interface_her_evpn(self):
        vxlan = parse_vxlan_interface(_read("eos", "show_interfaces_vxlan1.txt"))
        self.assertEqual((vxlan["source_interface"], vxlan["source_ip"]), ("Loopback1", "10.1.1.11"))
        self.assertEqual((vxlan["replication_mode"], vxlan["flood_list_source"]), ("headend", "EVPN"))
        self.assertEqual(vxlan["udp_port"], 4789)
        self.assertEqual(vxlan["vrf_vni"], {"TENANT1": 50001, "TENANT2": 50002})
        self.assertIn({"vlan_id": 200, "vni": 10200}, vxlan["vlan_vni"])

    def test_vxlan_interface_static_flood_list(self):
        vxlan = parse_vxlan_interface(_read("eos", "show_interfaces_vxlan1_static_flood.txt"))
        self.assertEqual(vxlan["flood_list_source"], "CLI")
        self.assertEqual(vxlan["flood_vteps"], ["10.1.1.11", "10.1.1.13"])
        self.assertFalse(parse_vxlan_interface("% Interface does not exist")["present"])

    def test_vxlan_vlan_to_vni(self):
        self.assertEqual(
            parse_vxlan_vlan_to_vni(_read("eos", "show_vxlan_vlan_to_vni.txt")),
            [{"vlan_id": 200, "vni": 10200}, {"vlan_id": 300, "vni": 50100}],
        )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        text = _read("nxos", "show_run_prefix_route_map.txt")
        self.prefix_lists = parse_prefix_lists(text)
        self.route_maps = parse_route_maps(text)

    def test_prefix_lists_one_line_and_block_form(self):
        self.assertEqual(self.prefix_lists["PL_TENANT1"][0]["le"], 24)
        block = parse_prefix_lists(_read("eos", "show_run_prefix_list_block.txt"))
        self.assertEqual([e["seq"] for e in block["PL_WEB"]], [10, 20])
        self.assertEqual((block["PL_WEB"][1]["ge"], block["PL_WEB"][1]["le"]), (24, 28))

    def test_route_map_permits_vlan_prefix(self):
        result = evaluate_route_map(
            "RM_CONN", "10.10.100.0/24", self.route_maps, self.prefix_lists
        )
        self.assertEqual(result["verdict"], "permit")
        self.assertIn("PL_TENANT1 seq 5", result["reason"])

    def test_route_map_denies_vlan_prefix(self):
        result = evaluate_route_map(
            "RM_FW_OUT", "10.10.100.0/24", self.route_maps, self.prefix_lists
        )
        self.assertEqual(result["verdict"], "deny")
        other = evaluate_route_map("RM_FW_OUT", "10.20.0.0/24", self.route_maps, self.prefix_lists)
        self.assertEqual(other["verdict"], "permit")

    def test_route_map_unknown_and_missing(self):
        self.assertEqual(
            evaluate_route_map("RM_COMM", "10.10.100.0/24", self.route_maps, self.prefix_lists)["verdict"],
            "unknown",
        )
        self.assertEqual(
            evaluate_route_map("RM_NOPE", "10.10.100.0/24", self.route_maps, self.prefix_lists)["verdict"],
            "missing",
        )
        self.assertEqual(evaluate_route_map("", "10.10.100.0/24", {}, {})["verdict"], "none")

    def test_prefix_list_exact_length_without_ge_le(self):
        block = parse_prefix_lists(_read("eos", "show_run_prefix_list_block.txt"))
        maps = parse_route_maps(_read("eos", "show_run_prefix_list_block.txt"))
        self.assertEqual(evaluate_route_map("RM_CONN", "10.10.100.0/24", maps, block)["verdict"], "permit")
        self.assertEqual(evaluate_route_map("RM_CONN", "10.10.100.0/25", maps, block)["verdict"], "deny")
        self.assertEqual(evaluate_route_map("RM_CONN", "10.20.5.0/26", maps, block)["verdict"], "permit")

    def test_bgp_prefix_detail(self):
        found = parse_bgp_prefix_detail(_read("nxos", "show_ip_bgp_prefix.txt"))
        self.assertTrue(found["present"])
        self.assertTrue(found["local"])
        self.assertEqual(found["advertised_to"], ["10.10.1.1", "10.1.0.22"])
        missing = parse_bgp_prefix_detail(_read("eos", "show_ip_bgp_prefix_not_found.txt"))
        self.assertFalse(missing["present"])

    def test_collect_command_errors(self):
        errors = collect_command_errors(
            [
                {"failed": False, "stdout": "ok"},
                {"skipped": True},
                {
                    "failed": True,
                    "msg": "command timeout triggered",
                    "invocation": {"module_args": {"command": "show lldp neighbors detail"}},
                },
                {"results": [{"failed": True, "msg": "bad", "item": "Eth1/1"}]},
                {
                    "failed": False,
                    "failed_when_result": False,
                    "msg": "command timeout triggered",
                    "invocation": {"module_args": {"command": "show vlan id 100"}},
                },
                {"failed": False, "stdout": "", "msg": "ignored when stdout exists"},
            ]
        )
        self.assertEqual(
            errors,
            [
                {"command": "show lldp neighbors detail", "error": "command timeout triggered"},
                {"command": "Eth1/1", "error": "bad"},
                {"command": "show vlan id 100", "error": "command timeout triggered"},
            ],
        )


if __name__ == "__main__":
    unittest.main()


class EndpointVerifyTests(unittest.TestCase):
    def test_baseline_and_ping_parsing(self):
        from vlan_lib.verify import endpoint_reachability, parse_endpoint_baseline

        csv_text = (
            "vlan_id,ip,mac,location,switch,interface,port_faces,role,flags,also_on\n"
            "300,10.30.0.2,00:23:04:ee:be:02,uplink_only,,,\"\",,\"gateway\",\"\"\n"
            "300,10.30.0.10,00:50:56:ab:30:10,edge,leaf1,Et3,\"esx\",compute,\"\",\"\"\n"
            "300,,00:50:56:ab:30:99,edge,leaf1,Et4,\"\",compute,\"\",\"\"\n"
            "301,10.31.0.10,00:50:56:ab:31:10,edge,leaf1,Et3,\"\",compute,\"\",\"\"\n"
        )
        self.assertEqual(parse_endpoint_baseline(csv_text, 300), ["10.30.0.10"])
        summary = endpoint_reachability(
            [
                {"item": "10.30.0.10", "stdout": "2 packets transmitted, 2 received, 0% packet loss"},
                {"item": "10.30.0.11", "stdout": "2 packets transmitted, 0 received, 100% packet loss"},
                {"item": "10.30.0.12", "failed": True, "msg": "timeout"},
                {"skipped": True},
            ]
        )
        self.assertEqual(summary, {"reachable": ["10.30.0.10"], "unreachable": ["10.30.0.11", "10.30.0.12"]})
