#!/usr/bin/env python3
"""MOP review rules: one VLAN per trunk line, the VLAN record's VNI wins, the
source gateway waits for validation, and the MOP steps follow the cutover
order (manual SVI shutdown, deploy, validate, BGP withdrawal, teardown)."""

import json
import os
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

from vlan_lib.db import load_starter_vnis, load_vlan_db_from_directory  # noqa: E402
from vlan_lib.mop import build_mop_input, gateway_validated_vlans  # noqa: E402
from vlan_lib.prune import build_device_prune_plan  # noqa: E402
from vlan_lib.reports import build_action_items, build_vlan_discovery_reports, gate_teardown_plans  # noqa: E402
from vlan_lib.schema import validate_against_schema  # noqa: E402

RANGES = {"omaha": {"internal": {"start": 10000, "end": 10999}, "external": {"start": 11000, "end": 11999}}}


def _per_vlan(vlan_id, **kwargs):
    entry = {
        "vlan_id": vlan_id,
        "os_family": "nxos",
        "vlan_present": True,
        "svi_present": False,
        "trunk_ports": ["port-channel40"],
        "switch_uplink_ports": ["port-channel40"],
        "compute_ports": [],
        "unknown_ports": [],
    }
    entry.update(kwargs)
    return entry


def _device(hostname, **kwargs):
    device = {
        "hostname": hostname,
        "os_family": "nxos",
        "vlan_present": True,
        "svi_present": False,
        "svi_vrf": "",
        "svi_details": {},
        "compute_ports": [],
        "access_ports": [],
        "unknown_ports": [],
        "switch_uplink_ports": ["port-channel40"],
        "trunk_ports": ["port-channel40"],
        "endpoint_mac_entries": [],
        "mac_entries": [],
        "port_attachments": [],
        "arp_entries": [],
        "bgp_as": "",
    }
    device.update(kwargs)
    return device


class OneVlanPerTrunkLineTests(unittest.TestCase):
    def test_device_plan_keeps_vlans_on_separate_lines(self):
        plan = build_device_prune_plan("sw16", [_per_vlan(3039), _per_vlan(3040)])
        trunk = [a for a in plan["actions"] if a["op"] == "trunk_remove_vlans"]
        self.assertEqual([a["vlans"] for a in trunk], [[3039], [3040]])
        for action in trunk:
            self.assertNotIn(",", action["cli"])
        self.assertNotIn("3039,3040", plan["execution"]["full_cli"])

    def test_vlan_report_carries_only_its_own_vlan(self):
        # A device plan from a run of 3039 and 3040 together, with the old merged line.
        merged = {
            "op": "trunk_remove_vlans",
            "interface": "port-channel40",
            "vlans": [3039, 3040],
            "cli": "interface port-channel40\n  switchport trunk allowed vlan remove 3039,3040",
        }
        device = _device("sw07", prune_plan={"os_family": "nxos", "actions": [merged], "status": "candidate"})
        hostvars = {"sw07": {"_vlan_discovery": device}}
        reports = build_vlan_discovery_reports(
            [{"id": 3039, "name": "a", "service_type": "l2"}, {"id": 3040, "name": "b", "service_type": "l2"}],
            ["sw07"],
            hostvars,
        )
        for report in reports:
            for plan in report["prune_plans"]:
                text = json.dumps(plan)
                self.assertNotIn("3039,3040", text)
                for action in plan.get("actions") or []:
                    self.assertEqual(action["vlans"], [report["vlan_id"]])
                    self.assertTrue(action["cli"].endswith("remove %s" % report["vlan_id"]))


