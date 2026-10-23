# -*- coding: utf-8 -*-
"""Aggregated VLAN discovery report builders."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import _str_equal, sanitize_report_slug
from vlan_lib.parsers import network_prefix, normalize_mac_address
from vlan_lib.placement import (
    PRUNE_STATUS_BY_ROLE,
    attach_associated_routes,
    build_vlan_placement,
    device_tier,
)
from vlan_lib.vni import allocate_vni, used_vnis, vni_range_problems
from vlan_lib.prune import _trunk_remove_cli, build_device_prune_plan, build_prune_execution

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
# HSRP v1/v2 and VRRP virtual MACs. These are gateways, not hosts.
_FHRP_MAC_PREFIXES = ("00:00:0c:07:ac", "00:00:0c:9f:f", "00:00:5e:00:01", "00:00:5e:00:02")


def _ip_sort_key(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return (999,)


def _host_part(cidr):
    return str(cidr or "").split("/")[0]


def build_endpoint_table(devices, gateway=None):
    """One row per active IP endpoint, located at its edge port.

    ARP (from the SVI owners) gives IP to MAC. The MAC table gives the port.
    A MAC learned on an endpoint-facing port is located there; a MAC seen only
    on switch-to-switch trunks is ``uplink_only`` (it sits behind a switch
    outside discovery scope, or in the legacy L2 beyond it). Gateway and FHRP
    addresses are flagged so they are not mistaken for hosts.
    """
    gateway = gateway or {}
    infra_ips = set()
    for entry in list(gateway.get("svis") or []) + list(gateway.get("inactive_svis") or []):
        for cidr in entry.get("ip_addresses") or []:
            infra_ips.add(_host_part(cidr))
        infra_ips.update(entry.get("gateway_addresses") or [])

    edge = {}
    uplink = {}
    for device in devices or []:
        hostname = device.get("hostname") or ""
        for entry in device.get("mac_entries") or []:
            if not isinstance(entry, dict):
                continue
            mac = normalize_mac_address(entry.get("mac"))
            if not mac:
                continue
            role = entry.get("attachment_role") or entry.get("port_role") or "unknown"
            if role in _ENDPOINT_ROLES:
                edge.setdefault(mac, []).append(
                    {
                        "hostname": hostname,
                        "interface": entry.get("interface") or "",
                        "description": entry.get("description") or "",
                        "neighbor": entry.get("neighbor") or "",
                        "role": role,
                    }
                )
            else:
                uplink.setdefault(mac, [])
                if hostname not in uplink[mac]:
                    uplink[mac].append(hostname)

    arp = {}
    for device in devices or []:
        for entry in device.get("arp_entries") or []:
            if not isinstance(entry, dict) or not entry.get("ip"):
                continue
            row = arp.setdefault(
                entry["ip"],
                {"mac": normalize_mac_address(entry.get("mac")), "seen_by": [], "age": entry.get("age") or ""},
            )
            if device.get("hostname") not in row["seen_by"]:
                row["seen_by"].append(device.get("hostname"))

    def located(mac):
        places = edge.get(mac) or []
        if places:
            return "edge", places[0], places[1:]
        if mac in uplink:
            return "uplink_only", {}, []
        return "not_in_mac_table", {}, []

    rows = []
    for ip_addr in sorted(arp, key=_ip_sort_key):
        mac = arp[ip_addr]["mac"]
        where, place, others = located(mac)
        flags = []
        if ip_addr in infra_ips:
            flags.append("gateway")
        if mac.startswith(_FHRP_MAC_PREFIXES):
            flags.append("fhrp_mac")
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
                "arp_from": arp[ip_addr]["seen_by"],
                "uplink_seen_on": uplink.get(mac, []) if where != "edge" else [],
                "flags": flags,
            }
        )
    arp_macs = {row["mac"] for row in rows}
    mac_only = []
    for mac in sorted(edge):
        if mac in arp_macs:
            continue
        place = edge[mac][0]
        mac_only.append(
            {
                "ip": "",
                "mac": mac,
                "location": "edge",
                "hostname": place["hostname"],
                "interface": place["interface"],
                "port_label": place.get("neighbor") or place.get("description") or "",
                "role": place["role"],
                "also_on": ["%s %s" % (o["hostname"], o["interface"]) for o in edge[mac][1:]],
                "flags": ["fhrp_mac"] if mac.startswith(_FHRP_MAC_PREFIXES) else [],
            }
        )
    hosts = [row for row in rows if not row["flags"]]
    return {
        "endpoints": rows,
        "mac_only": mac_only,
        "summary": {
            "active_ips": len(hosts),
            "gateway_or_fhrp": len(rows) - len(hosts),
            "mac_only": len(mac_only),
            "uplink_only": len([row for row in hosts if row["location"] == "uplink_only"]),
            "not_in_mac_table": len([row for row in hosts if row["location"] == "not_in_mac_table"]),
            "arp_available": any(device.get("svi_present") for device in devices or []),
        },
    }


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


def gate_teardown_plans(prune_plans, devices):
    """Block a full prune on a switch whose SVI is still the active gateway.

    Removing the SVI before the gateway moves to the fabric drops the
    gateway for every endpoint. The plan keeps its CLI under
    ``blocked_actions`` until a re-run finds the SVI shut or gone.
    """
    by_host = {d.get("hostname"): d for d in devices or []}
    for plan in prune_plans:
        plan.setdefault("phase", PRUNE_PHASES["teardown"])
        if plan.get("placement_role") != "prune_eligible":
            continue
        if not active_svi(by_host.get(plan.get("hostname"))) or not plan.get("actions"):
            continue
        plan["blocked_actions"] = list(plan.get("actions") or [])
        plan["actions"] = []
        plan["status"] = "blocked_until_gateway_moved"
        plan["blocked_reason"] = (
            "Vlan SVI on %s is still the active gateway; move the gateway to the fabric first"
            % plan.get("hostname")
        )
    return prune_plans


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
            "Secondary subnet(s) %s on the legacy SVI are not migrated. Hosts there lose their "
            "gateway when the MLS SVI goes; renumber them or decide to carry them." % ", ".join(secondary)
        )
    mtu = (gateway or {}).get("mtu")
    block = model.setdefault("gateway", {})
    if mtu:
        block["svi_mtu"] = min(int(mtu), EOS_SVI_MTU_MAX)
        if int(mtu) > EOS_SVI_MTU_MAX:
            notes.append(
                "Legacy SVI MTU %s is above the EOS maximum; the fabric SVI uses %s."
                % (mtu, EOS_SVI_MTU_MAX)
            )
    return notes


def fabric_readiness(report):
    """Is every fabric leaf that will carry the VLAN a ready HER VTEP?

    The fabric is Arista EOS built by AVD. BUM is head-end replication with
    an EVPN flood list, sourced from the VTEP loopback. For an L3 migration
    the gateway VRF must already have an L3 VNI on the fabric.
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
    checked = []
    issues = []
    missing_vrf = []
    for hostname in wanted:
        device = devices.get(hostname)
        if device is None:
            issues.append("%s is a fabric leaf for this VLAN but was not discovered; its VTEP is unchecked." % hostname)
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
        }
        checked.append(row)
        if not row["present"]:
            issues.append("%s has no Vxlan interface; it is not a VTEP yet." % hostname)
            continue
        if row["replication_mode"] != "headend":
            issues.append(
                "%s floods BUM with %s, not head-end replication."
                % (hostname, row["replication_mode"] or "an unknown mode")
            )
        if row["flood_list_source"].upper() != "EVPN":
            issues.append(
                "%s builds its head-end flood list from %s, not EVPN type-3 routes."
                % (hostname, row["flood_list_source"] or "an unknown source")
            )
        if row["source_interface"].lower() != expected.lower():
            issues.append(
                "%s VTEP source is %s, expected %s."
                % (hostname, row["source_interface"] or "unset", expected)
            )
        if l3 and row["l3_vni"] is None:
            missing_vrf.append(hostname)
    if missing_vrf:
        issues.append(
            "VRF %s has no L3 VNI on %s. Define the VRF in AVD before the gateway moves."
            % (vrf, ", ".join(missing_vrf))
        )
    return {"checked": checked, "issues": issues, "vrf": vrf if l3 else ""}


