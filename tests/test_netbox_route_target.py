#!/usr/bin/env python3
"""VLAN records to NetBox route targets (roles/netbox_export)."""

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

from vlan_lib.netbox import ROUTE_TARGET_DESCRIPTION, netbox_route_target  # noqa: E402

RECORD = {
    "id": 1521,
    "name": "rewards",
    "vlan_name": "REWARDS_APP",
    "action": "migrate",
    "service_type": "l2",
    "vni": 22010,
    "ownership": {"tenant": "0000964", "owner": "Rewards Trust"},
}


def record(**changes):
    result = dict(RECORD)
    result.update(changes)
    return result


class RouteTarget(unittest.TestCase):
    def test_name_is_vni_colon_vlan_id(self):
        self.assertEqual(netbox_route_target(RECORD)["name"], "22010:1521")

    def test_explicit_l2_rt_wins(self):
        self.assertEqual(netbox_route_target(record(l2_rt="65000:1521"))["name"], "65000:1521")

    def test_tenant_owner_and_description(self):
        rt = netbox_route_target(RECORD)
        self.assertEqual(rt["tenant"], "0000964")
        self.assertEqual(rt["owner"], "Rewards Trust")
        self.assertEqual(rt["description"], "REWARDS_APP - Created by Ansible VXLAN Automation")
        self.assertEqual(rt["problems"], [])

    def test_flat_tenant_and_owner(self):
        flat = record(tenant="0000001", owner="FNTS")
        flat.pop("ownership")
        rt = netbox_route_target(flat)
        self.assertEqual((rt["tenant"], rt["owner"]), ("0000001", "FNTS"))
        self.assertEqual(rt["problems"], [])

    def test_description_falls_back_to_record_name(self):
        rt = netbox_route_target(record(vlan_name=""))
        self.assertEqual(rt["description"], "rewards - %s" % ROUTE_TARGET_DESCRIPTION)

    def test_description_marker_can_be_changed(self):
        self.assertEqual(netbox_route_target(RECORD, "Created by X")["description"], "REWARDS_APP - Created by X")

    def test_description_fits_netbox(self):
        rt = netbox_route_target(record(vlan_name="V" * 300))
        self.assertEqual(len(rt["description"]), 200)
        self.assertTrue(rt["description"].endswith(" - " + ROUTE_TARGET_DESCRIPTION))

    def test_no_vni_is_a_problem(self):
        rt = netbox_route_target(record(vni=None))
        self.assertEqual(rt["name"], "")
        self.assertIn("VLAN 1521 has no VNI, so it has no route target", rt["problems"])

    def test_missing_tenant_and_owner_are_problems(self):
        rt = netbox_route_target(record(ownership={"tenant": "", "owner": ""}))
        self.assertEqual(rt["problems"], [
            "VLAN 1521 has no tenant (ownership.tenant)",
            "VLAN 1521 has no owner (ownership.owner, the customer name)",
        ])

    def test_unquoted_tenant_id_is_refused(self):
        # YAML 1.1 reads an unquoted id made only of the digits 0-7 as octal:
        # 0000001 is 1 and 0000707 is 455. 0000964 stays a string.
        loaded = yaml.safe_load("a: 0000001\nb: 0000707\nc: 0000964\n")
        self.assertEqual((loaded["a"], loaded["b"], loaded["c"]), (1, 455, "0000964"))
        rt = netbox_route_target(record(ownership={"tenant": loaded["b"], "owner": "X"}))
        self.assertEqual(rt["tenant"], "")
        self.assertEqual(len(rt["problems"]), 1)
        self.assertIn("tenant is the number 455: quote the tenant id", rt["problems"][0])

    def test_non_numeric_tenant_is_refused(self):
        rt = netbox_route_target(record(ownership={"tenant": "Rewards Trust", "owner": "Rewards Trust"}))
        self.assertEqual(rt["problems"], ["VLAN 1521 tenant 'Rewards Trust' is not a numeric tenant id"])


if __name__ == "__main__":
    unittest.main()
