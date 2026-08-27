#!/usr/bin/env python3
"""Unit tests for structured MAC/ARP discovery parsers."""

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
    classify_mac_port_role,
    extract_vlan_targeted_discovery,
    normalize_mac_address,
    parse_arp_entries,
    parse_mac_address_table,
)


SAMPLE_REPORT_MAC = """
[oma01-blf01] VLAN 3398 --- ispa_altrdstate_prod
Dynamic Mac Address Table - VLAN 3398
00:50:56:95:0d:9a Port-Channel32
00:50:56:b4:37:61 Port-Channel101
00:50:56:b4:4d:4b Port-Channel101
00:50:56:b4:54:2c Port-Channel101
00:50:56:b4:5b:41 Port-Channel101
00:50:56:b4:74:0c Port-Channel101
00:50:56:b4:77:81 Port-Channel101
00:50:56:b4:ba:47 Port-Channel101
"""

EOS_MAC = """
          Mac Address Table
------------------------------------------------------------------
Vlan    Mac Address       Type        Ports      Moves   Last Move
----    -----------       ----        -----      -----   ---------
3398    0050.5695.0d9a    DYNAMIC     Po32
3398    0050.56b4.3761    DYNAMIC     Port-Channel101
Total Mac Addresses for this criterion: 2
"""

NXOS_MAC = """
Legend:
        * - primary entry, G - Gateway MAC, (R) - Routed MAC, O - Overlay MAC
VLAN     MAC Address      Type      Age       Secure NTFY Ports
---------+-----------------+--------+---------+------+----+------------------
* 3398   0050.5695.0d9a   dynamic   0         F      F    Po32
* 3398   0050.56b4.3761   dynamic   0         F      F    Po101
"""

EOS_ARP = """
Address         Age (sec)  Hardware Addr   Interface
Internet  10.102.60.1            0   0050.5695.0d9a  Vlan3398
Internet  10.102.60.10          12   0050.56b4.3761  Vlan3398
"""


class MacArpParserTests(unittest.TestCase):
    def test_normalize_mac_address(self):
        self.assertEqual(normalize_mac_address("0050.5695.0d9a"), "00:50:56:95:0d:9a")
        self.assertEqual(normalize_mac_address("00:50:56:95:0d:9a"), "00:50:56:95:0d:9a")
        self.assertEqual(normalize_mac_address("00-50-56-B4-37-61"), "00:50:56:b4:37:61")

    def test_parse_sample_report_mac_style(self):
        entries = parse_mac_address_table(SAMPLE_REPORT_MAC, 3398)
        self.assertEqual(len(entries), 8)
        self.assertEqual(entries[0]["mac"], "00:50:56:95:0d:9a")
        self.assertEqual(entries[0]["interface"], "Port-Channel32")
        po101 = [e for e in entries if e["interface"] == "Port-Channel101"]
        self.assertEqual(len(po101), 7)

    def test_parse_eos_mac_table(self):
        entries = parse_mac_address_table(EOS_MAC, 3398)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["mac"], "00:50:56:95:0d:9a")
        self.assertEqual(entries[0]["entry_type"], "dynamic")
        self.assertIn(entries[0]["interface"], ("Po32", "Port-Channel32"))

    def test_parse_nxos_mac_table(self):
        entries = parse_mac_address_table(NXOS_MAC, 3398)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["mac"], "00:50:56:b4:37:61")
        self.assertEqual(entries[1]["interface"], "Po101")

    def test_parse_arp_entries(self):
        entries = parse_arp_entries(EOS_ARP, 3398)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["ip"], "10.102.60.1")
        self.assertEqual(entries[0]["mac"], "00:50:56:95:0d:9a")
        self.assertEqual(entries[0]["interface"], "Vlan3398")

    def test_classify_mac_port_role(self):
        self.assertEqual(
            classify_mac_port_role("Port-Channel101", [], ["Po101"], []),
            "trunk",
        )
        self.assertEqual(
            classify_mac_port_role("Et23", ["Ethernet23"], ["Po1"], []),
            "access",
        )
        self.assertEqual(
            classify_mac_port_role("Po1", [], [], ["Port-Channel1"]),
            "exempt_trunk",
        )

    def test_extract_includes_structured_mac_and_uplinks(self):
        record = extract_vlan_targeted_discovery(
            {"id": 3398, "name": "ispa_altrdstate_prod", "vrf": "default"},
            {
                "platform": "eos",
                "vlan_id_output": """
VLAN  Name                             Status    Ports
----- -------------------------------- --------- -------------------------------
3398  ispa_altrdstate_prod             active    Po32, Port-Channel101
""",
                "svi_output": "",
                "mac_output": SAMPLE_REPORT_MAC,
                "arp_output": "",
                "trunk_interface_names": ["Port-Channel32", "Port-Channel101", "Port-Channel10"],
                "exempt_trunk_interface_names": [],
            },
        )
        self.assertTrue(record["mac_learned"])
        self.assertEqual(len(record["mac_entries"]), 8)
        self.assertIn("Po32", record["trunk_ports"] + record["uplinks"])
        self.assertTrue(
            any(e["port_role"] == "trunk" for e in record["mac_entries"])
        )
        self.assertEqual(record["access_ports"], [])


if __name__ == "__main__":
    unittest.main()