class SourceGatewayGateTests(unittest.TestCase):
    @staticmethod
    def _plans():
        return [
            {"hostname": "mls01", "placement_role": "prune_eligible", "source_gateway": True,
             "actions": [{"op": "no_interface_vlan", "cli": "no interface Vlan3040"}]},
            {"hostname": "sw07", "placement_role": "prune_eligible", "actions": [{"op": "no_vlan", "cli": "no vlan 3040"}]},
        ]

    def test_shut_svi_still_waits_for_validation(self):
        devices = [
            {"hostname": "mls01", "svi_present": True, "svi_details": {"ip_addresses": ["10.1.1.2/24"], "shutdown": True}},
            {"hostname": "sw07"},
        ]
        plans = gate_teardown_plans(self._plans(), devices)
        self.assertEqual(plans[0]["status"], "blocked_until_gateway_validated")
        self.assertEqual(plans[0]["actions"], [])
        self.assertTrue(plans[0]["blocked_actions"])
        self.assertTrue(plans[1]["actions"])  # not a source gateway
        released = gate_teardown_plans(self._plans(), devices, gateway_validated=True)
        self.assertTrue(released[0]["actions"])

    def test_active_svi_waits_for_the_move_even_when_validated(self):
        devices = [{"hostname": "mls01", "svi_present": True, "svi_details": {"ip_addresses": ["10.1.1.2/24"]}}]
        plans = gate_teardown_plans(self._plans(), devices, gateway_validated=True)
        self.assertEqual(plans[0]["status"], "blocked_until_gateway_moved")

    def test_validated_vlans_from_migrate_summary_or_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = os.path.join(tmp, "omaha", "_runs")
            os.makedirs(runs)

            def summary(vid, ts, outcome, problems):
                with open(os.path.join(runs, "migrate_vlan%s_%s.json" % (vid, ts)), "w") as handle:
                    json.dump({"outcome": outcome, "endpoints": {"compared": True, "problems": problems}}, handle)

            summary(3040, "20261101T010000Z", "deployed_and_verified", 2)
            summary(3040, "20261102T010000Z", "deployed_and_verified", 0)  # newest wins
            summary(3039, "20261102T010000Z", "verification_failed", 0)
            self.assertEqual(gateway_validated_vlans(tmp, "omaha", [3039, 3040]), [3040])
            self.assertEqual(gateway_validated_vlans(tmp, "omaha", [3039, 3040], "true"), [3039, 3040])
            self.assertEqual(gateway_validated_vlans(tmp, "omaha", [3039, 3040], "3039"), [3039])
            self.assertEqual(gateway_validated_vlans(tmp, "lisle", [3040]), [])


