#!/usr/bin/env python3
"""Studios deploy (cv_studio_leaf_vlans, cv_studio_structured_config,
avd_eos_config), the Undefined / Unassigned container split, and the
per-VLAN verification report."""

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

from ansible.errors import AnsibleFilterError  # noqa: E402
from vlan_lib.cvp import (  # noqa: E402
    cv_studio_leaf_vlans,
    cv_studio_structured_config,
    cvp_device_containers,
)
from vlan_lib.placement import flatten_deployment_model  # noqa: E402
from vlan_lib.verify import build_verification_report, verification_report_markdown  # noqa: E402

try:
    import pyavd  # noqa: F401

    HAS_PYAVD = True
except ImportError:
    HAS_PYAVD = False


def _rec(vid, hosts, **extra):
    record = {"id": vid, "name": "v%s" % vid, "action": "migrate", "vni": 10000 + vid, "target_switches": hosts}
    record.update(extra)
    return record


class ContainerTests(unittest.TestCase):
    def test_undefined_and_unassigned_are_reported_separately(self):
        facts = {"cvp_devices": [
            {"hostname": "a", "parentContainerName": "Omaha"},
            {"hostname": "b", "parentContainerName": "Unassigned"},
            {"hostname": "c", "parentContainerName": "undefined"},
        ]}
        out = cvp_device_containers(facts, ["a", "b", "c", "d"])
        self.assertEqual(list(out["found"]), ["a"])
        self.assertEqual(out["missing"], ["b", "c", "d"])
        self.assertEqual(out["unassigned"], ["b", "c"])


class StudioLeafVlanTests(unittest.TestCase):
    RECORDS = [
        _rec(100, ["a", "b"], deployed=True),
        _rec(200, ["a"]),  # not deployed: left out
        _rec(300, ["b"], deployed=True),
        _rec(400, ["z"], deployed=True),  # other leaf
        _rec(450, ["a"], deployed=True, action="decommission"),
        _rec(460, ["a"], deployed=True, _probe=True),
    ]

    def _ids(self, plan):
        return {leaf: [r["id"] for r in recs] for leaf, recs in plan["leafs"].items()}

    def test_deploy_keeps_deployed_vlans_and_adds_this_one(self):
        plan = cv_studio_leaf_vlans(self.RECORDS, _rec(500, ["a", "b"]), ["a", "b"])
        self.assertEqual(self._ids(plan), {"a": [100, 500], "b": [100, 300, 500]})
        self.assertEqual(plan["problems"], [])

    def test_rollback_drops_this_vlan_only(self):
        plan = cv_studio_leaf_vlans(self.RECORDS, _rec(100, ["a", "b"], deployed=True), ["a", "b"], True)
        self.assertEqual(self._ids(plan), {"a": [], "b": [300]})

    def test_current_record_wins_over_its_file(self):
        current = _rec(100, ["a", "b"], vni=19999)
        plan = cv_studio_leaf_vlans(self.RECORDS, current, ["a", "b"])
        self.assertEqual(plan["leafs"]["a"][0]["vni"], 19999)

    def test_gateway_leafs_count_as_config_hosts(self):
        records = [_rec(700, ["a"], deployed=True, gateway_leafs=["g"])]
        plan = cv_studio_leaf_vlans(records, _rec(800, ["g"]), ["g"])
        self.assertEqual(self._ids(plan), {"g": [700, 800]})

    def test_deployed_vlan_without_vni_is_a_problem(self):
        records = [_rec(900, ["a"], deployed=True, vni="")]
        plan = cv_studio_leaf_vlans(records, _rec(500, ["a"]), ["a"])
        self.assertEqual(self._ids(plan), {"a": [500]})
        self.assertEqual(len(plan["problems"]), 1)
        self.assertIn("VLAN 900", plan["problems"][0])

    def test_nested_record_carries_deployed(self):
        top = flatten_deployment_model({"site": {"vlan": {"id": 10, "name": "x"}}, "deployed": True})
        self.assertIs(top["deployed"], True)
        under = flatten_deployment_model({"site": {"vlan": {"id": 10, "name": "x"}}, "migration": {"deployed": True}})
        self.assertIs(under["deployed"], True)
        absent = flatten_deployment_model({"site": {"vlan": {"id": 10, "name": "x"}}})
        self.assertNotIn("deployed", absent)


