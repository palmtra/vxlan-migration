#!/usr/bin/env python3
"""VLAN records to AVD services-repo entries (palmtra/avd-vxlan-repo)."""

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

import yaml  # noqa: E402

from vlan_lib.avd_services import (  # noqa: E402
    avd_leaf_pairs,
    avd_service_entries,
    avd_services_file_text,
    merge_avd_services,
)

FABRIC = {
    "l3leaf": {
        "node_groups": [
            {"group": "PAIR1", "bgp_as": "65101", "nodes": [
                {"name": "leaf1a", "id": 1, "loopback_ipv4_address": "10.250.0.1"},
                {"name": "leaf1b", "id": 2, "loopback_ipv4_address": "10.250.0.2"},
            ]},
            {"group": "PAIR2", "bgp_as": "65102", "nodes": [
                {"name": "leaf2a", "id": 3, "loopback_ipv4_address": "10.250.0.3"},
                {"name": "leaf2b", "id": 4, "loopback_ipv4_address": "10.250.0.4"},
            ]},
        ]
    }
}

L3 = {
    "id": 100,
    "name": "legacy_web",
    "vlan_name": "WEB",
    "action": "migrate",
    "service_type": "l3",
    "data_center": "lisle",
    "vni": 20100,
    "vni_pool": "customer",
    "vrf": "PROD",
    "l3_vni": 50001,
    "gateway": "10.100.0.1",
    "prefixes": ["10.100.0.0/24"],
    "target_switches": ["leaf1a", "leaf2b"],
    "gateway_leafs": [],
    "svi_mtu": 9214,
    "svi_description": "web tier",
}


class LeafPairs(unittest.TestCase):
    def test_pairs_from_node_groups(self):
        self.assertEqual(
            avd_leaf_pairs(FABRIC),
            {"leaf1a": "PAIR1", "leaf1b": "PAIR1", "leaf2a": "PAIR2", "leaf2b": "PAIR2"},
        )


class ServiceEntries(unittest.TestCase):
    def test_distributed_anycast_svi(self):
        result = avd_service_entries(L3, FABRIC)
        self.assertEqual(result["problems"], [])
        self.assertIsNone(result["l2vlan"])
        self.assertEqual(result["vrf"], {"name": "PROD", "vrf_vni": 50001})
        self.assertEqual(result["svi"], {
            "id": 100,
            "name": "WEB",
            "vni_override": 20100,
            "rd_override": 100,
            "rt_override": "50001:100",
            "enabled": True,
            "tags": ["PAIR1", "PAIR2"],
            "ip_address_virtual": "10.100.0.1/24",
            "mtu": 9214,
            "description": "web tier",
            "_migration": {"source": "vxlan-migration lisle VLAN 100", "vni_pool": "customer", "gateway_mode": "distributed_anycast"},
        })

    def test_gateway_leafs_add_their_pair(self):
        record = dict(L3, target_switches=["leaf1a"], gateway_leafs=["leaf2a"])
        self.assertEqual(avd_service_entries(record, FABRIC)["svi"]["tags"], ["PAIR1", "PAIR2"])

    def test_centralized_anycast_splits_svi_and_l2vlan(self):
        record = dict(L3, gateway_mode="centralized_anycast", gateway_leafs=["leaf2a", "leaf2b"], target_switches=["leaf1a", "leaf1b", "leaf2a"])
        result = avd_service_entries(record, FABRIC)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["svi"]["tags"], ["PAIR2"])
        self.assertEqual(result["l2vlan"]["tags"], ["PAIR1"])
        self.assertEqual(result["l2vlan"]["vni_override"], 20100)
        self.assertEqual(result["l2vlan"]["name"], result["svi"]["name"])
        self.assertNotIn("gateway_mode", result["l2vlan"]["_migration"])

    def test_centralized_needs_gateway_leafs(self):
        record = dict(L3, gateway_mode="centralized_anycast")
        self.assertIn("centralized_anycast needs gateway_leafs", avd_service_entries(record, FABRIC)["problems"])

    def test_external_gateway_is_l2vlan(self):
        record = dict(L3, service_type="l2", gateway="", prefixes=[])
        result = avd_service_entries(record, FABRIC)
        self.assertEqual(result["problems"], [])
        self.assertIsNone(result["svi"])
        self.assertIsNone(result["vrf"])
        self.assertEqual(result["l2vlan"]["tags"], ["PAIR1", "PAIR2"])

    def test_default_vrf_gets_an_id(self):
        record = dict(L3, vrf="default")
        record.pop("l3_vni")
        result = avd_service_entries(record, FABRIC)
        self.assertEqual(result["vrf"], {"name": "default", "vrf_id": 1})
        # No route-target in the record: AVD's <vni>:<vni>, RD still <router-id>:<vlan>.
        self.assertNotIn("rt_override", result["svi"])
        self.assertEqual(result["svi"]["rd_override"], 100)
        self.assertTrue(any("AVD uses 20100:20100" in note for note in result["notes"]))

    def test_explicit_route_target(self):
        record = dict(L3, rt_import="65000:100", rt_export="65000:100")
        self.assertEqual(avd_service_entries(record, FABRIC)["svi"]["rt_override"], "65000:100")

    def test_asymmetric_route_target_is_a_problem(self):
        record = dict(L3, rt_import="65000:100", rt_export="65000:200")
        self.assertTrue(any("one route-target per VLAN" in p for p in avd_service_entries(record, FABRIC)["problems"]))

    def test_problems(self):
        cases = [
            (dict(L3, vni=None), "has no VNI"),
            (dict(L3, target_switches=["leaf9"]), "leaf leaf9 is not in any leaf pair"),
            (dict(L3, gateway=""), "needs gateway and prefixes"),
            (dict(L3, vrf="TENANT_A", l3_vni=None), "VRF TENANT_A needs l3_vni"),
            (dict(L3, gateway_mode="anycast"), "gateway_mode 'anycast'"),
            (dict(L3, service_type="l2", target_switches=[]), "has no leaf pair to go on"),
        ]
        for record, expected in cases:
            problems = avd_service_entries(record, FABRIC)["problems"]
            self.assertTrue(any(expected in p for p in problems), (expected, problems))

    def test_fabric_drift_from_discovery(self):
        record = dict(L3, leaf_bgp={"leaf1a": {"asn": "65101", "router_id": "10.250.0.9"}, "leaf2b": {"asn": "65999"}})
        problems = avd_service_entries(record, FABRIC)["problems"]
        self.assertIn("leaf1a router-id is 10.250.0.9 on the switch but 10.250.0.1 in the AVD repo", problems)
        self.assertIn("leaf2b ASN is 65999 on the switch but 65102 in the AVD repo", problems)

    def test_secondaries_are_noted_not_moved(self):
        record = dict(L3, secondary_prefixes=["10.101.0.0/24"])
        result = avd_service_entries(record, FABRIC)
        self.assertNotIn("ip_address_virtual_secondaries", result["svi"])
        self.assertTrue(any("10.101.0.0/24" in note for note in result["notes"]))


