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
    arp_table_parse_check,
    classify_mac_port_role,
    extract_vlan_targeted_discovery,
    mac_table_parse_check,
    normalize_mac_address,
    parse_arp_entries,
    parse_mac_address_table,
)
from vlan_lib.parsers import parse_mac_table_detail  # noqa: E402


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
        self.assertEqual(record["endpoint_mac_entries"], [])
        self.assertTrue(record["switch_uplink_ports"])


NXOS_MAC_SPECIAL_ROWS = """
Legend:
        * - primary entry, G - Gateway MAC, (R) - Routed MAC, O - Overlay MAC
   VLAN     MAC Address      Type      age     Secure NTFY Ports
---------+-----------------+--------+---------+------+----+------------------
*  100     0050.56b3.0001   dynamic  0         F      F    Po10
+  100     0050.56b3.0002   dynamic  0         F      F    vPC Peer-Link
C  100     0050.56b3.0003   dynamic  0         F      F    nve1(10.1.1.1)
G  100     0000.0c9f.f064   static   -         F      F    sup-eth1(R)
*  100     0050.56b3.0004   dynamic  0         F      F    Eth101/1/5
*  100     0050.56b3.0005   dynamic  0         F      F    Twe1/1
*  100     0050.56b3.0006   dynamic  0         F      F    Hu1/49
*  200     0050.56b3.0008   dynamic  0         F      F    Po10
*  100     0100.5e00.0001   dynamic  0         F      F    Po10
"""

EOS_MAC_WITH_MOVES = """
          Mac Address Table
------------------------------------------------------------------
Vlan    Mac Address       Type        Ports      Moves   Last Move
----    -----------       ----        -----      -----   ---------
 100    0050.56b3.0011    DYNAMIC     Vx1        1       0:00:12 ago
 100    0050.56b3.0012    DYNAMIC     Et49/1     7       0:00:12 ago
 100    0050.56b3.0013    DYNAMIC     Po1000     1       2 days, 3:04:05 ago
Total Mac Addresses for this criterion: 3
"""

NXOS_ARP_WITH_TOTAL = """
IP ARP Table for context TENANT1
Total number of entries: 4
Address         Age       MAC Address     Interface       Flags
10.1.0.10       00:01:02  0050.56b3.0001  Vlan100
10.1.0.11       00:01:02  0050.56b3.0002  Vlan100         +
10.1.0.12       00:01:02  INCOMPLETE      Vlan100
10.1.0.13       00:01:02  0050.56b3.0003  Vlan100         *
"""


class MacRowAccountingTests(unittest.TestCase):
    """Every MAC row is a front-panel port, a known special port, or reported."""

    def test_only_front_panel_ports_are_endpoints(self):
        entries = parse_mac_address_table(NXOS_MAC_SPECIAL_ROWS, 100)
        self.assertEqual(
            [e["interface"] for e in entries], ["Po10", "Eth101/1/5", "Twe1/1", "Hu1/49"]
        )
        self.assertNotIn("eth1", [e["interface"].lower() for e in entries])

    def test_special_rows_are_kept_as_evidence(self):
        detail = parse_mac_table_detail(NXOS_MAC_SPECIAL_ROWS, 100)
        kinds = {e["mac"]: e["port_kind"] for e in detail["entries"]}
        self.assertEqual(kinds["00:50:56:b3:00:02"], "peer_link")
        self.assertEqual(kinds["00:50:56:b3:00:03"], "overlay")
        self.assertEqual(kinds["00:00:0c:9f:f0:64"], "self")
        overlay = next(e for e in detail["entries"] if e["port_kind"] == "overlay")
        self.assertEqual(overlay["remote_vtep"], "10.1.1.1")
        self.assertEqual((detail["other_vlan"], detail["multicast"], detail["unparsed"]), (1, 1, []))

    def test_eos_moves_and_vxlan_rows(self):
        detail = parse_mac_table_detail(EOS_MAC_WITH_MOVES, 100)
        by_mac = {e["mac"]: e for e in detail["entries"]}
        self.assertEqual(by_mac["00:50:56:b3:00:11"]["port_kind"], "overlay")
        moved = by_mac["00:50:56:b3:00:12"]
        self.assertEqual((moved["moves"], moved["last_move"]), (7, "0:00:12 ago"))
        self.assertEqual(by_mac["00:50:56:b3:00:13"]["last_move"], "2 days, 3:04:05 ago")

    def test_parse_check_passes_when_every_row_is_accounted_for(self):
        check = mac_table_parse_check(EOS_MAC_WITH_MOVES, 100)
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual((check["parsed"], check["overlay"], check["device_total"]), (2, 1, 3))
        self.assertTrue(mac_table_parse_check(NXOS_MAC_SPECIAL_ROWS, 100)["ok"])

    def test_parse_check_reports_unknown_ports_and_total_mismatch(self):
        unknown = NXOS_MAC_SPECIAL_ROWS + "*  100     0050.56b3.0007   dynamic  0  F  F    Weird9\n"
        check = mac_table_parse_check(unknown, 100)
        self.assertFalse(check["ok"])
        self.assertEqual(len(check["unparsed_lines"]), 1)
        self.assertIn("Weird9", check["problems"][0])
        short = EOS_MAC_WITH_MOVES.replace("criterion: 3", "criterion: 4")
        check = mac_table_parse_check(short, 100)
        self.assertEqual(check["problems"], ["the switch reports 4 MAC(s) but 3 row(s) were read"])

    def test_extract_keeps_peer_link_and_overlay_out_of_endpoints(self):
        record = extract_vlan_targeted_discovery(
            {"id": 100, "name": "web"},
            {"platform": "nxos", "mac_output": NXOS_MAC_SPECIAL_ROWS, "vlan_id_output": "", "svi_output": ""},
        )
        self.assertEqual(len(record["mac_entries"]), 4)
        self.assertEqual([e["mac"] for e in record["peer_link_mac_entries"]], ["00:50:56:b3:00:02"])
        self.assertEqual([e["mac"] for e in record["overlay_mac_entries"]], ["00:50:56:b3:00:03"])
        self.assertTrue(record["parse_checks"]["mac_table"]["ok"])
        self.assertNotIn("arp", record["parse_checks"])


class ArpRowAccountingTests(unittest.TestCase):
    def test_incomplete_rows_count_toward_the_total(self):
        check = arp_table_parse_check(NXOS_ARP_WITH_TOTAL, 100)
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual((check["parsed"], check["incomplete"], check["device_total"]), (3, 1, 4))

    def test_total_mismatch_is_reported(self):
        check = arp_table_parse_check(NXOS_ARP_WITH_TOTAL.replace("entries: 4", "entries: 3"), 100)
        self.assertEqual(check["problems"], ["the switch reports 3 ARP entries but 4 row(s) were read"])

    def test_eos_include_filter_rows_for_other_svis_are_accounted_for(self):
        eos = (
            "10.1.0.10  0:01:02  0050.56b3.0001  Vlan100, Port-Channel10\n"
            "10.2.0.10  0:01:02  0050.56b3.0099  Vlan1000, Ethernet3\n"
        )
        check = arp_table_parse_check(eos, 100)
        self.assertEqual((check["ok"], check["parsed"], check["other_vlan"]), (True, 1, 1))
        self.assertEqual(parse_arp_entries(eos, 100)[0]["port"], "Port-Channel10")


if __name__ == "__main__":
    unittest.main()
