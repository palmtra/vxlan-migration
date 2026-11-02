#!/usr/bin/env python3
"""Unit tests for bulk endpoint tables, IPv6 neighbors, samples and ARP refresh."""

import json
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

from vlan_lib.discovery import extract_vlan_targeted_discovery  # noqa: E402
from vlan_lib.endpoints import (  # noqa: E402
    arp_refresh_command,
    arp_refresh_owner_targets,
    arp_refresh_summary,
    arp_refresh_targets,
    endpoint_bulk_command,
    endpoint_collection_mode,
    endpoint_tables_for_vlan,
    merge_endpoint_samples,
    nd_discovery_command,
    parse_endpoint_tables_json,
    parse_mac_table_json,
    parse_nd_entries,
    parse_neighbors_json,
)
from vlan_lib.parsers import mac_discovery_command  # noqa: E402
from vlan_lib.reports import build_action_items, build_endpoint_table, summarize_endpoint_collection  # noqa: E402

EOS_MAC_JSON = json.dumps(
    {
        "unicastTable": {
            "tableEntries": [
                {"vlanId": 3000, "macAddress": "00:50:56:b3:00:10", "entryType": "dynamic",
                 "interface": "Ethernet5", "moves": 1, "lastMove": 1793000000.0},
                {"vlanId": 3000, "macAddress": "00:50:56:b3:00:11", "entryType": "dynamic", "interface": "Vxlan1"},
                {"vlanId": 3000, "macAddress": "00:1c:73:00:00:01", "entryType": "static", "interface": "Router"},
                {"vlanId": 3000, "macAddress": "00:50:56:b3:00:12", "entryType": "configured",
                 "interface": "Port-Channel10"},
                {"vlanId": 10, "macAddress": "00:50:56:b3:00:99", "entryType": "dynamic", "interface": "Ethernet1"},
            ]
        },
        "multicastTable": {
            "tableEntries": [
                {"vlanId": 3000, "macAddress": "01:00:5e:00:00:01", "type": "igmp", "interfaces": ["Ethernet5"]}
            ]
        },
    }
)

NXOS_MAC_JSON = json.dumps(
    {
        "TABLE_mac_address": {
            "ROW_mac_address": [
                {"disp_mac_addr": "0050.56b3.0010", "disp_type": "* ", "disp_vlan": "3000",
                 "disp_is_static": "disabled", "disp_port": "Po101"},
                {"disp_mac_addr": "0026.98aa.0001", "disp_type": "G ", "disp_vlan": "3000",
                 "disp_is_static": "enabled", "disp_port": "vPC Peer-Link(R)"},
                {"disp_mac_addr": "0050.56b3.0011", "disp_type": "+ ", "disp_vlan": "3000",
                 "disp_is_static": "disabled", "disp_port": "vPC Peer-Link"},
                {"disp_mac_addr": "0050.56b3.0012", "disp_type": "C ", "disp_vlan": "3000",
                 "disp_is_static": "disabled", "disp_port": "nve1(10.1.1.1)"},
                {"disp_mac_addr": "0050.56b3.0013", "disp_type": "* ", "disp_vlan": "3000",
                 "disp_is_static": "enabled", "disp_port": "Eth1/7"},
            ]
        }
    }
)

EOS_ARP_JSON = json.dumps(
    {
        "vrfs": {
            "v0000822a": {
                "ipV4Neighbors": [
                    {"address": "10.230.0.10", "hwAddress": "0050.56b3.0010", "interface": "Vlan3000, Ethernet5", "age": 12},
                    {"address": "10.230.0.11", "hwAddress": "0050.56b3.0011", "interface": "Vlan3000, Vxlan1", "age": 12},
                    {"address": "10.230.0.12", "hwAddress": "0050.56b3.0012", "interface": "Vlan3000, not learned"},
                    {"address": "10.9.9.1", "hwAddress": "0050.56b3.0099", "interface": "Ethernet49/1"},
                ]
            },
            "default": {
                "ipV4Neighbors": [{"address": "10.1.0.1", "hwAddress": "0050.56b3.0098", "interface": "Vlan10, Ethernet1"}]
            },
        }
    }
)

