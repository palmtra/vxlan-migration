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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins", "filter"))

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
            "---\n_meta:\n  example: true\nid: 999\nname: example\n",
        )
        self._write(
            "lisle/0100_legacy_web.yml",
            "---\nid: 100\nname: legacy_web\naction: migrate\n",
        )
        result = load_vlan_db_from_directory(self.tempdir, "lisle")
        self.assertEqual([record["id"] for record in result["vlans"]], [100])

    def test_targeted_load_only_matching_prefix(self):
        self._write("lisle/0100_legacy_web.yml", "---\nid: 100\nname: a\n")
        self._write("lisle/0200_legacy_app.yml", "---\nid: 200\nname: b\n")
        result = load_vlan_db_from_directory(self.tempdir, "lisle", [100])
        self.assertEqual([record["id"] for record in result["vlans"]], [100])

    def test_duplicate_vlan_ids_fail(self):
        self._write("lisle/0100_a.yml", "---\nid: 100\nname: a\n")
        self._write("lisle/0100_b.yml", "---\nid: 100\nname: b\n")
        with self.assertRaises(Exception):
            load_vlan_db_from_directory(self.tempdir, "lisle")

    def test_parse_vlan_record_list_wrapper(self):
        record = parse_vlan_record({"vlans": [{"id": 10, "name": "x"}]}, "test.yml")
        self.assertEqual(record["id"], 10)


    def test_coalesce_trimmed(self):
        from vlan_filters import coalesce_trimmed

        self.assertEqual(coalesce_trimmed("", None, "lisle"), "lisle")
        self.assertEqual(coalesce_trimmed("  omaha  ", "lisle"), "omaha")
        self.assertEqual(coalesce_trimmed("", "   ", None), "")

    def test_union_vlan_discovery_hosts(self):
        from vlan_filters import union_vlan_discovery_hosts

        vlans = [
            {"id": 100, "discovery_switches": ["a", "b"], "target_switches": ["x"]},
            {"id": 200, "target_switches": ["b", "c"]},
        ]
        self.assertEqual(union_vlan_discovery_hosts(vlans), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
