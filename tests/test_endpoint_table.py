#!/usr/bin/env python3
"""Unit tests for endpoint location: ranking, conflicts, ARP-port fallback."""

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

from vlan_lib.reports import build_action_items, build_endpoint_table  # noqa: E402
from vlan_lib.verify import parse_endpoint_baseline  # noqa: E402

GATEWAY = {"svis": [{"ip_addresses": ["10.30.0.2/24"], "gateway_addresses": ["10.30.0.1"]}]}


def mac(addr, iface, role, **extra):
    return dict({"mac": addr, "interface": iface, "attachment_role": role}, **extra)


def device(hostname, macs=None, arp=None, ports=None, peer_link=None):
    return {
        "hostname": hostname,
        "svi_present": bool(arp),
        "mac_entries": macs or [],
        "arp_entries": arp or [],
        "port_attachments": ports or [],
        "peer_link_mac_entries": peer_link or [],
    }


def rows_by_ip(table):
    return {row["ip"]: row for row in table["endpoints"]}


class EdgeRankingTests(unittest.TestCase):
    def test_compute_port_beats_unclassified_port_in_any_order(self):
        unknown = device("sw-a", [mac("00:50:56:00:00:10", "Et7", "unknown")])
        compute = device("sw-b", [mac("00:50:56:00:00:10", "Et3", "compute", description="esx-01")])
        gw = device("mls-01", arp=[{"ip": "10.30.0.10", "mac": "00:50:56:00:00:10"}])
        for order in ([unknown, compute, gw], [compute, unknown, gw]):
            row = rows_by_ip(build_endpoint_table(order, GATEWAY))["10.30.0.10"]
            self.assertEqual((row["hostname"], row["interface"], row["role"]), ("sw-b", "Et3", "compute"))
            self.assertEqual(row["also_on"], ["sw-a Et7"])
            self.assertEqual(row["flags"], [])

    def test_downstream_switch_port_is_the_path_not_a_conflict(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:20", "Po3", "downstream_switch")]),
                device("acc-01", [mac("00:50:56:00:00:20", "Et10", "compute")]),
                device("mls-01", arp=[{"ip": "10.30.0.20", "mac": "00:50:56:00:00:20"}]),
            ],
            GATEWAY,
        )
        row = rows_by_ip(table)["10.30.0.20"]
        self.assertEqual((row["hostname"], row["interface"]), ("acc-01", "Et10"))
        self.assertNotIn("multiple_edge_ports", row["flags"])

    def test_same_port_channel_on_both_peers_is_dual_homed(self):
        table = build_endpoint_table(
            [
                device("leaf-02", [mac("00:50:56:00:00:30", "Port-Channel10", "compute")]),
                device("leaf-01", [mac("00:50:56:00:00:30", "Po10", "compute")]),
                device("mls-01", arp=[{"ip": "10.30.0.30", "mac": "00:50:56:00:00:30"}]),
            ],
            GATEWAY,
        )
        row = rows_by_ip(table)["10.30.0.30"]
        self.assertEqual((row["hostname"], row["interface"]), ("leaf-01", "Po10"))
        self.assertEqual(row["flags"], ["dual_homed"])
        self.assertEqual(table["summary"]["dual_homed"], 1)
        self.assertEqual(table["summary"]["multiple_edge_ports"], 0)

    def test_two_device_facing_ports_are_a_conflict(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:40", "Et3", "compute")]),
                device("leaf-02", [mac("00:50:56:00:00:40", "Et9", "compute")]),
                device("mls-01", arp=[{"ip": "10.30.0.40", "mac": "00:50:56:00:00:40"}]),
            ],
            GATEWAY,
        )
        row = rows_by_ip(table)["10.30.0.40"]
        self.assertIn("multiple_edge_ports", row["flags"])
        self.assertEqual(row["also_on"], ["leaf-02 Et9"])
        items = build_action_items({"endpoint_table": table, "switches_vlan_defined": ["leaf-01"]})
        self.assertTrue(any("00:50:56:00:00:40 on leaf-01 Et3, leaf-02 Et9" in item for item in items))

    def test_mac_moves_are_flagged(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:41", "Et3", "compute", moves=4, last_move="0:00:09 ago")]),
                device("mls-01", arp=[{"ip": "10.30.0.41", "mac": "00:50:56:00:00:41"}]),
            ],
            GATEWAY,
        )
        self.assertEqual(rows_by_ip(table)["10.30.0.41"]["flags"], ["mac_moved"])