# A single-VRF NX-OS reply: ROW_vrf and ROW_adj are objects, not lists.
NXOS_ARP_JSON = json.dumps(
    {
        "TABLE_vrf": {
            "ROW_vrf": {
                "vrf-name-out": "v0000822a",
                "cnt-total": 2,
                "TABLE_adj": {
                    "ROW_adj": [
                        {"intf-out": "Vlan3000", "ip-addr-out": "10.230.0.10", "time-stamp": "00:01:02", "mac": "0050.56b3.0010"},
                        {"intf-out": "Vlan3000", "ip-addr-out": "10.230.0.40", "time-stamp": "00:01:02", "incomplete": "true"},
                    ]
                },
            }
        }
    }
)

EOS_ND_JSON = json.dumps(
    {
        "vrfs": {
            "v1": {
                "ipV6Neighbors": [
                    {"address": "2001:db8:3000::10", "hwAddress": "0050.56b3.0010", "interface": "Vlan3000, Ethernet5"},
                    {"address": "fe80::250:56ff:feb3:10", "hwAddress": "0050.56b3.0010", "interface": "Vlan3000, Ethernet5"},
                ]
            }
        }
    }
)


class CommandTests(unittest.TestCase):
    def test_bulk_and_per_vlan_commands(self):
        self.assertEqual(endpoint_bulk_command("eos", "mac"), "show mac address-table | json")
        self.assertEqual(endpoint_bulk_command("nxos", "nd"), "show ipv6 neighbor vrf all | json")
        self.assertEqual(endpoint_bulk_command("ios", "mac"), "")
        self.assertEqual(nd_discovery_command("eos", "TENANT1"), "show ipv6 neighbors vrf TENANT1")
        self.assertEqual(nd_discovery_command("nxos", ""), "show ipv6 neighbor vrf default")
        self.assertEqual(mac_discovery_command("nxos", 300), "show mac address-table dynamic vlan 300")
        self.assertEqual(mac_discovery_command("nxos", 300, True), "show mac address-table vlan 300")

    def test_refresh_ping_commands(self):
        self.assertEqual(arp_refresh_command("eos", "10.1.0.5", "T1"), "ping vrf T1 10.1.0.5 repeat 1 timeout 1")
        self.assertEqual(arp_refresh_command("nxos", "10.1.0.5", "T1"), "ping 10.1.0.5 vrf T1 count 1 timeout 1")

    def test_collection_mode(self):
        self.assertEqual(endpoint_collection_mode("auto", 1), "per_vlan")
        self.assertEqual(endpoint_collection_mode("auto", 5), "bulk")
        self.assertEqual(endpoint_collection_mode("auto", 1, passes=3), "bulk")
        self.assertEqual(endpoint_collection_mode("per_vlan", 50), "per_vlan")
        self.assertEqual(endpoint_collection_mode("bulk", 1), "bulk")
        self.assertEqual(endpoint_collection_mode("bulk", 9, platform="ios"), "per_vlan")


class MacJsonTests(unittest.TestCase):
    def test_eos_rows_are_split_by_vlan_and_kind(self):
        result = parse_mac_table_json(EOS_MAC_JSON, [3000])
        self.assertTrue(result["ok"])
        by_mac = {e["mac"]: e for e in result["entries"][3000]}
        self.assertEqual(set(result["entries"]), {3000})
        self.assertEqual(by_mac["00:50:56:b3:00:10"]["port_kind"], "port")
        self.assertEqual(by_mac["00:50:56:b3:00:10"]["interface"], "Ethernet5")
        self.assertEqual(by_mac["00:50:56:b3:00:11"]["port_kind"], "overlay")
        self.assertEqual(by_mac["00:1c:73:00:00:01"]["port_kind"], "self")
        self.assertEqual(by_mac["00:50:56:b3:00:12"]["entry_type"], "static")
        self.assertEqual(result["multicast"], 1)

    def test_nxos_flags_and_special_ports(self):
        result = parse_mac_table_json(NXOS_MAC_JSON, [3000])
        kinds = {e["mac"]: (e["port_kind"], e["entry_type"]) for e in result["entries"][3000]}
        self.assertEqual(kinds["00:50:56:b3:00:10"], ("port", "dynamic"))
        self.assertEqual(kinds["00:26:98:aa:00:01"], ("self", "static"))  # G: the vPC peer's gateway MAC
        self.assertEqual(kinds["00:50:56:b3:00:11"], ("peer_link", "dynamic"))
        self.assertEqual(kinds["00:50:56:b3:00:12"], ("overlay", "dynamic"))
        self.assertEqual(kinds["00:50:56:b3:00:13"], ("port", "static"))

    def test_unknown_port_and_bad_output_are_reported(self):
        odd = json.loads(EOS_MAC_JSON)
        odd["unicastTable"]["tableEntries"].append(
            {"vlanId": 3000, "macAddress": "00:50:56:b3:00:77", "entryType": "dynamic", "interface": "Weird9"}
        )
        result = parse_mac_table_json(json.dumps(odd), [3000])
        self.assertFalse(result["ok"])
        self.assertIn("Weird9", result["problems"][0])
        self.assertEqual(parse_mac_table_json("% Invalid input", [3000])["problems"], ["MAC table output is not JSON"])
        self.assertEqual(parse_mac_table_json("", [3000])["problems"], ["no MAC table output"])