class VniAuthorityTests(unittest.TestCase):
    ALLOCATION = {"ranges": RANGES, "default_pool": "internal"}

    def test_record_vni_is_never_replaced(self):
        vlan = {"id": 3040, "name": "app", "service_type": "l2", "data_center": "omaha", "vni": 10010}
        report = build_vlan_discovery_reports(
            [vlan], [], {}, [], dict(self.ALLOCATION, records=[vlan], starter_vnis={"3040": {"vni": 10500, "path": "x"}})
        )[0]
        self.assertEqual(report["deployment_model"]["evpn"]["l2"]["vni"], 10010)
        self.assertEqual(report["vni_source"], "record")
        self.assertEqual(report["vni_allocation"], {})
        self.assertTrue(report["in_vlan_db"])

    def test_vlan_outside_the_db_keeps_its_starter_vni(self):
        probe = {"id": 3040, "name": "vlan_3040", "service_type": "l2", "data_center": "omaha", "_probe": True}
        report = build_vlan_discovery_reports(
            [probe], [], {}, [],
            dict(self.ALLOCATION, records=[], starter_vnis={"3040": {"vni": 10010, "path": "reports/omaha/app/3040_app.yml"}}),
        )[0]
        self.assertEqual(report["deployment_model"]["evpn"]["l2"]["vni"], 10010)
        self.assertEqual(report["deployment_model"]["evpn"]["l2"]["vni_source"], "starter")
        self.assertNotIn("vni_proposed", report["deployment_model"]["evpn"]["l2"])
        items = build_action_items(dict(report, switches_vlan_defined=["leaf1"]))
        self.assertTrue(items[0].startswith("VLAN 3040 is not in the VLAN DB (vars/vlans/omaha/). Copy the starter "
                                            "record discovery wrote (reports/omaha/app/3040_app.yml)"))
        self.assertIn("VNI 10010 kept from the starter record reports/omaha/app/3040_app.yml. "
                      "Confirm it in evpn.l2.vni of the VLAN record.", items)

    def test_starter_vni_used_by_another_record_is_a_blocker(self):
        probe = {"id": 3040, "name": "vlan_3040", "service_type": "l2", "data_center": "omaha", "_probe": True}
        other = {"id": 3001, "name": "other", "service_type": "l2", "vni": 10010}
        report = build_vlan_discovery_reports(
            [probe], [], {}, [], dict(self.ALLOCATION, records=[other], starter_vnis={"3040": {"vni": 10010, "path": "p"}})
        )[0]
        doc = build_mop_input(dict(report, switches_vlan_defined=["leaf1"]), {}, "T")
        blockers = [f["text"] for f in doc["findings"] if f["kind"] == "blocker"]
        self.assertIn("VNI 10010 from the starter record is already the VNI of VLAN 3001 in the VLAN DB. "
                      "Pick another VNI in the starter record.", blockers)
        self.assertEqual(doc["vlan"]["l2_vni_source"], "starter")
        self.assertEqual(doc["vlan"]["status"], "blocked")

    def test_only_a_vlan_with_no_vni_gets_a_proposal(self):
        probe = {"id": 3040, "name": "vlan_3040", "service_type": "l2", "data_center": "omaha", "_probe": True}
        report = build_vlan_discovery_reports([probe], [], {}, [], dict(self.ALLOCATION, records=[]))[0]
        self.assertEqual(report["vni_source"], "proposed")
        self.assertEqual(report["deployment_model"]["evpn"]["l2"]["vni"], 10000)

    def test_starter_vnis_are_read_from_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "reports", "omaha", "app")
            os.makedirs(folder)
            with open(os.path.join(folder, "3040_app.yml"), "w") as handle:
                handle.write("site: {data_center: omaha, vlan: {id: 3040, name: app}}\nmigration: {type: l2}\n"
                             "evpn: {l2: {vni: 10010}}\nplacement: {participating_leafs: []}\n")
            with open(os.path.join(folder, "3039_other.yml"), "w") as handle:
                handle.write("site: {data_center: omaha, vlan: {id: 3039, name: other}}\nmigration: {type: l2}\n"
                             "evpn: {l2: {vni: 10011}}\nplacement: {participating_leafs: []}\n")
            reports_dir = os.path.join(tmp, "reports")
            found = load_starter_vnis(reports_dir, "omaha", [3040, 3039], skip_ids=[3039])
            self.assertEqual(found, {"3040": {"vni": 10010, "path": "reports/omaha/app/3040_app.yml"}})
            self.assertEqual(load_starter_vnis(reports_dir, "lisle", [3040]), {})


class VlanDbFileNameTests(unittest.TestCase):
    RECORD = "id: %d\nname: app\naction: migrate\nservice_type: l2\nvrf: default\nvni: 10010\ntarget_switches: [leaf1]\n"

    def _load(self, filename, vid):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "omaha"))
            with open(os.path.join(tmp, "omaha", filename), "w") as handle:
                handle.write(self.RECORD % vid)
            return load_vlan_db_from_directory(tmp, "omaha", [vid])

    def test_unpadded_name_is_found(self):
        loaded = self._load("100_app.yml", 100)
        self.assertEqual([r["id"] for r in loaded["vlans"]], [100])
        self.assertEqual(loaded["warnings"], [])

    def test_misnamed_file_is_found_and_named(self):
        loaded = self._load("vlan3040.yml", 3040)
        self.assertEqual([r["vni"] for r in loaded["vlans"]], [10010])
        self.assertEqual(loaded["warnings"], ["VLAN 3040 is in vlan3040.yml: rename it 3040_app.yml so the VLAN DB finds it by name."])


