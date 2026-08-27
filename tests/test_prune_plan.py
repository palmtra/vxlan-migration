#!/usr/bin/env python3
"""Unit tests for SVI/static/BGP parsers and prune_plan retain logic."""

import os
import sys
import tempfile
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins", "filter"))

from vlan_filters import (  # noqa: E402
    build_device_prune_plan,
    load_service_from_directory,
    parse_bgp_neighbors,
    parse_ip_route_statics,
    parse_svi_details,
    service_vlan_ids,
)


SVI_SAMPLE = """
interface Vlan1759
   description 822_actv_windows_inside
   mtu 9214
   vrf forwarding v0000822b
   ip address 10.112.93.2/24
   ip virtual-router address 10.112.93.1
"""

ROUTE_SAMPLE = """
ip route vrf v0000822b 0.0.0.0/0 10.102.60.18 name aspira_static
ip route vrf v0000822a 10.210.93.0/24 10.102.60.1 name aspira_static
ip route vrf v0000822a 10.210.204.0/24 10.102.60.1 name aspira_static
"""

BGP_SAMPLE = """
router bgp 20034
   vlan 890
      rd 66.180.0.17:890
      route-target both 22015:890
      redistribute learned
   !
   vrf v0000822a
      rd 66.180.0.17:1822
      route-target import evpn 822:1
      neighbor 169.254.255.29 remote-as 65063
      neighbor 169.254.255.29 description AWS_DX
   !
   vrf v0000001c
      neighbor 10.202.9.117 remote-as 4210000822
      neighbor 10.202.9.117 update-source Vlan3369
      neighbor 10.202.9.117 description 822-chi-aspira-fw01/02
      neighbor 10.202.9.117 route-map v0000001c_fnts_routes_out out
      address-family ipv4
         neighbor 10.202.9.117 activate
"""


class PrunePlanParserTests(unittest.TestCase):
    def test_parse_svi_details(self):
        details = parse_svi_details(SVI_SAMPLE, "eos")
        self.assertTrue(details["present"])
        self.assertEqual(details["description"], "822_actv_windows_inside")
        self.assertEqual(details["mtu"], 9214)
        self.assertEqual(details["vrf"], "v0000822b")
        self.assertEqual(details["ip_addresses"], ["10.112.93.2/24"])
        self.assertEqual(details["virtual_router_addresses"], ["10.112.93.1"])

    def test_parse_ip_route_statics(self):
        routes = parse_ip_route_statics(ROUTE_SAMPLE)
        self.assertEqual(len(routes), 3)
        default_route = next(item for item in routes if item["prefix"] == "0.0.0.0/0")
        self.assertEqual(default_route["vrf"], "v0000822b")
        self.assertEqual(default_route["next_hop"], "10.102.60.18")
        self.assertEqual(default_route["name"], "aspira_static")

    def test_parse_bgp_neighbors(self):
        neighbors = parse_bgp_neighbors(BGP_SAMPLE)
        by_key = {(item["vrf"], item["neighbor"]): item for item in neighbors}
        self.assertIn(("v0000001c", "10.202.9.117"), by_key)
        peer = by_key[("v0000001c", "10.202.9.117")]
        self.assertEqual(peer["remote_as"], "4210000822")
        self.assertEqual(peer["update_source"], "Vlan3369")
        self.assertIn("v0000001c_fnts_routes_out out", peer["route_maps"])
        self.assertIn(("v0000822a", "169.254.255.29"), by_key)

    def test_build_device_prune_plan_retain(self):
        per_vlan = [
            {
                "vlan_id": 1759,
                "vlan_present": True,
                "svi_present": True,
                "svi_vrf": "v0000822b",
                "trunk_ports": ["Port-Channel2"],
            },
            {
                "vlan_id": 3883,
                "vlan_present": True,
                "svi_present": True,
                "svi_vrf": "v0000822a",
                "trunk_ports": ["Port-Channel2"],
            },
            {
                "vlan_id": 3369,
                "vlan_present": True,
                "svi_present": True,
                "svi_vrf": "v0000001c",
                "trunk_ports": [],
            },
        ]
        retain = {
            "vrfs": ["v0000822a"],
            "bgp_neighbors": [
                {"vrf": "v0000822a", "neighbor": "169.254.255.29"},
            ],
            "interfaces": ["Ethernet3.200"],
        }
        statics = parse_ip_route_statics(ROUTE_SAMPLE)
        neighbors = parse_bgp_neighbors(BGP_SAMPLE)
        plan = build_device_prune_plan(
            "aggpe01",
            per_vlan,
            retain,
            statics,
            neighbors,
        )

        ops = [action["op"] for action in plan["actions"]]
        self.assertIn("trunk_remove_vlans", ops)
        self.assertIn("no_vlan", ops)
        self.assertIn("no_interface_vlan", ops)
        self.assertIn("no_ip_route", ops)
        self.assertIn("no_bgp_neighbor", ops)

        peer_actions = [
            item
            for item in plan["actions"]
            if item["op"] == "no_bgp_neighbor" and item["neighbor"] == "10.202.9.117"
        ]
        self.assertEqual(len(peer_actions), 1)

        blocked_ops = {
            (item["op"], item.get("name") or item.get("vrf"))
            for item in plan["blocked_by_retain"]
        }
        self.assertIn(("no_vrf", "v0000822a"), blocked_ops)
        # Retained VRF statics must not be delete candidates.
        retained_static_prefixes = {
            item["prefix"]
            for item in plan["blocked_by_retain"]
            if item["op"] == "no_ip_route"
        }
        self.assertIn("10.210.93.0/24", retained_static_prefixes)
        action_static_prefixes = {
            item["prefix"] for item in plan["actions"] if item["op"] == "no_ip_route"
        }
        self.assertIn("0.0.0.0/0", action_static_prefixes)
        self.assertNotIn("10.210.93.0/24", action_static_prefixes)

    def test_load_service_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            dc_dir = os.path.join(tmp, "lisle")
            os.makedirs(dc_dir)
            path = os.path.join(dc_dir, "aspira_0000822.yml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "id: aspira_0000822\n"
                    "data_center: lisle\n"
                    "vlans:\n"
                    "  - id: 890\n"
                    "  - id: 1759\n"
                    "prune:\n"
                    "  retain:\n"
                    "    vrfs: [v0000822a]\n"
                )
            record = load_service_from_directory(tmp, "lisle", "aspira_0000822")
            self.assertEqual(record["id"], "aspira_0000822")
            self.assertEqual(service_vlan_ids(record), [890, 1759])
            self.assertEqual(record["prune"]["retain"]["vrfs"], ["v0000822a"])


if __name__ == "__main__":
    unittest.main()