class ArpPortFallbackTests(unittest.TestCase):
    def test_aged_out_mac_is_located_by_the_eos_arp_port(self):
        ports = [
            {"interface": "Ethernet5", "role": "compute", "description": "esx-05", "neighbor": ""},
            {"interface": "Port-Channel1", "role": "switch_uplink", "description": "", "neighbor": ""},
        ]
        gw = device(
            "eos-gw-01",
            arp=[
                {"ip": "10.30.0.50", "mac": "00:50:56:00:00:50", "port": "Et5"},
                {"ip": "10.30.0.51", "mac": "00:50:56:00:00:51", "port": "Port-Channel1"},
                {"ip": "10.30.0.52", "mac": "00:50:56:00:00:52", "port": ""},
            ],
            ports=ports,
        )
        table = build_endpoint_table([gw], GATEWAY)
        rows = rows_by_ip(table)
        located = rows["10.30.0.50"]
        self.assertEqual(
            (located["location"], located["hostname"], located["interface"], located["port_label"]),
            ("edge", "eos-gw-01", "Ethernet5", "esx-05"),
        )
        self.assertEqual(located["flags"], ["located_by_arp"])
        self.assertEqual(rows["10.30.0.51"]["location"], "uplink_only")
        self.assertEqual(rows["10.30.0.51"]["uplink_seen_on"], ["eos-gw-01"])
        self.assertEqual(rows["10.30.0.52"]["location"], "not_in_mac_table")
        summary = table["summary"]
        self.assertEqual(
            (summary["active_ips"], summary["located_by_arp"], summary["uplink_only"], summary["not_in_mac_table"]),
            (3, 1, 1, 1),
        )

    def test_mac_table_wins_over_arp_port(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:53", "Et3", "compute")]),
                device(
                    "eos-gw-01",
                    arp=[{"ip": "10.30.0.53", "mac": "00:50:56:00:00:53", "port": "Port-Channel1"}],
                    ports=[{"interface": "Port-Channel1", "role": "switch_uplink"}],
                ),
            ],
            GATEWAY,
        )
        row = rows_by_ip(table)["10.30.0.53"]
        self.assertEqual((row["hostname"], row["interface"], row["flags"]), ("leaf-01", "Et3", []))


class ArpConflictTests(unittest.TestCase):
    def test_ip_answered_by_two_macs_is_flagged_not_dropped(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:61", "Et3", "compute")]),
                device("mls-01", arp=[{"ip": "10.30.0.60", "mac": "00:50:56:00:00:61"}]),
                device("mls-02", arp=[{"ip": "10.30.0.60", "mac": "00:50:56:00:00:62"}]),
            ],
            GATEWAY,
        )
        row = rows_by_ip(table)["10.30.0.60"]
        # Tie on how many gateways agree: the MAC that is located wins.
        self.assertEqual(row["mac"], "00:50:56:00:00:61")
        self.assertEqual(row["other_macs"], ["00:50:56:00:00:62"])
        self.assertEqual(row["arp_from"], ["mls-01", "mls-02"])
        self.assertIn("ip_conflict", row["flags"])
        self.assertEqual(table["summary"]["ip_conflicts"], 1)
        self.assertEqual(table["summary"]["active_ips"], 1)
        items = build_action_items({"endpoint_table": table, "switches_vlan_defined": ["leaf-01"]})
        self.assertTrue(
            any("10.30.0.60 (00:50:56:00:00:61, 00:50:56:00:00:62)" in item for item in items), items
        )

    def test_one_mac_with_several_ips_is_marked_shared(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:a0:c9:00:00:70", "Et20", "network_service")]),
                device(
                    "mls-01",
                    arp=[
                        {"ip": "10.30.0.70", "mac": "00:a0:c9:00:00:70"},
                        {"ip": "10.30.0.71", "mac": "00:a0:c9:00:00:70"},
                    ],
                ),
            ],
            GATEWAY,
        )
        rows = rows_by_ip(table)
        self.assertEqual(rows["10.30.0.70"]["flags"], ["shared_mac"])
        self.assertEqual(rows["10.30.0.71"]["flags"], ["shared_mac"])
        self.assertEqual(table["summary"]["active_ips"], 2)

    def test_gateway_rows_stay_out_of_the_host_count(self):
        table = build_endpoint_table(
            [device("mls-01", arp=[{"ip": "10.30.0.2", "mac": "00:00:0c:9f:f0:64"}])], GATEWAY
        )
        row = rows_by_ip(table)["10.30.0.2"]
        self.assertEqual(row["flags"], ["fhrp_mac", "gateway"])
        self.assertEqual((table["summary"]["rows"], table["summary"]["active_ips"]), (1, 0))


