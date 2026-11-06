#!/usr/bin/env python3
"""Unit tests for gateway MAC facts, typed findings, the MOP input and the run index."""

import hashlib
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

from vlan_lib.mop import build_mop_input, build_run_index, file_manifest, normalize_run  # noqa: E402
from vlan_lib.parsers import fhrp_virtual_mac, parse_svi_details, parse_virtual_router_mac  # noqa: E402
from vlan_lib.reports import (  # noqa: E402
    build_action_items,
    build_findings,
    build_gateway_mac_change,
    resolve_l3_vni,
)
from vlan_lib.schema import validate_against_schema  # noqa: E402


class VirtualMacTests(unittest.TestCase):
    def test_well_known_macs(self):
        def mac(protocol, group, family="ipv4", version=1):
            return fhrp_virtual_mac({"protocol": protocol, "group": group, "family": family}, version)

        self.assertEqual(mac("hsrp", 30), ("00:00:0c:07:ac:1e", "derived"))
        self.assertEqual(mac("hsrp", 3000, version=2), ("00:00:0c:9f:fb:b8", "derived"))
        self.assertEqual(mac("hsrp", 3000), ("", "unknown"))  # v1 groups stop at 255
        self.assertEqual(mac("hsrp", 10, "ipv6", 2), ("00:05:73:a0:00:0a", "derived"))
        self.assertEqual(mac("vrrp", 10), ("00:00:5e:00:01:0a", "derived"))
        self.assertEqual(mac("vrrp", 10, "ipv6"), ("00:00:5e:00:02:0a", "derived"))
        self.assertEqual(mac("vrrp", 300), ("", "unknown"))

    def test_configured_mac_and_use_bia(self):
        self.assertEqual(
            fhrp_virtual_mac({"protocol": "hsrp", "group": 1, "mac": "0000.1111.2222"}), ("00:00:11:11:22:22", "configured")
        )
        self.assertEqual(fhrp_virtual_mac({"protocol": "hsrp", "group": 1, "use_bia": True}), ("", "use-bia"))

    def test_svi_parser_reads_version_mac_and_use_bia(self):
        nxos = (
            "interface Vlan3000\n  ip address 10.230.0.2/24\n  hsrp version 2\n  hsrp 3000\n"
            "    ip 10.230.0.1\n  hsrp 3001\n    ip 10.230.1.1\n    mac-address 0000.1111.2222\n"
            "  hsrp 3002\n    ip 10.230.2.1\n    use-bia\n"
        )
        groups = {g["group"]: g for g in parse_svi_details(nxos, "nxos")["fhrp_groups"]}
        self.assertEqual(groups[3000]["virtual_mac"], "00:00:0c:9f:fb:b8")
        self.assertEqual((groups[3001]["virtual_mac"], groups[3001]["mac_source"]), ("00:00:11:11:22:22", "configured"))
        self.assertEqual((groups[3002]["virtual_mac"], groups[3002]["mac_source"]), ("", "use-bia"))
        ios = "interface Vlan30\n standby version 2\n standby 30 ip 10.30.0.1\n"
        self.assertEqual(parse_svi_details(ios, "ios")["fhrp_groups"][0]["virtual_mac"], "00:00:0c:9f:f0:1e")

    def test_fabric_virtual_router_mac(self):
        self.assertEqual(parse_virtual_router_mac("ip virtual-router mac-address 001c.7300.0099\n"), "00:1c:73:00:00:99")
        self.assertEqual(parse_virtual_router_mac(""), "")