class StudioRenderTests(unittest.TestCase):
    PARTS = [
        {"vlans": [{"id": 7, "name": "x"}], "eos_cli": "interface Vxlan1\n   vxlan vlan 7 vni 10007\n!\n"},
        {"vlans": [{"id": 3, "name": "y"}], "eos_cli": "interface Vxlan1\n   vxlan vlan 3 vni 10003\n"},
    ]

    def test_merge_sorts_vlans_and_joins_cli(self):
        merged = cv_studio_structured_config(self.PARTS, "a")
        self.assertEqual([v["id"] for v in merged["vlans"]], [3, 7])
        self.assertIn("vxlan vlan 7 vni 10007", merged["eos_cli"])
        self.assertIn("vxlan vlan 3 vni 10003", merged["eos_cli"])
        self.assertNotIn("hostname", merged)

    def test_empty_leaf_has_no_cli(self):
        self.assertEqual(cv_studio_structured_config([], "a"), {"vlans": []})

    @unittest.skipUnless(HAS_PYAVD, "pyavd not installed")
    def test_pyavd_renders_and_validates(self):
        from vlan_lib.avd_render import avd_eos_config

        text = avd_eos_config(cv_studio_structured_config(self.PARTS, "a"), "a")
        self.assertIn("vlan 3\n   name y", text)
        self.assertIn("vxlan vlan 7 vni 10007", text)
        self.assertEqual(avd_eos_config({"vlans": []}, "a"), "")
        with self.assertRaises(AnsibleFilterError):
            avd_eos_config({"vlans": [{"id": "x"}]}, "a")


class VerificationReportTests(unittest.TestCase):
    RECORD = {"id": 100, "name": "web app", "vni": 10100, "vrf": "PROD", "service_type": "l3"}
    OK = {"bgp_evpn": {"passed": True, "detail": "2 peers"}, "vni_mapping": {"passed": True, "detail": "ok"}}
    BAD = {"bgp_evpn": {"passed": True, "detail": "2 peers"}, "vni_mapping": {"passed": "False", "detail": "missing"}}

    def test_all_pass(self):
        r = build_verification_report([{"host": "l1", "migration": self.OK}], self.RECORD, {"data_center": "omaha"})
        self.assertEqual(r["outcome"], "passed")
        self.assertEqual(r["vlan_slug"], "web_app")

    def test_failed_check_and_silent_switch(self):
        r = build_verification_report(
            [{"host": "l2", "migration": self.BAD}, {"host": "l1", "migration": {}}], self.RECORD, {})
        self.assertEqual(r["outcome"], "failed")
        self.assertEqual(r["failed_checks"], [{"host": "l2", "check": "vni_mapping", "detail": "missing"}])
        self.assertEqual(r["not_reported"], ["l1"])
        md = verification_report_markdown(r)
        self.assertIn("**Outcome: FAILED**", md)
        self.assertIn("- l2: vni_mapping failed. missing", md)
        self.assertIn("| l2 | pass | FAIL |", md)

    def test_rollback_outcomes(self):
        ok = build_verification_report([{"host": "l1", "migration": self.BAD, "rollback": self.OK}], self.RECORD, {})
        self.assertEqual(ok["outcome"], "rolled_back")
        bad = build_verification_report([{"host": "l1", "migration": self.BAD, "rollback": self.BAD}], self.RECORD, {})
        self.assertEqual(bad["outcome"], "escalated")


if __name__ == "__main__":
    unittest.main()
