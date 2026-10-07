#!/usr/bin/env python3
"""CLI fixture tests for EOS/NXOS VLAN, MAC, ARP, SVI, and trunk parsers."""

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
sys.path.insert(0, os.path.join(_REPO, "plugins", "filter"))

from vlan_filters import (  # noqa: E402
    analyze_trunk_vlan_carriage,
    build_device_prune_plan,
    build_vlan_discovery_reports,
    extract_vlan_targeted_discovery,
    parse_bgp_context,
    parse_ip_route_statics,
    parse_svi_details,
    parse_trunk_interfaces,
    parse_vlan_id_ports,
    resolve_trunk_allowed_vlans,
    trunk_interface_names,
)


def _read(*parts):
    with open(os.path.join(_FIXTURES, *parts), encoding="utf-8") as handle:
        return handle.read()


class EosCliFixtureTests(unittest.TestCase):
    def test_vlan_id_ports_and_name(self):
        result = parse_vlan_id_ports(
            _read("eos", "show_vlan_id_100.txt"),
            100,
            "eos",
            ["Port-Channel1", "Port-Channel3"],
            ["Port-Channel3"],
        )
        self.assertTrue(result["vlan_present"])
        self.assertEqual(result["vlan_name_on_box"], "WEB_VXLAN")
        self.assertEqual(result["access_ports"], ["Et3", "Et4"])
        self.assertEqual(result["trunk_ports"], ["Po1"])
        self.assertEqual(result["exempt_trunk_ports"], ["Po3"])

    def test_svi_details(self):
        details = parse_svi_details(
            _read("eos", "show_run_interface_vlan100.txt"), "eos"
        )
        self.assertTrue(details["present"])
        self.assertEqual(details["ip_addresses"], ["10.10.100.2/24"])
        self.assertEqual(details["virtual_router_addresses"], ["10.10.100.1"])
        self.assertEqual(details["vrf"], "default")

    def test_extract_end_to_end(self):
        trunks = parse_trunk_interfaces(
            _read("eos", "show_interfaces_trunk.txt"), "eos"
        )
        names = [item["interface"] for item in trunks]
        record = extract_vlan_targeted_discovery(
            {"id": 100, "name": "legacy_web", "vrf": "default"},
            {
                "platform": "eos",
                "vlan_id_output": _read("eos", "show_vlan_id_100.txt"),
                "svi_output": _read("eos", "show_run_interface_vlan100.txt"),
                "mac_output": _read("eos", "show_mac_vlan_100.txt"),
                "arp_output": _read("eos", "show_ip_arp_vlan100.txt"),
                "trunk_interface_names": names,
                "exempt_trunk_interface_names": ["Po3"],
                "trunk_summaries": trunks,
            },
        )
        self.assertEqual(record["vlan_name_on_box"], "WEB_VXLAN")
        self.assertEqual(len(record["mac_entries"]), 3)
        self.assertEqual(len(record["arp_entries"]), 3)
        self.assertTrue(record["svi_present"])
        self.assertEqual(record["os_family"], "eos")
        po1 = [item for item in record["trunk_allowed"] if item["interface"] == "Po1"]
        self.assertEqual(len(po1), 1)
        self.assertIn("100", po1[0]["allowed_summary"])