class Merge(unittest.TestCase):
    def test_new_tenant_vrf_and_svi(self):
        result = merge_avd_services({"tenants": []}, "lisle", [avd_service_entries(L3, FABRIC)])
        self.assertEqual(result["problems"], [])
        (tenant,) = result["services"]["tenants"]
        self.assertEqual(tenant["name"], "LISLE_MIGRATION")
        self.assertEqual(tenant["vrfs"][0]["name"], "PROD")
        self.assertEqual([svi["id"] for svi in tenant["vrfs"][0]["svis"]], [100])
        self.assertNotIn("l2vlans", tenant)

    def test_reexport_replaces_the_vlan(self):
        first = merge_avd_services({"tenants": []}, "lisle", [avd_service_entries(L3, FABRIC)])["services"]
        moved = dict(L3, service_type="l2")
        second = merge_avd_services(first, "lisle", [avd_service_entries(moved, FABRIC)])
        (tenant,) = second["services"]["tenants"]
        self.assertNotIn("vrfs", tenant)
        self.assertEqual([item["id"] for item in tenant["l2vlans"]], [100])

    def test_keeps_other_vlans_and_sorts(self):
        existing = {"tenants": [{"name": "LISLE_MIGRATION", "vrfs": [{"name": "PROD", "vrf_vni": 50001, "svis": [{"id": 300}]}],
                                 "l2vlans": [{"id": 50}]}]}
        result = merge_avd_services(existing, "lisle", [avd_service_entries(L3, FABRIC)])
        tenant = result["services"]["tenants"][0]
        self.assertEqual([svi["id"] for svi in tenant["vrfs"][0]["svis"]], [100, 300])
        self.assertEqual([item["id"] for item in tenant["l2vlans"]], [50])
        self.assertEqual(existing["tenants"][0]["vrfs"][0]["svis"], [{"id": 300}])  # input not mutated

    def test_vrf_vni_mismatch_writes_nothing(self):
        existing = {"tenants": [{"name": "LISLE_MIGRATION", "vrfs": [{"name": "PROD", "vrf_vni": 50002, "svis": [{"id": 300}]}]}]}
        result = merge_avd_services(existing, "lisle", [avd_service_entries(L3, FABRIC)])
        self.assertEqual(result["services"], existing)
        self.assertIn("VRF PROD has vrf_vni 50002 in the AVD repo but 50001 in the record", result["problems"])

    def test_entry_problems_write_nothing(self):
        existing = {"tenants": []}
        result = merge_avd_services(existing, "lisle", [avd_service_entries(dict(L3, vni=None), FABRIC)])
        self.assertIs(result["services"], existing)
        self.assertTrue(result["problems"])


class FileText(unittest.TestCase):
    def test_header_comment_is_kept(self):
        original = "---\n# Lisle services.\n# Rules...\n\ntenants: []\n"
        services = merge_avd_services({"tenants": []}, "lisle", [avd_service_entries(L3, FABRIC)])["services"]
        text = avd_services_file_text(original, services)
        self.assertTrue(text.startswith("---\n# Lisle services.\n# Rules...\ntenants:\n"))
        self.assertEqual(yaml.safe_load(text), services)

    def test_str_subclasses_are_dumped_plain(self):
        class Unsafe(str):
            # Like AnsibleUnsafeText: str() and encode/decode keep the subclass.
            def __str__(self):
                return self

            def encode(self, *args, **kwargs):
                return self

        text = avd_services_file_text("", {"tenants": [{"name": Unsafe("LISLE_MIGRATION"), "l2vlans": [{"id": 5}]}]})
        self.assertEqual(yaml.safe_load(text)["tenants"][0]["name"], "LISLE_MIGRATION")

    def test_no_header(self):
        self.assertEqual(avd_services_file_text("", {"tenants": []}), "---\ntenants: []\n")


if __name__ == "__main__":
    unittest.main()