def _report(**overrides):
    """A small L3 report: two legacy HSRP gateways, two fabric leaves, one prune plan."""
    report = {
        "vlan_id": 3000,
        "vlan_name": "ACME_APP",
        "vlan_slug": "ACME_APP",
        "data_center": "omaha",
        "migration_type": "l3",
        "switches_vlan_defined": ["mls-01", "leaf-01"],
        "switches_scanned": ["leaf-01", "leaf-02", "mls-01", "mls-02"],
        "not_discovered": [],
        "gateway_assessment": {
            "location": "legacy",
            "mode": "distributed_anycast",
            "vrf": "v1",
            "subnets": [{"prefix": "10.230.0.0/24", "gateway_ip": "10.230.0.1", "secondary": False}],
            "svis": [
                {
                    "hostname": host,
                    "vrf": "v1",
                    "ip_addresses": [ip],
                    "fhrp_groups": [
                        {"protocol": "hsrp", "group": 3000, "family": "ipv4", "ip": "10.230.0.1",
                         "priority": prio, "virtual_mac": "00:00:0c:9f:fb:b8", "mac_source": "derived"}
                    ],
                }
                for host, ip, prio in (("mls-01", "10.230.0.2/24", 110), ("mls-02", "10.230.0.3/24", 100))
            ],
        },
        "deployment_model": {
            "gateway": {"virtual_ip": "10.230.0.1", "mode": "distributed_anycast", "svi_mtu": 9214},
            "evpn": {"l2": {"vni": 11002}, "l3": {"vni": 822}},
            "placement": {
                "participating_leafs": [{"hostname": "leaf-01"}, {"hostname": "leaf-02"}],
                "gateway_leafs": [],
            },
        },
        "devices": [
            {"hostname": "leaf-01", "os_family": "eos", "virtual_router_mac": "00:1c:73:00:00:99", "vlan_present": True},
            {"hostname": "leaf-02", "os_family": "eos", "virtual_router_mac": "00:1c:73:00:00:99", "vlan_present": True},
            {"hostname": "mls-01", "os_family": "nxos", "svi_present": True, "vlan_present": True,
             "svi_details": {"primary_ip": "10.230.0.2/24", "ip_addresses": ["10.230.0.2/24"]},
             "errors": [{"command": "show spanning-tree vlan 3000", "error": "timeout"}]},
            {"hostname": "mls-02", "os_family": "nxos", "svi_present": True, "vlan_present": True,
             "svi_details": {"primary_ip": "10.230.0.3/24", "ip_addresses": ["10.230.0.3/24"]}},
        ],
        "placement_analysis": {"switches": [
            {"hostname": "leaf-01", "role": "participating", "switch_role": "vtep", "evidence": ["Et5 compute"]},
            {"hostname": "mls-01", "role": "prune_eligible", "switch_role": "mls", "nonqualifying": ["uplink only"]},
        ]},
        "prune_plans": [
            {"hostname": "mls-01", "placement_role": "prune_eligible", "phase": 3, "phase_label": "teardown",
             "status": "blocked_until_gateway_moved", "blocked_reason": "SVI still active", "actions": [],
             "blocked_actions": [{"cli": "no vlan 3000"}],
             "execution": {"full_cli": "checkpoint prune_v3000\nconfigure terminal\nno vlan 3000\nend\n",
                           "rollback": "rollback running-config checkpoint prune_v3000",
                           "save": "copy running-config startup-config"}},
            {"hostname": "leaf-01", "placement_role": "retained_participating", "phase": 3, "actions": []},
            {"hostname": "leaf-09", "placement_role": "prune_eligible", "phase": 3, "phase_label": "teardown",
             "status": "ready", "actions": [{"cli": "no vlan 3000"}],
             "execution": {"platform": "eos", "full_cli": "configure session prune_v3000\nno vlan 3000\n!\n"
                           "show session-config diffs\ncommit timer 00:10:00\n", "commit_timer": "00:10:00",
                           "commit_timer_cmd": "commit timer 00:10:00", "confirm": "configure session prune_v3000 commit",
                           "abort": "configure session prune_v3000 abort"}},
        ],
        "endpoint_table": {
            "endpoints": [
                {"ip": "10.230.0.2", "mac": "00:26:98:aa:00:01", "location": "uplink_only", "flags": ["gateway"]},
                {"ip": "10.230.0.10", "mac": "00:50:56:b3:00:10", "location": "edge", "hostname": "leaf-01",
                 "interface": "Et5", "role": "compute", "flags": [], "arp_from": ["mls-01"], "ipv6": [], "seen": None},
            ],
            "mac_only": [{"ip": "", "mac": "00:50:56:b3:00:30", "location": "edge", "hostname": "leaf-01",
                          "interface": "Et6", "flags": []}],
            "summary": {"active_ips": 1},
        },
        "vni_allocation": {"vni": 11002, "reason": "next free"},
    }
    report.update(overrides)
    report["gateway_mac"] = build_gateway_mac_change(report)
    return report


