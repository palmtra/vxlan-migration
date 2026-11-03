#!/usr/bin/env python3
"""Unit tests for preflight, validation data capture and the endpoint comparison."""

import csv
import io
import os
import re
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

from vlan_lib.capture import command_slug, redact_data, redact_text  # noqa: E402
from vlan_lib.endpoint_compare import compare_endpoints, endpoint_comparison_csv, strip_endpoint_tables  # noqa: E402
from vlan_lib.preflight import preflight_report, switch_identity  # noqa: E402

EOS = "Arista DCS-7050SX3-48YC8\nHardware version: 11.00\nSoftware image version: 4.31.2F\n"
NXOS9K = (
    "Cisco Nexus Operating System (NX-OS) Software\nSoftware\n  BIOS: version 05.45\n"
    "  NXOS: version 9.3(8)\nHardware\n  cisco Nexus9000 C93180YC-EX chassis\n"
)
NXOS_OLD = "Cisco Nexus Operating System (NX-OS) Software\n  system:    version 7.0(3)I7(9)\n  cisco Nexus 3048 Chassis\n"


class SwitchIdentityTests(unittest.TestCase):
    def test_eos(self):
        self.assertEqual(switch_identity(EOS, "eos"),
                         {"os_family": "eos", "model": "DCS-7050SX3-48YC8", "version": "4.31.2F", "matches": True})

    def test_nxos_models_not_the_banner(self):
        self.assertEqual(switch_identity(NXOS9K, "nxos")["model"], "Nexus9000 C93180YC-EX")
        ident = switch_identity(NXOS_OLD, "nxos")
        self.assertEqual((ident["model"], ident["version"]), ("Nexus 3048", "7.0(3)I7(9)"))

    def test_wrong_os_in_the_inventory(self):
        self.assertFalse(switch_identity(NXOS9K, "eos")["matches"])

    def test_unrecognised_output(self):
        self.assertEqual(switch_identity("% Invalid input", "eos")["matches"], False)
        self.assertEqual(switch_identity(None)["os_family"], "")


class PreflightReportTests(unittest.TestCase):
    def test_failures_first_and_overall_status(self):
        report = preflight_report([
            {"check": "switch", "target": "leaf-01", "status": "ok", "detail": "EOS"},
            {"check": "netbox", "target": "netbox_url", "status": "warn", "detail": "not configured"},
            {"check": "cloudvision", "target": "leaf-02", "status": "fail", "detail": "not in CloudVision"},
            {"bogus": True},
        ])
        self.assertEqual((report["status"], report["failed"], report["warnings"]), ("fail", 1, 1))
        self.assertEqual([c["target"] for c in report["checks"]], ["leaf-02", "netbox_url", "leaf-01"])
        self.assertTrue(report["lines"][0].startswith("FAIL cloudvision"))

    def test_warnings_only(self):
        self.assertEqual(preflight_report([{"check": "netbox", "status": "warn"}])["status"], "warn")
        self.assertEqual(preflight_report([])["status"], "ok")


