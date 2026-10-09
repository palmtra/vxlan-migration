# -*- coding: utf-8 -*-
"""Aggregated VLAN discovery report builders."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, sanitize_report_slug
from vlan_lib.parsers import network_prefix
from vlan_lib.placement import (
    PRUNE_STATUS_BY_ROLE,
    attach_associated_routes,
    build_vlan_placement,
)
from vlan_lib.prune import build_device_prune_plan, build_prune_execution

_PLACEHOLDER_VLAN_NAME = re.compile(r"^vlan[_-]?\d+$", re.I)

_SHARED_VRF_NOTE = (
    "This VRF may be shared by other VLANs. Neighbors, static routes, RD/RT, "
    "and VRF teardown are not unique to this VLAN. Review with the app owner; "
    "do not remove them solely because this VLAN is migrating."
)


def _norm_vrf(vrf):
    return str(vrf or "default")


def _filter_by_vrf(items, vrf):
    want = _norm_vrf(vrf)
    return [
        item
        for item in (items or [])
        if isinstance(item, dict) and _norm_vrf(item.get("vrf")) == want
    ]


def _build_endpoint_inventory(devices):
    endpoints = []
    compute = []
    uplinks = []
    unknown = []
    access = []
    trunks = []
    for device in devices:
        hostname = device.get("hostname")
        for entry in device.get("endpoint_mac_entries") or []:
            endpoints.append(
                {
                    "hostname": hostname,
                    "mac": entry.get("mac", ""),
                    "interface": entry.get("interface", ""),
                    "port_role": entry.get("port_role", "unknown"),
                    "attachment_role": entry.get("attachment_role", "unknown"),
                    "description": entry.get("description", ""),
                    "entry_type": entry.get("entry_type", "learned"),
                }
            )
        for item in device.get("port_attachments") or []:
            row = {
                "hostname": hostname,
                "interface": item.get("interface", ""),
                "role": item.get("role", "unknown"),
                "description": item.get("description", ""),
                "mode": item.get("mode", ""),
                "raw": item.get("raw", ""),
                "mac_count": item.get("mac_count", 0),
            }
            role = item.get("role")
            if role == "compute":
                compute.append(row)
            elif role == "switch_uplink":
                uplinks.append(row)
            elif role == "peer_link":
                uplinks.append(dict(row, exempt=True))
            else:
                unknown.append(row)
        for iface in device.get("access_ports") or []:
            access.append({"hostname": hostname, "interface": iface})
        for iface in device.get("switch_uplink_ports") or device.get("trunk_ports") or []:
            trunks.append(
                {
                    "hostname": hostname,
                    "interface": iface,
                    "prune_candidate": True,
                    "exempt": False,
                }
            )
        for iface in device.get("peer_link_ports") or device.get("exempt_trunk_ports") or []:
            trunks.append(
                {
                    "hostname": hostname,
                    "interface": iface,
                    "prune_candidate": False,
                    "exempt": True,
                }
            )
    return endpoints, access, trunks, compute, uplinks, unknown


def build_l3_discovery(vlan_id, devices):
    """First-class L3 view for a VLAN: SVI/HSRP, VRF, statics, BGP.

    Objects are scoped to the SVI VRF. The VRF is always labelled shared -
    discovery cannot prove uniqueness without scanning every other VLAN.
    """
    devices_l3 = []
    vrfs = []
    for device in devices:
        if not device.get("svi_present"):
            continue
        details = device.get("svi_details") or {}
        vrf = device.get("svi_vrf") or details.get("vrf") or "default"
        if vrf and vrf not in vrfs:
            vrfs.append(vrf)
        statics = _filter_by_vrf(device.get("static_routes") or [], vrf)
        neighbors = _filter_by_vrf(device.get("bgp_neighbors") or [], vrf)
        vlan_bgp = [
            block
            for block in (device.get("bgp_vlan_blocks") or [])
            if str(block.get("vlan_id")) == str(vlan_id)
        ]
        vrf_bgp = [
            item
            for item in (device.get("bgp_vrfs") or [])
            if _norm_vrf(item.get("name") or item.get("vrf")) == _norm_vrf(vrf)
        ]
        bgp_as = device.get("bgp_as") or ""
        if not bgp_as and neighbors:
            bgp_as = neighbors[0].get("bgp_as") or ""
        devices_l3.append(
            {
                "hostname": device.get("hostname"),
                "os_family": device.get("os_family"),
                "vrf": vrf,
                "shared_vrf": True,
                "svi": {
                    "description": details.get("description") or "",
                    "mtu": details.get("mtu"),
                    "ip_addresses": details.get("ip_addresses") or [],
                    "virtual_router_addresses": details.get("virtual_router_addresses")
                    or [],
                    "hsrp_addresses": details.get("hsrp_addresses") or [],
                    "hsrp_groups": details.get("hsrp_groups") or [],
                },
                "arp_count": len(device.get("arp_entries") or []),
                "static_routes": statics,
                "bgp_as": bgp_as,
                "bgp_neighbors": neighbors,
                "bgp_vlan": vlan_bgp,
                "bgp_vrf": vrf_bgp,
            }
        )
    return {
        "present": bool(devices_l3),
        "vrfs": vrfs,
        "shared_vrf": bool(vrfs),
        "shared_vrf_note": _SHARED_VRF_NOTE if vrfs else "",
        "devices": devices_l3,
    }

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
                    "os_family": discovery.get("os_family", "")
                    or per_vlan.get("os_family", ""),
                    "timestamp": discovery.get("timestamp", ""),
                    "vlan_present": per_vlan.get("vlan_present", False),
                    "vlan_name_on_box": per_vlan.get("vlan_name_on_box", ""),
                    "has_port_membership": per_vlan.get("has_port_membership", False),
                    "svi_present": per_vlan.get("svi_present", False),
                    "trunks_present": per_vlan.get("trunks_present", False),
                    "access_ports": per_vlan.get("access_ports", []),
                    "trunk_ports": per_vlan.get("trunk_ports", []),
                    "exempt_trunk_ports": per_vlan.get("exempt_trunk_ports", []),
                    "compute_ports": per_vlan.get("compute_ports", []),
                    "switch_uplink_ports": per_vlan.get("switch_uplink_ports", []),
                    "peer_link_ports": per_vlan.get("peer_link_ports", []),
                    "unknown_ports": per_vlan.get("unknown_ports", []),
                    "port_attachments": per_vlan.get("port_attachments", []),
                    "trunk_allowed": per_vlan.get("trunk_allowed", []),
                    "all_ports": per_vlan.get("all_ports", []),
                    "ports_raw": per_vlan.get("ports_raw", ""),
                    "mac_learned": per_vlan.get("mac_learned", False),
                    "arp_learned": per_vlan.get("arp_learned", False),
                    "mac_entries": per_vlan.get("mac_entries", []),
                    "endpoint_mac_entries": per_vlan.get("endpoint_mac_entries", []),
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
                    "bgp_vlan_blocks": discovery.get("bgp_vlan_blocks", []),
                    "bgp_vrfs": discovery.get("bgp_vrfs", []),
                    "bgp_as": discovery.get("bgp_as", ""),
                    "router_id": discovery.get("router_id", ""),
                    "loopback0": discovery.get("loopback0", ""),
                    "loopback1": discovery.get("loopback1", ""),
                    "ip_route_raw": discovery.get("ip_route_raw", ""),
                    "bgp_raw": discovery.get("bgp_raw", ""),
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
        switches_with_compute_ports = [
            device["hostname"]
            for device in devices
            if device.get("compute_ports")
        ]
        switches_with_trunk_ports = [
            device["hostname"]
            for device in devices
            if device.get("switch_uplink_ports") or device.get("trunk_ports")
        ]
        switches_with_mac_learning = [
            device["hostname"]
            for device in devices
            if device.get("endpoint_mac_entries") or device.get("mac_learned")
        ]
        switches_with_compute_macs = [
            device["hostname"]
            for device in devices
            if device.get("endpoint_mac_entries")
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

        # Classify before prune. Participating leaves and named gateway leaves
        # are withheld. Source gateways with no local endpoints stay
        # prune-eligible and are ordered first.
        placement = build_vlan_placement(vlan, devices)
        by_placement = placement["by_hostname"]

        # VLAN-scoped prune plans (routing objects stay in l3_review, never actions).
        prune_plans = []
        for device in devices:
            device_plan = device.get("prune_plan") or {}
            if not device_plan:
                device_plan = build_device_prune_plan(
                    device.get("hostname", ""),
                    [
                        {
                            "vlan_id": vlan_id,
                            "os_family": device.get("os_family"),
                            "vlan_present": device.get("vlan_present"),
                            "svi_present": device.get("svi_present"),
                            "svi_vrf": device.get("svi_vrf"),
                            "svi_details": device.get("svi_details"),
                            "trunk_ports": device.get("trunk_ports"),
                            "access_ports": device.get("access_ports"),
                            "compute_ports": device.get("compute_ports"),
                            "unknown_ports": device.get("unknown_ports"),
                            "switch_uplink_ports": device.get("switch_uplink_ports"),
                        }
                    ],
                    retain=None,
                    static_routes=device.get("static_routes"),
                    bgp_neighbors=device.get("bgp_neighbors"),
                    vlan_ids=[vlan_id],
                )
            else:
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
                l3_review = list(device_plan.get("l3_review") or [])
                blocked = list(device_plan.get("blocked_by_retain") or [])
                endpoint_ports = list(device.get("compute_ports") or []) + list(
                    device.get("unknown_ports") or []
                )
                if not endpoint_ports:
                    endpoint_ports = list(device.get("access_ports") or [])
                status = device_plan.get("status") or "candidate"
                if endpoint_ports:
                    status = "blocked_by_compute_endpoints"
                elif not vlan_actions:
                    status = "none"
                os_family = device.get("os_family") or device_plan.get("os_family", "")
                device_plan = {
                    "hostname": device.get("hostname"),
                    "os_family": os_family,
                    "vlan_ids": [int(vlan_id)],
                    "svi_vrfs": [device.get("svi_vrf")] if device.get("svi_vrf") else [],
                    "endpoint_ports": endpoint_ports,
                    "svi_inventory": device_plan.get("svi_inventory") or [],
                    "actions": vlan_actions,
                    "l3_review": l3_review,
                    "blocked_by_retain": blocked,
                    "retain": device_plan.get("retain", {}),
                    "destructive": False,
                    "human_required": True,
                    "apply_automated": False,
                    "status": status,
                    "order": device_plan.get("order")
                    or [
                        "Rehome or shut compute endpoints (servers / IBM Z / Nutanix / UCS / HCI)",
                        "Remove VLAN from switch-to-switch trunks (EOS session + commit timer)",
                        "no interface Vlan<id> (gateway)",
                        "no vlan <id>",
                    ],
                    "execution": build_prune_execution(
                        os_family, [vlan_id], vlan_actions
                    ),
                }
            decision = by_placement.get(device.get("hostname")) or {}
            role = decision.get("role") or "absent"
            device["placement_role"] = role
            if role != "prune_eligible":
                device_plan = dict(device_plan)
                device_plan["actions"] = []
                device_plan["status"] = PRUNE_STATUS_BY_ROLE.get(
                    role, "not_prune_eligible"
                )
                device_plan["placement_role"] = role
                device_plan["execution"] = build_prune_execution(
                    device_plan.get("os_family") or device.get("os_family"),
                    [vlan_id],
                    [],
                )
            else:
                device_plan["placement_role"] = "prune_eligible"
                device_plan["source_gateway"] = bool(decision.get("source_gateway"))
            if (
                device_plan.get("actions")
                or device_plan.get("l3_review")
                or device_plan.get("blocked_by_retain")
                or device_plan.get("endpoint_ports")
                or device_plan.get("svi_inventory")
            ):
                prune_plans.append(device_plan)

        prune_rank = {
            hostname: index
            for index, hostname in enumerate(placement["prune_order"])
        }
        prune_plans.sort(
            key=lambda plan: prune_rank.get(plan.get("hostname"), len(prune_rank))
        )

        ssot = _ssot_from_discovery(vlan, devices)
        ssot["inferred_service_type"] = placement["migration_type"]
        ssot["target_switches"] = [
            leaf["hostname"]
            for leaf in placement["deployment_model"]["placement"]["participating_leafs"]
        ]
        routing = placement["deployment_model"]["routing"]
        record_routing = vlan.get("routing") if isinstance(vlan.get("routing"), dict) else {}
        if not (vlan.get("gateway") or record_routing.get("gateway_ip")):
            routing["gateway_ip"] = ssot.get("gateway") or ""
        if not (vlan.get("prefixes") or record_routing.get("prefixes")):
            routing["prefixes"] = list(ssot.get("prefixes") or [])
        if ssot.get("gateway_vrf"):
            routing["vrf"] = ssot["gateway_vrf"]
        elif (
            not _named_vrf(vlan.get("vrf"))
            and not _named_vrf(record_routing.get("vrf"))
            and _named_vrf(ssot.get("discovered_vrf"))
        ):
            routing["vrf"] = _named_vrf(ssot["discovered_vrf"])
        site_vlan = placement["deployment_model"]["site"]["vlan"]
        if ssot.get("discovered_vlan_name"):
            site_vlan["name"] = ssot["discovered_vlan_name"]
        elif not site_vlan.get("name"):
            site_vlan["name"] = ssot.get("name") or vlan.get("name") or ""
        if ssot.get("svi_description"):
            site_vlan["description"] = ssot["svi_description"]
        attach_associated_routes(placement["deployment_model"], vlan_id, devices)
        (
            endpoints,
            access_inventory,
            trunk_inventory,
            compute_inventory,
            uplink_inventory,
            unknown_inventory,
        ) = _build_endpoint_inventory(devices)
        l3_discovery = build_l3_discovery(vlan_id, devices)

        report = {
            "vlan_id": vlan_id,
            "vlan_name": ssot.get("name") or vlan.get("name", ""),
            "vlan_slug": sanitize_report_slug(
                ssot.get("name") or vlan.get("name")
            ),
            "vlan_name_on_box": ssot.get("vlan_name_on_box", ""),
            "discovered_vlan_name": ssot.get("discovered_vlan_name", ""),
            "name_candidates": ssot.get("name_candidates", []),
            "needs_vlan_name": ssot.get("needs_vlan_name", False),
            "svi_description": ssot.get("svi_description", ""),
            "description_candidates": ssot.get("description_candidates", []),
            "needs_svi_description": ssot.get("needs_svi_description", False),
            "gateway_vrf": ssot.get("gateway_vrf", ""),
            "vrf_candidates": ssot.get("vrf_candidates", []),
            "needs_vrf": ssot.get("needs_vrf", False),
            "service_type": placement["migration_type"],
            "inferred_service_type": ssot.get("inferred_service_type", ""),
            "target_switches": vlan.get("target_switches")
            or ssot.get("target_switches", []),
            "snippet_target_switches": ssot.get("target_switches", []),
            "snippet_discovery_switches": ssot.get("discovery_switches", []),
            "switches_found": switches_found,
            "switches_vlan_defined": switches_vlan_defined,
            "switches_with_access_ports": switches_with_access_ports,
            "switches_with_compute_ports": switches_with_compute_ports,
            "switches_with_trunk_ports": switches_with_trunk_ports,
            "switches_with_mac_learning": switches_with_mac_learning,
            "switches_with_compute_macs": switches_with_compute_macs,
            "switches_with_svi": switches_with_svi,
            "switches_with_arp": switches_with_arp,
            "trunk_cleanup_candidates": trunk_cleanup_candidates,
            "prune_plans": prune_plans,
            "vni": vlan.get("vni"),
            "vrf": vlan.get("vrf") or ssot.get("discovered_vrf", "default"),
            "discovered_vrf": ssot.get("discovered_vrf", ""),
            "gateway": ssot.get("gateway", ""),
            "prefixes": ssot.get("prefixes", []),
            "svi_inventory": ssot.get("svi_inventory", []),
            "endpoint_inventory": endpoints,
            "access_inventory": access_inventory,
            "trunk_inventory": trunk_inventory,
            "compute_inventory": compute_inventory,
            "uplink_inventory": uplink_inventory,
            "unknown_inventory": unknown_inventory,
            "l3_discovery": l3_discovery,
            "data_center": vlan.get("data_center", ""),
            "devices": devices,
            "migration_type": placement["migration_type"],
            "migration_reason": placement["migration_reason"],
            "deployment_model": placement["deployment_model"],
            "placement_analysis": placement["analysis"],
        }
        report["deployment_model"]["site"]["data_center"] = (
            report["deployment_model"]["site"].get("data_center")
            or vlan.get("data_center")
            or ""
        )
        report["discovery_export"] = _discovery_export(report)
        report["prune_export"] = _prune_export(report)
        reports.append(report)
    return reports


_DISCOVERY_OMIT = frozenset(
    {
        "prune_plans",
        "trunk_cleanup_candidates",
        "discovery_export",
        "prune_export",
        "deployment_model",
        "placement_analysis",
    }
)
_DEVICE_DISCOVERY_OMIT = frozenset(
    {"prune_plan", "trunk_cleanup_recommendations"}
)


def _discovery_export(report):
    """Public discovery payload: no prune CLI or session text."""
    export = {key: value for key, value in report.items() if key not in _DISCOVERY_OMIT}
    devices = []
    for device in export.get("devices") or []:
        devices.append(
            {
                key: value
                for key, value in device.items()
                if key not in _DEVICE_DISCOVERY_OMIT
            }
        )
    export["devices"] = devices
    return export


def _prune_export(report):
    """Public prune payload: execution-shaped CLI plus L3 review."""
    return {
        "vlan_id": report.get("vlan_id"),
        "vlan_name": report.get("vlan_name"),
        "vlan_slug": report.get("vlan_slug"),
        "data_center": report.get("data_center"),
        "gateway": report.get("gateway"),
        "prefixes": report.get("prefixes"),
        "compute_inventory": report.get("compute_inventory"),
        "unknown_inventory": report.get("unknown_inventory"),
        "uplink_inventory": report.get("uplink_inventory"),
        "svi_inventory": report.get("svi_inventory"),
        "l3_discovery": report.get("l3_discovery"),
        "trunk_cleanup_candidates": report.get("trunk_cleanup_candidates"),
        "prune_plans": report.get("prune_plans"),
        "placement": (report.get("deployment_model") or {}).get("placement") or {},
        "withheld": [
            {
                "hostname": item.get("hostname"),
                "role": item.get("role"),
                "reason": item.get("role"),
            }
            for item in ((report.get("placement_analysis") or {}).get("switches") or [])
            if item.get("role") != "prune_eligible"
        ],
        "apply_automated": False,
        "human_required": True,
    }


def _placeholder_vlan_name(name, vlan_id):
    """True for an empty name or the synthetic vlan_<id> placeholder."""
    text = str(name or "").strip()
    if not text:
        return True
    if _PLACEHOLDER_VLAN_NAME.match(text):
        return True
    if vlan_id is not None and text.lower() in (
        "vlan%s" % vlan_id,
        "vlan_%s" % vlan_id,
        "vlan-%s" % vlan_id,
    ):
        return True
    return False


def _named_vrf(value):
    """Return a tenant VRF name. The global table is not a gateway VRF."""
    text = str(value or "").strip()
    if not text or text.lower() == "default":
        return ""
    return text


def _unique_texts(values):
    found = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def resolve_discovered_labels(vlan, devices):
    """Choose the VLAN name and the previous-gateway interface description.

    The VLAN name is the on-box name. A source gateway (SVI present) wins
    over any other switch. One agreed value is used. Differing values are
    left for the operator to answer.

    The interface description is the description on that previous gateway
    SVI, with the same agree-or-ask rule. A placeholder record name is not
    used as a description.

    The gateway VRF is the VRF on that same SVI. The fabric SVI and the BGP
    network statement both use it. ``default`` is not a gateway VRF.
    """
    vlan = vlan or {}
    vlan_id = vlan.get("id")
    source_names = []
    other_names = []
    source_descriptions = []
    source_vrfs = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        name = str(device.get("vlan_name_on_box") or "").strip()
        if name and _placeholder_vlan_name(name, vlan_id):
            name = ""
        details = device.get("svi_details") or {}
        description = str(details.get("description") or "").strip()
        if description and _placeholder_vlan_name(description, vlan_id):
            description = ""
        if device.get("svi_present"):
            if name:
                source_names.append(name)
            if description:
                source_descriptions.append(description)
            vrf_text = str(
                device.get("svi_vrf") or details.get("vrf") or ""
            ).strip()
            if vrf_text:
                source_vrfs.append(vrf_text)
        elif name:
            other_names.append(name)

    record_name = str(vlan.get("vlan_name") or vlan.get("name") or "").strip()
    record_description = str(
        vlan.get("svi_description") or vlan.get("description") or ""
    ).strip()
    record_vrf = _named_vrf(vlan.get("vrf"))

    name_candidates = _unique_texts(source_names) or _unique_texts(other_names)
    description_candidates = _unique_texts(source_descriptions)
    vrf_candidates = _unique_texts(source_vrfs)
    named_vrfs = [_named_vrf(item) for item in vrf_candidates if _named_vrf(item)]

    vlan_name = ""
    name_source = ""
    if len(name_candidates) == 1:
        vlan_name = name_candidates[0]
        name_source = "discovery"
    elif (
        not name_candidates
        and record_name
        and not _placeholder_vlan_name(record_name, vlan_id)
    ):
        vlan_name = record_name
        name_source = "record"

    gateway_vrf = ""
    vrf_source = ""
    if len(vrf_candidates) > 1:
        gateway_vrf = ""
    elif len(named_vrfs) == 1:
        gateway_vrf = named_vrfs[0]
        vrf_source = "source_gateway"
    elif record_vrf:
        gateway_vrf = record_vrf
        vrf_source = "record"

    svi_description = ""
    description_source = ""
    if len(description_candidates) == 1:
        svi_description = description_candidates[0]
        description_source = "source_gateway"
    elif (
        not description_candidates
        and record_description
        and not _placeholder_vlan_name(record_description, vlan_id)
    ):
        svi_description = record_description
        description_source = "record"

    return {
        "vlan_name": vlan_name,
        "vlan_name_on_box": name_candidates[0] if name_candidates else "",
        "name_candidates": name_candidates,
        "name_source": name_source,
        "needs_vlan_name": not bool(vlan_name),
        "svi_description": svi_description,
        "description_candidates": description_candidates,
        "description_source": description_source,
        "needs_svi_description": not bool(svi_description),
        "gateway_vrf": gateway_vrf,
        "vrf_candidates": vrf_candidates,
        "vrf_source": vrf_source,
        "needs_vrf": not bool(gateway_vrf),
    }


def merge_discovered_labels(record, reports):
    """Copy a resolved discovery name and SVI description onto a VLAN record."""
    if not isinstance(record, dict):
        record = {}
    else:
        record = dict(record)
    report = {}
    for item in reports or []:
        if isinstance(item, dict) and _str_equal(item.get("vlan_id"), record.get("id")):
            report = item
            break
    name = str(report.get("discovered_vlan_name") or "").strip()
    if name:
        record["name"] = name
        record["vlan_name"] = name
    description = str(report.get("svi_description") or "").strip()
    if description:
        record["description"] = description
        record["svi_description"] = description
    gateway_vrf = _named_vrf(report.get("gateway_vrf"))
    if gateway_vrf:
        record["vrf"] = gateway_vrf
    record["_label_needs_vlan_name"] = bool(report.get("needs_vlan_name"))
    record["_label_needs_svi_description"] = bool(report.get("needs_svi_description"))
    record["_label_needs_vrf"] = bool(report.get("needs_vrf"))
    record["_name_candidates"] = list(report.get("name_candidates") or [])
    record["_description_candidates"] = list(report.get("description_candidates") or [])
    record["_vrf_candidates"] = list(report.get("vrf_candidates") or [])
    return record


def _ssot_from_discovery(vlan, devices):
    """Derive paste-ready VLAN facts from discovery. Migration type is set by placement."""
    prefixes = []
    prefix_seen = set()
    gateways = []
    gateway_seen = set()
    vrfs = []
    names_on_box = []
    svi_inventory = []
    discovery_switches = []
    target_switches = []

    for device in devices:
        hostname = device.get("hostname")
        if device.get("vlan_present") and hostname:
            discovery_switches.append(hostname)
        box_name = device.get("vlan_name_on_box") or ""
        if box_name:
            names_on_box.append(box_name)
        details = device.get("svi_details") or {}
        if not device.get("svi_present"):
            continue
        vrf = device.get("svi_vrf") or details.get("vrf") or "default"
        if vrf and vrf not in vrfs:
            vrfs.append(vrf)
        ips = list(details.get("ip_addresses") or [])
        vips = list(details.get("virtual_router_addresses") or [])
        hsrp = list(details.get("hsrp_addresses") or [])
        svi_inventory.append(
            {
                "hostname": hostname,
                "vrf": vrf,
                "ip_addresses": ips,
                "virtual_router_addresses": vips,
                "hsrp_addresses": hsrp,
                "hsrp_groups": details.get("hsrp_groups") or [],
                "description": details.get("description") or "",
            }
        )
        for cidr in ips:
            prefix = network_prefix(cidr)
            if prefix and prefix not in prefix_seen:
                prefix_seen.add(prefix)
                prefixes.append(prefix)
        for vip in vips + hsrp:
            if vip and vip not in gateway_seen:
                gateway_seen.add(vip)
                gateways.append(vip)
        os_family = str(device.get("os_family") or "").lower()
        if hostname and os_family == "eos" and hostname not in target_switches:
            target_switches.append(hostname)

    if not target_switches:
        for device in devices:
            hostname = device.get("hostname")
            if hostname and (
                device.get("svi_present")
                or device.get("access_ports")
                or device.get("compute_ports")
            ):
                if hostname not in target_switches:
                    target_switches.append(hostname)

    # Placement finalizes L2 vs L3. Endpoint attachment is not an l2_l3 type.
    inferred = "l2"

    labels = resolve_discovered_labels(vlan, devices)
    discovered_vrf = vrfs[0] if vrfs else (vlan.get("vrf") or "default")

    return {
        "name": labels["vlan_name"] or vlan.get("name") or "",
        "discovered_vlan_name": labels["vlan_name"],
        "vlan_name_on_box": labels["vlan_name_on_box"] or (names_on_box[0] if names_on_box else ""),
        "name_candidates": labels["name_candidates"],
        "needs_vlan_name": labels["needs_vlan_name"],
        "svi_description": labels["svi_description"],
        "description_candidates": labels["description_candidates"],
        "needs_svi_description": labels["needs_svi_description"],
        "gateway_vrf": labels["gateway_vrf"],
        "vrf_candidates": labels["vrf_candidates"],
        "needs_vrf": labels["needs_vrf"],
        "inferred_service_type": inferred,
        "discovered_vrf": discovered_vrf,
        "gateway": gateways[0] if gateways else "",
        "prefixes": prefixes,
        "svi_inventory": svi_inventory,
        "discovery_switches": discovery_switches,
        "target_switches": target_switches,
    }


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


