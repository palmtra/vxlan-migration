# -*- coding: utf-8 -*-
"""Per-VLAN discovery record builders and verification helpers."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, _vlan_present
from vlan_lib.parsers import (
    classify_mac_port_role,
    parse_arp_entries,
    parse_mac_address_table,
    parse_svi_details,
    parse_svi_vrf,
    parse_vlan_id_ports,
    svi_command_indicates_present,
)

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
    for entry in mac_entries:
        entry["port_role"] = classify_mac_port_role(
            entry.get("interface", ""),
            access_ports,
            trunk_ports,
            exempt_trunk_ports,
        )

    svi_details = parse_svi_details(svi_output, platform) if svi_present else {
        "present": False,
        "description": "",
        "mtu": None,
        "vrf": "",
        "ip_addresses": [],
        "virtual_router_addresses": [],
    }
    trunk_cleanup_recommendations = [
        {
            "interface": iface,
            "port_role": "trunk",
            "recommendation": (
                "VLAN is carried on trunk "
                f"{iface}. Plan trunk prune during legacy decommission "
                "(candidate only; discovery never applies deletes)."
            ),
        }
        for iface in trunk_ports
    ]

    # Uplinks = trunks carrying the VLAN (Ports column ∩ trunk list), plus exempt peer-links.
    uplinks = list(trunk_ports) + [
        iface for iface in exempt_trunk_ports if iface not in trunk_ports
    ]

    return {
        "vlan_id": vlan_id,
        "vlan_name": vlan.get("name", ""),
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
        "uplinks": uplinks,
        "all_ports": port_membership.get("all_ports", []),
        "ports_raw": port_membership.get("ports_raw", ""),
        "mac_learned": mac_learned,
        "arp_learned": arp_learned,
        "mac_entries": mac_entries,
        "arp_entries": arp_entries,
        "vlan_id_raw": vlan_output,
        "svi_raw": svi_output,
        "mac_table_raw": mac_output,
        "arp_raw": arp_output,
        "trunk_cleanup_recommendations": trunk_cleanup_recommendations,
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
        vni_mapped = _vlan_present(vlan_id, vni_output) and _vlan_present(vni, vni_output)

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