class NxosCliFixtureTests(unittest.TestCase):
    def test_show_interface_trunk_uses_allowed_table(self):
        trunks = parse_trunk_interfaces(
            _read("nxos", "show_interface_trunk.txt"), "nxos"
        )
        by_iface = {item["interface"]: item["allowed_summary"] for item in trunks}
        self.assertEqual(by_iface["Po10"], "1-100,200")
        self.assertEqual(by_iface["Po1"], "1-4094")
        self.assertNotIn("trunking", by_iface.get("Po10", ""))
        self.assertEqual(
            set(trunk_interface_names(_read("nxos", "show_interface_trunk.txt"), "nxos")),
            {"Eth1/1", "Po1", "Po10"},
        )

    def test_svi_dotted_mask_and_hsrp(self):
        details = parse_svi_details(
            _read("nxos", "show_run_interface_vlan100.txt"), "nxos"
        )
        self.assertEqual(details["vrf"], "TENANT1")
        self.assertEqual(details["ip_addresses"], ["10.10.100.2/24"])
        self.assertEqual(details["hsrp_addresses"], ["10.10.100.1"])
        self.assertEqual(
            details["hsrp_groups"], [{"group": 1, "ip": "10.10.100.1"}]
        )

    def test_l3_bgp_split_stanzas_and_vrf_context_statics(self):
        context = parse_bgp_context(_read("nxos", "show_run_bgp.txt"))
        self.assertEqual(context["bgp_as"], "65001")
        by_key = {(item["vrf"], item["neighbor"]): item for item in context["neighbors"]}
        self.assertEqual(by_key[("default", "10.1.0.21")]["remote_as"], "65001")
        self.assertEqual(by_key[("TENANT1", "10.10.1.1")]["remote_as"], "65099")
        self.assertEqual(by_key[("TENANT1", "10.10.1.1")]["update_source"], "Vlan100")
        vrf = next(item for item in context["vrfs"] if item["name"] == "TENANT1")
        self.assertEqual(vrf["rd"], "auto")
        self.assertTrue(any("both auto" in item for item in vrf["route_targets"]))

        routes = parse_ip_route_statics(_read("nxos", "show_run_vrf_context.txt"))
        by_prefix = {(item["vrf"], item["prefix"]): item for item in routes}
        self.assertEqual(by_prefix[("TENANT1", "0.0.0.0/0")]["next_hop"], "10.10.1.1")
        self.assertEqual(by_prefix[("TENANT1", "10.20.0.0/24")]["next_hop"], "10.10.1.2")
        self.assertEqual(by_prefix[("default", "0.0.0.0/0")]["name"], "fabric_default")

    def test_eos_bgp_vlan_block(self):
        context = parse_bgp_context(_read("eos", "show_run_bgp.txt"))
        vlan_100 = next(item for item in context["vlan_blocks"] if item["vlan_id"] == 100)
        self.assertEqual(vlan_100["rd"], "10.1.0.17:100")
        self.assertIn("both 65001:100", vlan_100["route_targets"])
        tenant = next(item for item in context["vrfs"] if item["name"] == "TENANT1")
        self.assertEqual(tenant["rd"], "10.1.0.17:1")

    def test_extract_end_to_end(self):
        trunks = parse_trunk_interfaces(
            _read("nxos", "show_interface_trunk.txt"), "nxos"
        )
        names = [item["interface"] for item in trunks]
        record = extract_vlan_targeted_discovery(
            {"id": 100, "name": "vlan_100", "vrf": "default"},
            {
                "platform": "nxos",
                "vlan_id_output": _read("nxos", "show_vlan_id_100.txt"),
                "svi_output": _read("nxos", "show_run_interface_vlan100.txt"),
                "mac_output": _read("nxos", "show_mac_vlan_100.txt"),
                "arp_output": _read("nxos", "show_ip_arp_vlan100.txt"),
                "trunk_interface_names": names,
                "exempt_trunk_interface_names": ["Po1"],
                "trunk_summaries": trunks,
            },
        )
        self.assertEqual(record["vlan_name_on_box"], "WEB_VXLAN")
        self.assertEqual(record["trunk_ports"], ["Po10"])
        self.assertEqual(record["exempt_trunk_ports"], ["Po1"])
        self.assertEqual(len(record["mac_entries"]), 2)
        self.assertEqual(len(record["arp_entries"]), 2)
        self.assertEqual(record["svi_details"]["hsrp_addresses"], ["10.10.100.1"])

    def test_cumulative_allow_list(self):
        config = _read("nxos", "trunk_allowed_cumulative.txt")
        resolved = resolve_trunk_allowed_vlans(config)
        self.assertEqual(resolved["mode"], "allow_list")
        self.assertIn(100, resolved["vlans"])
        self.assertIn(200, resolved["vlans"])
        self.assertNotIn(50, resolved["vlans"])
        carriage = analyze_trunk_vlan_carriage(
            100, [{"interface": "Po10", "config": config}]
        )
        self.assertTrue(carriage[0]["vlan_carried"])
        carriage_50 = analyze_trunk_vlan_carriage(
            50, [{"interface": "Po10", "config": config}]
        )
        self.assertFalse(carriage_50[0]["vlan_carried"])