def _l3_report(**overrides):
    """L3 VLAN 3040: HSRP on two NX-OS MLSs, a BGP network on mls01, one non-gateway prune."""
    svis = [
        {"hostname": host, "vrf": "v0000001b", "ip_addresses": [ip], "fhrp_groups": []}
        for host, ip in (("mls01", "10.101.26.2/24"), ("mls02", "10.101.26.3/24"))
    ]

    def nxos_plan(host, actions, status="ready"):
        lines = "\n".join(a["cli"] for a in actions)
        return {
            "hostname": host, "os_family": "nxos", "vlan_ids": [3040], "placement_role": "prune_eligible",
            "phase": 3, "phase_label": "teardown", "status": status, "source_gateway": host.startswith("mls"),
            "actions": actions if status == "ready" else [], "blocked_actions": [] if status == "ready" else actions,
            "execution": {"platform": "nxos", "full_cli": "checkpoint prune_v3040\nconfigure terminal\n%s\nend\n" % lines,
                          "rollback": "rollback running-config checkpoint prune_v3040",
                          "save": "copy running-config startup-config"},
        }

    trunk = {"op": "trunk_remove_vlans", "interface": "port-channel1", "vlans": [3040],
             "cli": "interface port-channel1\n  switchport trunk allowed vlan remove 3040"}
    report = {
        "vlan_id": 3040,
        "vlan_name": "APP",
        "vlan_slug": "APP",
        "data_center": "omaha",
        "migration_type": "l3",
        "gateway_assessment": {"location": "legacy", "vrf": "v0000001b", "virtual_ip": "10.101.26.1", "svis": svis},
        "deployment_model": {
            "gateway": {"virtual_ip": "10.101.26.1", "mode": "distributed_anycast"},
            "evpn": {"l2": {"vni": 10010, "vni_source": "record"}},
            "placement": {"participating_leafs": [{"hostname": "leaf-01"}, {"hostname": "leaf-02"}],
                          "source_gateway_devices": ["mls01", "mls02"]},
        },
        "devices": [
            {"hostname": "leaf-01", "os_family": "eos"},
            {"hostname": "leaf-02", "os_family": "eos"},
            {"hostname": "mls01", "os_family": "nxos", "bgp_as": "65001", "svi_present": True},
            {"hostname": "mls02", "os_family": "nxos", "bgp_as": "65001", "svi_present": True},
            {"hostname": "sw07", "os_family": "nxos"},
        ],
        "routing_detail": {
            "bgp_networks": [
                {"hostname": "mls01", "vrf": "v0000001b", "kind": "network", "prefix": "10.101.26.0/24"},
                {"hostname": "mls02", "vrf": "v0000001b", "kind": "redistribute", "method": "direct"},
            ],
            "bgp_fabric": [{"hostname": "blf01"}, {"hostname": "blf02"}],
        },
        "prune_plans": [
            nxos_plan("sw07", [trunk, {"op": "no_vlan", "cli": "no vlan 3040"}]),
            nxos_plan("mls01", [trunk, {"op": "no_interface_vlan", "cli": "no interface Vlan3040"},
                                {"op": "no_vlan", "cli": "no vlan 3040"}], status="blocked_until_gateway_validated"),
            nxos_plan("mls02", [trunk, {"op": "no_interface_vlan", "cli": "no interface Vlan3040"},
                                {"op": "no_vlan", "cli": "no vlan 3040"}], status="blocked_until_gateway_validated"),
        ],
        "endpoint_table": {"endpoints": [], "mac_only": [], "summary": {"active_ips": 3}},
    }
    report.update(overrides)
    return report