class NeighborJsonTests(unittest.TestCase):
    def test_eos_arp_keeps_vrf_and_port(self):
        result = parse_neighbors_json(EOS_ARP_JSON, 4, [3000])
        rows = {e["ip"]: e for e in result["entries"][3000]}
        self.assertEqual((rows["10.230.0.10"]["port"], rows["10.230.0.10"]["vrf"]), ("Ethernet5", "v0000822a"))
        self.assertEqual(rows["10.230.0.11"]["port"], "")  # behind a remote VTEP, not local
        self.assertEqual(rows["10.230.0.12"]["port"], "")  # "not learned"
        self.assertEqual((result["not_vlan"], set(result["entries"])), (1, {3000}))

    def test_nxos_single_vrf_object_and_incomplete_rows(self):
        result = parse_neighbors_json(NXOS_ARP_JSON, 4, [3000])
        self.assertEqual([e["ip"] for e in result["entries"][3000]], ["10.230.0.10"])
        self.assertEqual(result["entries"][3000][0]["vrf"], "v0000822a")
        self.assertEqual(result["incomplete"], 1)

    def test_ipv6_neighbors_json(self):
        result = parse_neighbors_json(EOS_ND_JSON, 6, [3000])
        self.assertEqual(
            [(e["ip"], e["port"]) for e in result["entries"][3000]],
            [("2001:db8:3000::10", "Ethernet5"), ("fe80::250:56ff:feb3:10", "Ethernet5")],
        )

    def test_ipv6_neighbors_with_unknown_key_names(self):
        blob = json.dumps({"TABLE_vrf": {"ROW_vrf": {"vrf-name-out": "v1", "TABLE_adj": {"ROW_adj": {
            "intf-out": "Vlan3000", "some-new-addr-key": "2001:db8:3000::20", "mac": "0050.56b3.0020"}}}}})
        result = parse_neighbors_json(blob, 6, [3000])
        self.assertEqual([e["ip"] for e in result["entries"][3000]], ["2001:db8:3000::20"])


class NdTextTests(unittest.TestCase):
    def test_nxos_wrapped_rows_and_eos_short_names(self):
        nxos = (
            "IPv6 Adjacency Table for VRF v1\n"
            "Address         Age       MAC Address     Pref Source     Interface\n"
            "2001:db8:3000:0:abcd:ef01:2345:6789\n"
            "                00:02:11  0050.56b3.0010  50   icmpv6     Vlan3000\n"
            "2001:db8:9::1   00:02:11  0050.56b3.0099  50   icmpv6     Vlan900\n"
        )
        self.assertEqual(
            [(e["ip"], e["mac"]) for e in parse_nd_entries(nxos, 3000)],
            [("2001:db8:3000:0:abcd:ef01:2345:6789", "00:50:56:b3:00:10")],
        )
        eos = "2001:db8:3000::20  0:00:12  0050.56b3.0020  REACH  Vl3000, Et7\n"
        self.assertEqual(parse_nd_entries(eos, 3000)[0]["port"], "Et7")


