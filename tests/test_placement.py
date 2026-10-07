#!/usr/bin/env python3
"""Placement: participating leaves, prune-eligible leaves, L2/L3 only."""

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

from vlan_lib.placement import build_vlan_placement  # noqa: E402
from vlan_lib.reports import build_vlan_discovery_reports  # noqa: E402


def _device(hostname, **kwargs):
    device = {
        "hostname": hostname,
        "os_family": "eos",
        "vlan_present": True,
        "svi_present": False,
        "svi_vrf": "",
        "svi_details": {},
        "compute_ports": [],
        "access_ports": [],
        "unknown_ports": [],
        "switch_uplink_ports": [],
        "trunk_ports": [],
        "endpoint_mac_entries": [],
        "mac_entries": [],
        "port_attachments": [],
        "arp_entries": [],
        "bgp_as": "",
    }
    device.update(kwargs)
    return device


class PlacementTests(unittest.TestCase):
    def test_fnis_pattern_is_normal_l3(self):
        vlan = {
            "id": 100,
            "name": "fnis",
            "service_type": "l3",
            "vrf": "default",
            "gateway_leafs": ["cma01-blf01", "cma01-blf02"],
        }
        devices = [
            _device("cma01-blf01"),
            _device("cma01-blf02"),
            _device("fntc-aggacc-sw05", compute_ports=["Ethernet1"]),
            _device("fntc-aggacc-sw06", compute_ports=["Ethernet2"]),
            _device(
                "fntc-agg-sw01",
                switch_uplink_ports=["Port-Channel1"],
                trunk_ports=["Port-Channel1"],
                mac_entries=[
                    {
                        "mac": "0011.2233.4455",
                        "interface": "Port-Channel1",
                        "attachment_role": "switch_uplink",
                        "port_role": "trunk",
                    }
                ],
            ),
        ]
        placement = build_vlan_placement(vlan, devices)
        self.assertEqual(placement["migration_type"], "l3")
        self.assertEqual(
            placement["participating_order"],
            [
                "cma01-blf01",
                "cma01-blf02",
                "fntc-aggacc-sw05",
                "fntc-aggacc-sw06",
            ],
        )
        self.assertEqual(
            placement["gateway_order"], ["cma01-blf01", "cma01-blf02"]
        )
        self.assertEqual(placement["prune_order"], ["fntc-agg-sw01"])
        self.assertNotIn("l2_l3", placement["migration_reason"])

    def test_svi_is_source_gateway_not_fabric_gateway(self):
        vlan = {"id": 10, "name": "app", "service_type": "l2"}
        devices = [
            _device(
                "mls01",
                svi_present=True,
                svi_vrf="TENANT",
                switch_uplink_ports=["Po10"],
                trunk_ports=["Po10"],
            )
        ]
        placement = build_vlan_placement(vlan, devices)
        self.assertEqual(placement["migration_type"], "l2")
        self.assertEqual(placement["gateway_order"], [])
        self.assertEqual(placement["source_gateway_order"], ["mls01"])
        self.assertEqual(placement["participating_order"], [])
        self.assertEqual(placement["prune_order"], [])
        self.assertEqual(placement["by_hostname"]["mls01"]["role"], "source_gateway")

    def test_deprecated_l2_l3_without_gateway_leafs_is_l2(self):
        placement = build_vlan_placement(
            {"id": 1, "name": "old", "service_type": "l2_l3"},
            [_device("leaf01", compute_ports=["Ethernet1"])],
        )
        self.assertEqual(placement["migration_type"], "l2")
        self.assertEqual(placement["participating_order"], ["leaf01"])
        self.assertEqual(placement["gateway_order"], [])

    def test_uplink_macs_do_not_participate(self):
        placement = build_vlan_placement(
            {"id": 1, "name": "transit", "service_type": "l2"},
            [
                _device(
                    "agg01",
                    switch_uplink_ports=["Po1"],
                    trunk_ports=["Po1"],
                    mac_entries=[
                        {
                            "mac": "aa:bb:cc:dd:ee:ff",
                            "interface": "Po1",
                            "attachment_role": "switch_uplink",
                        }
                    ],
                )
            ],
        )
        self.assertEqual(placement["by_hostname"]["agg01"]["role"], "prune_eligible")

    def test_protected_vlan_is_not_prune_eligible(self):
        placement = build_vlan_placement(
            {"id": 1, "name": "keep", "service_type": "l2", "protected_vlan": True},
            [_device("agg01", trunk_ports=["Po1"], switch_uplink_ports=["Po1"])],
        )
        self.assertEqual(placement["prune_order"], [])
        self.assertEqual(placement["by_hostname"]["agg01"]["role"], "protected")

    def test_fabric_interconnect_does_not_participate(self):
        placement = build_vlan_placement(
            {"id": 1, "name": "fi", "service_type": "l2"},
            [
                _device(
                    "tor01",
                    port_attachments=[
                        {
                            "interface": "Ethernet10",
                            "role": "transit",
                            "description": "fabric interconnect A",
                        }
                    ],
                )
            ],
        )
        self.assertEqual(placement["by_hostname"]["tor01"]["role"], "prune_eligible")
        self.assertTrue(
            any(
                "fabric interconnect" in item
                for item in placement["by_hostname"]["tor01"]["nonqualifying"]
            )
        )

    def test_prune_eligible_switch_keeps_prune_actions(self):
        per_vlan = {
            "vlan_id": 50,
            "os_family": "eos",
            "vlan_present": True,
            "svi_present": False,
            "switch_uplink_ports": ["Port-Channel10"],
            "trunk_ports": ["Port-Channel10"],
            "compute_ports": [],
            "access_ports": [],
            "unknown_ports": [],
            "endpoint_mac_entries": [],
            "mac_entries": [
                {
                    "mac": "00:11:22:33:44:55",
                    "interface": "Port-Channel10",
                    "attachment_role": "switch_uplink",
                    "port_role": "trunk",
                }
            ],
            "port_attachments": [],
            "svi_details": {},
        }
        hostvars = {
            "agg-sw01": {
                "_vlan_discovery": {
                    "hostname": "agg-sw01",
                    "os_family": "eos",
                    "per_vlan": [per_vlan],
                    "prune_plan": {},
                }
            }
        }
        reports = build_vlan_discovery_reports(
            [{"id": 50, "name": "transit", "service_type": "l2"}],
            ["agg-sw01"],
            hostvars,
        )
        report = reports[0]
        self.assertEqual(report["migration_type"], "l2")
        self.assertEqual(
            report["deployment_model"]["placement"]["prune_eligible_leafs"],
            [{"hostname": "agg-sw01"}],
        )
        plan = report["prune_plans"][0]
        self.assertEqual(plan["status"], "candidate")
        ops = {action["op"] for action in plan["actions"]}
        self.assertIn("trunk_remove_vlans", ops)
        self.assertIn("no_vlan", ops)
        self.assertNotIn("deployment_model", report["discovery_export"])


if __name__ == "__main__":
    unittest.main()
