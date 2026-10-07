#!/usr/bin/env python3
"""Tests for compute vs switch-uplink classification and prune session CLI."""

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
sys.path.insert(0, os.path.join(_REPO, "plugins", "filter"))

from vlan_filters import (  # noqa: E402
    build_device_prune_plan,
    classify_attachment_role,
    extract_vlan_targeted_discovery,
    parse_l2_interface_config,
    prune_apply_commands,
)


Z14_CFG = """
interface Ethernet22
   description z14 OSA for SAND Box
   switchport mode trunk
   storm-control multicast level rate 1 bps
"""

OOB_PO_CFG = """
interface Port-Channel14
   description PO to fnts-chi01-oob-sw01/02
   switchport mode trunk
   mlag 4
"""

UPLINK_CFG = """
interface Port-Channel2
   description uplink to fnts-chi01-aggpe-sw01
   switchport mode trunk
"""

NUTANIX_CFG = """
interface Port-Channel21
   description Nutanix cluster AHV
   switchport mode trunk
"""

PEER_CFG = """
interface Port-Channel3
   description MLAG peer
   switchport mode trunk
   switchport trunk group mlagpeer
"""


class AttachmentClassificationTests(unittest.TestCase):
    def test_parse_z14_endpoint(self):
        parsed = parse_l2_interface_config(Z14_CFG, platform="eos")
        self.assertEqual(parsed["interface"], "Ethernet22")
        self.assertEqual(parsed["description"], "z14 OSA for SAND Box")
        self.assertEqual(parsed["mode"], "trunk")
        self.assertFalse(parsed["peer_link"])
        self.assertIn("storm-control", parsed["raw"])

    def test_roles_match_existing_tool_examples(self):
        z14 = parse_l2_interface_config(Z14_CFG)
        self.assertEqual(
            classify_attachment_role("Ethernet22", z14, [], ["Ethernet22"], []),
            "compute",
        )
        oob = parse_l2_interface_config(OOB_PO_CFG)
        self.assertEqual(
            classify_attachment_role("Port-Channel14", oob, [], ["Port-Channel14"], []),
            "switch_uplink",
        )
        nutanix = parse_l2_interface_config(NUTANIX_CFG)
        self.assertEqual(
            classify_attachment_role("Port-Channel21", nutanix, [], ["Po21"], []),
            "compute",
        )
        peer = parse_l2_interface_config(PEER_CFG, platform="eos")
        self.assertEqual(
            classify_attachment_role(
                "Port-Channel3", peer, [], [], ["Port-Channel3"]
            ),
            "peer_link",
        )

    def test_access_without_description_is_compute(self):
        self.assertEqual(
            classify_attachment_role("Ethernet5", {}, ["Ethernet5"], ["Po1"], []),
            "compute",
        )

    def test_unlabeled_trunk_is_switch_uplink(self):
        self.assertEqual(
            classify_attachment_role("Port-Channel2", {}, [], ["Po2"], []),
            "switch_uplink",
        )

    def test_fabric_interconnect_is_transit(self):
        parsed = parse_l2_interface_config(
            "interface Ethernet10\n   description UCS fabric interconnect A\n   switchport mode trunk\n"
        )
        self.assertEqual(
            classify_attachment_role("Ethernet10", parsed, [], ["Ethernet10"], []),
            "transit",
        )