class DiscoveryReportSsotTests(unittest.TestCase):
    def test_ssot_and_human_prune_from_fixtures(self):
        eos_trunks = parse_trunk_interfaces(
            _read("eos", "show_interfaces_trunk.txt"), "eos"
        )
        eos = extract_vlan_targeted_discovery(
            {"id": 100, "name": "legacy_web", "service_type": "l3", "vrf": "default"},
            {
                "platform": "eos",
                "vlan_id_output": _read("eos", "show_vlan_id_100.txt"),
                "svi_output": _read("eos", "show_run_interface_vlan100.txt"),
                "mac_output": _read("eos", "show_mac_vlan_100.txt"),
                "arp_output": _read("eos", "show_ip_arp_vlan100.txt"),
                "trunk_interface_names": [item["interface"] for item in eos_trunks],
                "exempt_trunk_interface_names": ["Po3"],
                "trunk_summaries": eos_trunks,
            },
        )
        nxos_trunks = parse_trunk_interfaces(
            _read("nxos", "show_interface_trunk.txt"), "nxos"
        )
        nxos = extract_vlan_targeted_discovery(
            {"id": 100, "name": "legacy_web", "service_type": "l3", "vrf": "default"},
            {
                "platform": "nxos",
                "vlan_id_output": _read("nxos", "show_vlan_id_100.txt"),
                "svi_output": _read("nxos", "show_run_interface_vlan100.txt"),
                "mac_output": _read("nxos", "show_mac_vlan_100.txt"),
                "arp_output": _read("nxos", "show_ip_arp_vlan100.txt"),
                "trunk_interface_names": [item["interface"] for item in nxos_trunks],
                "exempt_trunk_interface_names": ["Po1"],
                "trunk_summaries": nxos_trunks,
            },
        )
        eos_bgp = parse_bgp_context(_read("eos", "show_run_bgp.txt"))
        nxos_bgp = parse_bgp_context(_read("nxos", "show_run_bgp.txt"))
        nxos_statics = parse_ip_route_statics(_read("nxos", "show_run_vrf_context.txt"))
        eos_plan = build_device_prune_plan(
            "eos-leaf-lis-01",
            [eos],
            static_routes=[
                {
                    "vrf": "default",
                    "prefix": "0.0.0.0/0",
                    "next_hop": "10.1.0.1",
                    "name": "fabric_default",
                }
            ],
            bgp_neighbors=[
                {"vrf": "default", "neighbor": "10.1.0.21", "remote_as": "65001"}
            ],
            vlan_ids=[100],
        )
        hostvars = {
            "eos-leaf-lis-01": {
                "data_center": "lisle",
                "_vlan_discovery": {
                    "hostname": "eos-leaf-lis-01",
                    "data_center": "lisle",
                    "os_family": "eos",
                    "per_vlan": [eos],
                    "static_routes": [
                        {
                            "vrf": "default",
                            "prefix": "0.0.0.0/0",
                            "next_hop": "10.1.0.1",
                            "name": "fabric_default",
                        }
                    ],
                    "bgp_neighbors": eos_bgp["neighbors"],
                    "bgp_vlan_blocks": eos_bgp["vlan_blocks"],
                    "bgp_vrfs": eos_bgp["vrfs"],
                    "bgp_as": eos_bgp["bgp_as"],
                    "prune_plan": eos_plan,
                },
            },
            "nxos-spine-lis-01": {
                "data_center": "lisle",
                "_vlan_discovery": {
                    "hostname": "nxos-spine-lis-01",
                    "data_center": "lisle",
                    "os_family": "nxos",
                    "per_vlan": [nxos],
                    "static_routes": nxos_statics,
                    "bgp_neighbors": nxos_bgp["neighbors"],
                    "bgp_vlan_blocks": nxos_bgp["vlan_blocks"],
                    "bgp_vrfs": nxos_bgp["vrfs"],
                    "bgp_as": nxos_bgp["bgp_as"],
                    "prune_plan": {},
                },
            },
        }
        vlan = {
            "id": 100,
            "name": "legacy_web",
            "service_type": "l3",
            "vrf": "default",
            "data_center": "lisle",
            "vni": 50100,
            "target_switches": ["eos-leaf-lis-01"],
        }
        reports = build_vlan_discovery_reports(
            [vlan], ["eos-leaf-lis-01", "nxos-spine-lis-01"], hostvars
        )
        report = reports[0]
        self.assertEqual(report["gateway"], "10.10.100.1")
        self.assertIn("10.10.100.0/24", report["prefixes"])
        self.assertEqual(report["inferred_service_type"], "l3")
        self.assertEqual(report["migration_type"], "l3")
        self.assertEqual(report["vlan_name_on_box"], "WEB_VXLAN")
        self.assertIn("eos-leaf-lis-01", report["snippet_target_switches"])
        self.assertNotIn("nxos-spine-lis-01", report["snippet_target_switches"])
        model = report["deployment_model"]
        self.assertEqual(model["migration"]["type"], "l3")
        self.assertEqual(model["placement"]["gateway_leafs"], [])
        participating = [
            item["hostname"] for item in model["placement"]["participating_leafs"]
        ]
        self.assertEqual(participating, ["eos-leaf-lis-01"])
        sources = [
            item["hostname"] for item in model["placement"]["source_gateway_devices"]
        ]
        self.assertIn("eos-leaf-lis-01", sources)
        self.assertIn("nxos-spine-lis-01", sources)
        prune_hosts = [
            item["hostname"] for item in model["placement"]["prune_eligible_leafs"]
        ]
        self.assertNotIn("eos-leaf-lis-01", prune_hosts)
        self.assertEqual(prune_hosts, ["nxos-spine-lis-01"])
        self.assertEqual(model["routing"]["gateway_ip"], "10.10.100.1")
        eos_plan_row = next(
            plan for plan in report["prune_plans"] if plan["hostname"] == "eos-leaf-lis-01"
        )
        self.assertEqual(eos_plan_row["status"], "retained_participating")
        self.assertEqual(eos_plan_row["actions"], [])
        self.assertTrue(eos_plan_row["human_required"])
        self.assertFalse(eos_plan_row["apply_automated"])
        l3_ops = {item["op"] for item in eos_plan_row.get("l3_review") or []}
        self.assertIn("no_ip_route", l3_ops)
        nxos_plan_row = next(
            plan for plan in report["prune_plans"] if plan["hostname"] == "nxos-spine-lis-01"
        )
        self.assertEqual(nxos_plan_row["placement_role"], "prune_eligible")
        self.assertTrue(nxos_plan_row["source_gateway"])
        nxos_ops = {action["op"] for action in nxos_plan_row["actions"]}
        self.assertIn("no_interface_vlan", nxos_ops)
        self.assertIn("no_vlan", nxos_ops)
        self.assertNotIn("no_ip_route", nxos_ops)
        self.assertNotIn("no_bgp_neighbor", nxos_ops)

        self.assertTrue(report["l3_discovery"]["present"])
        self.assertTrue(report["l3_discovery"]["shared_vrf"])
        self.assertTrue(report["endpoint_inventory"])
        self.assertIn("discovery_export", report)
        self.assertNotIn("prune_plans", report["discovery_export"])
        self.assertIn("prune_plans", report["prune_export"])
        self.assertIn("execution", eos_plan_row)
        self.assertIn("configure session", eos_plan_row["execution"]["full_cli"])
        l3_by_host = {
            item["hostname"]: item for item in report["l3_discovery"]["devices"]
        }
        self.assertIn("eos-leaf-lis-01", l3_by_host)
        self.assertIn("nxos-spine-lis-01", l3_by_host)
        eos_l3 = l3_by_host["eos-leaf-lis-01"]
        self.assertEqual(eos_l3["vrf"], "default")
        self.assertTrue(any(block["vlan_id"] == 100 for block in eos_l3["bgp_vlan"]))
        nxos_l3 = l3_by_host["nxos-spine-lis-01"]
        self.assertEqual(nxos_l3["vrf"], "TENANT1")
        self.assertEqual(nxos_l3["svi"]["hsrp_addresses"], ["10.10.100.1"])
        self.assertEqual(
            nxos_l3["svi"]["hsrp_groups"], [{"group": 1, "ip": "10.10.100.1"}]
        )
        self.assertTrue(
            any(peer["neighbor"] == "10.10.1.1" for peer in nxos_l3["bgp_neighbors"])
        )
        self.assertFalse(
            any(peer["neighbor"] == "10.1.0.21" for peer in nxos_l3["bgp_neighbors"])
        )
        self.assertTrue(
            any(route["prefix"] == "0.0.0.0/0" for route in nxos_l3["static_routes"])
        )


if __name__ == "__main__":
    unittest.main()
