#!/usr/bin/env python3
"""VNI allocation from per-DC internal/external customer ranges."""

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

from vlan_lib.placement import flatten_deployment_model  # noqa: E402
from vlan_lib.reports import build_action_items, build_vlan_discovery_reports  # noqa: E402
from vlan_lib.vni import allocate_vni, used_vnis, vni_range_problems  # noqa: E402

RANGES = {
    "lisle": {"internal": {"start": 10000, "end": 10009}, "external": {"start": 20000, "end": 20999}},
    "omaha": {"internal": {"start": 30000, "end": 30009}, "external": {"start": 40000, "end": 40999}},
}


class VniTests(unittest.TestCase):
    def test_next_free_skips_records_and_discovered_maps(self):
        used = used_vnis(
            [{"id": 1, "vni": 20000}, {"id": 2, "l3_vni": "20001"}],
            [{"vxlan_interface": {"vlan_vni": [{"vlan_id": 9, "vni": 20002}], "vrf_vni": {"T1": 20003}}}],
        )
        result = allocate_vni("lisle", "external", RANGES, used)
        self.assertEqual(result["vni"], 20004)
        self.assertEqual(result["range"], "20000-20999")

    def test_pools_and_dcs_are_separate(self):
        self.assertEqual(allocate_vni("lisle", "internal", RANGES, set())["vni"], 10000)
        self.assertEqual(allocate_vni("omaha", "internal", RANGES, set())["vni"], 30000)

    def test_full_missing_and_bad_input(self):
        full = allocate_vni("lisle", "internal", RANGES, set(range(10000, 10010)))
        self.assertIsNone(full["vni"])
        self.assertIn("is full", full["reason"])
        self.assertIn("no external VNI range configured for dallas", allocate_vni("dallas", "external", RANGES, set())["reason"])
        self.assertIn("internal or external", allocate_vni("lisle", "other", RANGES, set())["reason"])

    def test_range_problems(self):
        self.assertEqual(vni_range_problems(RANGES), [])
        self.assertEqual(vni_range_problems({"lisle": {"internal": {"start": None, "end": None}}}), [])
        self.assertIn("malformed", vni_range_problems({"lisle": {"internal": {"start": 5, "end": 1}}})[0])
        bad = {"lisle": {"internal": {"start": 1, "end": 100}, "external": {"start": 50, "end": 200}}}
        self.assertEqual(vni_range_problems(bad), ["VNI ranges lisle/internal and lisle/external overlap"])

    def test_report_proposes_distinct_vnis_in_one_run(self):
        vlans = [
            {"id": 300, "name": "a", "service_type": "l2", "data_center": "lisle"},
            {"id": 301, "name": "b", "service_type": "l2", "data_center": "lisle", "customer_type": "internal"},
            {"id": 302, "name": "c", "service_type": "l2", "data_center": "lisle"},
        ]
        reports = build_vlan_discovery_reports(
            vlans, [], {}, [], {"ranges": RANGES, "records": [{"id": 5, "vni": 20000}], "default_pool": "external"}
        )
        self.assertEqual([r["vni_allocation"]["vni"] for r in reports], [20001, 10000, 20002])
        items = build_action_items(dict(reports[0], switches_vlan_defined=["leaf1"]))
        self.assertIn(
            "VNI 20001 proposed (next free in lisle external 20000-20999). Confirm it, then write it to evpn.l2.vni in the SSOT record.",
            items,
        )
        self.assertEqual(flatten_deployment_model(reports[0]["deployment_model"])["vni"], 20001)


if __name__ == "__main__":
    unittest.main()