class SampleTests(unittest.TestCase):
    def test_samples_merge_with_seen_counts(self):
        first = parse_endpoint_tables_json("eos", EOS_MAC_JSON, EOS_ARP_JSON, EOS_ND_JSON, [3000])
        quiet = json.loads(EOS_MAC_JSON)
        quiet["unicastTable"]["tableEntries"] = [
            e for e in quiet["unicastTable"]["tableEntries"] if e["macAddress"] != "00:50:56:b3:00:10"
        ]
        second = parse_endpoint_tables_json("eos", json.dumps(quiet), EOS_ARP_JSON, "", [3000])
        merged = merge_endpoint_samples([first, second])
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["passes"], 2)
        seen = {e["mac"]: e["seen"] for e in merged["mac"]["3000"]}
        self.assertEqual(seen["00:50:56:b3:00:10"], 1)
        self.assertEqual(seen["00:50:56:b3:00:12"], 2)
        sliced = endpoint_tables_for_vlan(merged, 3000)
        self.assertEqual([e["mac"] for e in sliced["peer_link_mac_entries"]], [])
        self.assertEqual([e["mac"] for e in sliced["overlay_mac_entries"]], ["00:50:56:b3:00:11"])
        self.assertEqual(len(sliced["nd_entries"]), 2)

    def test_unreadable_sample_fails_the_merge(self):
        bad = parse_endpoint_tables_json("nxos", "", NXOS_ARP_JSON, "", [3000])
        merged = merge_endpoint_samples([bad, bad])
        self.assertFalse(merged["ok"])
        self.assertEqual(merged["problems"], ["no MAC table output"])
        self.assertFalse(merge_endpoint_samples([])["ok"])


class RefreshTests(unittest.TestCase):
    SVI = (
        "interface Vlan3000\n  vrf member v0000822a\n  ip address 10.230.0.2/29\n"
        "  hsrp 3000\n    ip 10.230.0.1\n"
    )

    def test_targets_skip_own_and_gateway_addresses(self):
        plan = arp_refresh_targets([{"item": {"id": 3000}, "stdout": self.SVI}], "nxos")
        self.assertEqual([t["ip"] for t in plan["targets"]], ["10.230.0.3", "10.230.0.4", "10.230.0.5", "10.230.0.6"])
        self.assertEqual({t["vrf"] for t in plan["targets"]}, {"v0000822a"})

    def test_budget_and_shut_svis(self):
        big = self.SVI.replace("/29", "/22")
        plan = arp_refresh_targets([{"item": {"id": 3000}, "stdout": big}], "nxos", max_hosts=100)
        self.assertEqual(plan["targets"], [])
        self.assertIn("Vlan3000 10.230.0.0/22", plan["skipped"][0])
        shut = self.SVI + "  shutdown\n"
        self.assertEqual(arp_refresh_targets([{"item": {"id": 3000}, "stdout": shut}], "nxos")["targets"], [])

    def test_one_owner_sweeps_each_vlan(self):
        plan = arp_refresh_targets([{"item": {"id": 3000}, "stdout": self.SVI}], "nxos")
        by_host = {"mls-02": plan, "mls-01": plan, "leaf-01": {}}
        self.assertEqual(len(arp_refresh_owner_targets(by_host, "mls-01")), 4)
        self.assertEqual(arp_refresh_owner_targets(by_host, "mls-02"), [])

    def test_summary(self):
        results = [
            {"stdout": "1 packets transmitted, 1 packets received"},
            {"stdout": "1 packets transmitted, 0 packets received"},
            {"skipped": True},
        ]
        self.assertEqual(arp_refresh_summary(results), {"sent": 2, "answered": 1})