class GatewayMacChangeTests(unittest.TestCase):
    def test_change_is_detected_and_explained(self):
        report = _report()
        gmac = report["gateway_mac"]
        self.assertTrue(gmac["applies"] and gmac["changes"])
        self.assertEqual((gmac["legacy_macs"], gmac["fabric_mac"]), (["00:00:0c:9f:fb:b8"], "00:1c:73:00:00:99"))
        items = build_action_items(report)
        self.assertTrue(any(i.startswith("The gateway MAC changes at cutover: hosts cache 10.230.0.1 as 00:00:0c:9f:fb:b8") for i in items))

    def test_missing_and_differing_fabric_macs(self):
        report = _report()
        report["devices"][1]["virtual_router_mac"] = ""
        report["gateway_mac"] = build_gateway_mac_change(report)
        self.assertEqual(report["gateway_mac"]["fabric_missing"], ["leaf-02"])
        self.assertTrue(any("Set `ip virtual-router mac-address` on leaf-02" in i for i in build_action_items(report)))
        report["devices"][1]["virtual_router_mac"] = "00:1c:73:00:00:98"
        report["gateway_mac"] = build_gateway_mac_change(report)
        self.assertEqual(report["gateway_mac"]["fabric_mac"], "")
        self.assertTrue(any(i.startswith("Make the anycast gateway MAC the same") for i in build_action_items(report)))

    def test_l2_migration_does_not_apply(self):
        report = _report(migration_type="l2")
        self.assertFalse(report["gateway_mac"]["applies"])
        self.assertFalse(any("gateway MAC" in i for i in build_action_items(report)))


class FindingTests(unittest.TestCase):
    def test_findings_are_typed_and_match_action_items(self):
        report = _report(not_discovered=["mls-99"])
        findings = build_findings(report)
        self.assertEqual([f["text"] for f in findings], build_action_items(report))
        kinds = {(f["kind"], f["category"], f["severity"]) for f in findings}
        self.assertIn(("blocker", "coverage", "high"), kinds)  # not discovered, failed command
        self.assertIn(("risk", "gateway", "high"), kinds)  # gateway MAC change
        self.assertIn(("decision", "vni", "medium"), kinds)  # proposed VNI
        for item in findings:
            self.assertIn(item["kind"], ("blocker", "decision", "risk", "task", "sequencing", "info"))