class MopOrderTests(unittest.TestCase):
    def test_l3_cutover_order(self):
        doc = build_mop_input(_l3_report(), {}, "T")
        validate_against_schema(doc, "mop_input")
        steps = doc["steps"]
        by_id = {s["id"]: s for s in steps}
        phases = [(s["phase"], s["switch"]) for s in steps]
        self.assertEqual(phases, [
            ("0-prechecks", ""), ("0-prechecks", ""), ("0-prechecks", ""), ("0-prechecks", ""),
            ("1-gateway-shutdown", "mls01"), ("1-gateway-shutdown", "mls02"),
            ("2-deploy", "leaf-01"), ("2-deploy", "leaf-02"),
            ("5-validate", ""),
            ("6-routing-withdrawal", "mls01"),
            ("7-teardown", "sw07"),
            ("8-source-gateway-teardown", "mls01"), ("8-source-gateway-teardown", "mls01"),
            ("8-source-gateway-teardown", "mls02"), ("8-source-gateway-teardown", "mls02"),
            ("9-verify", ""),
        ])
        vrf = steps[3]
        self.assertEqual(vrf["title"], "Verify VRF v0000001b exists on the gateway leaves and reuse it")
        self.assertIn("do not create them for this VLAN", vrf["notes"][0])
        shut = [s for s in steps if s["phase"] == "1-gateway-shutdown"]
        self.assertTrue(all(s["manual"] for s in shut))
        validate = next(s for s in steps if s["phase"] == "5-validate")
        self.assertTrue(validate["gate"])
        withdraw = next(s for s in steps if s["phase"] == "6-routing-withdrawal")
        self.assertEqual(
            withdraw["cli"],
            "router bgp 65001\n  vrf v0000001b\n    address-family ipv4 unicast\n      no network 10.101.26.0/24",
        )
        self.assertEqual(withdraw["rollback"], withdraw["cli"].replace("no network", "network"))
        self.assertIn("show ip bgp vrf v0000001b 10.101.26.0/24 on blf01, blf02", withdraw["verify"][0])
        self.assertEqual(withdraw["depends_on"], [validate["id"]])
        sw07 = next(s for s in steps if s["phase"] == "7-teardown")
        self.assertEqual(sw07["depends_on"], [withdraw["id"]])
        source = [s for s in steps if s["phase"] == "8-source-gateway-teardown"]
        svi, rest = source[0], source[1]
        self.assertEqual(svi["cli"], "checkpoint prune_v3040_svi\nconfigure terminal\nno interface Vlan3040\nend\n")
        self.assertIn("checkpoint prune_v3040_l2\n", rest["cli"])
        self.assertNotIn("no interface Vlan3040", rest["cli"])
        self.assertEqual(svi["rollback"], "rollback running-config checkpoint prune_v3040_svi")
        self.assertEqual(svi["status"], "blocked_until_gateway_validated")
        self.assertEqual(svi["depends_on"], [sw07["id"]])
        self.assertEqual(rest["depends_on"], [svi["id"]])
        verify = steps[-1]
        self.assertEqual(sorted(verify["depends_on"]), sorted([source[1]["id"], source[3]["id"]]))
        for step in steps:
            for dep in step["depends_on"]:
                self.assertLess(list(by_id).index(dep), list(by_id).index(step["id"]))
        self.assertEqual(doc["vlan"]["l2_vni_source"], "record")
        self.assertTrue(any("Fabric interconnects (UCS FI) are the host boundary" in rule for rule in doc["rules"]))

    def test_l2_vlan_has_no_shutdown_or_withdrawal(self):
        report = _l3_report(migration_type="l2", gateway_assessment={"location": "external"}, routing_detail={})
        report["prune_plans"] = report["prune_plans"][:1]
        doc = build_mop_input(report, {}, "T")
        phases = [s["phase"] for s in doc["steps"]]
        self.assertNotIn("1-gateway-shutdown", phases)
        self.assertNotIn("6-routing-withdrawal", phases)
        self.assertEqual(phases[-4:], ["2-deploy", "5-validate", "7-teardown", "9-verify"])

    def test_withdrawal_without_asn_names_the_engineer(self):
        report = _l3_report()
        report["devices"][2]["bgp_as"] = ""
        withdraw = next(s for s in build_mop_input(report, {}, "T")["steps"] if s["phase"] == "6-routing-withdrawal")
        self.assertIsNone(withdraw["cli"])
        self.assertEqual(withdraw["cli_source"], "engineer (BGP ASN not discovered on mls01)")


if __name__ == "__main__":
    unittest.main()
