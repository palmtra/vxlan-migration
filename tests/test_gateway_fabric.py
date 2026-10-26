#!/usr/bin/env python3
"""Fabric SVI shape: primary subnet only, legacy MTU capped at the EOS maximum."""

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
_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "cli")
sys.path.insert(0, os.path.join(_REPO, "plugins"))

from vlan_lib.discovery import extract_vlan_targeted_discovery  # noqa: E402
from vlan_lib.placement import flatten_deployment_model  # noqa: E402
from vlan_lib.reports import build_action_items, build_vlan_discovery_reports, fabric_readiness  # noqa: E402


def _report(svi_text):
    vlan = {"id": 200, "name": "app", "service_type": "l3", "vrf": "TENANT1", "data_center": "omaha"}
    record = extract_vlan_targeted_discovery(
        vlan,
        {
            "platform": "nxos",
            "vlan_id_output": "200  APP  active    Po10\n",
            "svi_output": svi_text,
            "trunk_interface_names": ["Po10"],
        },
    )
    hostvars = {"mls1": {"_vlan_discovery": {"hostname": "mls1", "os_family": "nxos", "per_vlan": [record]}}}
    return build_vlan_discovery_reports([vlan], ["mls1"], hostvars, ["mls1"])[0]


class GatewayFabricTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_FIXTURES, "nxos", "show_run_interface_vlan200_rich.txt"), encoding="utf-8") as handle:
            self.svi = handle.read()

    def test_secondary_subnet_is_not_migrated_but_flagged(self):
        report = _report(self.svi)
        record = flatten_deployment_model(report["deployment_model"])
        self.assertEqual(record["prefixes"], ["10.20.0.0/24"])
        self.assertEqual(record["secondary_prefixes"], ["10.20.1.0/24"])
        self.assertEqual(record["gateway"], "10.20.0.1")
        items = build_action_items(report)
        self.assertTrue(any("Renumber the hosts in secondary subnet(s) 10.20.1.0/24" in item for item in items))

    def test_legacy_mtu_is_kept_and_capped_at_eos_maximum(self):
        self.assertEqual(flatten_deployment_model(_report(self.svi)["deployment_model"])["svi_mtu"], 9000)
        report = _report(self.svi.replace("mtu 9000", "mtu 9216"))
        self.assertEqual(flatten_deployment_model(report["deployment_model"])["svi_mtu"], 9214)
        self.assertTrue(
            any("Nothing to do: legacy SVI MTU 9216 is above the EOS maximum" in item for item in build_action_items(report))
        )


class FabricReadinessModeTests(unittest.TestCase):
    """The gateway VRF's L3 VNI is only required where the SVI goes."""

    def _report(self, mode):
        def leaf(hostname, l3_vni):
            return {
                "hostname": hostname,
                "os_family": "eos",
                "expected_vtep_source": "Loopback1",
                "vxlan_interface": {
                    "present": True,
                    "source_interface": "Loopback1",
                    "replication_mode": "headend",
                    "flood_list_source": "EVPN",
                    "vrf_vni": {"v0000001b": l3_vni} if l3_vni else {},
                },
            }

        return {
            "migration_type": "l3",
            "gateway_assessment": {"vrf": "v0000001b", "mode": mode},
            "deployment_model": {
                "placement": {
                    "participating_leafs": [{"hostname": "sw11"}, {"hostname": "sw13"}],
                    "gateway_leafs": [{"hostname": "sw13"}],
                }
            },
            "devices": [leaf("sw11", None), leaf("sw13", None)],
        }

    def test_distributed_needs_the_vrf_on_every_participating_leaf(self):
        issues = fabric_readiness(self._report("distributed_anycast"))["issues"]
        self.assertEqual(len(issues), 1)
        self.assertIn("Create VRF v0000001b on sw13, sw11 first: neither leaf has", issues[0])

    def test_centralized_needs_the_vrf_on_the_gateway_leafs_only(self):
        readiness = fabric_readiness(self._report("centralized_anycast"))
        self.assertEqual(len(readiness["issues"]), 1)
        self.assertIn("Create VRF v0000001b on sw13 first: the leaf does not have", readiness["issues"][0])
        rows = {row["hostname"]: row["svi_host"] for row in readiness["checked"]}
        self.assertEqual(rows, {"sw11": False, "sw13": True})


if __name__ == "__main__":
    unittest.main()