def build_action_items(report):
    """Everything an engineer must resolve before this VLAN can migrate."""
    items = []
    for host in report.get("not_discovered") or []:
        items.append("%s was not discovered (unreachable or failed); placement and prune lists may be incomplete." % host)
    for device in report.get("devices") or []:
        for error in device.get("errors") or []:
            items.append("%s: `%s` failed: %s" % (device.get("hostname"), error.get("command"), error.get("error")))
    if not report.get("switches_vlan_defined") and not report.get("switches_with_svi"):
        items.append(
            "VLAN %s is not configured on any discovered switch. Nothing to migrate or prune here."
            % report.get("vlan_id")
        )
        return items
    items.extend((report.get("gateway_assessment") or {}).get("warnings") or [])
    items.extend((report.get("fabric_readiness") or {}).get("issues") or [])
    items.extend(report.get("gateway_notes") or [])
    items.extend((report.get("l2_attachment") or {}).get("issues") or [])
    for plan in report.get("link_prune_plans") or []:
        if str(plan.get("status") or "").startswith("blocked"):
            items.append(
                "Link prune on %s is blocked: %s." % (plan["hostname"], plan.get("blocked_reason") or "")
            )
    for plan in report.get("prune_plans") or []:
        if plan.get("status") == "blocked_until_gateway_moved":
            items.append("Teardown of %s is blocked: %s." % (plan["hostname"], plan.get("blocked_reason") or ""))
    for item in (report.get("placement_analysis") or {}).get("switches") or []:
        if item.get("role") == "review":
            items.append("%s needs review: %s" % (item.get("hostname"), "; ".join(item.get("nonqualifying") or [])))
    if report.get("needs_vlan_name"):
        items.append(
            "VLAN name is not agreed across switches (%s)."
            % (", ".join(report.get("name_candidates") or []) or "none found")
        )
    stp = report.get("spanning_tree") or {}
    if stp.get("present") and not stp.get("root_hostname"):
        items.append("STP root %s is not a discovered switch; the VLAN extends beyond discovery scope." % stp.get("root_address"))
    if stp.get("inconsistent_roots"):
        items.append("Switches disagree on the STP root (%s); the L2 domain may be split." % ", ".join([stp.get("root_address")] + stp["inconsistent_roots"]))
    for item in report.get("vni_conflicts") or []:
        items.append("VNI %s is already mapped to VLAN %s on %s." % (item["vni"], item["vlan_id"], item["hostname"]))
    for item in report.get("vxlan_already_mapped") or []:
        items.append("VLAN is already VXLAN-mapped on %s (VNI %s)." % (item["hostname"], item["vni"]))
    summary = (report.get("endpoint_table") or {}).get("summary") or {}
    if summary.get("uplink_only"):
        items.append(
            "%s active IP(s) are only seen on switch-to-switch trunks; their edge switch is outside discovery scope."
            % summary["uplink_only"]
        )
    if summary.get("not_in_mac_table"):
        items.append(
            "%s active IP(s) are in ARP but not in any MAC table (aged out, or behind a switch that was not "
            "discovered). Re-run discovery or check them by hand." % summary["not_in_mac_table"]
        )
    allocation = report.get("vni_allocation") or {}
    if allocation.get("vni"):
        items.append(
            "VNI %s proposed (%s). Confirm it, then write it to evpn.l2.vni in the SSOT record."
            % (allocation["vni"], allocation.get("reason"))
        )
    elif report.get("vni") in (None, "", 0) and report.get("vlan_id") is not None:
        items.append(
            "No VNI: %s. Set evpn.l2.vni in the SSOT record."
            % (allocation.get("reason") or "assign one")
        )
    items.extend(report.get("vni_range_problems") or [])
    return items