class DiscoveryAndReportTests(unittest.TestCase):
    def _bulk_record(self, merged):
        return extract_vlan_targeted_discovery(
            {"id": 3000, "name": "app"},
            {
                "platform": "eos",
                "vlan_id_output": "",
                "svi_output": "interface Vlan3000\n   vrf v0000822a\n   ip address 10.230.0.2/24\n",
                "endpoint_tables": endpoint_tables_for_vlan(merged, 3000),
                "collection_extra": {"arp_refresh": {"sent": 4, "answered": 1, "skipped_subnets": []}, "note": ""},
            },
        )

    def test_bulk_slice_feeds_the_record(self):
        merged = merge_endpoint_samples([parse_endpoint_tables_json("eos", EOS_MAC_JSON, EOS_ARP_JSON, EOS_ND_JSON, [3000])])
        record = self._bulk_record(merged)
        self.assertEqual(record["endpoint_collection"]["mode"], "bulk")
        self.assertEqual(record["endpoint_collection"]["arp_refresh"]["sent"], 4)
        self.assertNotIn("note", record["endpoint_collection"])
        self.assertEqual([e["mac"] for e in record["mac_entries"]], ["00:50:56:b3:00:10", "00:50:56:b3:00:12"])
        self.assertEqual(len(record["arp_entries"]), 3)
        self.assertEqual(len(record["nd_entries"]), 2)
        self.assertTrue(record["parse_checks"]["bulk"]["ok"])

    def test_per_vlan_ipv6_text_is_read_when_svi_present(self):
        record = extract_vlan_targeted_discovery(
            {"id": 3000, "name": "app"},
            {
                "platform": "eos",
                "vlan_id_output": "",
                "svi_output": "interface Vlan3000\n   ip address 10.230.0.2/24\n",
                "nd_output": "2001:db8:3000::20  0:00:12  0050.56b3.0020  REACH  Vl3000, Et7\n",
            },
        )
        self.assertEqual(record["endpoint_collection"], {"mode": "per_vlan", "passes": 1})
        self.assertEqual(record["nd_entries"][0]["ip"], "2001:db8:3000::20")

    def test_ipv6_and_samples_in_the_endpoint_table(self):
        devices = [
            {
                "hostname": "leaf-01",
                "endpoint_collection": {"mode": "bulk", "passes": 3},
                "mac_entries": [
                    {"mac": "00:50:56:b3:00:10", "interface": "Ethernet5", "attachment_role": "compute", "seen": 3},
                    {"mac": "00:50:56:b3:00:30", "interface": "Ethernet6", "attachment_role": "compute", "seen": 1},
                ],
            },
            {
                "hostname": "gw-01",
                "svi_present": True,
                "endpoint_collection": {"mode": "bulk", "passes": 3},
                "arp_entries": [{"ip": "10.230.0.10", "mac": "00:50:56:b3:00:10", "seen": 3}],
                "nd_entries": [
                    {"ip": "2001:db8:3000::10", "mac": "00:50:56:b3:00:10", "seen": 3},
                    {"ip": "fe80::250:56ff:feb3:10", "mac": "00:50:56:b3:00:10", "seen": 3},
                    {"ip": "2001:db8:3000::40", "mac": "00:50:56:b3:00:40", "port": "Ethernet9", "seen": 2},
                ],
                "port_attachments": [{"interface": "Ethernet9", "role": "compute", "description": "v6-only"}],
            },
        ]
        table = build_endpoint_table(devices, {})
        row = table["endpoints"][0]
        self.assertEqual((row["ipv6"], row["seen"], row["flags"]), (["2001:db8:3000::10"], 3, []))
        only = {r["mac"]: r for r in table["mac_only"]}
        self.assertEqual(only["00:50:56:b3:00:30"]["flags"], ["intermittent"])
        v6 = only["00:50:56:b3:00:40"]
        self.assertEqual((v6["location"], v6["hostname"], v6["ipv6"]), ("edge", "gw-01", ["2001:db8:3000::40"]))
        self.assertEqual(v6["flags"], ["located_by_arp", "intermittent"])
        summary = table["summary"]
        self.assertEqual((summary["passes"], summary["ipv6_hosts"], summary["intermittent"]), (3, 2, 2))

    def test_collection_summary_and_action_items(self):
        devices = [
            {"hostname": "leaf-01", "endpoint_collection": {"mode": "bulk", "passes": 2}},
            {
                "hostname": "mls-01",
                "endpoint_collection": {
                    "mode": "per_vlan",
                    "passes": 1,
                    "note": "bulk tables not readable (no MAC table output), read per VLAN instead",
                    "arp_refresh": {"sent": 252, "answered": 7, "skipped_subnets": ["Vlan3000 10.0.0.0/20: 4094 addresses"]},
                },
            },
        ]
        summary = summarize_endpoint_collection(devices)
        self.assertEqual(summary["modes"], {"bulk": ["leaf-01"], "per_vlan": ["mls-01"]})
        self.assertEqual((summary["passes"], summary["refresh"]["sent"], summary["refresh"]["answered"]), (2, 252, 7))
        items = build_action_items({"endpoint_collection": summary, "switches_vlan_defined": ["leaf-01"]})
        self.assertIn("Endpoint tables: mls-01: bulk tables not readable", items[0])
        self.assertIn("ARP refresh skipped mls-01: Vlan3000 10.0.0.0/20", items[1])


if __name__ == "__main__":
    unittest.main()
