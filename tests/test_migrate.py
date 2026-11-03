#!/usr/bin/env python3
"""Unit tests for the migration readiness gate and post-change VNI/MAC checks."""

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

from vlan_lib.discovery import build_vlan_verification  # noqa: E402
from vlan_lib.mop import migrate_readiness, prune_readiness  # noqa: E402


def doc(*findings):
    return {"vlan": {"id": 3000}, "findings": list(findings)}


VNI = {"id": "D-01", "kind": "decision", "category": "vni", "severity": "medium", "text": "VNI 11002 proposed",
       "topic": "proposed", "vni": 11002}
NO_VNI = {"id": "B-02", "kind": "blocker", "category": "vni", "severity": "high", "text": "No VNI", "topic": "missing"}
MAPPED = {"id": "D-03", "kind": "decision", "category": "vni", "severity": "medium",
          "text": "Confirm VNI 11002 matches", "topic": "existing_mapping", "vni": 11002}
CLASH = {"id": "B-03", "kind": "blocker", "category": "vni", "severity": "high", "text": "Pick another VNI"}
NAME = {"id": "D-02", "kind": "decision", "category": "naming", "severity": "medium", "text": "Agree the VLAN name"}
BLOCK = {"id": "B-01", "kind": "blocker", "category": "coverage", "severity": "high", "text": "mls-99 not discovered"}
RISK = {"id": "R-01", "kind": "risk", "category": "gateway", "severity": "high", "text": "Gateway MAC changes"}
LOW = {"id": "R-02", "kind": "risk", "category": "endpoints", "severity": "low", "text": "1 IP not in MAC table"}


class ReadinessTests(unittest.TestCase):
    def test_ready_with_high_risks_listed(self):
        gate = migrate_readiness([doc(RISK, LOW)], {"id": 3000})
        self.assertTrue(gate["ready"])
        self.assertEqual(gate["high_risks"], ["VLAN 3000 [R-01] Gateway MAC changes"])

    def test_blockers_always_stop(self):
        gate = migrate_readiness([doc(BLOCK)], {"id": 3000}, decisions_reviewed=True)
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["blockers"], ["VLAN 3000 [B-01] mls-99 not discovered"])

    def test_decisions_stop_until_reviewed(self):
        gate = migrate_readiness([doc(NAME)], {"id": 3000})
        self.assertFalse(gate["ready"])
        self.assertIn("-e migrate_decisions_reviewed=true", gate["reasons"][0])
        self.assertTrue(migrate_readiness([doc(NAME)], {"id": 3000}, decisions_reviewed=True)["ready"])

    def test_netbox_vni_settles_the_vni_decision_only(self):
        self.assertTrue(migrate_readiness([doc(VNI)], {"id": 3000}, netbox_vni=11002)["ready"])
        gate = migrate_readiness([doc(VNI, NAME)], {"id": 3000}, netbox_vni="11002")
        self.assertEqual(gate["decisions"], ["VLAN 3000 [D-02] Agree the VLAN name"])

    def test_netbox_vni_settles_a_missing_vni_but_not_a_clash(self):
        self.assertFalse(migrate_readiness([doc(NO_VNI)], {"id": 3000})["ready"])
        self.assertTrue(migrate_readiness([doc(NO_VNI)], {"id": 3000}, netbox_vni=11002)["ready"])
        gate = migrate_readiness([doc(NO_VNI, CLASH)], {"id": 3000}, netbox_vni=11002)
        self.assertEqual(gate["blockers"], ["VLAN 3000 [B-03] Pick another VNI"])

    def test_an_existing_mapping_to_another_vni_stays_open(self):
        self.assertTrue(migrate_readiness([doc(MAPPED)], {"id": 3000}, netbox_vni=11002)["ready"])
        gate = migrate_readiness([doc(MAPPED)], {"id": 3000}, netbox_vni=11005)
        self.assertEqual(gate["decisions"], ["VLAN 3000 [D-03] Confirm VNI 11002 matches"])

    def test_a_vlan_not_in_the_vlan_db_stops(self):
        gate = migrate_readiness([doc()], {"id": 3000, "_probe": True}, decisions_reviewed=True)
        self.assertFalse(gate["ready"])
        self.assertIn("VLAN 3000 is not in the VLAN DB", gate["reasons"][0])

    def test_no_discovery_report_stops(self):
        self.assertFalse(migrate_readiness([], {"id": 3000})["ready"])


class PruneReadinessTests(unittest.TestCase):
    def test_ready_and_blocked_plans_by_phase(self):
        report = {"prune_plans": [
            {"hostname": "mls-02", "placement_role": "prune_eligible", "phase": 3, "vlan_ids": [3000],
             "actions": [], "blocked_actions": [{"op": "svi_remove"}], "status": "blocked_until_gateway_moved",
             "blocked_reason": "Vlan SVI on mls-02 is still the active gateway"},
            {"hostname": "acc-01", "placement_role": "link_prune", "phase": 2, "vlan_ids": [3000],
             "actions": [], "blocked_actions": [{"op": "trunk_remove_vlans"}], "status": "blocked_until_vni_live"},
            {"hostname": "mls-01", "placement_role": "link_prune", "phase": 1, "vlan_ids": [3000],
             "actions": [{"op": "trunk_remove_vlans"}], "status": "ready"},
            {"hostname": "leaf-01", "placement_role": "retained_participating", "actions": []},
            {"hostname": "mls-03", "placement_role": "prune_eligible", "actions": [], "blocked_actions": []},
        ]}
        result = prune_readiness([report])
        self.assertEqual([row["hostname"] for row in result["plans"]], ["mls-01", "acc-01", "mls-02"])
        self.assertEqual(result["ready"], ["mls-01"])
        self.assertEqual(result["blocked"], ["acc-01", "mls-02"])
        self.assertEqual(result["plans"][2]["status"], "blocked_until_gateway_moved")
        self.assertEqual(result["plans"][0]["kind"], "link")

    def test_a_prune_eligible_plan_with_actions_is_ready(self):
        result = prune_readiness([{"prune_plans": [
            {"hostname": "mls-01", "placement_role": "prune_eligible", "actions": [{"op": "vlan_remove"}]}]}])
        self.assertEqual(result["plans"][0], {"hostname": "mls-01", "vlan_ids": [], "kind": "teardown",
                                              "phase": 3, "status": "ready", "reason": ""})

    def test_no_reports(self):
        self.assertEqual(prune_readiness(None), {"plans": [], "ready": [], "blocked": []})


class VerificationTests(unittest.TestCase):
    def test_dotted_eos_macs_count_as_learned(self):
        result = build_vlan_verification(3000, {
            "vni_output": "3000       11002        static       Vxlan1          3000",
            "mac_outputs": [{"item": 3000, "stdout": " 3000    0050.56b3.0010    DYNAMIC     Et5   1   0:00:12 ago"}],
            "arp_output": "",
            "registry": [{"id": 3000, "vni": 11002}],
        })
        self.assertEqual((result["vni_mapped"], result["mac_or_arp_learned"]), (True, True))

    def test_the_first_registry_entry_wins(self):
        # verify puts the deployed record (VNI possibly from NetBox) first.
        result = build_vlan_verification(3000, {
            "vni_output": "3000       11002        static       Vxlan1          3000",
            "registry": [{"id": 3000, "vni": 11002}, {"id": 3000}],
        })
        self.assertTrue(result["vni_mapped"])


if __name__ == "__main__":
    unittest.main()
