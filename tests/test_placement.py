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
        self.assertEqual(placement["prune_order"], ["mls01"])
        self.assertEqual(placement["by_hostname"]["mls01"]["role"], "prune_eligible")
        self.assertTrue(placement["by_hostname"]["mls01"]["source_gateway"])

    def test_source_gateway_is_pruned_before_other_leaves(self):
        placement = build_vlan_placement(
            {"id": 10, "name": "app", "service_type": "l2"},
            [
                _device(
                    "agg02",
                    switch_uplink_ports=["Po1"],
                    trunk_ports=["Po1"],
                ),
                _device(
                    "mls01",
                    svi_present=True,
                    switch_uplink_ports=["Po10"],
                    trunk_ports=["Po10"],
                ),
                _device("leaf01", access_ports=["Ethernet1"], svi_present=True),
            ],
        )
        self.assertEqual(placement["prune_order"], ["mls01", "agg02"])
        self.assertEqual(placement["participating_order"], ["leaf01"])
        self.assertTrue(placement["by_hostname"]["leaf01"]["source_gateway"])
        self.assertFalse(placement["by_hostname"]["leaf01"]["prune_eligible"])

    def test_protected_source_gateway_is_not_pruned(self):
        placement = build_vlan_placement(
            {"id": 1, "name": "keep", "protected_vlan": True},
            [_device("mls01", svi_present=True)],
        )
        self.assertEqual(placement["prune_order"], [])
        self.assertEqual(placement["by_hostname"]["mls01"]["role"], "protected")
        self.assertEqual(placement["source_gateway_order"], ["mls01"])

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


