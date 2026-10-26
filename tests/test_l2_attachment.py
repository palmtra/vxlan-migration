#!/usr/bin/env python3
"""Non-VTEP switches: attach through a VTEP only, prune the MLS link.

VLAN 50. vtep1/vtep2 are EOS VTEPs, mls1 is the legacy MLS (SVI owner).

* acc1 (non-VTEP): server on Et5, Po1 -> vtep1, Et48 -> mls1. Dual-attached.
* acc2 (non-VTEP): server on Et5, Et48 -> mls1 only. No VTEP uplink.
* acc3 (non-VTEP): no endpoints, Et48 -> mls1. Transit only.
* vtep1 Po100 -> mls1: the fabric-legacy interconnect.
"""

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

from vlan_lib.l2_domain import device_role  # noqa: E402
from vlan_lib.placement import flatten_deployment_model  # noqa: E402
from vlan_lib.reports import build_vlan_discovery_reports  # noqa: E402

VLAN = {"id": 50, "name": "app", "service_type": "l3", "vrf": "T1"}


def _att(interface, role, *peers):
    return {
        "interface": interface,
        "role": role,
        "description": "",
        "neighbor": ", ".join(peers),
        "peers": [{"hostname": peer, "port": ""} for peer in peers],
    }


def _per_vlan(attachments, **kwargs):
    record = {
        "vlan_id": 50,
        "vlan_present": True,
        "port_attachments": attachments,
        "compute_ports": [a["interface"] for a in attachments if a["role"] == "compute"],
        "switch_uplink_ports": [a["interface"] for a in attachments if a["role"] == "switch_uplink"],
        "trunk_ports": [a["interface"] for a in attachments if a["role"] == "switch_uplink"],
    }
    record.update(kwargs)
    return record


def _host(hostname, os_family, per_vlan, tier="", vxlan=None):
    discovery = {
        "hostname": hostname,
        "os_family": os_family,
        "migration_tier": tier,
        "per_vlan": [per_vlan],
    }
    if vxlan is not None:
        discovery["vxlan_interface"] = vxlan
    return {"_vlan_discovery": discovery}


def _vxlan(*live_vlans):
    return {
        "present": True,
        "source_interface": "Loopback1",
        "replication_mode": "headend",
        "flood_list_source": "EVPN",
        "vlan_vni": [{"vlan_id": vid, "vni": 10000 + vid} for vid in live_vlans],
        "vrf_vni": {"T1": 50001},
    }


def _report(vtep1_live=True, acc1_uplinks=None):
    acc1_uplinks = acc1_uplinks or [_att("Po1", "switch_uplink", "vtep1")]
    hostvars = {
        "vtep1": _host(
            "vtep1",
            "eos",
            _per_vlan([_att("Et10", "switch_uplink", "acc1"), _att("Po100", "switch_uplink", "mls1")]),
            "fabric",
            _vxlan(50) if vtep1_live else _vxlan(),
        ),
        "vtep2": _host("vtep2", "eos", _per_vlan([]), "fabric", _vxlan(50)),
        "mls1": _host(
            "mls1",
            "nxos",
            _per_vlan(
                [
                    _att("Po30", "switch_uplink", "acc1"),
                    _att("Po31", "switch_uplink", "acc2"),
                    _att("Po32", "switch_uplink", "acc3"),
                    _att("Po100", "switch_uplink", "vtep1"),
                ],
                svi_present=True,
                svi_details={"ip_addresses": ["10.5.0.2/24"], "gateway_addresses": ["10.5.0.1"], "vrf": "T1"},
                svi_vrf="T1",
            ),
            "legacy",
        ),
        "acc1": _host(
            "acc1",
            "eos",
            _per_vlan([_att("Et5", "compute")] + acc1_uplinks + [_att("Et48", "switch_uplink", "mls1")]),
            "access",
        ),
        "acc2": _host(
            "acc2", "eos", _per_vlan([_att("Et5", "compute"), _att("Et48", "switch_uplink", "mls1")]), "access"
        ),
        "acc3": _host("acc3", "eos", _per_vlan([_att("Et48", "switch_uplink", "mls1")]), "access"),
    }
    hosts = list(hostvars)
    return build_vlan_discovery_reports([dict(VLAN)], hosts, hostvars, hosts)[0]


