#!/usr/bin/env python3
"""A CVP rollback reverses every switch and every line the forward deploy changed.

roles/cvp_deploy and roles/cvp_rollback must pick the same switches
(target_switches plus gateway_leafs), and the rollback configlet must remove
what the deploy configlet added on each one: the gateway SVI, the VXLAN map,
the BGP VLAN and the VRF network statement.
"""

import os
import sys
import types
import unittest

import jinja2
import yaml

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

from vlan_lib.placement import (  # noqa: E402
    bgp_vlan_evpn,
    virtual_gateway_address,
    vlan_config_hosts,
    vlan_gateway_hosts,
)

_DEPLOY = os.path.join(_REPO, "roles", "cvp_deploy")
_ROLLBACK = os.path.join(_REPO, "roles", "cvp_rollback")

# Omaha VLAN 3014 shape: the gateway pair is the border leaves, which are not
# in target_switches.
CENTRALIZED = {
    "id": 3014,
    "name": "network_mgmt_servers",
    "service_type": "l3",
    "gateway_mode": "centralized_anycast",
    "vrf": "v0000822a",
    "vni": 13014,
    "gateway": "10.231.14.1",
    "prefixes": ["10.231.14.0/24"],
    "target_switches": ["fntc-aggacc-sw11", "fntc-aggacc-sw12", "fntc-aggacc-sw13", "fntc-aggacc-sw14"],
    "gateway_leafs": ["oma01-blf01", "oma01-blf02"],
}
GATEWAYS = ["oma01-blf01", "oma01-blf02"]
ASN = {"oma01-blf01": "30452", "oma01-blf02": "30452"}
ROUTER_ID = {"oma01-blf01": "66.180.0.3", "oma01-blf02": "66.180.0.4"}


def _render(role, template, record, host):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(role, "templates")),
        trim_blocks=True,
    )
    env.filters.update({
        "bgp_vlan_evpn": bgp_vlan_evpn,
        "virtual_gateway_address": virtual_gateway_address,
        "vlan_gateway_hosts": vlan_gateway_hosts,
        "bool": lambda value: str(value).strip().lower() in ("1", "true", "yes", "on"),
    })
    return env.get_template(template).render(
        _vlan_to_migrate=record,
        target_vlan_id=record["id"],
        _device_hostname=host,
        _device_bgp_asn=ASN.get(host, "4200000104"),
        _device_loopback_ip=ROUTER_ID.get(host, "66.180.0.20"),
        cvp_rollback_allow_destructive_no_vlan=False,
    )


def _device_expression(role):
    with open(os.path.join(role, "tasks", "main.yml"), encoding="utf-8") as handle:
        tasks = yaml.safe_load(handle)
    for task in tasks:
        facts = task.get("ansible.builtin.set_fact") or {}
        if "_cvp_target_devices" in facts:
            return facts["_cvp_target_devices"].replace(" ", "")
    raise AssertionError("no _cvp_target_devices set_fact in %s" % role)


class RollbackDeviceTests(unittest.TestCase):
    def test_rollback_picks_the_same_switches_as_the_deploy(self):
        self.assertEqual(_device_expression(_ROLLBACK), _device_expression(_DEPLOY))
        self.assertEqual(_device_expression(_ROLLBACK), "{{_vlan_to_migrate|vlan_config_hosts}}")

    def test_centralized_gateway_pair_outside_target_switches_is_included(self):
        hosts = vlan_config_hosts(CENTRALIZED)
        self.assertEqual(hosts, CENTRALIZED["target_switches"] + GATEWAYS)


class RollbackConfigletTests(unittest.TestCase):
    def test_centralized_gateway_leaf_loses_svi_vni_bgp_vlan_and_network(self):
        for host in GATEWAYS:
            deploy = _render(_DEPLOY, "leaf_vxlan_configlet.j2", CENTRALIZED, host)
            self.assertIn("interface Vlan3014\n", deploy)
            self.assertIn(" ip address virtual 10.231.14.1/24\n", deploy)
            self.assertIn("  network 10.231.14.0/24\n", deploy)
            rollback = _render(_ROLLBACK, "leaf_vxlan_rollback_configlet.j2", CENTRALIZED, host)
            self.assertIn("no interface Vlan3014\n", rollback)
            self.assertIn(" no vxlan vlan 3014 vni 13014\n", rollback)
            self.assertIn("router bgp 30452\n no vlan 3014\n vrf v0000822a\n  no network 10.231.14.0/24\n", rollback)

    def test_centralized_participating_leaf_keeps_no_svi(self):
        host = "fntc-aggacc-sw11"
        self.assertNotIn("interface Vlan3014", _render(_DEPLOY, "leaf_vxlan_configlet.j2", CENTRALIZED, host))
        rollback = _render(_ROLLBACK, "leaf_vxlan_rollback_configlet.j2", CENTRALIZED, host)
        self.assertNotIn("interface Vlan3014", rollback)
        self.assertNotIn("network", rollback)
        self.assertIn(" no vxlan vlan 3014 vni 13014\n", rollback)
        self.assertIn("router bgp 4200000104\n no vlan 3014\n", rollback)

    def test_distributed_participating_leaf_loses_the_svi_it_was_given(self):
        # distributed_anycast puts the SVI on every participating leaf, named
        # in gateway_leafs or not; the rollback must remove it on each.
        record = dict(CENTRALIZED, gateway_mode="distributed_anycast", gateway_leafs=[])
        host = "fntc-aggacc-sw11"
        self.assertIn("interface Vlan3014\n", _render(_DEPLOY, "leaf_vxlan_configlet.j2", record, host))
        rollback = _render(_ROLLBACK, "leaf_vxlan_rollback_configlet.j2", record, host)
        self.assertIn("no interface Vlan3014\n", rollback)
        self.assertIn("  no network 10.231.14.0/24\n", rollback)

    def test_every_deployed_switch_with_an_svi_gets_it_removed(self):
        for mode in ("centralized_anycast", "distributed_anycast"):
            record = dict(CENTRALIZED, gateway_mode=mode)
            for host in vlan_config_hosts(record):
                deploy = _render(_DEPLOY, "leaf_vxlan_configlet.j2", record, host)
                rollback = _render(_ROLLBACK, "leaf_vxlan_rollback_configlet.j2", record, host)
                self.assertEqual("interface Vlan3014\n" in deploy, "no interface Vlan3014\n" in rollback, (mode, host))
                self.assertEqual("  network " in deploy, "  no network " in rollback, (mode, host))


if __name__ == "__main__":
    unittest.main()