class UplinkOnlyMacTests(unittest.TestCase):
    def test_trunk_only_macs_without_arp_are_listed(self):
        table = build_endpoint_table(
            [
                device("leaf-01", [mac("00:50:56:00:00:80", "Po1", "switch_uplink")]),
                device("mls-02", peer_link=[{"mac": "00:50:56:00:00:81", "interface": "vPC Peer-Link"}]),
            ],
            GATEWAY,
        )
        rows = {row["mac"]: row for row in table["mac_only"]}
        self.assertEqual(rows["00:50:56:00:00:80"]["location"], "uplink_only")
        self.assertEqual(rows["00:50:56:00:00:80"]["uplink_seen_on"], ["leaf-01"])
        self.assertEqual(rows["00:50:56:00:00:81"]["uplink_seen_on"], ["mls-02"])
        self.assertEqual((table["summary"]["mac_only"], table["summary"]["mac_only_uplink"]), (0, 2))
        items = build_action_items({"endpoint_table": table, "switches_vlan_defined": ["leaf-01"]})
        self.assertTrue(any("2 MAC(s) with no ARP entry are only seen" in item for item in items))


class ParseCheckActionItemTests(unittest.TestCase):
    def test_unread_rows_become_action_items(self):
        report = {
            "devices": [
                {
                    "hostname": "nxos-mls-01",
                    "parse_checks": {
                        "mac_table": {"ok": False, "problems": ["1 MAC table row(s) not understood, e.g. 'x'"]},
                        "arp": {"ok": True, "problems": []},
                    },
                }
            ]
        }
        items = build_action_items(report)
        self.assertEqual(len([item for item in items if "was not fully read" in item]), 1)
        self.assertIn("nxos-mls-01: the MAC table output was not fully read", items[0])


class BaselineCompatibilityTests(unittest.TestCase):
    def test_baseline_reader_takes_flagged_hosts_and_skips_gateways(self):
        csv_text = (
            "vlan_id,ip,mac,location,switch,interface,port_faces,role,flags,also_on,arp_from,uplink_seen_on,other_macs\n"
            '300,10.30.0.2,00:23:04:ee:be:02,uplink_only,,,"",,"gateway","","mls-02","leaf-01",""\n'
            '300,10.30.0.50,00:50:56:00:00:50,edge,eos-gw-01,Ethernet5,"esx-05",compute,"located_by_arp","","eos-gw-01","",""\n'
            '300,10.30.0.60,00:50:56:00:00:61,edge,leaf-01,Et3,"",compute,"ip_conflict","","mls-01;mls-02","","00:50:56:00:00:62"\n'
        )
        self.assertEqual(parse_endpoint_baseline(csv_text, 300), ["10.30.0.50", "10.30.0.60"])


if __name__ == "__main__":
    unittest.main()
