#!/usr/bin/env python3
"""Unit tests for the CloudVision push helpers (plugins/vlan_lib/cvp.py)."""

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
    cvp_change_control,
    cvp_change_control_state,
    cvp_device_assignments,
    cvp_device_containers,
    cvp_task_ids,
)

FACTS = {"data": {"cvp_devices": [
    {"hostname": "leaf1", "fqdn": "leaf1.dc.example.net", "parentContainerName": "Leaves", "serialNumber": "S1"},
    {"hostname": "leaf2", "fqdn": "leaf2", "parentContainerName": "Undefined", "serialNumber": "S2"},
    {"hostname": "", "fqdn": "leaf3.dc.example.net", "parentContainerName": "Leaves-B", "serialNumber": "S3"},
]}}


class ContainerTests(unittest.TestCase):
    def test_match_on_hostname_or_fqdn_label(self):
        result = cvp_device_containers(FACTS, ["leaf1", "LEAF3", "leaf2", "leaf9"])
        self.assertEqual(sorted(result["found"]), ["LEAF3", "leaf1"])
        self.assertEqual(result["found"]["leaf1"]["container"], "Leaves")
        self.assertEqual(result["found"]["LEAF3"]["container"], "Leaves-B")
        # Not in a container (Undefined) or not in CloudVision at all.
        self.assertEqual(result["missing"], ["leaf2", "leaf9"])

    def test_assignments_keep_each_switch_in_its_container(self):
        found = cvp_device_containers(FACTS, ["leaf1"])["found"]
        devices = cvp_device_assignments({"leaf1": ["vxlan_vlan100_leaf1"]}, found)
        self.assertEqual(devices, [{
            "fqdn": "leaf1.dc.example.net", "hostname": "leaf1",
            "parentContainerName": "Leaves", "configlets": ["vxlan_vlan100_leaf1"]}])
        with self.assertRaises(AnsibleFilterError):
            cvp_device_assignments({"leaf9": ["x"]}, found)


class TaskTests(unittest.TestCase):
    def test_task_ids_from_configlet_and_device_results(self):
        configlet = {"data": {"taskIds": ["90"]}}
        devices = {"results": [{"data": {"taskIds": ["101", "102"]}}, {"skipped": True}]}
        check = {"data": {"taskIds": ["check_mode"]}}
        given = [configlet, devices, check]
        self.assertEqual(cvp_task_ids(given), ["90", "101", "102"])
        self.assertEqual(len(given), 3)  # the caller's list is not changed
        self.assertEqual(cvp_task_ids({"data": {"taskIds": []}}), [])


class ChangeControlTests(unittest.TestCase):
    def test_change_matches_the_real_schema(self):
        change = cvp_change_control("VXLAN migration - VLAN 100", "Line one\n  line two", ["101", 102])
        self.assertEqual(change["notes"], "Line one line two")  # CloudVision takes one line
        self.assertEqual(change["activities"], [
            {"task_id": "101", "stage": "Configlet push"}, {"task_id": "102", "stage": "Configlet push"}])
        self.assertEqual(change["stages"], [{"name": "Configlet push", "mode": "parallel"}])
        with self.assertRaises(AnsibleFilterError):
            cvp_change_control("x", "y", [])

    @staticmethod
    def _show(status, error="", approved=True):
        return {"data": {"change_controls": [{"value": {
            "key": {"id": "cc1"}, "change": {"status": status, "error": error}, "approve": {"value": approved}}}]}}

    def test_states(self):
        done = cvp_change_control_state(self._show("CHANGE_CONTROL_STATUS_COMPLETED"))
        self.assertEqual(done, {"status": "completed", "approved": True, "error": "", "done": True})
        failed = cvp_change_control_state(self._show("CHANGE_CONTROL_STATUS_COMPLETED", "task 101 failed"))
        self.assertEqual((failed["status"], failed["done"], failed["error"]), ("failed", True, "task 101 failed"))
        waiting = cvp_change_control_state(self._show("CHANGE_CONTROL_STATUS_NOT_STARTED", approved=False))
        self.assertEqual((waiting["status"], waiting["approved"], waiting["done"]), ("not_started", False, False))
        running = cvp_change_control_state(self._show("CHANGE_CONTROL_STATUS_RUNNING"))
        self.assertFalse(running["done"])
        self.assertEqual(cvp_change_control_state({"data": {"change_controls": [None]}})["status"], "not_found")
        self.assertEqual(cvp_change_control_state({})["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
