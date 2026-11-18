#!/usr/bin/env python3
"""NetBox drift (vlan_lib/netbox_drift.py) and the VLAN export plan
(vlan_lib/netbox_export.py): pure functions, no NetBox."""

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

from vlan_lib.netbox_drift import netbox_drift_markdown, netbox_vlan_drift  # noqa: E402
from vlan_lib.netbox_export import netbox_export_plan, record_vrf  # noqa: E402

RECORD = {
    "id": 3000, "name": "ACME_APP", "vni": 11002, "vrf": "v0000822a",
    "prefixes": ["10.230.0.0/24"], "ownership": {"tenant": "0000822"},
}
VLAN = {"id": 300, "vid": 3000, "name": "ACME_APP", "tenant": {"id": 11, "name": "0000822"}}
EVPN = {"found": True, "vni": 11002, "import": ["11002:3000"], "export": ["11002:3000"], "name": "ACME_APP",
        "problems": [], "note": ""}


def _nb(**over):
    nb = {
        "vlan": VLAN, "vlan_problem": "", "evpn": EVPN,
        "route_targets": {"11002:3000": [{"id": 41, "name": "11002:3000", "tenant": {"name": "0000822"}}]},
        "prefixes": {"10.230.0.0/24": [{"id": 71, "prefix": "10.230.0.0/24", "vrf": {"id": 3, "name": "v0000822a"},
                                        "vlan": {"id": 300}}]},
    }
    nb.update(over)
    return nb


def _status(report, field):
    return {i["field"]: i["status"] for i in report["items"]}.get(field)


class DriftTests(unittest.TestCase):
    def test_in_sync(self):
        report = netbox_vlan_drift(RECORD, _nb())
        self.assertEqual(report["status"], "in_sync", report)
        self.assertEqual(report["netbox_vlan_id"], 300)

    def test_missing_vlan(self):
        report = netbox_vlan_drift(RECORD, _nb(vlan={}))
        self.assertEqual(report["status"], "missing")

    def test_ambiguous_is_an_error(self):
        report = netbox_vlan_drift(RECORD, _nb(vlan={}, vlan_problem="NetBox has 2 VLANs with VID 3000"))
        self.assertEqual(report["status"], "error")

    def test_vni_and_targets_differ(self):
        report = netbox_vlan_drift(dict(RECORD, vni=11005), _nb())
        self.assertEqual(report["status"], "drift")
        self.assertEqual(_status(report, "vni"), "differs")
        self.assertEqual(_status(report, "l2vpn_import_target"), "differs")
        self.assertEqual(_status(report, "ipam_route_target"), "missing_in_netbox")

    def test_vni_only_in_netbox_is_not_drift(self):
        report = netbox_vlan_drift(dict(RECORD, vni=None), _nb())
        self.assertEqual(_status(report, "vni"), "netbox_only")
        self.assertEqual(_status(report, "l2vpn_import_target"), "ok")
        self.assertEqual(report["status"], "in_sync")

    def test_tenant_differs(self):
        report = netbox_vlan_drift(dict(RECORD, ownership={"tenant": "0000964"}), _nb())
        self.assertEqual(_status(report, "tenant"), "differs")
        self.assertEqual(_status(report, "route_target_tenant"), "differs")

    def test_prefix_in_another_vrf(self):
        nb = _nb(prefixes={"10.230.0.0/24": [{"id": 71, "vrf": None, "vlan": {"id": 300}}]})
        report = netbox_vlan_drift(RECORD, nb)
        self.assertEqual(_status(report, "prefix 10.230.0.0/24 vrf"), "differs")

    def test_prefix_on_another_vlan(self):
        nb = _nb(prefixes={"10.230.0.0/24": [{"id": 71, "vrf": {"name": "v0000822a"}, "vlan": {"id": 999}}]})
        report = netbox_vlan_drift(RECORD, nb)
        self.assertEqual(_status(report, "prefix 10.230.0.0/24 vlan"), "differs")

    def test_no_l2vpn(self):
        report = netbox_vlan_drift(RECORD, _nb(evpn={"found": False, "note": "no L2VPN"}))
        self.assertEqual(_status(report, "l2vpn"), "missing_in_netbox")

    def test_markdown(self):
        md = netbox_drift_markdown({"data_center": "omaha", "checked_at": "now",
                                    "reports": [netbox_vlan_drift(dict(RECORD, vni=11005), _nb())]})
        self.assertIn("| drift | 1 |", md)
        self.assertIn("| vni | 11005 | 11002 | differs |", md)