class MopInputTests(unittest.TestCase):
    RUN = {"run_id": "omaha_T", "parameters": {"passes": "2", "arp_refresh": "False"}, "git": {"dirty": "True"}}

    def test_document_is_valid_and_ordered(self):
        doc = build_mop_input(_report(not_discovered=["mls-99"]), self.RUN, "T")
        validate_against_schema(doc, "mop_input")
        self.assertEqual(doc["vlan"]["status"], "blocked")
        self.assertEqual(doc["run"]["parameters"], {"passes": 2, "arp_refresh": False})
        self.assertFalse(doc["coverage"]["complete"])
        phases = [s["phase"] for s in doc["steps"]]
        self.assertEqual(phases, sorted(phases))  # phases never go backwards
        self.assertEqual(doc["steps"][0]["status"], "blocked")
        self.assertEqual(doc["steps"][0]["findings"], ["B-01", "B-02"])
        self.assertEqual(doc["files"]["mop_input"], "ACME_APP_mop_input_T.json")

    def test_steps_carry_cli_and_rollback(self):
        doc = build_mop_input(_report(), self.RUN, "T")
        by_phase = {}
        for step in doc["steps"]:
            by_phase.setdefault(step["phase"], []).append(step)
        # The legacy SVIs are shut by hand first; the deploy follows.
        shut = by_phase["1-gateway-shutdown"]
        self.assertEqual([s["switch"] for s in shut], ["mls-01", "mls-02"])
        self.assertTrue(all(s["manual"] for s in shut))
        self.assertEqual((shut[0]["cli"], shut[0]["rollback"]), ("interface Vlan3000\n  shutdown", "interface Vlan3000\n  no shutdown"))
        deploy = by_phase["2-deploy"]
        self.assertEqual([s["switch"] for s in deploy], ["leaf-01", "leaf-02"])
        self.assertIsNone(deploy[0]["cli"])
        self.assertIn("vxlan_vlan3000_leaf-01.cfg", deploy[0]["cli_source"])
        self.assertIn("leaf_vxlan_rollback_configlet.j2", deploy[0]["rollback"])
        self.assertIn("only after the legacy SVI is shut", deploy[0]["notes"][0])
        self.assertEqual(deploy[0]["depends_on"], [s["id"] for s in shut])
        validate = by_phase["5-validate"]
        self.assertEqual(len(validate), 1)
        self.assertTrue(validate[0]["gate"])
        self.assertEqual(validate[0]["depends_on"], [d["id"] for d in deploy])
        self.assertIn("00:00:0c:9f:fb:b8 to 00:1c:73:00:00:99", validate[0]["notes"][1])
        teardown = by_phase["7-teardown"]
        self.assertEqual([s["switch"] for s in teardown], ["leaf-09"])  # retained leaf-01 is not a step
        eos = teardown[0]
        self.assertEqual(eos["confirm"], "configure session prune_v3000 commit")
        self.assertEqual(
            eos["rollback"],
            "Do not commit the session again: it rolls back when the commit timer (00:10:00) expires. "
            "To roll back at once: configure session prune_v3000 abort",
        )
        self.assertIn("keep it with `configure session prune_v3000 commit` before the timer expires", eos["notes"][0])
        self.assertEqual(eos["depends_on"], [validate[0]["id"]])
        # The source gateway comes out last, the SVI on its own.
        source = by_phase["8-source-gateway-teardown"]
        self.assertEqual([s["switch"] for s in source], ["mls-01"])
        self.assertIsNone(source[0]["confirm"])
        self.assertEqual(source[0]["status"], "blocked_until_gateway_moved")
        self.assertEqual(source[0]["depends_on"], [eos["id"]])
        verify = by_phase["9-verify"][0]
        self.assertEqual(verify["depends_on"], [source[-1]["id"]])

    def test_endpoints_and_ids(self):
        doc = build_mop_input(_report(), self.RUN, "T")
        rows = doc["endpoints"]["rows"]
        self.assertEqual([r["id"] for r in rows], ["EP-3000-001", "EP-3000-002", "EP-3000-003"])
        self.assertEqual([r["baseline"] for r in rows], [False, True, False])
        self.assertEqual(doc["switches"][0]["id"], "SW-leaf-01")
        self.assertEqual(doc["gateway"]["mac_change"]["fabric_mac"], "00:1c:73:00:00:99")
        self.assertEqual(doc["acceptance"][-1]["check"], "Endpoints resolve 10.230.0.1 to the fabric MAC 00:1c:73:00:00:99")

    def test_same_input_gives_same_document(self):
        first = json.dumps(build_mop_input(_report(), self.RUN, "T"), sort_keys=True)
        second = json.dumps(build_mop_input(_report(), self.RUN, "T"), sort_keys=True)
        self.assertEqual(first, second)