def build_vlan_discovery_reports(vlans, play_hosts, hostvars, all_hosts=None, vni_allocation=None):
    """Build one aggregated discovery report dict per VLAN in *vlans*.

    *all_hosts* (``ansible_play_hosts_all``) lets the report name switches
    that dropped out of the play (unreachable or failed). Hosts that were
    skipped on purpose set ``_discovery_skipped`` and are not listed.

    *vni_allocation* (``{ranges, records, default_pool}``) proposes a VNI for
    a VLAN without one: the next free VNI in its DC and pool.
    """
    vni_allocation = vni_allocation if isinstance(vni_allocation, dict) else {}
    vni_ranges = vni_allocation.get("ranges") or {}
    range_problems = vni_range_problems(vni_ranges)
    taken_vnis = used_vnis(list(vni_allocation.get("records") or []) + list(vlans))
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
                    "errors": list(discovery.get("errors") or [])
                    + list(per_vlan.get("errors") or []),
                    "migration_tier": discovery.get("migration_tier", ""),
                    "neighbor_count": len(discovery.get("neighbors") or []),
                    "prefix_lists": discovery.get("prefix_lists", {}),
                    "route_maps": discovery.get("route_maps", {}),
                    "ospf": discovery.get("ospf", []),
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
        # Link prunes cut the EVPN-legacy bridge at cutover, before the MLS goes.
        link_prune_plans = build_link_prune_plans(
            vlan_id,
            devices,
            placement["l2"]["link_prunes"],
            {host: item.get("role") for host, item in by_placement.items()},
            gateway_reachable_from_fabric(placement["gateway"], placement["l2"]["interconnects"]),
        )
        gate_teardown_plans(prune_plans, devices)
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
        attach_associated_routes(placement["deployment_model"], vlan_id, devices)
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
        already_mapped, vni_conflicts = _vni_findings(vlan, devices)
        taken_vnis |= used_vnis(None, devices)
        proposed = {}
        if vlan.get("vni") in (None, "", 0) and vni_allocation:
            proposed = allocate_vni(
                vlan.get("data_center") or "",
                vlan.get("vni_pool") or vni_allocation.get("default_pool") or "customer",
                vni_ranges,
                taken_vnis,
            )
            if proposed.get("vni"):
                taken_vnis.add(proposed["vni"])
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
            "spanning_tree": summarize_spanning_tree(devices),
            "vxlan_already_mapped": already_mapped,
            "vni_conflicts": vni_conflicts,
            "vni_allocation": proposed,
            "gateway_notes": gateway_notes,
            "vni_range_problems": range_problems,
            "not_discovered": list(not_discovered),
            "switches_scanned": [device["hostname"] for device in devices],
        }
        if proposed.get("vni"):
            l2_evpn = report["deployment_model"].setdefault("evpn", {}).setdefault("l2", {})
            l2_evpn["vni"] = proposed["vni"]
            l2_evpn["vni_proposed"] = "%s pool %s" % (proposed.get("pool"), proposed.get("range"))
        report["fabric_readiness"] = fabric_readiness(report)
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


