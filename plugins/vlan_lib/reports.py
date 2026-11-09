# -*- coding: utf-8 -*-
"""Aggregated VLAN discovery report builders."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, sanitize_report_slug, vlan_db_record_filename
from vlan_lib.endpoints import ipv6_is_link_local
from vlan_lib.parsers import _normalize_interface_name, network_prefix, normalize_mac_address
from vlan_lib.placement import (
    PRUNE_STATUS_BY_ROLE,
    attach_associated_routes,
    build_vlan_placement,
    condense_associated_routes,
    device_tier,
    vlan_gateway_hosts,
)
from vlan_lib.vni import allocate_vni, used_vnis, vni_range_problems
from vlan_lib.prune import _prune_action, _trunk_remove_cli, build_device_prune_plan, build_prune_execution

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

_ENDPOINT_ROLES = frozenset(
    {"compute", "unknown", "access", "network_service", "downstream_switch", "fabric_interconnect"}
)
# When a MAC is learned on several endpoint-facing ports, the most specific
# port is where it lives. A downstream switch or an unclassified port is only
# the path toward it.
_EDGE_ROLE_RANK = {
    "compute": 0,
    "network_service": 1,
    "fabric_interconnect": 2,
    "access": 3,
    "unknown": 4,
    "downstream_switch": 5,
}
# Ports that end at a device rather than lead on to more switches. Two of
# these for one MAC (other than one dual-homed port-channel) is a conflict.
_TERMINAL_ROLES = frozenset({"compute", "network_service", "fabric_interconnect", "access"})
# Flags that mark a row as the gateway, not a host. verify.py skips these too.
_INFRA_FLAGS = frozenset({"gateway", "fhrp_mac"})
# HSRP v1/v2 and VRRP virtual MACs. These are gateways, not hosts.
_FHRP_MAC_PREFIXES = ("00:00:0c:07:ac", "00:00:0c:9f:f", "00:00:5e:00:01", "00:00:5e:00:02")


def _ip_sort_key(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return (999,)


def _host_part(cidr):
    return str(cidr or "").split("/")[0]


def _is_port_channel(interface):
    return bool(re.match(r"^po\d", _normalize_interface_name(interface or "")))


def _attachments_by_port(device):
    return {
        _normalize_interface_name(item.get("interface") or ""): item
        for item in device.get("port_attachments") or []
        if isinstance(item, dict) and item.get("interface")
    }


def _resolve_edge(places):
    """Pick where a MAC lives from the ports it was learned on.

    Returns ``(primary, others, flags)``. The most specific port wins
    (_EDGE_ROLE_RANK, then switch and port name, so the answer never depends on
    play order). The same port-channel on two switches is one dual-homed
    attachment (vPC / MLAG). Two different device-facing ports are a conflict
    (a loop, a move during discovery, or a misread port role).
    """
    ordered = sorted(
        places,
        key=lambda p: (_EDGE_ROLE_RANK.get(p["role"], 9), p["hostname"], _normalize_interface_name(p["interface"])),
    )
    primary = ordered[0]
    others = ordered[1:]
    flags = []
    norm = _normalize_interface_name(primary["interface"])
    if _is_port_channel(primary["interface"]) and any(
        o["hostname"] != primary["hostname"] and _normalize_interface_name(o["interface"]) == norm for o in others
    ):
        flags.append("dual_homed")
    if primary["role"] in _TERMINAL_ROLES:
        conflicting = [
            o
            for o in others
            if o["role"] in _TERMINAL_ROLES
            and not (o["hostname"] != primary["hostname"] and _normalize_interface_name(o["interface"]) == norm
                     and _is_port_channel(o["interface"]))
        ]
        if conflicting:
            flags.append("multiple_edge_ports")
    if any((p.get("moves") or 0) > 1 for p in places):
        flags.append("mac_moved")
    return primary, others, flags


def build_endpoint_table(devices, gateway=None):
    """One row per active IP endpoint, located at its edge port.

    ARP (from the SVI owners) gives IP to MAC. The MAC table gives the port;
    when it has aged out, the port EOS prints in the ARP row is used instead
    (flag ``located_by_arp``). A MAC seen only on switch-to-switch trunks or a
    vPC peer-link is ``uplink_only``: it sits behind a switch outside
    discovery scope. Gateway and FHRP addresses are flagged so they are not
    mistaken for hosts. Conflicts are flagged, never dropped: an IP answered by
    two MACs (``ip_conflict``), a MAC on two device-facing ports
    (``multiple_edge_ports``), a MAC that moved (``mac_moved``, EOS).

    IPv6 neighbors add global addresses to the row of their MAC (``ipv6``);
    a MAC known only from IPv6 gets a MAC-only row. With several samples
    (bulk collection), ``seen`` is how many samples saw the endpoint and
    ``intermittent`` flags one that was missing from some.
    """
    gateway = gateway or {}
    infra_ips = set()
    for entry in list(gateway.get("svis") or []) + list(gateway.get("inactive_svis") or []):
        for cidr in entry.get("ip_addresses") or []:
            infra_ips.add(_host_part(cidr))
        infra_ips.update(entry.get("gateway_addresses") or [])

    edge = {}
    uplink = {}
    seen = {}
    passes = max(
        [int((device.get("endpoint_collection") or {}).get("passes") or 1) for device in devices or []] or [1]
    )

    def note_seen(key, entry):
        if entry.get("seen") is not None:
            seen[key] = max(seen.get(key, 0), int(entry["seen"]))

    def note_uplink(mac, hostname):
        uplink.setdefault(mac, [])
        if hostname and hostname not in uplink[mac]:
            uplink[mac].append(hostname)

    for device in devices or []:
        hostname = device.get("hostname") or ""
        for entry in device.get("mac_entries") or []:
            if not isinstance(entry, dict):
                continue
            mac = normalize_mac_address(entry.get("mac"))
            if not mac:
                continue
            note_seen(mac, entry)
            role = entry.get("attachment_role") or entry.get("port_role") or "unknown"
            if role in _ENDPOINT_ROLES:
                edge.setdefault(mac, []).append(
                    {
                        "hostname": hostname,
                        "interface": entry.get("interface") or "",
                        "description": entry.get("description") or "",
                        "neighbor": entry.get("neighbor") or "",
                        "role": role,
                        "moves": entry.get("moves"),
                    }
                )
            else:
                note_uplink(mac, hostname)
        # The vPC peer learned it: it is behind the peer, not on this switch.
        for entry in device.get("peer_link_mac_entries") or []:
            if isinstance(entry, dict) and entry.get("mac"):
                note_uplink(normalize_mac_address(entry.get("mac")), hostname)

    arp = {}
    arp_ports = {}
    ipv6 = {}
    for device in devices or []:
        hostname = device.get("hostname") or ""
        attachments = _attachments_by_port(device)
        neighbors = [("4", e) for e in device.get("arp_entries") or []]
        neighbors += [("6", e) for e in device.get("nd_entries") or []]
        for family, entry in neighbors:
            if not isinstance(entry, dict) or not entry.get("ip"):
                continue
            mac = normalize_mac_address(entry.get("mac"))
            if family == "4":
                row = arp.setdefault(entry["ip"], {"macs": {}, "age": entry.get("age") or ""})
                seen_by = row["macs"].setdefault(mac, [])
                if hostname not in seen_by:
                    seen_by.append(hostname)
                note_seen((entry["ip"], mac), entry)
            elif mac:
                if not ipv6_is_link_local(entry["ip"]):
                    ipv6.setdefault(mac, set()).add(entry["ip"])
                else:
                    ipv6.setdefault(mac, set())
                note_seen(mac, entry)
            port = entry.get("port") or ""
            if port and mac:
                attached = attachments.get(_normalize_interface_name(port), {})
                arp_ports.setdefault(mac, []).append(
                    {
                        "hostname": hostname,
                        "interface": attached.get("interface") or port,
                        "description": attached.get("description") or "",
                        "neighbor": attached.get("neighbor") or "",
                        "role": attached.get("role") or "unknown",
                    }
                )

    def located(mac):
        """``(location, primary, others, flags)`` for one MAC."""
        if edge.get(mac):
            primary, others, flags = _resolve_edge(edge[mac])
            return "edge", primary, others, flags
        by_arp = [p for p in arp_ports.get(mac) or [] if p["role"] in _ENDPOINT_ROLES]
        if by_arp:
            primary, others, flags = _resolve_edge(by_arp)
            return "edge", primary, others, ["located_by_arp"] + flags
        for place in arp_ports.get(mac) or []:
            note_uplink(mac, place["hostname"])
        if mac in uplink:
            return "uplink_only", {}, [], []
        return "not_in_mac_table", {}, [], []

    def pick_mac(macs):
        """The MAC most SVI owners agree on; ties go to a located MAC, then the lowest."""
        return sorted(
            macs,
            key=lambda m: (-len(macs[m]), 0 if edge.get(m) else 1, m),
        )[0]

    rows = []
    ips_by_mac = {}
    for ip_addr in sorted(arp, key=_ip_sort_key):
        macs = arp[ip_addr]["macs"]
        mac = pick_mac(macs)
        ips_by_mac.setdefault(mac, []).append(ip_addr)
        where, place, others, flags = located(mac)
        flags = list(flags)
        if ip_addr in infra_ips:
            flags.insert(0, "gateway")
        if mac.startswith(_FHRP_MAC_PREFIXES):
            flags.insert(0, "fhrp_mac")
        other_macs = sorted(m for m in macs if m != mac)
        if other_macs:
            flags.append("ip_conflict")
        row_seen = max(seen.get((ip_addr, mac), 0), seen.get(mac, 0)) or None
        if passes > 1 and row_seen is not None and row_seen < passes:
            flags.append("intermittent")
        arp_from = []
        for seen_by in macs.values():
            arp_from.extend(host for host in seen_by if host not in arp_from)
        rows.append(
            {
                "ip": ip_addr,
                "mac": mac,
                "location": where,
                "hostname": place.get("hostname", ""),
                "interface": place.get("interface", ""),
                "port_label": place.get("neighbor") or place.get("description") or "",
                "role": place.get("role", ""),
                "also_on": ["%s %s" % (o["hostname"], o["interface"]) for o in others],
                "arp_from": arp_from,
                "uplink_seen_on": uplink.get(mac, []) if where != "edge" else [],
                "other_macs": other_macs,
                "ipv6": sorted(ipv6.get(mac, ())),
                "seen": row_seen,
                "flags": flags,
            }
        )
    for row in rows:
        if len(ips_by_mac.get(row["mac"], [])) > 1 and not set(row["flags"]) & _INFRA_FLAGS:
            row["flags"].append("shared_mac")

    arp_macs = set()
    for item in arp.values():
        arp_macs.update(item["macs"])
    mac_only = []
    for mac in sorted(edge):
        if mac in arp_macs:
            continue
        place, others, flags = _resolve_edge(edge[mac])
        mac_only.append(
            {
                "ip": "",
                "mac": mac,
                "location": "edge",
                "hostname": place["hostname"],
                "interface": place["interface"],
                "port_label": place.get("neighbor") or place.get("description") or "",
                "role": place["role"],
                "also_on": ["%s %s" % (o["hostname"], o["interface"]) for o in others],
                "arp_from": [],
                "uplink_seen_on": [],
                "other_macs": [],
                "ipv6": sorted(ipv6.get(mac, ())),
                "seen": seen.get(mac),
                "flags": (["fhrp_mac"] if mac.startswith(_FHRP_MAC_PREFIXES) else []) + flags
                + (["intermittent"] if passes > 1 and seen.get(mac) and seen[mac] < passes else []),
            }
        )
    # IPv6-only hosts: no ARP entry and no MAC on an edge port.
    for mac in sorted(ipv6):
        if mac in arp_macs or mac in edge or not ipv6[mac]:
            continue
        where, place, others, flags = located(mac)
        if where == "uplink_only":
            continue  # listed with the trunk-only MACs below
        mac_only.append(
            {
                "ip": "",
                "mac": mac,
                "location": where,
                "hostname": place.get("hostname", ""),
                "interface": place.get("interface", ""),
                "port_label": place.get("neighbor") or place.get("description") or "",
                "role": place.get("role", ""),
                "also_on": ["%s %s" % (o["hostname"], o["interface"]) for o in others],
                "arp_from": [],
                "uplink_seen_on": [],
                "other_macs": [],
                "ipv6": sorted(ipv6[mac]),
                "seen": seen.get(mac),
                "flags": list(flags) + (["intermittent"] if passes > 1 and seen.get(mac) and seen[mac] < passes else []),
            }
        )
    listed = {row["mac"] for row in mac_only}
    # Seen only on trunks and never in ARP: behind a switch outside discovery
    # scope. Listed so they are not lost; their edge switch must be found.
    for mac in sorted(uplink):
        if mac in arp_macs or mac in edge or mac in listed:
            continue
        mac_only.append(
            {
                "ip": "",
                "mac": mac,
                "location": "uplink_only",
                "hostname": "",
                "interface": "",
                "port_label": "",
                "role": "",
                "also_on": [],
                "arp_from": [],
                "uplink_seen_on": list(uplink[mac]),
                "other_macs": [],
                "ipv6": sorted(ipv6.get(mac, ())),
                "seen": seen.get(mac),
                "flags": ["fhrp_mac"] if mac.startswith(_FHRP_MAC_PREFIXES) else [],
            }
        )

    hosts = [row for row in rows if not set(row["flags"]) & _INFRA_FLAGS]

    def count(items, flag):
        return len([row for row in items if flag in row["flags"]])

    return {
        "endpoints": rows,
        "mac_only": mac_only,
        "summary": {
            "rows": len(rows),
            "active_ips": len(hosts),
            "gateway_or_fhrp": len(rows) - len(hosts),
            "mac_only": len([row for row in mac_only if row["location"] == "edge"]),
            "mac_only_uplink": len([row for row in mac_only if row["location"] == "uplink_only"]),
            "uplink_only": len([row for row in hosts if row["location"] == "uplink_only"]),
            "not_in_mac_table": len([row for row in hosts if row["location"] == "not_in_mac_table"]),
            "located_by_arp": count(hosts, "located_by_arp"),
            "ip_conflicts": count(rows, "ip_conflict"),
            "multiple_edge_ports": count(hosts + mac_only, "multiple_edge_ports"),
            "dual_homed": count(hosts + mac_only, "dual_homed"),
            "mac_moved": count(hosts + mac_only, "mac_moved"),
            "intermittent": count(hosts + mac_only, "intermittent"),
            "ipv6_hosts": len([mac for mac, addrs in ipv6.items() if addrs]),
            "passes": passes,
            "arp_available": any(device.get("svi_present") for device in devices or []),
        },
    }


def build_gateway_mac_change(report):
    """The MAC hosts use for the gateway today, and the one the fabric will use.

    Hosts cache the gateway's MAC in ARP. Today it is the HSRP/VRRP virtual
    MAC of the legacy SVIs; after the move it is the fabric's
    ``ip virtual-router mac-address``. Returns ``{applies, legacy: [...],
    legacy_macs, fabric_mac, fabric_macs: {mac: [hosts]}, fabric_missing,
    changes}``. Only an L3 migration moves the gateway.
    """
    gateway = report.get("gateway_assessment") or {}
    placement = (report.get("deployment_model") or {}).get("placement") or {}
    applies = report.get("migration_type") == "l3" and gateway.get("location") == "legacy"
    legacy = []
    for svi in gateway.get("svis") or []:
        for group in svi.get("fhrp_groups") or []:
            if group.get("family") != "ipv4" or not group.get("ip"):
                continue
            legacy.append(
                {
                    "hostname": svi.get("hostname") or "",
                    "protocol": group.get("protocol") or "",
                    "group": group.get("group"),
                    "vip": group.get("ip"),
                    "virtual_mac": group.get("virtual_mac") or "",
                    "mac_source": group.get("mac_source") or "unknown",
                }
            )
    legacy_macs = sorted({item["virtual_mac"] for item in legacy if item["virtual_mac"]})
    fabric_hosts = [
        item.get("hostname") if isinstance(item, dict) else item
        for item in (placement.get("gateway_leafs") or placement.get("participating_leafs") or [])
    ]
    by_host = {d.get("hostname"): d for d in report.get("devices") or []}
    fabric_macs = {}
    missing = []
    for host in fabric_hosts:
        mac = (by_host.get(host) or {}).get("virtual_router_mac") or ""
        if mac:
            fabric_macs.setdefault(mac, []).append(host)
        else:
            missing.append(host)
    fabric_mac = sorted(fabric_macs)[0] if len(fabric_macs) == 1 else ""
    return {
        "applies": bool(applies),
        "legacy": legacy,
        "legacy_macs": legacy_macs,
        "fabric_hosts": fabric_hosts,
        "fabric_mac": fabric_mac,
        "fabric_macs": fabric_macs,
        "fabric_missing": missing,
        "changes": bool(applies and fabric_mac and legacy_macs and legacy_macs != [fabric_mac]),
    }


def summarize_endpoint_collection(devices):
    """How the endpoint tables were read, across the switches of one VLAN.

    ``{modes: {mode: [hosts]}, passes, refresh: {sent, answered,
    skipped_subnets}, notes: [text]}``. The report states it so a reader knows
    how complete the endpoint list can be.
    """
    modes = {}
    passes = 1
    refresh = {"sent": 0, "answered": 0, "skipped_subnets": []}
    notes = []
    for device in devices or []:
        info = device.get("endpoint_collection") or {}
        if not info:
            continue
        hostname = device.get("hostname") or ""
        modes.setdefault(info.get("mode") or "per_vlan", []).append(hostname)
        passes = max(passes, int(info.get("passes") or 1))
        sweep = info.get("arp_refresh") or {}
        refresh["sent"] += int(sweep.get("sent") or 0)
        refresh["answered"] += int(sweep.get("answered") or 0)
        for item in sweep.get("skipped_subnets") or []:
            text = "%s: %s" % (hostname, item)
            if text not in refresh["skipped_subnets"]:
                refresh["skipped_subnets"].append(text)
        if info.get("note"):
            notes.append("%s: %s" % (hostname, info["note"]))
    return {"modes": modes, "passes": passes, "refresh": refresh, "notes": notes}


def summarize_spanning_tree(devices):
    """Resolve the STP root bridge to a discovered switch, or flag it as outside."""
    by_bridge = {}
    roots = []
    for device in devices or []:
        stp = device.get("stp") or {}
        if stp.get("bridge_address"):
            by_bridge[stp["bridge_address"]] = device.get("hostname")
        if stp.get("present") and stp.get("root_address"):
            roots.append((device.get("hostname"), stp))
    if not roots:
        return {"present": False}
    addresses = []
    for _host, stp in roots:
        if stp["root_address"] not in addresses:
            addresses.append(stp["root_address"])
    root_address = addresses[0]
    return {
        "present": True,
        "root_address": root_address,
        "root_priority": roots[0][1].get("root_priority"),
        "root_hostname": by_bridge.get(root_address, ""),
        "inconsistent_roots": addresses[1:],
        "blocked": [
            {"hostname": host, "ports": stp["blocked_ports"]}
            for host, stp in roots
            if stp.get("blocked_ports")
        ],
    }


def _vni_findings(vlan, devices):
    vlan_id = int(vlan.get("id"))
    wanted = vlan.get("vni")
    already = []
    conflicts = []
    for device in devices or []:
        for item in device.get("vxlan_mappings") or []:
            if item.get("vlan_id") == vlan_id:
                already.append({"hostname": device.get("hostname"), "vni": item.get("vni")})
            elif wanted not in (None, "") and str(item.get("vni")) == str(wanted):
                conflicts.append(
                    {"hostname": device.get("hostname"), "vlan_id": item.get("vlan_id"), "vni": wanted}
                )
    return already, conflicts


def gateway_reachable_from_fabric(gateway, interconnects):
    """Empty when endpoints keep their gateway after leaving the MLS, else why not.

    The gateway is reachable when an active SVI is on a fabric switch, a
    firewall/router faces a fabric switch, or a fabric-legacy interconnect
    still carries the VLAN to the MLS.
    """
    gateway = gateway or {}
    if interconnects:
        return ""
    if any(item.get("tier") == "fabric" for item in gateway.get("svis") or []):
        return ""
    if any(item.get("tier") == "fabric" for item in gateway.get("external_exits") or []):
        return ""
    where = "the legacy MLS" if gateway.get("svis") else "a firewall/router on the legacy side"
    return (
        "the gateway is still only on %s and no fabric-legacy interconnect carries the VLAN; "
        "the endpoints would lose their gateway" % where
    )


PRUNE_PHASES = {"mls_link": 1, "non_vtep_link": 2, "teardown": 3}
PRUNE_PHASE_LABELS = {
    1: "MLS links to non-VTEP switches (cuts the EVPN-legacy loop)",
    2: "non-VTEP side of those links (secondary cleanup)",
    3: "teardown after the gateway moved (trunks, SVI, VLAN)",
}


def active_svi(device):
    """True when the switch still answers as the gateway for the VLAN."""
    details = (device or {}).get("svi_details") or {}
    return bool(
        (device or {}).get("svi_present")
        and details.get("ip_addresses")
        and not details.get("shutdown")
    )


def gate_teardown_plans(prune_plans, devices, gateway_validated=False):
    """Block a full prune on a source gateway until the new gateway is validated.

    Removing the SVI before the gateway moves to the fabric drops the
    gateway for every endpoint. A plan whose SVI is still up waits for the
    gateway move (``blocked_until_gateway_moved``). Once the SVI is shut, a
    source gateway still waits until the new gateway is validated
    (``blocked_until_gateway_validated``): *gateway_validated* is true after a
    migrate_vlan.yml run verified the change with no endpoint problems, or
    when the engineer says so (``-e new_gateway_validated=true``). The CLI
    stays under ``blocked_actions`` meanwhile.
    """
    by_host = {d.get("hostname"): d for d in devices or []}
    for plan in prune_plans:
        plan.setdefault("phase", PRUNE_PHASES["teardown"])
        if plan.get("placement_role") != "prune_eligible" or not plan.get("actions"):
            continue
        device = by_host.get(plan.get("hostname"))
        if active_svi(device):
            status = "blocked_until_gateway_moved"
            reason = (
                "Vlan SVI on %s is still the active gateway; move the gateway to the fabric first"
                % plan.get("hostname")
            )
        elif (plan.get("source_gateway") or (device or {}).get("svi_present")) and not gateway_validated:
            status = "blocked_until_gateway_validated"
            reason = (
                "%s is a source gateway: its prune waits until the new gateway is validated (a "
                "migrate_vlan.yml run with verification passed and no endpoint problems, or "
                "-e new_gateway_validated=true after the MOP's validation step)" % plan.get("hostname")
            )
        else:
            continue
        plan["blocked_actions"] = list(plan.get("actions") or [])
        plan["actions"] = []
        plan["status"] = status
        plan["blocked_reason"] = reason
    return prune_plans


def _vlan_in(value, vlan_id):
    """``gateway_validated``: true/false for every VLAN, or a list of VLAN IDs."""
    if isinstance(value, (list, tuple, set)):
        return str(vlan_id) in {str(item) for item in value}
    return str(value).strip().lower() in ("true", "yes", "1")


def build_link_prune_plans(vlan_id, devices, link_prunes, roles_by_host, extra_block=""):
    """One plan per switch for the MLS links a non-VTEP switch must drop.

    Phase 1 is the MLS side: pruning the MLS trunk isolates the legacy L2
    and removes the loop while the interconnect keeps the MLS gateway in
    reach. Phase 2 is the non-VTEP side, a secondary cleanup. A plan has
    actions only when ready (the VLAN is VNI-mapped on the VTEPs the non-VTEP
    switch uplinks to, and the gateway is reachable from the fabric);
    otherwise the CLI is kept under ``blocked_actions``.
    """
    os_by_host = {d.get("hostname"): d.get("os_family") or "eos" for d in devices or []}
    plans = {}
    for item in link_prunes or []:
        host = item.get("hostname")
        if not host or not item.get("interface"):
            continue
        os_family = os_by_host.get(host, "eos")
        action = {
            "op": "trunk_remove_vlans",
            "scope": "link",
            "interface": item["interface"],
            "vlans": [int(vlan_id)],
            "peer": item.get("peer") or "",
            "peer_interface": item.get("peer_interface") or "",
            "reason": item.get("reason") or "",
            "cli": _trunk_remove_cli(item["interface"], [int(vlan_id)], os_family),
            "human_required": True,
        }
        plan = plans.setdefault(
            host,
            {
                "hostname": host,
                "os_family": os_family,
                "vlan_ids": [int(vlan_id)],
                "placement_role": "link_prune",
                "phase": PRUNE_PHASES["mls_link"] if item.get("side") == "mls" else PRUNE_PHASES["non_vtep_link"],
                "status": "ready",
                "actions": [],
                "blocked_actions": [],
                "blocked_reason": "",
                "destructive": False,
                "human_required": True,
                "apply_automated": False,
                "order": [
                    "Confirm the VLAN is VNI-mapped and forwarding on the VTEP uplinks",
                    "Remove the VLAN from the MLS-facing trunk on both ends",
                    "Check the endpoints from the baseline CSV",
                ],
            },
        )
        if item.get("ready") and not extra_block:
            plan["actions"].append(action)
        else:
            plan["blocked_actions"].append(action)
            plan["status"] = (
                "blocked_until_vni_live" if not item.get("ready") else "blocked_until_gateway_in_fabric"
            )
            plan["blocked_reason"] = "; ".join(
                reason for reason in (item.get("blocked_reason") or "", extra_block) if reason
            )
    result = []
    for plan in plans.values():
        if plan["blocked_actions"]:
            # All or nothing per switch: never prune half of a dual attachment.
            plan["blocked_actions"] = plan["blocked_actions"] + plan["actions"]
            plan["actions"] = []
        plan["execution"] = build_prune_execution(
            plan["os_family"], plan["vlan_ids"], plan["actions"] or plan["blocked_actions"]
        )
        result.append(plan)
    return result


EOS_SVI_MTU_MAX = 9214


def _split_gateway_for_fabric(model, gateway):
    """Shape the routing and gateway blocks for the fabric SVI.

    Only the primary subnet moves: secondary subnets stay out of the fabric
    and are flagged. Statics and BGP were already associated using every
    subnet. The SVI keeps the legacy MTU, capped at the EOS maximum.
    """
    notes = []
    routing = model.setdefault("routing", {})
    secondary = [
        item["prefix"] for item in (gateway or {}).get("subnets") or [] if item.get("secondary")
    ]
    if secondary:
        routing["prefixes"] = [p for p in routing.get("prefixes") or [] if p not in secondary]
        routing["secondary_prefixes"] = secondary
        notes.append(
            "Renumber the hosts in secondary subnet(s) %s, or plan to carry them: they are not "
            "migrated, and lose their gateway when the MLS SVI goes." % ", ".join(secondary)
        )
    mtu = (gateway or {}).get("mtu")
    block = model.setdefault("gateway", {})
    if mtu:
        block["svi_mtu"] = min(int(mtu), EOS_SVI_MTU_MAX)
        if int(mtu) > EOS_SVI_MTU_MAX:
            notes.append(
                "Nothing to do: legacy SVI MTU %s is above the EOS maximum, so the fabric SVI "
                "is generated with %s." % (mtu, EOS_SVI_MTU_MAX)
            )
    return notes


def fabric_readiness(report):
    """Is every fabric leaf that will carry the VLAN a ready HER VTEP?

    The fabric is Arista EOS built by AVD. BUM is head-end replication with
    an EVPN flood list, sourced from the VTEP loopback. For an L3 migration
    the gateway VRF must already have an L3 VNI on each leaf that hosts the
    SVI: every participating leaf (distributed_anycast) or the gateway leaf
    pair (centralized_anycast).
    """
    devices = {device.get("hostname"): device for device in report.get("devices") or []}
    placement = (report.get("deployment_model") or {}).get("placement") or {}
    wanted = []
    for key in ("gateway_leafs", "participating_leafs"):
        for leaf in placement.get(key) or []:
            hostname = leaf.get("hostname") if isinstance(leaf, dict) else leaf
            if hostname and hostname not in wanted:
                wanted.append(hostname)
    gateway = report.get("gateway_assessment") or {}
    vrf = gateway.get("vrf") or ""
    l3 = report.get("migration_type") == "l3" and vrf not in ("", "default")
    svi_hosts = vlan_gateway_hosts(
        {
            "service_type": report.get("migration_type"),
            "gateway_mode": gateway.get("mode"),
            "target_switches": placement.get("participating_leafs"),
            "gateway_leafs": placement.get("gateway_leafs"),
        }
    )
    checked = []
    issues = []
    missing_vrf = []
    for hostname in wanted:
        device = devices.get(hostname)
        if device is None:
            issues.append(
                "Re-run discovery with %s reachable: it is a fabric leaf for this VLAN and its VTEP "
                "is unchecked." % hostname
            )
            continue
        if device_tier(device) != "fabric":
            continue
        vxlan = device.get("vxlan_interface") or {}
        expected = device.get("expected_vtep_source") or "Loopback1"
        row = {
            "hostname": hostname,
            "present": bool(vxlan.get("present")),
            "source_interface": vxlan.get("source_interface") or "",
            "source_ip": vxlan.get("source_ip") or "",
            "replication_mode": vxlan.get("replication_mode") or "",
            "flood_list_source": vxlan.get("flood_list_source") or "",
            "l3_vni": (vxlan.get("vrf_vni") or {}).get(vrf) if vrf else None,
            "svi_host": hostname in svi_hosts,
        }
        checked.append(row)
        if not row["present"]:
            issues.append(
                "Build the VTEP on %s before the VNI goes there (Vxlan1 sourced from %s, head-end "
                "replication, EVPN flood list): it has no Vxlan interface." % (hostname, expected)
            )
            continue
        if row["replication_mode"] != "headend":
            issues.append(
                "Set head-end replication on %s: it floods BUM with %s."
                % (hostname, row["replication_mode"] or "an unknown mode")
            )
        if row["flood_list_source"].upper() != "EVPN":
            issues.append(
                "Build the head-end flood list on %s from EVPN type-3 routes: it uses %s."
                % (hostname, row["flood_list_source"] or "an unknown source")
            )
        if row["source_interface"].lower() != expected.lower():
            issues.append(
                "Change the VTEP source on %s to %s: it is %s."
                % (hostname, expected, row["source_interface"] or "unset")
            )
        if l3 and row["l3_vni"] is None and hostname in svi_hosts:
            missing_vrf.append(hostname)
    if missing_vrf:
        subject = {1: "the leaf does not have", 2: "neither leaf has"}.get(
            len(missing_vrf), "none of these leaves has"
        )
        issues.append(
            "Create VRF %s on %s first: %s that VRF set up for routing in the fabric (no L3 VNI). "
            "Add it in AVD with its L3 VNI and deploy it before the gateway moves; the VLAN "
            "configlet puts the SVI in the VRF but does not create it."
            % (vrf, ", ".join(missing_vrf), subject)
        )
    return {"checked": checked, "issues": issues, "vrf": vrf if l3 else ""}


def _prune_idle_ports(plan, device, decision, vlan_id):
    """Prune a switch whose endpoint-facing ports carry no MACs on the VLAN.

    Idle trunk ports lose the VLAN with the uplinks; every idle port is named
    in ``warnings``. Endpoint ports without MACs no longer block the plan.
    """
    plan = dict(plan)
    actions = list(plan.get("actions") or [])
    done = {
        _normalize_interface_name(action.get("interface") or "")
        for action in actions
        if action.get("op") == "trunk_remove_vlans"
    }
    trunks = {_normalize_interface_name(iface): iface for iface in device.get("trunk_ports") or []}
    os_family = plan.get("os_family") or device.get("os_family") or "eos"
    extra = []
    for iface in decision.get("idle_port_names") or []:
        trunk = trunks.get(_normalize_interface_name(iface))
        if trunk and _normalize_interface_name(trunk) not in done:
            done.add(_normalize_interface_name(trunk))
            extra.append(
                _prune_action(
                    "trunk_remove_vlans",
                    interface=trunk,
                    vlans=[int(vlan_id)],
                    cli=_trunk_remove_cli(trunk, [int(vlan_id)], os_family),
                    source="discovery",
                    confidence="medium",
                    note="idle port: no MACs on the VLAN",
                )
            )
    if extra:
        first_vlan_op = next(
            (index for index, action in enumerate(actions) if action.get("op") in ("no_interface_vlan", "no_vlan")),
            len(actions),
        )
        actions = actions[:first_vlan_op] + extra + actions[first_vlan_op:]
        plan["actions"] = actions
        plan["execution"] = build_prune_execution(os_family, [int(vlan_id)], actions)
    if actions and plan.get("status") == "blocked_by_compute_endpoints":
        plan["status"] = "candidate"
    plan["warnings"] = [
        "No MACs on VLAN %s, pruned anyway: %s." % (vlan_id, ", ".join(decision.get("idle_ports") or []))
    ]
    return plan


def _rebuild_items(report):
    """L3 objects the generated VLAN configlet does not carry; rebuilt by hand."""
    if report.get("migration_type") != "l3":
        return []
    items = []
    services = (report.get("gateway_assessment") or {}).get("services") or {}
    parts = []
    for item in services.get("dhcp_relay") or []:
        parts.append("DHCP relay %s%s" % (item.get("server"), " (vrf %s)" % item["vrf"] if item.get("vrf") else ""))
    for item in services.get("acls") or []:
        parts.append("ACL %s %s" % (item.get("name"), item.get("direction") or ""))
    for key, label in (("pbr", "PBR"), ("ipv6_addresses", "IPv6"), ("multicast", "multicast"), ("nat", "NAT"), ("urpf", "uRPF")):
        for value in services.get(key) or []:
            parts.append("%s %s" % (label, value))
    if parts:
        items.append(
            "Rebuild on the fabric SVI by hand, the VLAN configlet does not carry them: %s."
            % "; ".join(part.strip() for part in parts)
        )
    if services.get("other_lines"):
        items.append(
            "Review these legacy SVI lines, discovery did not recognise them: %s."
            % ", ".join("`%s`" % line for line in services["other_lines"])
        )
    routing = (report.get("deployment_model") or {}).get("routing") or {}
    parts = []
    for route in routing.get("static_routes") or []:
        parts.append(
            "static %s via %s%s%s"
            % (
                route.get("prefix"),
                route.get("next_hop") or route.get("interface"),
                " vrf %s" % route["vrf"] if route.get("vrf") else "",
                " (%s)" % route["name"] if route.get("name") else "",
            )
        )
    for peer in routing.get("bgp_peers") or []:
        parts.append(
            "BGP peer %s AS %s%s%s"
            % (
                peer.get("neighbor"),
                peer.get("remote_as") or "?",
                " vrf %s" % peer["vrf"] if peer.get("vrf") else "",
                " (%s)" % peer["description"] if peer.get("description") else "",
            )
        )
    if parts:
        items.append(
            "Recreate in the fabric before the gateway moves, the VLAN configlet does not carry them: %s."
            % "; ".join(parts)
        )
    return items


def resolve_l3_vni(report):
    """The L3 VNI of the gateway VRF: ``{vni, source, by_vni}``.

    The SSOT record's ``evpn.l3.vni`` wins. Otherwise it is the VNI every
    gateway leaf maps the VRF to in ``show interfaces Vxlan1`` (from
    fabric_readiness). When the leaves disagree, *vni* is None and *by_vni*
    names who has which. L2 migrations have no L3 VNI.
    """
    if report.get("migration_type") != "l3":
        return {"vni": None, "source": "not needed (L2 migration)", "by_vni": {}}
    record = (((report.get("deployment_model") or {}).get("evpn") or {}).get("l3") or {}).get("vni")
    if record not in (None, ""):
        return {"vni": int(record), "source": "SSOT record (evpn.l3.vni)", "by_vni": {}}
    readiness = report.get("fabric_readiness") or {}
    by_vni = {}
    for row in readiness.get("checked") or []:
        if row.get("svi_host") and row.get("l3_vni") is not None:
            by_vni.setdefault(int(row["l3_vni"]), []).append(row.get("hostname"))
    if len(by_vni) == 1:
        vni, hosts = next(iter(by_vni.items()))
        return {
            "vni": vni,
            "source": "fabric: VRF %s maps to it on %s (show interfaces Vxlan1)" % (readiness.get("vrf"), ", ".join(hosts)),
            "by_vni": by_vni,
        }
    if len(by_vni) > 1:
        return {"vni": None, "source": "fabric leaves disagree", "by_vni": by_vni}
    return {"vni": None, "source": "not found: no gateway leaf maps the VRF to a VNI", "by_vni": {}}


def _finding_vni(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_findings(report):
    """Everything an engineer must resolve or know before this VLAN migrates.

    Each finding is ``{kind, category, severity, text}``:

    - kind: ``blocker`` (fix before the change), ``decision`` (an engineer
      or app owner must choose or confirm), ``risk`` (proceed with the
      mitigation named), ``task`` (work the generated config does not do),
      ``sequencing`` (waits on an earlier step), ``info``.
    - category: coverage, scope, gateway, fabric, routing, l2, prune,
      placement, naming, vni, endpoints.
    - severity: high, medium, low.

    L2 VNI findings also carry ``topic`` (``proposed``, ``missing``,
    ``existing_mapping``) and ``vni``, so the migration gate can tell which
    ones a VNI from NetBox settles.

    The order is stable, so IDs assigned from it are stable between runs of
    the same network state.
    """
    found = []

    def add(kind, category, severity, text, **extra):
        found.append(dict({"kind": kind, "category": category, "severity": severity, "text": text}, **extra))

    def add_all(kind, category, severity, texts):
        for text in texts or []:
            add(kind, category, severity, text)

    def add_gateway_notes(texts):
        for text in texts or []:
            if str(text).startswith("Nothing to do"):
                add("info", "gateway", "low", text)
            else:
                add("risk", "gateway", "medium", text)

    if report.get("in_vlan_db") is False:
        data_center = (
            report.get("data_center")
            or ((report.get("deployment_model") or {}).get("site") or {}).get("data_center")
            or "<dc>"
        )
        starter_path = (report.get("vni_allocation") or {}).get("path") or "reports/%s/%s/%s" % (
            data_center,
            report.get("vlan_slug") or "<vlan>",
            vlan_db_record_filename(report.get("vlan_id") or 0, report.get("vlan_name") or "vlan"),
        )
        add("blocker", "scope", "high",
            "VLAN %s is not in the VLAN DB (vars/vlans/%s/). Copy the starter record discovery wrote (%s) "
            "there and set evpn.l2.vni: the VLAN record is authoritative, and nothing in this report replaces it."
            % (report.get("vlan_id"), data_center, starter_path),
        )
    for host in report.get("not_discovered") or []:
        add("blocker", "coverage", "high", 
            "Fix reachability to %s and re-run discovery: it was not discovered (unreachable or failed), "
            "so placement and prune lists may be incomplete." % host
        )
    for device in report.get("devices") or []:
        for error in device.get("errors") or []:
            add("blocker", "coverage", "high", 
                "%s: `%s` failed: %s. Re-run discovery or run the command by hand."
                % (device.get("hostname"), error.get("command"), error.get("error"))
            )
        for table, check in sorted((device.get("parse_checks") or {}).items()):
            for problem in (check or {}).get("problems") or []:
                add("blocker", "coverage", "high", 
                    "%s: the %s output was not fully read (%s). Endpoints may be missing: send the raw "
                    "output (in the JSON report) so the parser can be fixed."
                    % (device.get("hostname"), "MAC table" if table == "mac_table" else "ARP", problem)
                )
    if not report.get("switches_vlan_defined") and not report.get("switches_with_svi"):
        add("info", "scope", "low", 
            "VLAN %s is not configured on any discovered switch. Nothing to migrate or prune here."
            % report.get("vlan_id")
        )
        return found
    add_all("risk", "gateway", "medium", (report.get("gateway_assessment") or {}).get("warnings") or [])
    add_all("blocker", "fabric", "high", (report.get("fabric_readiness") or {}).get("issues") or [])
    l3_vni = resolve_l3_vni(report)
    if len(l3_vni["by_vni"]) > 1:
        add(
            "blocker",
            "fabric",
            "high",
            "Make the L3 VNI of VRF %s the same on every gateway leaf: %s."
            % (
                (report.get("fabric_readiness") or {}).get("vrf") or "",
                "; ".join("%s on %s" % (vni, ", ".join(hosts)) for vni, hosts in sorted(l3_vni["by_vni"].items())),
            ),
        )
    add_gateway_notes(report.get("gateway_notes") or [])
    add_all("task", "routing", "high", _rebuild_items(report))
    add_all("risk", "l2", "medium", (report.get("l2_attachment") or {}).get("issues") or [])
    for plan in report.get("link_prune_plans") or []:
        if str(plan.get("status") or "").startswith("blocked"):
            add("sequencing", "prune", "low", 
                "Link prune on %s is blocked: %s." % (plan["hostname"], plan.get("blocked_reason") or "")
            )
    for plan in report.get("prune_plans") or []:
        if plan.get("status") == "blocked_until_gateway_moved":
            add("sequencing", "prune", "low", "Teardown of %s is blocked: %s." % (plan["hostname"], plan.get("blocked_reason") or ""))
    for item in (report.get("placement_analysis") or {}).get("switches") or []:
        if item.get("role") == "prune_eligible" and item.get("idle_ports"):
            add("decision", "prune", "medium", 
                "Confirm nothing needs VLAN %s on %s before pruning: these ports face endpoints or other "
                "devices but carry no MACs on the VLAN, and the prune removes it from them: %s."
                % (report.get("vlan_id"), item.get("hostname"), ", ".join(item["idle_ports"]))
            )
        if item.get("role") == "review":
            add("decision", "placement", "medium", 
                "Review %s before migrating: %s" % (item.get("hostname"), "; ".join(item.get("nonqualifying") or []))
            )
    if report.get("needs_vlan_name"):
        add("decision", "naming", "medium", 
            "Agree the VLAN name and set site.vlan.name (or -e manual_vlan_name=): switches disagree (%s)."
            % (", ".join(report.get("name_candidates") or []) or "none found")
        )
    stp = report.get("spanning_tree") or {}
    if stp.get("present") and not stp.get("root_hostname"):
        add("blocker", "coverage", "medium", 
            "Find the switch that is STP root %s and add it to discovery: it is not a discovered switch, "
            "so the VLAN extends beyond discovery scope." % stp.get("root_address")
        )
    if stp.get("inconsistent_roots"):
        add("risk", "l2", "high", 
            "Check the L2 domain for a split: switches disagree on the STP root (%s)."
            % ", ".join([stp.get("root_address")] + stp["inconsistent_roots"])
        )
    for item in report.get("vni_conflicts") or []:
        add("blocker", "vni", "high", 
            "Pick another VNI: %s is already mapped to VLAN %s on %s." % (item["vni"], item["vlan_id"], item["hostname"])
        )
    for item in report.get("vxlan_already_mapped") or []:
        add("decision", "vni", "medium", 
            "Confirm VNI %s matches evpn.l2.vni: the VLAN is already VXLAN-mapped on %s."
            % (item["vni"], item["hostname"]),
            topic="existing_mapping", vni=_finding_vni(item.get("vni")),
        )
    summary = (report.get("endpoint_table") or {}).get("summary") or {}
    if summary.get("uplink_only"):
        add("risk", "endpoints", "medium", 
            "Extend discovery to the switches behind those trunks: %s active IP(s) are only seen on "
            "switch-to-switch trunks, so their edge switch is outside discovery scope." % summary["uplink_only"]
        )
    gmac = report.get("gateway_mac") or {}
    if gmac.get("applies"):
        for item in gmac.get("legacy") or []:
            if not item.get("virtual_mac"):
                add("decision", "gateway", "medium", 
                    "Find the MAC hosts use for gateway %s: %s group %s on %s has no well-known virtual MAC (%s). "
                    "Read it from a host's ARP table or `show %s` before the cutover."
                    % (item["vip"], item["protocol"].upper(), item["group"], item["hostname"], item["mac_source"],
                       item["protocol"])
                )
        if gmac.get("fabric_missing"):
            add("blocker", "fabric", "high", 
                "Set `ip virtual-router mac-address` on %s before the gateway moves: `ip address virtual` needs it, "
                "and discovery found none there." % ", ".join(gmac["fabric_missing"])
            )
        if len(gmac.get("fabric_macs") or {}) > 1:
            add("blocker", "fabric", "high", 
                "Make the anycast gateway MAC the same on every gateway leaf: %s."
                % "; ".join("%s on %s" % (mac, ", ".join(hosts)) for mac, hosts in sorted(gmac["fabric_macs"].items()))
            )
        if gmac.get("changes"):
            vips = ", ".join(sorted({item["vip"] for item in gmac.get("legacy") or []}))
            add("risk", "gateway", "high", 
                "The gateway MAC changes at cutover: hosts cache %s as %s; the fabric answers with %s. A host updates "
                "its entry when the fabric gateway ARPs for it (the post-cutover ping of every baseline endpoint does "
                "this) or when the entry ages out; until then it sends to the old MAC. Ping every endpoint from the "
                "fabric gateway right after the cutover, and check hosts that do not answer."
                % (vips, ", ".join(gmac.get("legacy_macs") or []), gmac["fabric_mac"])
            )
    collection = report.get("endpoint_collection") or {}
    for note in collection.get("notes") or []:
        add("risk", "coverage", "low", "Endpoint tables: %s. Check the JSON output on that switch." % note)
    for item in (collection.get("refresh") or {}).get("skipped_subnets") or []:
        add("risk", "coverage", "medium", 
            "ARP refresh skipped %s: silent hosts there may be missing. Raise "
            "discovery_arp_refresh_max_hosts or check those subnets by hand." % item
        )
    if summary.get("mac_only_uplink"):
        add("risk", "endpoints", "medium", 
            "Extend discovery to the switches behind those trunks: %s MAC(s) with no ARP entry are only seen "
            "on switch-to-switch trunks or a vPC peer-link, so their edge switch is outside discovery scope."
            % summary["mac_only_uplink"]
        )
    if summary.get("ip_conflicts"):
        conflicts = [
            "%s (%s)" % (row["ip"], ", ".join([row["mac"]] + row.get("other_macs", [])))
            for row in (report.get("endpoint_table") or {}).get("endpoints") or []
            if "ip_conflict" in row.get("flags", [])
        ]
        add("risk", "endpoints", "high", 
            "Resolve duplicate IPs before migrating: the gateways map these IPs to more than one MAC: %s."
            % "; ".join(conflicts)
        )
    if summary.get("multiple_edge_ports"):
        rows = [
            row
            for row in list((report.get("endpoint_table") or {}).get("endpoints") or [])
            + list((report.get("endpoint_table") or {}).get("mac_only") or [])
            if "multiple_edge_ports" in row.get("flags", [])
        ]
        add("risk", "endpoints", "medium", 
            "Check where these MACs really are: each is learned on more than one device-facing port "
            "(loop, a move during discovery, or a port role read wrong): %s."
            % "; ".join(
                "%s on %s" % (row["mac"], ", ".join(["%s %s" % (row["hostname"], row["interface"])] + row["also_on"]))
                for row in rows
            )
        )
    if summary.get("not_in_mac_table"):
        add("risk", "endpoints", "low", 
            "Re-run discovery or check them by hand: %s active IP(s) are in ARP but not in any MAC table "
            "(aged out, or behind a switch that was not discovered)." % summary["not_in_mac_table"]
        )
    allocation = report.get("vni_allocation") or {}
    if allocation.get("vni") and allocation.get("source") == "starter":
        for vlan_id in allocation.get("used_by_records") or []:
            add("blocker", "vni", "high",
                "VNI %s from the starter record is already the VNI of VLAN %s in the VLAN DB. Pick another "
                "VNI in the starter record." % (allocation["vni"], vlan_id),
            )
        add("decision", "vni", "medium",
            "VNI %s %s. Confirm it in evpn.l2.vni of the VLAN record." % (allocation["vni"], allocation.get("reason")),
            topic="proposed", vni=_finding_vni(allocation.get("vni")),
        )
    elif allocation.get("vni"):
        add("decision", "vni", "medium", 
            "VNI %s proposed (%s). Confirm it in evpn.l2.vni of the SSOT record."
            % (allocation["vni"], allocation.get("reason")),
            topic="proposed", vni=_finding_vni(allocation.get("vni")),
        )
    elif report.get("vni") in (None, "", 0) and report.get("vlan_id") is not None:
        add("blocker", "vni", "high", 
            "No VNI: %s. Set evpn.l2.vni in the SSOT record."
            % (allocation.get("reason") or "assign one"),
            topic="missing",
        )
    add_all("blocker", "vni", "high", report.get("vni_range_problems") or [])
    return found



def build_action_items(report):
    """The finding texts, in order (the report's Action items list)."""
    return [item["text"] for item in build_findings(report)]

def build_vlan_discovery_reports(
    vlans, play_hosts, hostvars, all_hosts=None, vni_allocation=None, placement_options=None
):
    """Build one aggregated discovery report dict per VLAN in *vlans*.

    *all_hosts* (``ansible_play_hosts_all``) lets the report name switches
    that dropped out of the play (unreachable or failed). Hosts that were
    skipped on purpose set ``_discovery_skipped`` and are not listed.

    *vni_allocation* (``{ranges, records, default_pool, starter_vnis}``)
    proposes a VNI for a VLAN without one: the next free VNI in its DC and
    pool. A VNI the record holds is never replaced. ``starter_vnis``
    (``{vlan id: {vni, path}}``, see ``load_starter_vnis``) keeps the VNI of
    the starter record an earlier run wrote for a VLAN that is not in the
    VLAN DB. ``records`` is the VLAN DB: a VLAN missing from it is a blocker.

    *placement_options* (``{keep_idle_endpoint_ports, gateway_validated}``):
    ``keep_idle_endpoint_ports`` true keeps a switch whose endpoint-facing
    ports carry no MACs on the VLAN; the default prunes it with a warning
    naming those ports. ``gateway_validated`` (bool, or the VLAN IDs whose
    new gateway is validated, see ``gateway_validated_vlans``) releases the
    source gateways' prune.
    """
    vni_allocation = vni_allocation if isinstance(vni_allocation, dict) else {}
    placement_options = placement_options if isinstance(placement_options, dict) else {}
    keep_idle = str(placement_options.get("keep_idle_endpoint_ports") or "").lower() in ("true", "yes", "1")
    vni_ranges = vni_allocation.get("ranges") or {}
    range_problems = vni_range_problems(vni_ranges)
    taken_vnis = used_vnis(list(vni_allocation.get("records") or []) + list(vlans))
    db_ids = {
        str(item.get("id"))
        for item in vni_allocation.get("records") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    starter_vnis = vni_allocation.get("starter_vnis") if isinstance(vni_allocation.get("starter_vnis"), dict) else {}
    starter_vnis = {str(key): value for key, value in starter_vnis.items() if isinstance(value, dict)}
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects a list, got %s" % type(vlans)
        )
    if not isinstance(play_hosts, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects play_hosts list, got %s"
            % type(play_hosts)
        )

    not_discovered = []
    for host in all_hosts or []:
        if host in play_hosts or host == "localhost":
            continue
        host_data = hostvars.get(host, {}) or {}
        if host_data.get("_discovery_skipped"):
            continue
        if host_data.get("_vlan_discovery"):
            continue
        not_discovered.append(host)

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
                    "service_ports": per_vlan.get("service_ports", []),
                    "downstream_switch_ports": per_vlan.get("downstream_switch_ports", []),
                    "mlag_peer_ports": per_vlan.get("mlag_peer_ports", []),
                    "vxlan_mapped": per_vlan.get("vxlan_mapped", False),
                    "stp": per_vlan.get("stp", {}),
                    "bgp_prefix_detail": per_vlan.get("bgp_prefix_detail", []),
                    "border_bgp": [
                        item
                        for item in discovery.get("border_bgp") or []
                        if isinstance(item, dict) and _str_equal(item.get("vlan_id"), vlan_id)
                    ],
                    "router_id": discovery.get("router_id") or discovery.get("loopback0") or "",
                    "bgp_as": discovery.get("bgp_as") or "",
                    "errors": list(discovery.get("errors") or [])
                    + list(per_vlan.get("errors") or []),
                    "migration_tier": discovery.get("migration_tier", ""),
                    "neighbor_count": len(discovery.get("neighbors") or []),
                    "prefix_lists": discovery.get("prefix_lists", {}),
                    "route_maps": discovery.get("route_maps", {}),
                    "vxlan_mappings": discovery.get("vxlan_mappings", []),
                    "vxlan_interface": discovery.get("vxlan_interface", {}),
                    "expected_vtep_source": discovery.get("expected_vtep_source", ""),
                    "acl_configs": per_vlan.get("acl_configs", {}),
                    "port_attachments": per_vlan.get("port_attachments", []),
                    "trunk_allowed": per_vlan.get("trunk_allowed", []),
                    "all_ports": per_vlan.get("all_ports", []),
                    "ports_raw": per_vlan.get("ports_raw", ""),
                    "mac_learned": per_vlan.get("mac_learned", False),
                    "arp_learned": per_vlan.get("arp_learned", False),
                    "mac_entries": per_vlan.get("mac_entries", []),
                    "endpoint_mac_entries": per_vlan.get("endpoint_mac_entries", []),
                    "peer_link_mac_entries": per_vlan.get("peer_link_mac_entries", []),
                    "overlay_mac_entries": per_vlan.get("overlay_mac_entries", []),
                    "arp_entries": per_vlan.get("arp_entries", []),
                    "nd_entries": per_vlan.get("nd_entries", []),
                    "endpoint_collection": per_vlan.get("endpoint_collection", {}),
                    "parse_checks": per_vlan.get("parse_checks", {}),
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
                    "virtual_router_mac": discovery.get("virtual_router_mac", ""),
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
        placement = build_vlan_placement(vlan, devices, keep_idle)
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
                        # Rebuild the CLI for this VLAN alone: the device plan
                        # covers every VLAN of the run, and its line may name
                        # another VLAN on the same trunk.
                        vlan_actions.append(
                            dict(
                                action,
                                vlans=[int(vlan_id)],
                                cli=_trunk_remove_cli(
                                    action.get("interface"),
                                    [int(vlan_id)],
                                    str(device.get("os_family") or device_plan.get("os_family") or "eos").lower(),
                                ),
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
                if decision.get("idle_port_names"):
                    device_plan = _prune_idle_ports(device_plan, device, decision, vlan_id)
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
        # Link prunes cut the EVPN-legacy bridge at cutover, before the MLS goes.
        link_prune_plans = build_link_prune_plans(
            vlan_id,
            devices,
            placement["l2"]["link_prunes"],
            {host: item.get("role") for host, item in by_placement.items()},
            gateway_reachable_from_fabric(placement["gateway"], placement["l2"]["interconnects"]),
        )
        gate_teardown_plans(
            prune_plans, devices, _vlan_in(placement_options.get("gateway_validated", False), vlan_id)
        )
        prune_plans = sorted(
            link_prune_plans + prune_plans, key=lambda plan: plan.get("phase") or 9
        )
        for plan in prune_plans:
            plan["phase_label"] = PRUNE_PHASE_LABELS.get(plan.get("phase"), "")

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
        # Full per-switch routing detail goes to the reports; the model keeps
        # the condensed summary (discovery_model_routing_detail adds it back).
        routing_detail = attach_associated_routes(placement["deployment_model"], vlan_id, devices)
        routing.pop("associated", None)
        routing.update(condense_associated_routes(routing_detail, devices))
        gateway_notes = _split_gateway_for_fabric(placement["deployment_model"], placement["gateway"])
        (
            endpoints,
            access_inventory,
            trunk_inventory,
            compute_inventory,
            uplink_inventory,
            unknown_inventory,
        ) = _build_endpoint_inventory(devices)
        l3_discovery = build_l3_discovery(vlan_id, devices)
        gateway = placement["gateway"]
        endpoint_table = build_endpoint_table(devices, gateway)
        # A VNI the record holds is never replaced. A VLAN that is not in the
        # VLAN DB keeps the VNI of the starter record an earlier discovery
        # wrote under reports/ (the engineer may have set it there); only a
        # VLAN with neither gets a proposal.
        # None when the caller did not pass the VLAN DB (no way to tell).
        in_vlan_db = (str(vlan_id) in db_ids) if "records" in vni_allocation else None
        starter = starter_vnis.get(str(vlan_id)) or {}
        vni_source = "record" if vlan.get("vni") not in (None, "", 0) else ""
        proposed = {}
        if not vni_source and starter.get("vni"):
            vlan = dict(vlan, vni=int(starter["vni"]))
            vni_source = "starter"
            proposed = {
                "vni": int(starter["vni"]),
                "source": "starter",
                "path": starter.get("path") or "",
                "reason": "kept from the starter record %s" % (starter.get("path") or "under reports/"),
                "used_by_records": sorted(
                    int(item.get("id"))
                    for item in vni_allocation.get("records") or []
                    if isinstance(item, dict)
                    and str(item.get("id")) != str(vlan_id)
                    and str(item.get("vni") or "") == str(starter["vni"])
                ),
            }
        already_mapped, vni_conflicts = _vni_findings(vlan, devices)
        taken_vnis |= used_vnis(None, devices)
        if vlan.get("vni") in (None, "", 0) and vni_allocation:
            proposed = allocate_vni(
                vlan.get("data_center") or "",
                vlan.get("customer_type") or vni_allocation.get("default_pool") or "external",
                vni_ranges,
                taken_vnis,
            )
            if proposed.get("vni"):
                taken_vnis.add(proposed["vni"])
                proposed["source"] = "proposed"
                vni_source = "proposed"
        for device in devices:
            device["migration_tier"] = device_tier(device)

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
            "routing_detail": routing_detail,
            "data_center": vlan.get("data_center", ""),
            "devices": devices,
            "migration_type": placement["migration_type"],
            "migration_reason": placement["migration_reason"],
            "deployment_model": placement["deployment_model"],
            "placement_analysis": placement["analysis"],
            "gateway_assessment": gateway,
            "l2_attachment": placement["l2"],
            "link_prune_plans": link_prune_plans,
            "endpoint_table": endpoint_table,
            "endpoint_collection": summarize_endpoint_collection(devices),
            "spanning_tree": summarize_spanning_tree(devices),
            "vxlan_already_mapped": already_mapped,
            "vni_conflicts": vni_conflicts,
            "vni_allocation": proposed,
            "gateway_notes": gateway_notes,
            "vni_range_problems": range_problems,
            "not_discovered": list(not_discovered),
            "switches_scanned": [device["hostname"] for device in devices],
        }
        l2_evpn = report["deployment_model"].setdefault("evpn", {}).setdefault("l2", {})
        if proposed.get("vni"):
            l2_evpn["vni"] = proposed["vni"]
            if proposed.get("source") == "proposed":
                l2_evpn["vni_proposed"] = "%s pool %s" % (proposed.get("pool"), proposed.get("range"))
        if vni_source:
            l2_evpn["vni_source"] = vni_source
        report["vni_source"] = vni_source
        report["in_vlan_db"] = in_vlan_db
        report["fabric_readiness"] = fabric_readiness(report)
        report["gateway_mac"] = build_gateway_mac_change(report)
        report["action_items"] = build_action_items(report)
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
        "link_prune_plans": report.get("link_prune_plans"),
        "l2_attachment": report.get("l2_attachment"),
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
        all_gateways = list(details.get("gateway_addresses") or (vips + hsrp))
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
        for vip in all_gateways:
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


