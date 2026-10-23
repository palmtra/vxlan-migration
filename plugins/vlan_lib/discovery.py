# -*- coding: utf-8 -*-
"""Per-VLAN discovery record builders and verification helpers."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, _vlan_present
from vlan_lib.parsers import (
    _normalize_interface_name,
    classify_attachment_role,
    classify_mac_port_role,
    index_interface_configs,
    parse_arp_entries,
    parse_mac_address_table,
    parse_svi_details,
    parse_svi_vrf,
    parse_vlan_id_ports,
    svi_command_indicates_present,
)
from vlan_lib.policy import parse_bgp_prefix_detail
from vlan_lib.topology import (
    classify_neighbor,
    short_hostname,
    neighbors_for_interface,
    parse_spanning_tree_vlan,
    parse_vxlan_vlan_to_vni,
)

# Ports whose MACs are local endpoints of the VLAN.
_ENDPOINT_MAC_ROLES = frozenset(
    {"compute", "unknown", "network_service", "downstream_switch", "fabric_interconnect"}
)

# When a port-channel's members see different neighbors, the strongest wins.
_NEIGHBOR_ROLE_RANK = (
    "switch",
    "downstream_switch",
    "network_service",
    "fabric_interconnect",
    "compute",
)


def _inventory_peers(neighbors, description, inventory):
    """Inventory switches on the far end: ``[{hostname, port}]``.

    LLDP/CDP names are matched on the short hostname. Without a neighbor, an
    inventory hostname in the port description is the peer (port unknown).
    """
    by_short = {short_hostname(name): name for name in inventory or [] if name}
    by_short.pop("", None)
    peers = []
    for item in neighbors or []:
        hostname = by_short.get(short_hostname(item.get("neighbor_name")))
        if hostname:
            peer = {"hostname": hostname, "port": item.get("neighbor_port") or ""}
            if peer not in peers:
                peers.append(peer)
    if not peers and description:
        desc = str(description).lower()
        for short, hostname in by_short.items():
            # Word boundaries: leaf-1 must not match inside leaf-10.
            if len(short) >= 4 and re.search(r"(?<![\w-])%s(?!\w)" % re.escape(short), desc):
                peers.append({"hostname": hostname, "port": ""})
    return peers


def _port_neighbor(iface, topology, platform):
    """Return ``(role, label, neighbors)`` for the neighbor(s) seen on *iface*."""
    neighbors = neighbors_for_interface(
        iface,
        topology.get("neighbors") or [],
        topology.get("port_channels") or {},
        platform,
    )
    if not neighbors:
        return "", "", []
    inventory = topology.get("inventory_hostnames") or []
    roles = [classify_neighbor(item, inventory) for item in neighbors]
    role = ""
    for candidate in _NEIGHBOR_ROLE_RANK:
        if candidate in roles:
            role = candidate
            break
    label = ", ".join(
        sorted(
            {
                ("%s %s" % (item.get("neighbor_name") or "?", item.get("neighbor_port") or "")).strip()
                for item in neighbors
            }
        )
    )
    return role, label, neighbors


def _attachment_for_port(
    iface,
    config_by_norm,
    platform,
    access_ports,
    trunk_ports,
    exempt_trunk_ports,
    switch_hostnames,
    mac_count=0,
    topology=None,
):
    parsed = config_by_norm.get(_normalize_interface_name(iface, platform), {})
    topology = topology or {}
    neighbor_role, neighbor, neighbors = _port_neighbor(iface, topology, platform)
    role = classify_attachment_role(
        iface,
        parsed,
        access_ports,
        trunk_ports,
        exempt_trunk_ports,
        switch_hostnames,
        neighbor_role,
    )
    return {
        "interface": parsed.get("interface") or iface,
        "role": role,
        "description": parsed.get("description") or "",
        "neighbor": neighbor,
        "neighbor_role": neighbor_role,
        "peers": _inventory_peers(
            neighbors, parsed.get("description") or "", topology.get("inventory_hostnames")
        ),
        "mode": parsed.get("mode") or "",
        "raw": parsed.get("raw") or "",
        "mac_count": mac_count,
    }


def _build_port_attachments(
    all_ports,
    mac_entries,
    config_by_norm,
    platform,
    access_ports,
    trunk_ports,
    exempt_trunk_ports,
    switch_hostnames,
    topology=None,
):
    mac_counts = {}
    extra_mac_ports = []
    for entry in mac_entries or []:
        iface = entry.get("interface") or ""
        if not iface:
            continue
        norm = _normalize_interface_name(iface, platform)
        mac_counts[norm] = mac_counts.get(norm, 0) + 1
        extra_mac_ports.append(iface)

    attachments = []
    seen = set()
    for iface in list(all_ports or []) + extra_mac_ports:
        if not iface:
            continue
        norm = _normalize_interface_name(iface, platform)
        if norm in seen:
            continue
        seen.add(norm)
        attachments.append(
            _attachment_for_port(
                iface,
                config_by_norm,
                platform,
                access_ports,
                trunk_ports,
                exempt_trunk_ports,
                switch_hostnames,
                mac_counts.get(norm, 0),
                topology,
            )
        )
    return attachments


def _ifaces_with_role(attachments, role):
    return [item["interface"] for item in attachments if item.get("role") == role]

def extract_vlan_targeted_discovery(vlan, outputs):
    """Build a per-VLAN discovery record from targeted CLI commands."""
    vlan_id = vlan.get("id")
    if vlan_id is None:
        raise AnsibleFilterError("extract_vlan_targeted_discovery requires vlan['id']")

    vlan_output = outputs.get("vlan_id_output", "")
    svi_output = outputs.get("svi_output", "")
    mac_output = outputs.get("mac_output", "")
    arp_output = outputs.get("arp_output", "")
    platform = outputs.get("platform", "eos")
    trunk_interface_names = outputs.get("trunk_interface_names", []) or []
    exempt_trunk_interface_names = outputs.get("exempt_trunk_interface_names", []) or []
    trunk_summaries = outputs.get("trunk_summaries") or []
    inventory_hostnames = outputs.get("inventory_hostnames") or []
    switch_hostnames = list(outputs.get("switch_hostnames") or [])
    for host in inventory_hostnames:
        if host not in switch_hostnames:
            switch_hostnames.append(host)
    topology = {
        "neighbors": outputs.get("neighbors") or [],
        "port_channels": outputs.get("port_channels") or {},
        "inventory_hostnames": switch_hostnames,
    }
    config_by_norm = index_interface_configs(
        outputs.get("interface_configs") or [], platform
    )

    port_membership = parse_vlan_id_ports(
        vlan_output,
        vlan_id,
        platform,
        trunk_interface_names,
        exempt_trunk_interface_names,
    )
    vlan_present = port_membership.get("vlan_present", False)
    svi_present = svi_command_indicates_present(svi_output, vlan_id)
    svi_vrf = parse_svi_vrf(svi_output, platform) if svi_present else ""
    mac_entries = parse_mac_address_table(mac_output, vlan_id)
    arp_entries = parse_arp_entries(arp_output, vlan_id) if svi_present else []
    mac_learned = bool(mac_entries)
    arp_learned = bool(arp_entries)

    access_ports = port_membership.get("access_ports", [])
    trunk_ports = port_membership.get("trunk_ports", [])
    exempt_trunk_ports = port_membership.get("exempt_trunk_ports", [])
    port_attachments = _build_port_attachments(
        port_membership.get("all_ports", []),
        mac_entries,
        config_by_norm,
        platform,
        access_ports,
        trunk_ports,
        exempt_trunk_ports,
        switch_hostnames,
        topology,
    )
    attachment_by_norm = {
        _normalize_interface_name(item["interface"], platform): item
        for item in port_attachments
    }
    compute_ports = _ifaces_with_role(port_attachments, "compute")
    switch_uplink_ports = _ifaces_with_role(port_attachments, "switch_uplink")
    peer_link_ports = _ifaces_with_role(port_attachments, "peer_link")
    unknown_ports = _ifaces_with_role(port_attachments, "unknown")
    service_ports = _ifaces_with_role(port_attachments, "network_service")
    downstream_switch_ports = _ifaces_with_role(port_attachments, "downstream_switch")
    for entry in mac_entries:
        iface = entry.get("interface", "")
        entry["port_role"] = classify_mac_port_role(
            iface,
            access_ports,
            trunk_ports,
            exempt_trunk_ports,
        )
        attached = attachment_by_norm.get(
            _normalize_interface_name(iface, platform), {}
        )
        entry["attachment_role"] = attached.get("role") or classify_attachment_role(
            iface,
            config_by_norm.get(_normalize_interface_name(iface, platform), {}),
            access_ports,
            trunk_ports,
            exempt_trunk_ports,
            switch_hostnames,
        )
        entry["neighbor"] = attached.get("neighbor") or ""
        entry["description"] = attached.get("description") or (
            config_by_norm.get(_normalize_interface_name(iface, platform), {}).get(
                "description"
            )
            or ""
        )
    endpoint_mac_entries = [
        entry
        for entry in mac_entries
        if entry.get("attachment_role") in _ENDPOINT_MAC_ROLES
    ]

    svi_details = parse_svi_details(svi_output if svi_present else "", platform)
    bgp_prefix_detail = []
    for item in outputs.get("bgp_prefix_outputs") or []:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        parsed_bgp = parse_bgp_prefix_detail(item.get("stdout") or "")
        parsed_bgp.update(
            {
                "prefix": item.get("prefix") or str(item.get("item") or ""),
                "vrf": item.get("vrf") or svi_vrf or "default",
            }
        )
        bgp_prefix_detail.append(parsed_bgp)
    acl_configs = {}
    for item in outputs.get("acl_outputs") or []:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        name = item.get("name") or str(item.get("item") or "")
        if name:
            acl_configs[name] = str(item.get("stdout") or "").strip()
    summary_by_norm = {}
    for item in trunk_summaries:
        if not isinstance(item, dict):
            continue
        iface = item.get("interface") or ""
        if iface:
            summary_by_norm[_normalize_interface_name(iface, platform)] = item.get(
                "allowed_summary", ""
            )
    trunk_allowed = []
    for iface in trunk_ports:
        trunk_allowed.append(
            {
                "interface": iface,
                "allowed_summary": summary_by_norm.get(
                    _normalize_interface_name(iface, platform), ""
                ),
            }
        )
    prune_trunks = switch_uplink_ports or trunk_ports
    trunk_cleanup_recommendations = [
        {
            "interface": iface,
            "port_role": "trunk",
            "cli": "switchport trunk allowed vlan remove %s" % int(vlan_id),
            "recommendation": (
                "VLAN %s is carried on switch-to-switch trunk %s. "
                "Prune CLI lives in the prune report, not discovery."
                % (vlan_id, iface)
            ),
        }
        for iface in prune_trunks
    ]

    # Uplinks = switch-to-switch trunks plus exempt peer-links.
    uplinks = list(switch_uplink_ports) + [
        iface for iface in peer_link_ports if iface not in switch_uplink_ports
    ]
    if not uplinks:
        uplinks = list(trunk_ports) + [
            iface for iface in exempt_trunk_ports if iface not in trunk_ports
        ]

    return {
        "vlan_id": vlan_id,
        "vlan_name": vlan.get("name", ""),
        "vlan_name_on_box": port_membership.get("vlan_name_on_box", ""),
        "os_family": str(platform).lower(),
        "target_vrf": svi_vrf or outputs.get("target_vrf", vlan.get("vrf", "")),
        "svi_vrf": svi_vrf,
        "svi_details": svi_details,
        "vlan_present": vlan_present,
        "has_port_membership": port_membership.get("has_port_membership", False),
        "svi_present": svi_present,
        "trunks_present": bool(trunk_ports),
        "access_ports": access_ports,
        "trunk_ports": trunk_ports,
        "exempt_trunk_ports": exempt_trunk_ports,
        "compute_ports": compute_ports,
        "switch_uplink_ports": switch_uplink_ports,
        "peer_link_ports": peer_link_ports,
        "unknown_ports": unknown_ports,
        "service_ports": service_ports,
        "downstream_switch_ports": downstream_switch_ports,
        "mlag_peer_ports": port_membership.get("mlag_peer_ports", []),
        "vxlan_mapped": port_membership.get("vxlan_mapped", False),
        "port_attachments": port_attachments,
        "trunk_allowed": trunk_allowed,
        "uplinks": uplinks,
        "all_ports": port_membership.get("all_ports", []),
        "ports_raw": port_membership.get("ports_raw", ""),
        "mac_learned": mac_learned,
        "arp_learned": arp_learned,
        "mac_entries": mac_entries,
        "endpoint_mac_entries": endpoint_mac_entries,
        "arp_entries": arp_entries,
        "vlan_id_raw": vlan_output,
        "svi_raw": svi_output,
        "mac_table_raw": mac_output,
        "arp_raw": arp_output,
        "trunk_cleanup_recommendations": trunk_cleanup_recommendations,
        "stp": parse_spanning_tree_vlan(outputs.get("stp_output") or ""),
        "bgp_prefix_detail": bgp_prefix_detail,
        "acl_configs": acl_configs,
        "errors": list(outputs.get("errors") or []),
    }


def extract_vlan_discovery(vlan, outputs):
    """Build a structured per-VLAN discovery record from raw CLI outputs.

    Args:
        vlan: VLAN dictionary from the registry (must contain 'id')
        outputs: dict with keys:
            vlan_output, svi_output, trunk_output,
            mac_outputs (list of result dicts from looped commands),
            arp_output

    Returns:
        Dictionary summarizing presence and raw output excerpts.
    """
    vlan_id = vlan.get("id")
    if vlan_id is None:
        raise AnsibleFilterError("extract_vlan_discovery requires vlan['id']")

    vlan_output = outputs.get("vlan_output", "")
    svi_output = outputs.get("svi_output", "")
    trunk_output = outputs.get("trunk_output", "")
    arp_output = outputs.get("arp_output", "")

    # Locate the mac table output that belongs to this VLAN ID.
    mac_outputs = outputs.get("mac_outputs", []) or []
    mac_output = ""
    for entry in mac_outputs:
        if _str_equal(entry.get("item"), vlan_id):
            mac_output = entry.get("stdout", "")
            break

    return {
        "vlan_id": vlan_id,
        "vlan_name": vlan.get("name", ""),
        "target_vrf": outputs.get("target_vrf", vlan.get("vrf", "")),
        "vlan_present": _vlan_present(vlan_id, vlan_output),
        "svi_present": _vlan_present(vlan_id, svi_output),
        "trunks_present": _vlan_present(vlan_id, trunk_output),
        "mac_table_raw": mac_output,
        "arp_raw": arp_output,
    }


def build_vlan_verification(vlan_id, outputs):
    """Build a post-change verification summary for a single VLAN.

    Args:
        vlan_id: the VLAN ID being verified
        outputs: dict with keys:
            vni_output: raw 'show vxlan vlan-to-vni' output
            mac_outputs: list of result dicts from the looped MAC table commands
            arp_output: raw ARP table output for the target VRF
            registry: list of VLAN dictionaries from the canonical registry
                      (used to look up the expected VNI for this VLAN)

    Returns:
        Dictionary with vlan_id, vni, vni_mapped, and mac_or_arp_learned.
    """
    vni_output = outputs.get("vni_output", "")
    arp_output = outputs.get("arp_output", "")
    registry = outputs.get("registry", []) or []

    vni = None
    for entry in registry:
        if _str_equal(entry.get("id"), vlan_id):
            vni = entry.get("vni")
            break

    vni_mapped = False
    if vni is not None:
        vni_mapped = any(
            str(item["vlan_id"]) == str(vlan_id) and str(item["vni"]) == str(vni)
            for item in parse_vxlan_vlan_to_vni(vni_output)
        )

    mac_outputs = outputs.get("mac_outputs", []) or []
    mac_output = ""
    for entry in mac_outputs:
        if _str_equal(entry.get("item"), vlan_id):
            mac_output = entry.get("stdout", "")
            break

    mac_pattern = r"([0-9a-fA-F]{2}[:.\-]){5}[0-9a-fA-F]{2}"
    mac_learned = bool(re.search(mac_pattern, mac_output))
    arp_learned = bool(re.search(r"\d+\.\d+\.\d+\.\d+", arp_output))

    return {
        "vlan_id": vlan_id,
        "vni": vni,
        "vni_mapped": vni_mapped,
        "mac_or_arp_learned": mac_learned or arp_learned,
    }