class L2AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.report = _report()
        self.roles = {
            item["hostname"]: item["role"] for item in self.report["placement_analysis"]["switches"]
        }

    def test_switch_roles(self):
        self.assertEqual(device_role({"os_family": "eos", "migration_tier": "access"}), "l2_switch")
        self.assertEqual(device_role({"os_family": "eos", "vxlan_interface": {"present": False}}), "l2_switch")
        self.assertEqual(device_role({"os_family": "eos", "vxlan_interface": {"present": True}}), "vtep")
        self.assertEqual(device_role({"os_family": "nxos"}), "mls")
        self.assertEqual(self.report["l2_attachment"]["roles"]["acc1"], "l2_switch")

    def test_non_vtep_with_endpoints_keeps_vlan_but_is_not_a_vni_target(self):
        self.assertEqual(self.roles["acc1"], "l2_attached")
        placement = self.report["deployment_model"]["placement"]
        self.assertNotIn("acc1", [leaf["hostname"] for leaf in placement["participating_leafs"]])
        attached = {item["hostname"]: item for item in placement["l2_attached_switches"]}
        self.assertEqual(attached["acc1"]["vtep_uplinks"], ["vtep1 Et10"])
        record = flatten_deployment_model(self.report["deployment_model"])
        self.assertNotIn("acc1", record["target_switches"])
        self.assertIn("acc1", record["discovery_switches"])

    def test_vtep_serving_a_non_vtep_switch_is_participating(self):
        self.assertEqual(self.roles["vtep1"], "participating")
        evidence = next(
            item["evidence"] for item in self.report["placement_analysis"]["switches"] if item["hostname"] == "vtep1"
        )
        self.assertIn("attachment point for non-VTEP switch acc1 (Et10 -> Po1)", evidence)

    def test_dual_attached_switch_gets_ready_link_prunes_on_both_ends(self):
        plans = {plan["hostname"]: plan for plan in self.report["link_prune_plans"]}
        # mls1 keeps its SVI here (not prune-eligible: acc2 has no VTEP uplink), so both ends are pruned.
        self.assertEqual(sorted(plans), ["acc1", "mls1"])
        acc1 = plans["acc1"]
        self.assertEqual(acc1["status"], "ready")
        self.assertEqual(
            [(a["interface"], a["peer"], a["cli"]) for a in acc1["actions"]],
            [("Et48", "mls1", "interface Et48\n   switchport trunk allowed vlan remove 50")],
        )
        self.assertEqual([a["interface"] for a in plans["mls1"]["actions"]], ["Po30"])
        self.assertIn("switchport trunk allowed vlan remove 50", plans["mls1"]["execution"]["full_cli"])
        self.assertEqual(self.report["prune_plans"][0]["hostname"], "mls1")
        self.assertEqual(self.report["prune_plans"][0]["phase"], 1)
        self.assertEqual(acc1["phase"], 2)
        self.assertEqual(
            [p["phase"] for p in self.report["prune_plans"]],
            sorted(p["phase"] for p in self.report["prune_plans"]),
        )

    def test_link_prune_is_blocked_until_the_vni_is_live(self):
        report = _report(vtep1_live=False)
        plan = next(p for p in report["link_prune_plans"] if p["hostname"] == "acc1")
        self.assertEqual(plan["status"], "blocked_until_vni_live")
        self.assertEqual(plan["actions"], [])
        self.assertEqual([a["interface"] for a in plan["blocked_actions"]], ["Et48"])
        self.assertIn("Link prune on acc1 is blocked: VLAN 50 is not VNI-mapped yet on vtep1", " | ".join(report["action_items"]))

    def test_link_prune_waits_for_the_gateway_when_there_is_no_interconnect(self):
        from vlan_lib.reports import gateway_reachable_from_fabric

        legacy_only = {"svis": [{"hostname": "mls1", "tier": "legacy"}]}
        self.assertIn("still only on the legacy MLS", gateway_reachable_from_fabric(legacy_only, []))
        self.assertEqual(gateway_reachable_from_fabric(legacy_only, [{"vtep": "vtep1"}]), "")
        fabric = {"svis": [{"hostname": "vtep1", "tier": "fabric"}]}
        self.assertEqual(gateway_reachable_from_fabric(fabric, []), "")
        external = {"svis": [], "external_exits": [{"hostname": "mls1", "tier": "legacy"}]}
        self.assertIn("firewall/router on the legacy side", gateway_reachable_from_fabric(external, []))

    def test_issues(self):
        items = " | ".join(self.report["action_items"])
        self.assertIn("acc1 bridges EVPN and legacy L2 for VLAN 50", items)
        self.assertIn("acc2 carries endpoints but has no link to a VTEP", items)
        self.assertIn("direct fabric-legacy interconnect (vtep1 Po100 - mls1)", items)
        self.assertEqual(self.roles["acc2"], "l2_attached")
        self.assertNotIn("acc2", [p["hostname"] for p in self.report["link_prune_plans"]])
        self.assertEqual(self.roles["acc3"], "prune_eligible")

    def test_mls_that_is_the_only_path_is_kept_and_never_a_vni_target(self):
        self.assertEqual(self.roles["mls1"], "legacy_retained")
        evidence = next(
            item["evidence"] for item in self.report["placement_analysis"]["switches"] if item["hostname"] == "mls1"
        )
        self.assertIn("only path for non-VTEP switch acc2, which has no VTEP uplink (Po31)", evidence)
        targets = [leaf["hostname"] for leaf in self.report["deployment_model"]["placement"]["participating_leafs"]]
        self.assertEqual(targets, ["vtep1"])

    def test_separate_links_to_two_vteps_are_flagged_but_a_bundle_is_not(self):
        separate = _report(
            acc1_uplinks=[_att("Et49", "switch_uplink", "vtep1"), _att("Et50", "switch_uplink", "vtep2")]
        )
        self.assertIn("acc1 reaches VTEPs vtep1, vtep2 over separate links (Et49, Et50)", " | ".join(separate["action_items"]))
        bundled = _report(acc1_uplinks=[_att("Po1", "switch_uplink", "vtep1", "vtep2")])
        self.assertNotIn("over separate links", " | ".join(bundled["action_items"]))


if __name__ == "__main__":
    unittest.main()
