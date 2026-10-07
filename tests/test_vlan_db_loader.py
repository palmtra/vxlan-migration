#!/usr/bin/env python3
"""Unit tests for per-VLAN directory loader helpers."""

import os
import shutil
import sys
import tempfile
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
    load_vlan_db_from_directory,
    normalize_target_vlan_ids,
    parse_vlan_record,
    vlan_db_record_filename,
)


class VlanDbLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.dc_dir = os.path.join(self.tempdir, "lisle")
        os.makedirs(self.dc_dir)

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def _write(self, relative_path, content):
        path = os.path.join(self.tempdir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_normalize_target_vlan_ids(self):
        self.assertEqual(normalize_target_vlan_ids(manual_vlan_id=100), [100])
        self.assertEqual(normalize_target_vlan_ids([100, 200]), [100, 200])
        self.assertEqual(normalize_target_vlan_ids("[100,200]"), [100, 200])
        self.assertEqual(normalize_target_vlan_ids("100,200"), [100, 200])

    def test_vlan_db_record_filename(self):
        self.assertEqual(vlan_db_record_filename(100, "legacy_web"), "0100_legacy_web.yml")
        self.assertEqual(vlan_db_record_filename(2911, "Customer WiFi"), "2911_Customer_WiFi.yml")

    def test_skips_example_files(self):
        self._write(
            "lisle/_example.yml",
            "---\n_meta:\n  example: true\nid: 999\nname: example\naction: migrate\nservice_type: l3\n",
        )
        self._write(
            "lisle/0100_legacy_web.yml",
            "---\nid: 100\nname: legacy_web\naction: migrate\nservice_type: l3\n",
        )
        result = load_vlan_db_from_directory(self.tempdir, "lisle")
        self.assertEqual([record["id"] for record in result["vlans"]], [100])

    def test_targeted_load_only_matching_prefix(self):
        self._write(
            "lisle/0100_legacy_web.yml",
            "---\nid: 100\nname: a\naction: migrate\nservice_type: l2\n",
        )
        self._write(
            "lisle/0200_legacy_app.yml",
            "---\nid: 200\nname: b\naction: migrate\nservice_type: l3\n",
        )
        result = load_vlan_db_from_directory(self.tempdir, "lisle", [100])
        self.assertEqual([record["id"] for record in result["vlans"]], [100])

    def test_duplicate_vlan_ids_fail(self):
        self._write(
            "lisle/0100_a.yml",
            "---\nid: 100\nname: a\naction: migrate\nservice_type: l3\n",
        )
        self._write(
            "lisle/0100_b.yml",
            "---\nid: 100\nname: b\naction: migrate\nservice_type: l3\n",
        )
        with self.assertRaises(Exception):
            load_vlan_db_from_directory(self.tempdir, "lisle")

    def test_parse_vlan_record_list_wrapper(self):
        record = parse_vlan_record(
            {"vlans": [{"id": 10, "name": "x", "action": "migrate", "service_type": "l2"}]},
            "test.yml",
        )
        self.assertEqual(record["id"], 10)

    def test_schema_rejects_invalid_service_type(self):
        with self.assertRaises(Exception):
            parse_vlan_record(
                {"id": 1, "name": "x", "action": "migrate", "service_type": "l4"},
                "bad.yml",
            )

    def test_schema_rejects_deprecated_l2_l3(self):
        with self.assertRaises(Exception):
            parse_vlan_record(
                {"id": 1, "name": "x", "action": "migrate", "service_type": "l2_l3"},
                "legacy.yml",
            )

    def test_nested_deployment_model_flattens(self):
        record = parse_vlan_record(
            {
                "site": {
                    "data_center": "lisle",
                    "vlan": {"id": 100, "name": "legacy_web"},
                },
                "migration": {"type": "L3", "protected_vlan": False},
                "routing": {
                    "vrf": "tenant",
                    "prefixes": ["10.0.0.0/24"],
                    "gateway_ip": "10.0.0.1",
                },
                "evpn": {
                    "l2": {"vni": 50100, "rt": "65001:100"},
                    "l3": {"vni": None, "rt_import": "", "rt_export": ""},
                },
                "placement": {
                    "participating_leafs": [
                        {"hostname": "cma01-blf01"},
                        {"hostname": "fntc-aggacc-sw05"},
                    ],
                    "gateway_leafs": [{"hostname": "cma01-blf01"}],
                    "prune_eligible_leafs": [{"hostname": "agg01"}],
                    "source_gateway_devices": [{"hostname": "mls01"}],
                },
            },
            "nested.yml",
        )
        self.assertEqual(record["service_type"], "l3")
        self.assertEqual(record["vni"], 50100)
        self.assertEqual(record["vrf"], "tenant")
        self.assertEqual(record["gateway"], "10.0.0.1")
        self.assertEqual(
            record["target_switches"], ["cma01-blf01", "fntc-aggacc-sw05"]
        )
        self.assertEqual(record["gateway_leafs"], ["cma01-blf01"])
        self.assertEqual(
            record["discovery_switches"],
            ["cma01-blf01", "fntc-aggacc-sw05", "agg01", "mls01"],
        )


    def test_coalesce_trimmed(self):
        from vlan_filters import coalesce_trimmed, resolve_data_center

        self.assertEqual(coalesce_trimmed("", None, "lisle"), "lisle")
        self.assertEqual(coalesce_trimmed("  omaha  ", "lisle"), "omaha")
        self.assertEqual(coalesce_trimmed("", "   ", None), "")
        self.assertEqual(coalesce_trimmed(["lisle", "", "lisle"]), "lisle")
        self.assertEqual(resolve_data_center("lisle", "", ""), "lisle")
        self.assertEqual(resolve_data_center("", "omaha", "lisle"), "omaha")

    def test_union_vlan_discovery_hosts(self):
        from vlan_filters import union_vlan_discovery_hosts

        vlans = [
            {"id": 100, "discovery_switches": ["a", "b"], "target_switches": ["x"]},
            {"id": 200, "target_switches": ["b", "c"]},
        ]
        self.assertEqual(union_vlan_discovery_hosts(vlans), ["a", "b", "c"])

    def test_mac_discovery_command(self):
        from vlan_filters import mac_discovery_command

        self.assertEqual(
            mac_discovery_command("eos", 810),
            "show mac address-table dynamic vlan 810",
        )
        self.assertEqual(
            mac_discovery_command("nxos", 100),
            "show mac address-table dynamic vlan 100",
        )

    def test_mac_table_has_learned_addresses_eos_dynamic(self):
        from vlan_filters import mac_table_has_learned_addresses

        eos_output = """
          Mac Address Table
------------------------------------------------------------------
Vlan    Mac Address       Type        Ports      Moves   Last Move
----    -----------       ----        -----      -----   ---------
 810    0011.2233.4455    DYNAMIC     Et23
Total Mac Addresses for this criterion: 1
"""
        self.assertTrue(mac_table_has_learned_addresses(eos_output))
        self.assertFalse(mac_table_has_learned_addresses("Total Mac Addresses for this criterion: 0"))

    def test_parse_vlan_id_ports_eos_access_and_trunk(self):
        from vlan_filters import parse_vlan_id_ports

        eos_output = """
VLAN  Name                             Status    Ports
----- -------------------------------- --------- -------------------------------
810   vlan_810                         active    Et23, Et24
                                                Po1
"""
        result = parse_vlan_id_ports(
            eos_output,
            810,
            "eos",
            ["Ethernet23", "Port-Channel1"],
        )
        self.assertTrue(result["vlan_present"])
        self.assertTrue(result["has_port_membership"])
        self.assertIn("Et24", result["access_ports"])
        self.assertIn("Et23", result["trunk_ports"])
        self.assertIn("Po1", result["trunk_ports"])

    def test_parse_vlan_id_ports_empty_ports(self):
        from vlan_filters import parse_vlan_id_ports

        eos_output = """
VLAN  Name                             Status    Ports
----- -------------------------------- --------- -------------------------------
810   vlan_810                         active
"""
        result = parse_vlan_id_ports(eos_output, 810, "eos", [])
        self.assertTrue(result["vlan_present"])
        self.assertFalse(result["has_port_membership"])
        self.assertEqual(result["all_ports"], [])

    def test_parse_vlan_id_ports_nxos(self):
        from vlan_filters import parse_vlan_id_ports

        nxos_output = """
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
810  vlan810                           active    Eth1/23, Eth1/24
"""
        result = parse_vlan_id_ports(
            nxos_output,
            810,
            "nxos",
            ["Eth1/23"],
            ["Eth1/23"],
        )
        self.assertEqual(result["trunk_ports"], [])
        self.assertEqual(result["exempt_trunk_ports"], ["Eth1/23"])
        self.assertEqual(result["access_ports"], ["Eth1/24"])

    def test_trunk_interface_is_maintenance_exempt(self):
        from vlan_filters import trunk_interface_is_maintenance_exempt

        eos_cfg = """
interface Port-Channel1
 switchport mode trunk
 switchport trunk group mlagpeer
"""
        nxos_cfg = """
interface port-channel1
 switchport mode trunk
 vpc peer-link
"""
        self.assertTrue(trunk_interface_is_maintenance_exempt(eos_cfg, "eos"))
        self.assertFalse(trunk_interface_is_maintenance_exempt("switchport mode trunk", "eos"))
        self.assertTrue(trunk_interface_is_maintenance_exempt(nxos_cfg, "nxos"))
        self.assertFalse(trunk_interface_is_maintenance_exempt("switchport mode trunk", "nxos"))


if __name__ == "__main__":
    unittest.main()