class L3VniTests(unittest.TestCase):
    @staticmethod
    def _readiness(*rows):
        return {"vrf": "v1", "checked": [
            {"hostname": host, "l3_vni": vni, "svi_host": svi} for host, vni, svi in rows
        ]}

    def test_record_wins_then_fabric(self):
        report = _report(fabric_readiness=self._readiness(("leaf-01", 822, True), ("leaf-02", 822, True)))
        self.assertEqual(resolve_l3_vni(report)["vni"], 822)  # record says 822 too
        report["deployment_model"]["evpn"]["l3"] = {}
        found = resolve_l3_vni(report)
        self.assertEqual(found["vni"], 822)
        self.assertIn("leaf-01, leaf-02", found["source"])
        doc = build_mop_input(report, {}, "T")
        self.assertEqual((doc["vlan"]["l3_vni"], doc["vlan"]["l3_vni_source"]), (822, found["source"]))
        validate_against_schema(doc, "mop_input")

    def test_disagreeing_leaves_are_a_blocker(self):
        report = _report(fabric_readiness=self._readiness(("leaf-01", 822, True), ("leaf-02", 823, True), ("leaf-03", 900, False)))
        report["deployment_model"]["evpn"]["l3"] = {}
        found = resolve_l3_vni(report)
        self.assertEqual((found["vni"], found["by_vni"]), (None, {822: ["leaf-01"], 823: ["leaf-02"]}))
        blockers = [f["text"] for f in build_findings(report) if f["kind"] == "blocker"]
        self.assertIn("Make the L3 VNI of VRF v1 the same on every gateway leaf: 822 on leaf-01; 823 on leaf-02.", blockers)
        self.assertEqual(build_mop_input(report, {}, "T")["vlan"]["status"], "blocked")

    def test_missing_and_l2(self):
        report = _report(fabric_readiness=self._readiness(("leaf-01", None, True)))
        report["deployment_model"]["evpn"]["l3"] = {}
        self.assertEqual(resolve_l3_vni(report)["vni"], None)
        self.assertIn("not found", resolve_l3_vni(report)["source"])
        self.assertEqual(resolve_l3_vni(_report(migration_type="l2"))["source"], "not needed (L2 migration)")


class RunIndexTests(unittest.TestCase):
    def test_order_shared_switches_and_totals(self):
        ready = build_mop_input(_report(vni_allocation={}), {}, "T")
        ready["vlan"]["status"] = "ready"
        blocked = build_mop_input(_report(vlan_id=3001, vlan_slug="OTHER", not_discovered=["x"]), {}, "T")
        index = build_run_index([blocked, ready], {"run_id": "r"}, [{"path": "a", "bytes": 1, "sha256": "0"}])
        self.assertEqual(index["order"], [3000, 3001])
        self.assertEqual([v["id"] for v in index["vlans"]], [3000, 3001])
        self.assertEqual(index["shared_switches"]["mls-01"], [3001, 3000])
        self.assertEqual((index["totals"]["ready"], index["totals"]["blocked"]), (1, 1))
        self.assertEqual(index["files"][0]["path"], "a")

    def test_manifest_hashes_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x", "a.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as handle:
                handle.write(b"{}\n")
            manifest = file_manifest([path, os.path.join(tmp, "missing.json")], tmp)
        self.assertEqual(manifest, [{"path": "x/a.json", "bytes": 3, "sha256": hashlib.sha256(b"{}\n").hexdigest()}])

    def test_normalize_run(self):
        run = normalize_run({"parameters": {"passes": "3", "vrf": "0123", "on": "True"}, "git": {"dirty": "false"}})
        self.assertEqual(run["parameters"], {"passes": 3, "vrf": "0123", "on": True})
        self.assertIs(run["git"]["dirty"], False)


if __name__ == "__main__":
    unittest.main()