class CaptureTests(unittest.TestCase):
    def test_slug_matches_the_mock_cli(self):
        mock = os.path.join(_REPO, "tests", "mock_cli", "ansible_collections", "ansible", "netcommon",
                            "plugins", "action", "cli_command.py")
        with open(mock, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('re.sub(r"[^a-z0-9]+", "_", str(command).lower()).strip("_")', source)
        self.assertEqual(command_slug("show mac address-table vlan 3000 | json"),
                         "show_mac_address_table_vlan_3000_json")

    def test_secrets_are_removed_from_config_lines(self):
        cases = {
            "   neighbor 10.0.0.1 password 7 0822455D0A16": "   neighbor 10.0.0.1 password <removed>",
            "username admin privilege 15 role network-admin secret sha512 $6$abc": (
                "username admin privilege 15 role network-admin secret <removed>"),
            "snmp-server community public ro": "snmp-server community <removed>",
            "tacacs-server host 10.1.1.1 key 7 070C285F4D06": "tacacs-server host 10.1.1.1 key <removed>",
            "ntp authentication-key 1 md5 7 0011": "ntp authentication-key <removed>",
            '  "password": "abc",': '  "password": "<removed>",',
        }
        for line, expected in cases.items():
            self.assertEqual(redact_text(line), expected, line)

    def test_routing_data_and_descriptions_stay(self):
        for line in ("  set community 65000:100 additive", "ip community-list standard X permit 65000:1",
                     "   description key server rack", "Port Description: secret-lab",
                     "Vlan3000 is up, line protocol is up", " 3000    0050.56b3.0010    DYNAMIC     Et5"):
            self.assertEqual(redact_text(line), line)

    def test_json_secrets_by_key(self):
        data = redact_data({"token": "abc", "headers": {"Authorization": "Token x"}, "name": "ACME",
                            "items": [{"password": ""}], "config": "a\nsnmp-server community x ro"})
        self.assertEqual(data["token"], "<removed>")
        self.assertEqual(data["headers"]["Authorization"], "<removed>")
        self.assertEqual((data["name"], data["items"][0]["password"]), ("ACME", ""))
        self.assertEqual(data["config"], "a\nsnmp-server community <removed>")


def table(endpoints, mac_only=()):
    return {"endpoints": list(endpoints), "mac_only": list(mac_only)}


def row(ip, mac, host, intf, location="edge", flags=()):
    return {"ip": ip, "mac": mac, "hostname": host, "interface": intf, "location": location, "flags": list(flags)}


class EndpointCompareTests(unittest.TestCase):
    def setUp(self):
        self.before = [{"vlan_id": 3000, "endpoint_table": table([
            row("10.0.0.1", "00:00:5e:00:01:01", "mls-01", "Vlan3000", flags=["gateway", "fhrp_mac"]),
            row("10.0.0.10", "0050.5600.0010", "acc-01", "Et1"),
            row("10.0.0.11", "00:50:56:00:00:11", "acc-01", "Et2"),
            row("10.0.0.12", "00:50:56:00:00:12", "acc-01", "Et3"),
            row("10.0.0.13", "00:50:56:00:00:13", "acc-01", "Et4"),
            row("10.0.0.14", "00:50:56:00:00:14", "acc-01", "Et5"),
            row("10.0.0.15", "00:50:56:00:00:15", "acc-01", "Et6", flags=["intermittent"]),
            row("10.0.0.16", "00:50:56:00:00:16", "acc-01", "Et7"),
            row("10.0.0.17", "00:50:56:00:00:17", "acc-01", "Et8"),
        ], [row("", "00:50:56:00:00:20", "acc-01", "Et9")])}]
        self.after = [{"vlan_id": 3000, "endpoint_table": table([
            row("10.0.0.10", "00:50:56:00:00:10", "acc-01", "Et1"),
            row("10.0.0.11", "00:50:56:00:00:11", "leaf-01", "Et7"),
            row("10.0.0.12", "00:50:56:00:00:99", "acc-01", "Et3"),
            row("10.0.0.16", "00:50:56:00:00:16", "", "", location="uplink_only"),
            row("10.0.0.17", "00:50:56:00:00:17", "acc-01", "Et8"),
            row("10.0.0.30", "00:50:56:00:00:30", "leaf-01", "Et8"),
        ], [row("", "00:50:56:00:00:14", "", "", location="uplink_only"),
            row("", "00:50:56:00:00:14", "acc-01", "Et5")])}]

    def statuses(self, reach=None):
        result = compare_endpoints(self.before, self.after, reach)
        return result, {r["ip"] or r["mac"]: r["status"] for r in result["rows"]}

    def test_each_status(self):
        result, by = self.statuses({"reachable": ["10.0.0.13", "10.0.0.10"], "unreachable": ["10.0.0.17"]})
        self.assertEqual(by, {
            "10.0.0.10": "same_port",            # MAC written differently, same MAC
            "10.0.0.11": "moved",
            "10.0.0.12": "mac_changed",
            "10.0.0.13": "reachable_not_learned",
            "10.0.0.14": "mac_only",             # the edge row wins over the trunk-only row
            "10.0.0.15": "missing",
            "10.0.0.16": "not_located",
            "10.0.0.17": "ping_failed",
            "00:50:56:00:00:20": "missing",
        })
        self.assertTrue(result["compared"])
        self.assertEqual(result["problems"], 4)
        self.assertEqual(result["new"], [{"vlan_id": 3000, "ip": "10.0.0.30", "mac": "00:50:56:00:00:30",
                                          "switch": "leaf-01", "interface": "Et8"}])
        notes = {r["ip"]: r["note"] for r in result["rows"]}
        self.assertEqual(notes["10.0.0.15"], "intermittent before the change")
        self.assertEqual(notes["10.0.0.14"], "no ARP entry after the change")
        self.assertTrue(result["counts_line"].startswith("2 missing"))

    def test_without_pings_a_missing_endpoint_stays_missing(self):
        _, by = self.statuses()
        self.assertEqual((by["10.0.0.13"], by["10.0.0.17"]), ("missing", "same_port"))

    def test_nothing_after_means_not_compared(self):
        result = compare_endpoints(self.before, [])
        self.assertFalse(result["compared"])
        self.assertTrue(all(r["status"] == "missing" for r in result["rows"]))

    def test_csv_and_strip(self):
        result = compare_endpoints(self.before, self.after)
        rows = list(csv.DictReader(io.StringIO(endpoint_comparison_csv(result))))
        self.assertEqual(len(rows), len(result["rows"]))
        self.assertEqual(rows[1]["after_switch"], "leaf-01")
        self.assertEqual(strip_endpoint_tables([{"vlan_id": 1, "endpoint_table": {"x": 1}, "devices": [1]}]),
                         [{"vlan_id": 1, "endpoint_table": {"x": 1}}])
        self.assertTrue(re.match(r"^vlan_id,ip,mac,status,ping,", endpoint_comparison_csv(None)))


if __name__ == "__main__":
    unittest.main()
