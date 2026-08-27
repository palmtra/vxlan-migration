# -*- coding: utf-8 -*-
"""Aggregated VLAN discovery report builders."""

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, sanitize_report_slug
from vlan_lib.prune import build_device_prune_plan

def build_vlan_discovery_reports(vlans, play_hosts, hostvars):
    """Build one aggregated discovery report dict per VLAN in *vlans*."""
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects a list, got %s" % type(vlans)
        )
    if not isinstance(play_hosts, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects play_hosts list, got %s"
            % type(play_hosts)
        )

    reports = []
    for vlan in vlans:
        vlan_id = vlan.get("id")
        devices = []
        for host in play_hosts:
            host_data = hostvars.get(host, {})
            discovery = host_data.get("_vlan_discovery", {})
            per_vlan = {}
            for entry in discovery.get("per_vlan", []) or []:
                if _str_equal(entry.get("vlan_id"), vlan_id):
                    per_vlan = entry
                    break
            devices.append(
                {
                    "hostname": discovery.get("hostname", host),
                    "data_center": discovery.get(
                        "data_center", host_data.get("data_center", "unknown")
                    ),
                    "os_family": discovery.get("os_family", ""),
                    "timestamp": discovery.get("timestamp", ""),
                    "vlan_present": per_vlan.get("vlan_present", False),
                    "has_port_membership": per_vlan.get("has_port_membership", False),
                    "svi_present": per_vlan.get("svi_present", False),
                    "trunks_present": per_vlan.get("trunks_present", False),
                    "access_ports": per_vlan.get("access_ports", []),
                    "trunk_ports": per_vlan.get("trunk_ports", []),
                    "exempt_trunk_ports": per_vlan.get("exempt_trunk_ports", []),
                    "all_ports": per_vlan.get("all_ports", []),
                    "ports_raw": per_vlan.get("ports_raw", ""),
                    "mac_learned": per_vlan.get("mac_learned", False),
                    "arp_learned": per_vlan.get("arp_learned", False),
                    "mac_entries": per_vlan.get("mac_entries", []),
                    "arp_entries": per_vlan.get("arp_entries", []),
                    "uplinks": per_vlan.get("uplinks", []),
                    "svi_vrf": per_vlan.get("svi_vrf", ""),
                    "svi_details": per_vlan.get("svi_details", {}),
                    "vlan_id_raw": per_vlan.get("vlan_id_raw", ""),
                    "svi_raw": per_vlan.get("svi_raw", ""),
                    "target_vrf": per_vlan.get("svi_vrf")
                    or per_vlan.get("target_vrf", vlan.get("vrf", "default")),
                    "mac_table_raw": per_vlan.get("mac_table_raw", ""),
                    "arp_raw": per_vlan.get("arp_raw", ""),
                    "trunk_cleanup_recommendations": per_vlan.get(
                        "trunk_cleanup_recommendations", []
                    ),
                    "static_routes": discovery.get("static_routes", []),
                    "bgp_neighbors": discovery.get("bgp_neighbors", []),
                    "prune_plan": discovery.get("prune_plan", {}),
                    "vxlan_raw": discovery.get("vxlan_raw", ""),
                }
            )

        trunk_cleanup_candidates = []
        for device in devices:
            for entry in device.get("trunk_cleanup_recommendations", []) or []:
                trunk_cleanup_candidates.append(
                    {
                        "hostname": device["hostname"],
                        "interface": entry.get("interface"),
                        "port_role": entry.get("port_role", "trunk"),
                        "cli": entry.get("cli", ""),
                        "recommendation": entry.get("recommendation"),
                    }
                )

        switches_found = [
            device["hostname"]
            for device in devices
            if device.get("has_port_membership")
        ]
        switches_vlan_defined = [
            device["hostname"]
            for device in devices
            if device.get("vlan_present")
        ]
        switches_with_access_ports = [
            device["hostname"]
            for device in devices
            if device.get("access_ports")
        ]
        switches_with_trunk_ports = [
            device["hostname"]
            for device in devices
            if device.get("trunk_ports")
        ]
        switches_with_mac_learning = [
            device["hostname"]
            for device in devices
            if device.get("mac_learned")
        ]
        switches_with_svi = [
            device["hostname"]
            for device in devices
            if device.get("svi_present")
        ]
        switches_with_arp = [
            device["hostname"]
            for device in devices
            if device.get("arp_learned")
        ]

        # VLAN-scoped prune plans (retain blocking already applied on device fact).
        prune_plans = []
        for device in devices:
            device_plan = device.get("prune_plan") or {}
            if not device_plan:
                device_plan = build_device_prune_plan(
                    device.get("hostname", ""),
                    [
                        {
                            "vlan_id": vlan_id,
                            "vlan_present": device.get("vlan_present"),
                            "svi_present": device.get("svi_present"),
                            "svi_vrf": device.get("svi_vrf"),
                            "svi_details": device.get("svi_details"),
                            "trunk_ports": device.get("trunk_ports"),
                        }
                    ],
                    retain=None,
                    static_routes=device.get("static_routes"),
                    bgp_neighbors=device.get("bgp_neighbors"),
                    vlan_ids=[vlan_id],
                )
            else:
                # Filter device-wide plan to actions relevant to this VLAN / its VRF.
                vlan_actions = []
                for action in device_plan.get("actions") or []:
                    op = action.get("op")
                    if op in ("no_vlan", "no_interface_vlan") and str(
                        action.get("vlan_id")
                    ) == str(vlan_id):
                        vlan_actions.append(action)
                    elif op == "trunk_remove_vlans" and int(vlan_id) in (
                        action.get("vlans") or []
                    ):
                        vlan_actions.append(
                            dict(
                                action,
                                vlans=[int(vlan_id)],
                            )
                        )
                    elif op in ("no_ip_route", "no_bgp_neighbor", "no_vrf"):
                        device_vrf = device.get("svi_vrf") or ""
                        if device_vrf and (
                            action.get("vrf") == device_vrf
                            or action.get("name") == device_vrf
                        ):
                            vlan_actions.append(action)
                blocked = []
                for action in device_plan.get("blocked_by_retain") or []:
                    if action.get("op") in ("no_ip_route", "no_bgp_neighbor", "no_vrf"):
                        device_vrf = device.get("svi_vrf") or ""
                        if device_vrf and (
                            action.get("vrf") == device_vrf
                            or action.get("name") == device_vrf
                        ):
                            blocked.append(action)
                device_plan = {
                    "hostname": device.get("hostname"),
                    "vlan_ids": [int(vlan_id)],
                    "svi_vrfs": [device.get("svi_vrf")] if device.get("svi_vrf") else [],
                    "actions": vlan_actions,
                    "blocked_by_retain": blocked,
                    "retain": device_plan.get("retain", {}),
                    "destructive": False,
                    "status": "candidate",
                }
            if device_plan.get("actions") or device_plan.get("blocked_by_retain"):
                prune_plans.append(device_plan)

        reports.append(
            {
                "vlan_id": vlan_id,
                "vlan_name": vlan.get("name", ""),
                "vlan_slug": sanitize_report_slug(vlan.get("name")),
                "service_type": vlan.get("service_type", ""),
                "target_switches": vlan.get("target_switches", []),
                "switches_found": switches_found,
                "switches_vlan_defined": switches_vlan_defined,
                "switches_with_access_ports": switches_with_access_ports,
                "switches_with_trunk_ports": switches_with_trunk_ports,
                "switches_with_mac_learning": switches_with_mac_learning,
                "switches_with_svi": switches_with_svi,
                "switches_with_arp": switches_with_arp,
                "trunk_cleanup_candidates": trunk_cleanup_candidates,
                "prune_plans": prune_plans,
                "vni": vlan.get("vni"),
                "vrf": vlan.get("vrf", "default"),
                "data_center": vlan.get("data_center", ""),
                "devices": devices,
            }
        )
    return reports


def union_vlan_discovery_hosts(vlans):
    """Return a de-duplicated list of discovery switches across VLAN records."""
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "union_vlan_discovery_hosts expects a list, got %s" % type(vlans)
        )

    hosts = []
    seen = set()
    for vlan in vlans:
        if not isinstance(vlan, dict):
            continue
        candidates = vlan.get("discovery_switches") or vlan.get("target_switches") or []
        for host in candidates:
            host_key = str(host)
            if host_key in seen:
                continue
            seen.add(host_key)
            hosts.append(host)
    return hosts


