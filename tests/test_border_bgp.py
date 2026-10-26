#!/usr/bin/env python3
"""Border-leaf BGP view of legacy VLAN subnets, and exact-prefix matching."""

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

from vlan_lib.discovery import border_bgp_targets, parse_border_bgp  # noqa: E402
from vlan_lib.placement import fabric_bgp_view  # noqa: E402
from vlan_lib.policy import parse_bgp_prefix_detail  # noqa: E402

# oma01-blf01 (AS 30452) learns the MLS subnet over iBGP: same AS as the MLS.
EOS_BORDER = """BGP routing table information for VRF v0000001a
Router identifier 66.180.0.3, local AS number 30452
BGP routing table entry for 10.101.10.0/24
 Paths: 2 available
  Local
    10.252.12.81 from 10.252.12.81 (66.180.0.30)
      Origin IGP, metric 0, localpref 100, IGP metric 0, weight 0, tag 0
      Received 3d01h ago, valid, internal, best
      Rx SAFI: Unicast
  4200000102
    172.16.0.5 from 66.180.0.10 (66.180.0.10)
      Origin IGP, metric 0, localpref 100, IGP metric 0, weight 0, tag 0
      Received 1d01h ago, valid, external
"""

EOS_LOCAL = """BGP routing table entry for 10.101.10.0/24
 Paths: 1 available
  Local
    - from - (66.180.0.30)
      Origin IGP, metric -, localpref -, IGP metric -, weight 0, tag 0
      Received 3d01h ago, valid, sourced, local, best
"""

NXOS_IBGP = """BGP routing table information for VRF v0000001a, address family IPv4 Unicast
BGP routing table entry for 10.101.10.0/24, version 12
Paths: (1 available, best #1)
  Advertised path-id 1
  Path type: internal, path is valid, is best path
  AS-Path: NONE, path sourced internal to AS
    10.252.12.6 (metric 0) from 10.252.12.6 (66.180.0.3)
      Origin IGP, MED not set, localpref 100, weight 0
"""


class PrefixDetail(unittest.TestCase):
    def test_eos_ibgp_from_same_as_is_not_local(self):
        result = parse_bgp_prefix_detail(EOS_BORDER, "10.101.10.0/24")
        self.assertTrue(result["present"])
        self.assertFalse(result["local"])
        best, other = result["paths"]
        self.assertEqual((best["as_path"], best["peer_router_id"], best["internal"], best["best"]), ("", "66.180.0.30", True, True))
        self.assertEqual((other["as_path"], other["external"], other["best"]), ("4200000102", True, False))

    def test_eos_locally_originated(self):
        result = parse_bgp_prefix_detail(EOS_LOCAL, "10.101.10.0/24")
        self.assertTrue(result["local"])
        self.assertTrue(result["paths"][0]["local"])

    def test_nxos_path_type_before_next_hop(self):
        (path,) = parse_bgp_prefix_detail(NXOS_IBGP, "10.101.10.0/24")["paths"]
        self.assertEqual((path["internal"], path["best"], path["local"], path["as_path"]), (True, True, False, ""))

    def test_only_the_exact_prefix_counts(self):
        covering = EOS_BORDER.replace("entry for 10.101.10.0/24", "entry for 10.101.0.0/16")
        result = parse_bgp_prefix_detail(covering, "10.101.10.0/24")
        self.assertFalse(result["present"])
        self.assertEqual(result["entry"], "10.101.0.0/16")
        self.assertEqual(result["paths"], [])
        # Same network written differently still matches.
        self.assertTrue(parse_bgp_prefix_detail(EOS_BORDER, "10.101.10.5/24")["present"])

    def test_not_in_table(self):
        self.assertFalse(parse_bgp_prefix_detail("% Network not in table", "10.101.10.0/24")["present"])


MLS_SVI = """interface Vlan1010
  vrf member v0000001a
  ip address 10.101.10.2/24
  ip address 10.101.20.1/24 secondary
"""


class BorderLookup(unittest.TestCase):
    def test_targets_are_every_svi_subnet_in_its_vrf(self):
        hostvars = {
            "mls01": {"_vlan_discovery": {"os_family": "nxos", "per_vlan": [
                {"vlan_id": 1010, "svi_present": True, "svi_vrf": "v0000001a", "svi_raw": MLS_SVI, "os_family": "nxos"},
                {"vlan_id": 1011, "svi_present": False, "svi_raw": ""},
            ]}},
            "mls02": {"_vlan_discovery": {"os_family": "nxos", "per_vlan": [
                {"vlan_id": 1010, "svi_present": True, "svi_vrf": "v0000001a", "svi_raw": MLS_SVI, "os_family": "nxos"},
            ]}},
            "blf01": {"_vlan_discovery": {"os_family": "eos", "per_vlan": []}},
        }
        self.assertEqual(border_bgp_targets(["mls01", "mls02", "blf01"], hostvars), [
            {"vlan_id": 1010, "prefix": "10.101.10.0/24", "vrf": "v0000001a"},
            {"vlan_id": 1010, "prefix": "10.101.20.0/24", "vrf": "v0000001a"},
        ])

    def test_parse_border_results(self):
        results = [
            {"item": {"vlan_id": 1010, "prefix": "10.101.10.0/24", "vrf": "v0000001a"}, "stdout": EOS_BORDER},
            {"item": {"vlan_id": 1010, "prefix": "10.101.20.0/24", "vrf": "v0000001a"}, "stdout": ""},
            {"skipped": True},
        ]
        parsed = parse_border_bgp(results)
        self.assertEqual([(p["prefix"], p["present"]) for p in parsed], [("10.101.10.0/24", True), ("10.101.20.0/24", False)])
        self.assertEqual(parsed[0]["vlan_id"], 1010)


class FabricView(unittest.TestCase):
    def test_ibgp_from_the_mls_is_named(self):
        devices = [
            {"hostname": "fnts-fntc03-aggpe-mls01", "router_id": "66.180.0.30", "border_bgp": []},
            {"hostname": "fntc-aggacc-sw01", "router_id": "66.180.0.10", "border_bgp": []},
            {
                "hostname": "oma01-blf01",
                "router_id": "66.180.0.3",
                "bgp_as": "30452",
                "border_bgp": parse_border_bgp([
                    {"item": {"vlan_id": 1010, "prefix": "10.101.10.0/24", "vrf": "v0000001a"}, "stdout": EOS_BORDER},
                    {"item": {"vlan_id": 1010, "prefix": "10.101.20.0/24", "vrf": "v0000001a"}, "stdout": ""},
                ]),
            },
        ]
        advertised, missing = fabric_bgp_view(devices)
        self.assertEqual((advertised["hostname"], advertised["bgp_as"], advertised["present"]), ("oma01-blf01", "30452", True))
        self.assertEqual(advertised["best"]["kind"], "iBGP")
        self.assertEqual(advertised["best"]["from"], "fnts-fntc03-aggpe-mls01")
        self.assertEqual([(p["kind"], p["from"], p["best"]) for p in advertised["paths"]], [
            ("iBGP", "fnts-fntc03-aggpe-mls01", True),
            ("eBGP", "fntc-aggacc-sw01", False),
        ])
        self.assertFalse(missing["present"])
        self.assertIsNone(missing["best"])


if __name__ == "__main__":
    unittest.main()
