#!/usr/bin/env python3
"""Unit tests for the VNI and route targets read from NetBox L2VPNs."""

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

from vlan_lib.netbox import merge_netbox_evpn, netbox_l2vpn_evpn, netbox_vlan_pick  # noqa: E402
from vlan_lib.placement import bgp_vlan_evpn  # noqa: E402


def l2vpn(identifier=11002, imports=("11002:3000",), exports=("11002:3000",), kind="vxlan-evpn", name="ACME_APP", lid=5):
    return {
        "id": lid, "identifier": identifier, "name": name,
        "type": {"value": kind, "label": kind.upper()},
        "import_targets": [{"id": 40 + i, "name": n} for i, n in enumerate(imports)],
        "export_targets": [{"id": 50 + i, "name": n} for i, n in enumerate(exports)],
    }


TERMS = {"results": [{"id": 9, "l2vpn": {"id": 5, "identifier": 11002, "name": "ACME_APP", "type": {"value": "vxlan-evpn"}}}]}


class VlanPickTests(unittest.TestCase):
    def test_one_match(self):
        pick = netbox_vlan_pick({"results": [{"id": 300, "vid": 3000, "name": "ACME_APP"}]}, 3000)
        self.assertEqual((pick["vlan"]["id"], pick["problem"]), (300, ""))
        self.assertEqual(netbox_vlan_pick({"results": []}, 3000)["vlan"], {})

    def test_several_matches_need_a_scope(self):
        pick = netbox_vlan_pick({"results": [
            {"id": 1, "name": "A", "site": {"slug": "oma01"}},
            {"id": 2, "name": "B", "group": {"slug": "lisle"}},
        ]}, 3000)
        self.assertEqual(pick["vlan"], {})
        self.assertIn("NetBox has 2 VLANs with VID 3000 (A (id 1, site oma01); B (id 2, group lisle))", pick["problem"])
        self.assertIn("netbox_vlan_query_by_dc", pick["problem"])


class L2vpnTests(unittest.TestCase):
    def test_full_object_wins_over_the_termination_summary(self):
        evpn = netbox_l2vpn_evpn(TERMS, [{"json": l2vpn(imports=("65000:1",), exports=("65000:2",))}])
        self.assertEqual((evpn["found"], evpn["vni"], evpn["import"], evpn["export"]), (True, 11002, ["65000:1"], ["65000:2"]))

    def test_summary_only_still_gives_the_vni(self):
        evpn = netbox_l2vpn_evpn(TERMS, [])
        self.assertEqual((evpn["vni"], evpn["import"], evpn["export"]), (11002, [], []))

    def test_not_evpn_and_none(self):
        other = netbox_l2vpn_evpn({"results": []}, [{"json": l2vpn(kind="vpls")}])
        self.assertFalse(other["found"])
        self.assertIn("not VXLAN-EVPN: ACME_APP (vpls)", other["note"])
        self.assertEqual(netbox_l2vpn_evpn({"results": []}, [])["note"], "NetBox has no L2VPN terminated on this VLAN")

    def test_bad_or_several_l2vpns_are_problems(self):
        no_id = netbox_l2vpn_evpn({}, [{"json": l2vpn(identifier=None)}])
        self.assertEqual(no_id["problems"], ["NetBox L2VPN ACME_APP has no identifier (the VNI)"])
        two = netbox_l2vpn_evpn({}, [{"json": l2vpn()}, {"json": l2vpn(name="OTHER", lid=6)}])
        self.assertIn("terminated on 2 VXLAN-EVPN L2VPNs", two["problems"][0])


class MergeTests(unittest.TestCase):
    def test_netbox_fills_what_the_record_leaves_empty(self):
        merged = merge_netbox_evpn({"id": 3000, "name": "ACME_APP"}, netbox_l2vpn_evpn({}, [{"json": l2vpn()}]))
        self.assertEqual(merged["problems"], [])
        self.assertEqual((merged["record"]["vni"], merged["record"]["l2_rt"]), (11002, "11002:3000"))
        self.assertEqual((merged["vni_source"], merged["rt_source"]), ("netbox", "netbox"))

    def test_agreeing_values_are_fine(self):
        merged = merge_netbox_evpn({"id": 3000, "vni": "11002", "l2_rt": "11002:3000"},
                                   netbox_l2vpn_evpn({}, [{"json": l2vpn()}]))
        self.assertEqual((merged["problems"], merged["vni_source"]), ([], "record and netbox"))

    def test_conflicts_leave_the_record_unchanged(self):
        record = {"id": 3000, "vni": 11009, "l2_rt": "11009:3000"}
        merged = merge_netbox_evpn(record, netbox_l2vpn_evpn({}, [{"json": l2vpn()}]))
        self.assertEqual(merged["record"], record)
        self.assertEqual(merged["problems"], [
            "VNI conflict for VLAN 3000: the record says 11009, NetBox L2VPN ACME_APP says 11002",
            "Route-target conflict for VLAN 3000: the record says 11009:3000, NetBox L2VPN ACME_APP says 11002:3000",
        ])

    def test_split_targets_render_import_and_export(self):
        merged = merge_netbox_evpn({"id": 3000, "vni": 11002},
                                   netbox_l2vpn_evpn({}, [{"json": l2vpn(imports=("65000:3000",), exports=("65000:3001",))}]))
        record = merged["record"]
        self.assertNotIn("l2_rt", record)
        self.assertEqual((record["l2_rt_import"], record["l2_rt_export"]), ("65000:3000", "65000:3001"))
        bgp = bgp_vlan_evpn(record, "leaf1", "65001", "10.0.0.1")
        self.assertEqual((bgp["rt_import"], bgp["rt_export"]), ("65000:3000", "65000:3001"))
        self.assertNotIn("rt_both", bgp)

    def test_more_than_one_target_is_a_problem(self):
        merged = merge_netbox_evpn({"id": 3000}, netbox_l2vpn_evpn({}, [{"json": l2vpn(imports=("1:1", "1:2"))}]))
        self.assertIn("more than one import or export route target", merged["problems"][0])

    def test_no_route_targets_keeps_the_derived_one(self):
        merged = merge_netbox_evpn({"id": 3000}, netbox_l2vpn_evpn({}, [{"json": l2vpn(imports=(), exports=())}]))
        self.assertEqual(merged["record"]["vni"], 11002)
        self.assertNotIn("l2_rt", merged["record"])
        self.assertIn("has no route targets; using <vni>:<vlan id>", merged["notes"][0])
        self.assertEqual(bgp_vlan_evpn(merged["record"], "leaf1", "65001", "10.0.0.1")["rt_both"], "11002:3000")

    def test_no_l2vpn_leaves_the_record_alone(self):
        merged = merge_netbox_evpn({"id": 3000, "vni": 11002}, netbox_l2vpn_evpn({"results": []}, []))
        self.assertEqual((merged["record"], merged["problems"]), ({"id": 3000, "vni": 11002}, []))
        self.assertEqual(merged["notes"], ["NetBox has no L2VPN terminated on this VLAN"])


if __name__ == "__main__":
    unittest.main()
