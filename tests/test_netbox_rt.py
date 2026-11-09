#!/usr/bin/env python3
"""VNI and route target from NetBox IPAM route targets (plugins/vlan_lib/netbox_rt.py)."""

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

from vlan_lib.netbox import netbox_vlan_pick  # noqa: E402
from vlan_lib.netbox_rt import (  # noqa: E402
    allocate_route_target,
    existing_route_target,
    name_prefixes,
    pool_for_tenant,
    route_target_parts,
    taken_by_other_vlan,
    used_numbers,
)

RANGES = {
    "lisle": {"internal": {"start": 20000, "end": 21999}, "external": {"start": 22000, "end": 23999}},
    "omaha": {"internal": {"start": 10000, "end": 10999}, "external": {"start": 11000, "end": 11999}},
}
# The Lisle external route targets seen on 2026-11-05: 22030 and 22032-22034 are free.
LISLE = ["22027:2324", "22028:1484", "22029:1483", "22031:1648", "22035:1471", "22036:1473",
         "22037:1474", "22038:1475", "22039:3478", "22040:2202"] + ["%d:%d" % (n, n - 20000) for n in range(22000, 22027)]


class NetboxRouteTargetTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(route_target_parts("10010:3052"), (10010, 3052))
        self.assertIsNone(route_target_parts("65000:10010:3052"))
        self.assertIsNone(route_target_parts("rt-10010"))

    def test_prefixes_cover_each_range(self):
        self.assertEqual(name_prefixes((10000, 10999)), ["10"])
        self.assertEqual(name_prefixes((11000, 11999)), ["11"])
        self.assertEqual(name_prefixes((20000, 21999)), ["20", "21"])
        self.assertEqual(name_prefixes((22000, 23999)), ["22", "23"])
        self.assertEqual(name_prefixes((5, 1200)), [str(digit) for digit in range(1, 10)])

    def test_fnts_is_internal_everyone_else_external(self):
        self.assertEqual(pool_for_tenant({"name": "0000001"})["pool"], "internal")
        self.assertEqual(pool_for_tenant("0000874")["pool"], "external")
        self.assertEqual(pool_for_tenant("0000910")["pool"], "external")
        self.assertEqual(pool_for_tenant("", customer_type="internal")["source"], "customer_type internal (no tenant)")
        self.assertEqual(pool_for_tenant(None)["pool"], "external")

    def test_existing_route_target_belongs_to_the_dc_by_range(self):
        rts = [{"name": "10010:3052", "tenant": {"name": "0000001"}, "id": 7}, {"name": "22027:3052"}]
        omaha = existing_route_target(rts, 3052, "omaha", RANGES)
        self.assertEqual((omaha["found"], omaha["vni"], omaha["pool"], omaha["tenant"]), (True, 10010, "internal", "0000001"))
        self.assertEqual(omaha["elsewhere"], ["22027:3052"])
        self.assertEqual(existing_route_target(rts, 3052, "lisle", RANGES)["vni"], 22027)
        self.assertFalse(existing_route_target(rts, 3060, "omaha", RANGES)["found"])
        two = existing_route_target(rts + [{"name": "11003:3052"}], 3052, "omaha", RANGES)
        self.assertFalse(two["found"])
        self.assertIn("NetBox has 2 route targets for VLAN 3052 in omaha's ranges (10010:3052, 11003:3052)", two["problems"][0])

    def test_lowest_free_number_fills_gaps(self):
        result = allocate_route_target(3052, "lisle", "external", RANGES, LISLE)
        self.assertEqual((result["vni"], result["route_target"]), (22030, "22030:3052"))
        self.assertEqual(result["reason"], "lowest free number in lisle external 22000-23999 (37 taken)")
        # L2VPN identifiers and VNIs outside NetBox count as taken.
        self.assertEqual(allocate_route_target(3052, "lisle", "external", RANGES, LISLE, [22030], [22032])["vni"], 22033)

    def test_used_numbers_stay_in_the_range(self):
        used = used_numbers(["100:5", "10010:3052", "11000:2100", "bad"], (10000, 10999), ["10012", None], [10500, 99])
        self.assertEqual(used, {10010, 10012, 10500})

    def test_full_range(self):
        names = ["%d:1" % n for n in range(11000, 12000)]
        result = allocate_route_target(3061, "omaha", "external", RANGES, names)
        self.assertIsNone(result["vni"])
        self.assertIn("is full", result["reason"])

    def test_vni_taken_by_another_vlan(self):
        self.assertEqual(taken_by_other_vlan(["10011:2011", "10011:3064", "10012:2011"], 10011, 3064), ["10011:2011"])


class VlanPickTests(unittest.TestCase):
    VLANS = {"results": [
        {"id": 501, "vid": 3052, "name": "A", "group": {"slug": "omaha-datacenter", "name": "omaha-datacenter"}},
        {"id": 502, "vid": 3052, "name": "B", "group": {"slug": "lisle-datacenter", "name": "lisle-datacenter"}},
    ]}

    def test_the_dc_group_picks_one_of_two(self):
        self.assertEqual(netbox_vlan_pick(self.VLANS, 3052, "omaha")["vlan"]["id"], 501)
        self.assertEqual(netbox_vlan_pick(self.VLANS, 3052, "Lisle")["vlan"]["id"], 502)

    def test_without_a_dc_it_is_ambiguous(self):
        pick = netbox_vlan_pick(self.VLANS, 3052)
        self.assertEqual(pick["vlan"], {})
        self.assertIn("NetBox has 2 VLANs with VID 3052", pick["problem"])
        self.assertTrue(netbox_vlan_pick(self.VLANS, 3052, "dallas")["problem"])


if __name__ == "__main__":
    unittest.main()