class AssociatedRouteTests(unittest.TestCase):
    def test_bgp_network_and_aggregate_are_parsed(self):
        from vlan_lib.parsers import parse_bgp_context

        context = parse_bgp_context(
            """
router bgp 65001
   vrf TENANT1
      rd 10.1.0.17:1
      network 10.10.100.0/24
      aggregate-address 10.10.0.0/16 summary-only
      redistribute connected
      neighbor 10.10.100.5 remote-as 65099
      neighbor 10.10.100.5 description firewall
   network 10.9.0.0 255.255.255.0
"""
        )
        tenant = next(item for item in context["vrfs"] if item["name"] == "TENANT1")
        self.assertEqual(tenant["networks"], ["10.10.100.0/24"])
        self.assertEqual(tenant["aggregates"], ["10.10.0.0/16"])
        self.assertIn("connected", tenant["redistribute"])
        default = next(item for item in context["vrfs"] if item["name"] == "default")
        self.assertEqual(default["networks"], ["10.9.0.0/24"])

    def test_vlan_associated_routes_skip_fabric_default(self):
        per_vlan = {
            "vlan_id": 100,
            "os_family": "nxos",
            "vlan_present": True,
            "svi_present": True,
            "svi_vrf": "TENANT1",
            "svi_details": {
                "ip_addresses": ["10.10.100.2/24"],
                "virtual_router_addresses": ["10.10.100.1"],
                "hsrp_addresses": [],
            },
            "compute_ports": [],
            "access_ports": [],
            "unknown_ports": [],
            "switch_uplink_ports": [],
            "trunk_ports": [],
            "endpoint_mac_entries": [],
            "mac_entries": [],
            "port_attachments": [],
        }
        hostvars = {
            "mls01": {
                "_vlan_discovery": {
                    "hostname": "mls01",
                    "os_family": "nxos",
                    "per_vlan": [per_vlan],
                    "static_routes": [
                        {
                            "vrf": "TENANT1",
                            "prefix": "192.0.2.0/24",
                            "next_hop": "10.10.100.5",
                            "name": "firewall",
                        },
                        {
                            "vrf": "default",
                            "prefix": "0.0.0.0/0",
                            "next_hop": "10.1.0.1",
                            "name": "fabric_default",
                        },
                    ],
                    "bgp_neighbors": [
                        {
                            "vrf": "TENANT1",
                            "neighbor": "10.10.100.5",
                            "remote_as": "65099",
                            "update_source": "Vlan100",
                            "description": "firewall",
                        },
                        {
                            "vrf": "default",
                            "neighbor": "10.1.0.21",
                            "remote_as": "65001",
                            "update_source": "Loopback0",
                            "description": "spine",
                        },
                    ],
                    "bgp_vrfs": [
                        {
                            "name": "TENANT1",
                            "networks": ["10.10.100.0/24"],
                            "aggregates": ["10.10.0.0/16"],
                            "redistribute": ["connected"],
                        }
                    ],
                    "prune_plan": {},
                }
            }
        }
        report = build_vlan_discovery_reports(
            [{"id": 100, "name": "legacy_web", "service_type": "l3", "vrf": "TENANT1"}],
            ["mls01"],
            hostvars,
        )[0]
        associated = report["deployment_model"]["routing"]["associated"]
        statics = associated["static_routes"]
        self.assertEqual(len(statics), 1)
        self.assertEqual(statics[0]["next_hop"], "10.10.100.5")
        self.assertEqual(statics[0]["name"], "firewall")
        kinds = {(item["kind"], item["prefix"] or item["method"]) for item in associated["bgp_networks"]}
        self.assertIn(("network", "10.10.100.0/24"), kinds)
        self.assertIn(("aggregate", "10.10.0.0/16"), kinds)
        self.assertIn(("redistribute", "connected"), kinds)
        peers = [item["neighbor"] for item in associated["bgp_neighbors"]]
        self.assertEqual(peers, ["10.10.100.5"])
        self.assertNotIn("no_ip_route", {action["op"] for plan in report["prune_plans"] for action in plan["actions"]})

    def test_static_route_name_tag_matches_case_insensitively(self):
        per_vlan = {
            "vlan_id": 100,
            "os_family": "eos",
            "vlan_present": True,
            "svi_present": True,
            "svi_vrf": "TENANT1",
            "svi_details": {"ip_addresses": ["10.10.100.2/24"]},
            "compute_ports": [],
            "access_ports": [],
            "unknown_ports": [],
            "switch_uplink_ports": [],
            "trunk_ports": [],
            "endpoint_mac_entries": [],
            "mac_entries": [],
            "port_attachments": [],
        }
        hostvars = {
            "mls01": {
                "_vlan_discovery": {
                    "hostname": "mls01",
                    "os_family": "eos",
                    "per_vlan": [per_vlan],
                    "static_routes": [
                        {
                            "vrf": "default",
                            "prefix": "1.1.1.0/24",
                            "next_hop": "2.2.2.2",
                            "name": "cust_a",
                        },
                        {
                            "vrf": "default",
                            "prefix": "9.9.9.0/24",
                            "next_hop": "3.3.3.3",
                            "name": "other_customer",
                        },
                        {
                            "vrf": "default",
                            "prefix": "8.8.8.0/24",
                            "next_hop": "4.4.4.4",
                            "name": "cust_a_extra",
                        },
                        {
                            "vrf": "default",
                            "prefix": "0.0.0.0/0",
                            "next_hop": "10.1.0.1",
                            "name": "fabric_default",
                        },
                    ],
                    "prune_plan": {},
                }
            }
        }
        vlan = {
            "id": 100,
            "name": "legacy_web",
            "service_type": "l3",
            "vrf": "TENANT1",
            "static_route_tags": ["CUST_A"],
        }
        report = build_vlan_discovery_reports([vlan], ["mls01"], hostvars)[0]
        statics = report["deployment_model"]["routing"]["associated"]["static_routes"]
        by_name = {item["name"]: item for item in statics}
        self.assertIn("cust_a", by_name)
        self.assertNotIn("other_customer", by_name)
        self.assertNotIn("cust_a_extra", by_name)
        self.assertNotIn("fabric_default", by_name)
        self.assertIn("CUST_A", by_name["cust_a"]["reason"])
        self.assertEqual(
            report["deployment_model"]["routing"]["static_route_tags"],
            ["CUST_A"],
        )

        untagged = dict(vlan)
        untagged.pop("static_route_tags")
        missed = build_vlan_discovery_reports([untagged], ["mls01"], hostvars)[0]
        missed_names = [
            item["name"]
            for item in missed["deployment_model"]["routing"]["associated"]["static_routes"]
        ]
        self.assertNotIn("cust_a", missed_names)
        self.assertNotIn("static_route_tags", missed["deployment_model"]["routing"])

    def test_participating_leaf_carries_asn_rd_and_vtep(self):
        vlan = {"id": 2903, "name": "external", "service_type": "l2"}
        devices = [
            _device(
                "oma01-blf01",
                compute_ports=["Ethernet1"],
                bgp_as="30452",
                router_id="66.180.0.3",
                loopback0="66.180.0.3",
                loopback1="172.16.0.3",
            ),
            _device(
                "oma-ce-sw01",
                compute_ports=["Ethernet2"],
                bgp_as="4200000106",
                loopback0="66.180.0.46",
                loopback1="172.16.0.15",
                bgp_vlan_blocks=[{"vlan_id": 2903, "rd": "66.180.0.46:2903"}],
            ),
        ]
        model = build_vlan_placement(vlan, devices)["deployment_model"]
        leaves = {
            item["hostname"]: item
            for item in model["placement"]["participating_leafs"]
        }
        self.assertEqual(leaves["oma01-blf01"]["asn"], "30452")
        self.assertEqual(leaves["oma01-blf01"]["router_id"], "66.180.0.3")
        self.assertEqual(leaves["oma01-blf01"]["rd"], "66.180.0.3:2903")
        self.assertEqual(leaves["oma01-blf01"]["vtep"], "172.16.0.3")
        self.assertEqual(leaves["oma-ce-sw01"]["asn"], "4200000106")
        self.assertEqual(leaves["oma-ce-sw01"]["router_id"], "66.180.0.46")
        self.assertEqual(leaves["oma-ce-sw01"]["rd"], "66.180.0.46:2903")
        self.assertEqual(leaves["oma-ce-sw01"]["vtep"], "172.16.0.15")

    def test_host_vtep_bgp_uses_discovery_not_management_address(self):
        from vlan_lib.placement import host_vtep_bgp

        learned = host_vtep_bgp(
            {
                "ansible_host": "10.9.9.9",
                "bgp_as": "1",
                "router_id": "1.1.1.1",
                "_vlan_discovery": {
                    "bgp_as": "30452",
                    "router_id": "66.180.0.3",
                    "loopback0": "66.180.0.3",
                    "loopback1": "172.16.0.3",
                },
            }
        )
        self.assertEqual(learned["asn"], "30452")
        self.assertEqual(learned["router_id"], "66.180.0.3")

        fallback = host_vtep_bgp(
            {
                "ansible_host": "10.9.9.9",
                "bgp_as": "4200000106",
                "router_id": "66.180.0.46",
                "_vlan_discovery": {"bgp_as": "", "router_id": ""},
            }
        )
        self.assertEqual(fallback["asn"], "4200000106")
        self.assertEqual(fallback["router_id"], "66.180.0.46")
        self.assertNotIn("10.9.9.9", fallback.values())

        from collections.abc import Mapping

        class _HostVars(Mapping):
            def __init__(self, data):
                self._data = data

            def __getitem__(self, key):
                return self._data[key]

            def __iter__(self):
                return iter(self._data)

            def __len__(self):
                return len(self._data)

        mapped = host_vtep_bgp(
            _HostVars(
                {
                    "ansible_host": "10.9.9.9",
                    "_vlan_discovery": _HostVars(
                        {"bgp_as": "30452", "loopback0": "66.180.0.4"}
                    ),
                }
            )
        )
        self.assertEqual(mapped["asn"], "30452")
        self.assertEqual(mapped["router_id"], "66.180.0.4")

    def test_virtual_gateway_uses_prefix_length(self):
        from vlan_lib.placement import virtual_gateway_address

        self.assertEqual(
            virtual_gateway_address("66.180.1.1", ["66.180.1.0/27"]),
            "66.180.1.1/27",
        )


if __name__ == "__main__":
    unittest.main()