class ExportPlanTests(unittest.TestCase):
    def test_new_vlan_goes_in_the_group_with_its_tenant(self):
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": {}, "tenant": {"id": 11}, "vrf": {"id": 3},
                                                    "group_id": 7, "site_id": 1, "prefixes": {}})
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["vlan"]["action"], "create")
        payload = plan["vlan"]["payload"]
        self.assertEqual((payload["group"], payload["tenant"], payload["vid"]), (7, 11, 3000))
        self.assertNotIn("site", payload)
        self.assertEqual(plan["prefixes"][0]["action"], "create")
        self.assertEqual(plan["prefixes"][0]["payload"]["vrf"], 3)
        self.assertIsNone(plan["prefixes"][0]["payload"]["vlan"])  # filled after the VLAN is created

    def test_existing_vlan_keeps_its_scope(self):
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": VLAN, "tenant": {"id": 11}, "vrf": {"id": 3},
                                                    "group_id": 7, "prefixes": {}})
        self.assertEqual(plan["vlan"]["action"], "update")
        self.assertNotIn("group", plan["vlan"]["payload"])
        self.assertNotIn("site", plan["vlan"]["payload"])

    def test_site_when_no_group(self):
        plan = netbox_export_plan(dict(RECORD, prefixes=[]), "omaha",
                                  {"vlan": {}, "tenant": {"id": 11}, "site_id": 1})
        self.assertEqual(plan["vlan"]["payload"]["site"], 1)

    def test_missing_tenant_or_vrf_stops_the_vlan(self):
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": {}, "tenant": None, "vrf": None, "prefixes": {}})
        self.assertEqual(len(plan["problems"]), 2)

    def test_numeric_tenant_is_a_problem(self):
        plan = netbox_export_plan(dict(RECORD, ownership={"tenant": 455}), "omaha",
                                  {"vlan": {}, "vrf": {"id": 3}, "prefixes": {}})
        self.assertIn("quote the tenant id", plan["problems"][0])

    def test_no_tenant_leaves_it_unset(self):
        plan = netbox_export_plan(dict(RECORD, ownership={}, prefixes=[]), "omaha", {"vlan": {}})
        self.assertEqual(plan["problems"], [])
        self.assertNotIn("tenant", plan["vlan"]["payload"])

    def test_ambiguous_vlan_stops(self):
        plan = netbox_export_plan(RECORD, "omaha", {"vlan_problem": "2 VLANs", "tenant": {"id": 11}, "vrf": {"id": 3}})
        self.assertEqual(plan["problems"], ["2 VLANs"])

    def test_existing_prefix_in_vrf_is_updated_only_when_needed(self):
        same = {"10.230.0.0/24": [{"id": 71, "vrf": {"name": "v0000822a"}, "vlan": {"id": 300}, "tenant": {"id": 11}}]}
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": VLAN, "tenant": {"id": 11}, "vrf": {"id": 3},
                                                    "prefixes": same})
        self.assertEqual(plan["prefixes"][0]["action"], "none")
        moved = {"10.230.0.0/24": [{"id": 71, "vrf": {"name": "v0000822a"}, "vlan": {"id": 9}, "tenant": None}]}
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": VLAN, "tenant": {"id": 11}, "vrf": {"id": 3},
                                                    "prefixes": moved})
        self.assertEqual(plan["prefixes"][0], {"prefix": "10.230.0.0/24", "action": "update", "id": 71,
                                               "payload": {"vlan": 300, "tenant": 11}})

    def test_prefix_in_another_vrf_is_left_alone(self):
        other = {"10.230.0.0/24": [{"id": 71, "vrf": None, "vlan": {"id": 300}}]}
        plan = netbox_export_plan(RECORD, "omaha", {"vlan": VLAN, "tenant": {"id": 11}, "vrf": {"id": 3},
                                                    "prefixes": other})
        self.assertEqual(plan["prefixes"][0]["action"], "create")

    def test_default_vrf_is_global(self):
        self.assertEqual(record_vrf({"vrf": "default"}), "")
        plan = netbox_export_plan(dict(RECORD, vrf="default"), "omaha", {"vlan": VLAN, "tenant": {"id": 11},
                                                                          "prefixes": {}})
        self.assertEqual(plan["problems"], [])
        self.assertIsNone(plan["prefixes"][0]["payload"]["vrf"])


if __name__ == "__main__":
    unittest.main()