class ComputeMacDiscoveryTests(unittest.TestCase):
    def test_discovery_keeps_compute_macs_drops_uplink_macs(self):
        record = extract_vlan_targeted_discovery(
            {"id": 1107, "name": "fnts_management"},
            {
                "platform": "eos",
                "vlan_id_output": """
VLAN  Name                             Status    Ports
----- -------------------------------- --------- -------------------------------
1107  fnts_management                  active    Et22, Po2, Po14
""",
                "svi_output": "",
                "mac_output": """
Vlan    Mac Address       Type        Ports
----    -----------       ----        -----
1107    0050.568f.820a    DYNAMIC     Po2
1107    dead.babe.cafe    DYNAMIC     Et22
""",
                "arp_output": "",
                "trunk_interface_names": [
                    "Ethernet22",
                    "Port-Channel2",
                    "Port-Channel14",
                ],
                "exempt_trunk_interface_names": [],
                "interface_configs": [
                    parse_l2_interface_config(Z14_CFG),
                    parse_l2_interface_config(UPLINK_CFG),
                    parse_l2_interface_config(OOB_PO_CFG),
                ],
                "switch_hostnames": ["fnts-chi01-aggacc-sw14"],
            },
        )
        self.assertEqual(record["compute_ports"], ["Ethernet22"])
        self.assertIn("Port-Channel2", record["switch_uplink_ports"])
        self.assertIn("Port-Channel14", record["switch_uplink_ports"])
        compute_macs = [entry["mac"] for entry in record["endpoint_mac_entries"]]
        self.assertEqual(compute_macs, ["de:ad:ba:be:ca:fe"])
        self.assertEqual(len(record["mac_entries"]), 2)

        plan = build_device_prune_plan(
            "fnts-chi01-aggacc-sw14",
            [record],
        )
        self.assertEqual(plan["status"], "blocked_by_compute_endpoints")
        self.assertIn("Ethernet22", plan["endpoint_ports"])
        prune_ifaces = [
            action["interface"]
            for action in plan["actions"]
            if action["op"] == "trunk_remove_vlans"
        ]
        self.assertIn("Port-Channel2", prune_ifaces)
        self.assertNotIn("Ethernet22", prune_ifaces)
        self.assertIn("configure session", plan["execution"]["full_cli"])
        self.assertIn("commit timer 00:10:00", plan["execution"]["full_cli"])
        self.assertIn("configure confirm", plan["execution"]["confirm"])
        self.assertFalse(plan["apply_automated"])


class PruneExecutionTests(unittest.TestCase):
    def test_eos_session_wraps_trunk_remove(self):
        plan = build_device_prune_plan(
            "eos-leaf-01",
            [
                {
                    "vlan_id": 1107,
                    "os_family": "eos",
                    "vlan_present": True,
                    "svi_present": False,
                    "compute_ports": [],
                    "unknown_ports": [],
                    "switch_uplink_ports": ["Port-Channel2"],
                }
            ],
        )
        self.assertEqual(plan["status"], "candidate")
        cli = plan["execution"]["full_cli"]
        self.assertIn("configure session prune_v1107", cli)
        self.assertIn("switchport trunk allowed vlan remove 1107", cli)
        self.assertIn("show session-config diffs", cli)
        self.assertIn("commit timer", cli)
        commands = prune_apply_commands(plan["execution"])
        self.assertTrue(commands[0].startswith("configure session"))
        self.assertIn("commit timer 00:10:00", commands)
        self.assertNotIn("configure confirm", commands)

    def test_nxos_checkpoint_wraps_config(self):
        plan = build_device_prune_plan(
            "nxos-leaf-01",
            [
                {
                    "vlan_id": 100,
                    "os_family": "nxos",
                    "vlan_present": True,
                    "svi_present": False,
                    "compute_ports": [],
                    "unknown_ports": [],
                    "switch_uplink_ports": ["Po10"],
                }
            ],
        )
        self.assertEqual(plan["execution"]["platform"], "nxos")
        self.assertIn("checkpoint prune_v100", plan["execution"]["full_cli"])
        self.assertIn("rollback running-config checkpoint", plan["execution"]["rollback"])
        commands = prune_apply_commands(plan["execution"])
        self.assertEqual(commands[0], "checkpoint prune_v100")
        self.assertEqual(commands[-1], "end")
        self.assertNotIn("copy running-config startup-config", commands)


if __name__ == "__main__":
    unittest.main()
