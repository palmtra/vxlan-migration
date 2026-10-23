# -*- coding: utf-8 -*-
"""Normalize discovery into participating leaves, prune-eligible leaves, and L2/L3.

Migration type is only ``l2`` or ``l3``:

* ``l3`` - the gateway is hosted inside the VXLAN fabric (``gateway_leafs``).
* ``l2`` - the gateway stays outside the fabric. The fabric provides Layer-2
  transport. Extra endpoint leaves are normal EVPN placement, not a stretch type.

``gateway_leafs`` are engineer-supplied. An SVI found in discovery is a
``source_gateway`` (legacy MLS / aggPE) and is not a fabric gateway leaf.
A source gateway with no local endpoints is prune-eligible, and it is
listed first, because it is the root of the legacy L2.
"""

from ansible.errors import AnsibleFilterError
from collections.abc import Mapping

from vlan_lib.l2_domain import analyze_l2_domains, device_tier  # noqa: F401
from vlan_lib.parsers import _normalize_interface_name, network_prefix, normalize_mac_address
from vlan_lib.policy import evaluate_route_map

_ENDPOINT_MAC_ROLES = frozenset(
    {"compute", "unknown", "access", "network_service", "downstream_switch", "fabric_interconnect"}
)
_UPLINK_MAC_ROLES = frozenset(
    {"switch_uplink", "peer_link", "trunk", "exempt_trunk"}
)

_PLACEMENT_RULES = [
    "participating_leafs are where the VLAN/VNI must exist after migration.",
    "A switch is participating when it has local endpoint attachment, or when the engineer listed it in gateway_leafs.",
    "VLAN database presence, trunk allowance, uplink-only MAC learning, and transit aggregation do not qualify. A UCS fabric interconnect does: the blades behind it are endpoints.",
    "prune_eligible_leafs carry the VLAN today and have no local endpoints. Source gateways are included and listed first: they are the root of the legacy L2.",
    "A source gateway that also has local endpoints, or that the engineer named in gateway_leafs, stays participating and is not pruned.",
    "gateway_leafs are explicit. Discovery never infers fabric gateway ownership from an SVI.",
    "A switch that is not a VTEP keeps the VLAN as l2_attached when it, or a non-VTEP switch linked to it, has endpoints. It is never a VNI target. The VTEP it uplinks to is participating.",
    "A non-VTEP switch linked to both a VTEP and the MLS bridges EVPN and legacy L2. Its MLS links are link prunes, ready once the VLAN is VNI-mapped on its VTEPs.",
    "Only VTEPs are participating (VNI targets). A legacy MLS with local endpoints, or that is the only path for a non-VTEP switch, is legacy_retained: kept until the endpoints are re-homed, never pruned or configured with a VNI.",
    "source_gateway_devices names the current SVI owners. An SVI with no local endpoints is prune-eligible.",
    "Endpoint VLAN presence on additional leaves is normal EVPN placement, not an L2-stretch type.",
]


def _hostname_of(item):
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("hostname") or "").strip()
    return ""


def _declared_leaves(items):
    """Map hostname -> declared leaf fields. Accepts strings or ``{hostname: ...}``."""
    leaves = {}
    for item in items or []:
        hostname = _hostname_of(item)
        if not hostname:
            continue
        fields = dict(item) if isinstance(item, dict) else {"hostname": hostname}
        fields["hostname"] = hostname
        leaves[hostname] = fields
    return leaves


def _explicit_gateway_leaves(vlan):
    vlan = vlan or {}
    placement = vlan.get("placement") if isinstance(vlan.get("placement"), dict) else {}
    raw = vlan.get("gateway_leafs")
    if not raw:
        raw = placement.get("gateway_leafs") or []
    return _declared_leaves(raw)


def _protected_hosts(vlan):
    vlan = vlan or {}
    hosts = set()
    for item in vlan.get("protected_switches") or []:
        hostname = _hostname_of(item)
        if hostname:
            hosts.add(hostname)
    return hosts


def _protected_vlan(vlan):
    vlan = vlan or {}
    migration = vlan.get("migration") if isinstance(vlan.get("migration"), dict) else {}
    return bool(vlan.get("protected_vlan") or migration.get("protected_vlan"))


GATEWAY_MODES = ("distributed_anycast", "centralized_anycast")

def _unique(items):
    found = []
    for item in items:
        if item and item not in found:
            found.append(item)
    return found


def assess_gateway(vlan, devices):
    """Where is the first-hop gateway today, and what has to move with it?

    ``location`` is ``legacy`` (legacy MLS / core), ``fabric``, ``mixed``, or
    ``external`` (no active SVI on a discovered switch: a firewall or router
    outside the fabric is the gateway). An SVI that is shut or has no IPv4
    address is listed under ``inactive_svis`` and is not a gateway.
    """
    vlan = vlan or {}
    active = []
    inactive = []
    exits = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        hostname = device.get("hostname") or ""
        tier = device_tier(device)
        for item in device.get("port_attachments") or []:
            if isinstance(item, dict) and item.get("role") == "network_service":
                exits.append(
                    {
                        "hostname": hostname,
                        "tier": tier,
                        "interface": item.get("interface") or "",
                        "description": item.get("description") or "",
                        "neighbor": item.get("neighbor") or "",
                    }
                )
        if not device.get("svi_present"):
            continue
        details = device.get("svi_details") or {}
        entry = {
            "hostname": hostname,
            "tier": tier,
            "vrf": device.get("svi_vrf") or details.get("vrf") or "default",
            "description": details.get("description") or "",
            "mtu": details.get("mtu"),
            "shutdown": bool(details.get("shutdown")),
            "ip_addresses": list(details.get("ip_addresses") or []),
            "primary_ip": details.get("primary_ip") or "",
            "secondary_ips": list(details.get("secondary_ips") or []),
            "gateway_addresses": list(details.get("gateway_addresses") or []),
            "redundancy": details.get("redundancy") or "",
            "fhrp_groups": list(details.get("fhrp_groups") or details.get("hsrp_groups") or []),
            "ipv6_addresses": list(details.get("ipv6_addresses") or []),
            "dhcp_relay": list(details.get("dhcp_relay") or []),
            "acls": list(details.get("acls") or []),
            "pbr": list(details.get("pbr") or []),
            "ospf": list(details.get("ospf") or []),
            "multicast": list(details.get("multicast") or []),
            "nat": list(details.get("nat") or []),
            "urpf": list(details.get("urpf") or []),
            "other_lines": list(details.get("other_lines") or []),
        }
        if entry["shutdown"] or not entry["ip_addresses"]:
            inactive.append(entry)
        else:
            active.append(entry)

    tiers = {entry["tier"] for entry in active}
    warnings = []
    if not active:
        location = "external"
        recommended = "l2"
        reason = "no active SVI on a discovered switch; the gateway is outside the fabric"
        if not exits:
            warnings.append(
                "No SVI and no firewall/router port was found. Confirm where the gateway "
                "lives before migrating; it may sit behind a switch outside discovery scope."
            )
    else:
        location = "legacy" if tiers == {"legacy"} else "fabric" if tiers == {"fabric"} else "mixed"
        recommended = "l3"
        reason = "the gateway is an SVI on %s and must be rebuilt as an anycast gateway in the fabric" % (
            ", ".join(entry["hostname"] for entry in active)
        )
        if location == "mixed":
            warnings.append(
                "Active SVIs exist on both legacy and fabric switches. Only one gateway may "
                "answer for the subnet during cutover."
            )

    vrfs = _unique(entry["vrf"] for entry in active)
    if len(vrfs) > 1:
        warnings.append("Active SVIs disagree on VRF: %s." % ", ".join(vrfs))
    for entry in inactive:
        warnings.append(
            "SVI on %s is %s; remove it with the VLAN, it is not a gateway."
            % (entry["hostname"], "shut down" if entry["shutdown"] else "configured without an IPv4 address")
        )
    for item in exits:
        if item["tier"] == "legacy":
            warnings.append(
                "%s %s faces a firewall/router%s. Re-home it to the fabric before %s is pruned."
                % (
                    item["hostname"],
                    item["interface"],
                    " (%s)" % (item["neighbor"] or item["description"])
                    if (item["neighbor"] or item["description"])
                    else "",
                    item["hostname"],
                )
            )

    subnets = []
    for entry in active:
        for cidr in entry["ip_addresses"]:
            prefix = network_prefix(cidr)
            if any(item["prefix"] == prefix for item in subnets):
                continue
            gateway_ip = ""
            for address in entry["gateway_addresses"]:
                if _ip_in_prefixes(address, [prefix]):
                    gateway_ip = address
                    break
            subnets.append(
                {
                    "prefix": prefix,
                    "gateway_ip": gateway_ip,
                    "secondary": cidr in entry["secondary_ips"],
                }
            )

    def merged(key):
        values = []
        for entry in active:
            for value in entry[key]:
                if value not in values:
                    values.append(value)
        return values

    mode = str(vlan.get("gateway_mode") or "").strip().lower()
    services = {
        key: merged(key)
        for key in (
            "dhcp_relay",
            "acls",
            "pbr",
            "ipv6_addresses",
            "ospf",
            "multicast",
            "nat",
            "urpf",
            "other_lines",
        )
    }
    mtus = _unique(entry["mtu"] for entry in active)
    return {
        "location": location,
        "recommended_migration_type": recommended,
        "reason": reason,
        "mode": mode if mode in GATEWAY_MODES else "",
        "invalid_mode": mode if mode and mode not in GATEWAY_MODES else "",
        "vrf": vrfs[0] if len(vrfs) == 1 else "",
        "redundancy": _unique(entry["redundancy"] for entry in active),
        "virtual_ip": next(
            (item["gateway_ip"] for item in subnets if item["gateway_ip"] and not item["secondary"]),
            "",
        ),
        "subnets": subnets,
        "mtu": mtus[0] if len(mtus) == 1 else None,
        "services": services,
        "source_devices": [entry["hostname"] for entry in active],
        "svis": active,
        "inactive_svis": inactive,
        "external_exits": exits,
        "warnings": warnings,
    }


def _explicit_migration_type(vlan):
    vlan = vlan or {}
    if vlan.get("_probe"):
        # A probe record is synthetic; discovery decides its type.
        return ""
    migration = vlan.get("migration") if isinstance(vlan.get("migration"), dict) else {}
    raw = migration.get("type") or vlan.get("service_type") or ""
    return str(raw).strip().lower().replace("-", "_")


def resolve_migration_type(vlan, gateway_hosts, assessment=None):
    """Return ``(type, reason)`` where type is ``l2`` or ``l3``.

    ``l2_l3`` is deprecated. Gateway-in-fabric is ``l3``. Gateway-outside is
    ``l2``. An explicit ``service_type`` on the record wins over discovery;
    discovery only decides for a probe record or a record with no type.
    """
    explicit = _explicit_migration_type(vlan)
    if not gateway_hosts and not explicit and assessment:
        return assessment["recommended_migration_type"], "discovery: %s" % assessment["reason"]
    if gateway_hosts:
        return (
            "l3",
            "gateway_leafs place the gateway inside the VXLAN fabric",
        )
    if explicit == "l3":
        return (
            "l3",
            "service_type is l3; name gateway_leafs before deploy. "
            "Discovery does not infer them from the legacy SVI",
        )
    if explicit == "l2_l3":
        return (
            "l2",
            "l2_l3 is deprecated and no gateway_leafs were supplied, "
            "so the gateway stays outside the fabric",
        )
    return (
        "l2",
        "gateway remains outside the VXLAN fabric; the fabric provides Layer-2 transport",
    )


def _endpoint_macs(device):
    macs = []
    for entry in device.get("endpoint_mac_entries") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("attachment_role") or entry.get("port_role") or ""
        if role in _UPLINK_MAC_ROLES:
            continue
        macs.append(entry)
    if macs:
        return macs
    for entry in device.get("mac_entries") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("attachment_role") or entry.get("port_role") or ""
        if role in _ENDPOINT_MAC_ROLES:
            macs.append(entry)
    return macs


def _uplink_only_macs(device, endpoint_macs):
    if endpoint_macs:
        return []
    found = []
    for entry in device.get("mac_entries") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("attachment_role") or entry.get("port_role") or ""
        if role in _UPLINK_MAC_ROLES:
            found.append(entry)
    return found


def _arp_matches_local_macs(device, endpoint_macs):
    wanted = {
        normalize_mac_address(entry.get("mac"))
        for entry in endpoint_macs
        if entry.get("mac")
    }
    wanted.discard("")
    if not wanted:
        return False
    for entry in device.get("arp_entries") or []:
        if not isinstance(entry, dict):
            continue
        if normalize_mac_address(entry.get("mac")) in wanted:
            return True
    return False


def local_endpoint_evidence(device):
    """Return ``(qualifying, nonqualifying)`` evidence strings for one switch.

    Qualifying evidence is local endpoint attachment only. Trunk allowance,
    VLAN-database presence, uplink MACs, and fabric interconnects do not qualify.
    """
    device = device or {}
    qualifying = []
    interfaces = []
    seen_norm = set()
    for key in ("compute_ports", "access_ports"):
        for iface in device.get(key) or []:
            # compute_ports carry the show-run name (Ethernet3), access_ports
            # the show vlan name (Et3). List each port once.
            norm = _normalize_interface_name(iface)
            if iface and norm not in seen_norm:
                seen_norm.add(norm)
                interfaces.append(iface)
    if interfaces:
        qualifying.append(
            "endpoint-facing interfaces: %s" % ", ".join(interfaces)
        )
    fis = [
        item.get("interface")
        for item in (device.get("port_attachments") or [])
        if isinstance(item, dict) and item.get("role") == "fabric_interconnect" and item.get("interface")
    ]
    if fis:
        qualifying.append("UCS fabric interconnect: %s" % ", ".join(_ports_with_neighbor(device, fis)))
    services = _ports_with_neighbor(device, device.get("service_ports"))
    if services:
        qualifying.append("firewall / load balancer / router: %s" % ", ".join(services))

    endpoint_macs = _endpoint_macs(device)
    mac_ifaces = []
    for entry in endpoint_macs:
        iface = entry.get("interface") or ""
        if iface and iface not in mac_ifaces:
            mac_ifaces.append(iface)
    if mac_ifaces:
        qualifying.append(
            "local MAC learning on endpoint-facing ports: %s" % ", ".join(mac_ifaces)
        )
    if _arp_matches_local_macs(device, endpoint_macs):
        qualifying.append("ARP ownership tied to locally learned MACs")

    # A switch outside the inventory carries endpoints only when MACs are
    # learned toward it. Without MACs it is a decision, never a silent prune.
    mac_norm = {_normalize_interface_name(iface) for iface in mac_ifaces}
    downstream_with_macs = [
        iface
        for iface in (device.get("downstream_switch_ports") or [])
        if _normalize_interface_name(iface) in mac_norm
    ]
    if downstream_with_macs:
        qualifying.append(
            "MACs behind a switch outside discovery scope: %s"
            % ", ".join(_ports_with_neighbor(device, downstream_with_macs))
        )

    netbox = device.get("netbox_endpoints") or device.get("endpoint_mappings") or []
    if netbox:
        qualifying.append("NetBox endpoint mapping")

    nonqualifying = []
    if _uplink_only_macs(device, endpoint_macs):
        nonqualifying.append("MAC learned only on an uplink")
    if not qualifying:
        if device.get("switch_uplink_ports") or device.get("trunk_ports"):
            nonqualifying.append("VLAN allowed on a trunk")
        elif device.get("vlan_present"):
            nonqualifying.append("VLAN present in the VLAN database")
    return qualifying, nonqualifying


def _ports_with_neighbor(device, ports):
    """``Eth1/5 (lis-fw01 ethernet1/1)`` labels for report evidence."""
    labels = {}
    for item in device.get("port_attachments") or []:
        if isinstance(item, dict) and item.get("interface"):
            label = item.get("neighbor") or item.get("description") or ""
            labels[item["interface"]] = label
    found = []
    for iface in ports or []:
        label = labels.get(iface, "")
        found.append("%s (%s)" % (iface, label) if label else iface)
    return found


def silent_downstream_switch_ports(device):
    """Trunks to switches outside the inventory with no MACs learned on them."""
    mac_ifaces = {
        _normalize_interface_name(entry.get("interface") or "")
        for entry in (device.get("mac_entries") or [])
        if isinstance(entry, dict)
    }
    return [
        iface
        for iface in (device.get("downstream_switch_ports") or [])
        if _normalize_interface_name(iface) not in mac_ifaces
    ]


def _leaf_record(hostname, declared=None, device=None, vlan_id=None):
    """BGP identity for one targeted VTEP.

    ASN comes from ``router bgp``. Router-id comes from BGP, then Loopback0.
    RD is the on-box VLAN RD when this VLAN is already in BGP, otherwise
    ``<router-id>:<vlan id>``. VTEP address is Loopback1.
    """
    declared = declared or {}
    device = device or {}
    bgp_as = str(device.get("bgp_as") or "").strip()
    router_id = str(
        declared.get("router_id")
        or device.get("router_id")
        or device.get("loopback0")
        or ""
    ).strip()
    vtep = str(
        declared.get("vtep") or device.get("vtep") or device.get("loopback1") or ""
    ).strip()
    rd = str(declared.get("rd") or "").strip()
    if not rd and vlan_id not in (None, ""):
        for block in device.get("bgp_vlan_blocks") or []:
            if not isinstance(block, dict):
                continue
            if str(block.get("vlan_id")) != str(vlan_id):
                continue
            if block.get("rd"):
                rd = str(block.get("rd")).strip()
                break
    if not rd and router_id and vlan_id not in (None, ""):
        rd = "%s:%s" % (router_id, vlan_id)
    return {
        "hostname": hostname,
        "asn": str(declared.get("asn") or bgp_as or "").strip(),
        "router_id": router_id,
        "rd": rd,
        "vtep": vtep,
    }


def _host_only(hostname):
    return {"hostname": hostname}


def _leaf_bgp_map(placement):
    """Per-VTEP BGP identity from participating and gateway leaves.

    Hostnames stay on ``target_switches``. ASN, router-id, route distinguisher,
    and VTEP address stay here so the VLAN BGP block can be rendered per leaf.
    """
    leaves = {}
    if not isinstance(placement, dict):
        return leaves
    for key in ("participating_leafs", "gateway_leafs"):
        for item in placement.get(key) or []:
            if not isinstance(item, dict):
                continue
            hostname = _hostname_of(item)
            if not hostname:
                continue
            current = dict(leaves.get(hostname) or {})
            for field in ("asn", "router_id", "rd", "vtep"):
                value = item.get(field)
                if value in (None, ""):
                    continue
                text = str(value).strip()
                if text and not current.get(field):
                    current[field] = text
            if current:
                leaves[hostname] = current
    return leaves


def _vlan_route_target(record):
    """Route-target for ``router bgp / vlan``: the L2 VNI's, both directions.

    The fabric (both DCs) uses ``<l2 vni>:<vlan id>``. An explicit ``l2_rt``
    on the record wins. The L3 VRF targets (``rt_import`` / ``rt_export``,
    ``l3_vni``) belong to the VRF, which is already built, never to the VLAN.
    """
    if not isinstance(record, dict):
        return {}
    l2_rt = str(record.get("l2_rt") or "").strip()
    if l2_rt:
        return {"rt_both": l2_rt}
    vni = record.get("vni")
    vlan_id = record.get("id")
    if str(vni or "").strip().isdigit() and vlan_id not in (None, ""):
        return {"rt_both": "%s:%s" % (int(vni), vlan_id)}
    return {}


def _as_mapping(value):
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        return value
    return {}


def host_vtep_bgp(host):
    """ASN and router-id learned for one inventory host.

    Discovery facts from this run win. Inventory ``bgp_as`` and ``router_id``
    are the fallback. ``ansible_host`` is the management address and is not a
    route distinguisher.

    Ansible passes ``hostvars[hostname]`` as a mapping, not a dict.
    """
    host = _as_mapping(host)
    discovery = _as_mapping(host.get("_vlan_discovery"))
    asn = str(discovery.get("bgp_as") or host.get("bgp_as") or "").strip()
    router_id = str(
        discovery.get("router_id")
        or discovery.get("loopback0")
        or host.get("router_id")
        or ""
    ).strip()
    return {"asn": asn, "router_id": router_id}


def virtual_gateway_address(gateway, prefixes=None):
    """Anycast ``ip address virtual`` from the gateway and its VLAN prefix.

    ``66.180.1.1`` plus ``66.180.1.0/27`` becomes ``66.180.1.1/27``.
    """
    import ipaddress

    text = str(gateway or "").strip()
    if not text or "/" in text:
        return text
    try:
        host = ipaddress.ip_address(text)
    except ValueError:
        return text
    chosen = None
    for prefix in prefixes or []:
        try:
            network = ipaddress.ip_network(str(prefix).strip(), strict=False)
        except ValueError:
            continue
        if host in network:
            chosen = network
            break
        if chosen is None:
            chosen = network
    if chosen is None:
        return text
    return "%s/%s" % (host, chosen.prefixlen)


def bgp_vlan_evpn(record, hostname, host_asn="", host_router_id=""):
    """EOS ``router bgp / vlan`` values for one VTEP.

    RD is the leaf route distinguisher, or ``<router-id>:<vlan id>``. The
    route-target is ``<l2 vni>:<vlan id>`` (see ``_vlan_route_target``).
    An empty result means this VLAN has no L2 VNI, so nothing to publish.
    ``missing`` lists asn or rd when a target exists but the leaf cannot build it.
    """
    if not isinstance(record, dict):
        return {}
    route_target = _vlan_route_target(record)
    if not route_target:
        return {}
    leaf_bgp = record.get("leaf_bgp") if isinstance(record.get("leaf_bgp"), dict) else {}
    leaf = leaf_bgp.get(hostname) if isinstance(leaf_bgp.get(hostname), dict) else {}
    asn = str(leaf.get("asn") or host_asn or "").strip()
    router_id = str(leaf.get("router_id") or host_router_id or "").strip()
    rd = str(leaf.get("rd") or "").strip()
    vlan_id = record.get("id")
    if not rd and router_id and vlan_id not in (None, ""):
        rd = "%s:%s" % (router_id, vlan_id)
    result = {"asn": asn, "rd": rd}
    result.update(route_target)
    missing = []
    if not asn:
        missing.append("asn")
    if not rd:
        missing.append("rd")
    if missing:
        result["missing"] = missing
    return result


def normalize_static_route_tags(items):
    """Engineer tags used to match ``ip route ... name``. Case-insensitive, optional."""
    tags = []
    seen = set()
    for item in items or []:
        text = str(item or "").strip()
        key = text.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        tags.append(text)
    return tags


def _matching_route_tag(name, tags):
    """Return the engineer tag that equals the route name, ignoring case."""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    for tag in tags or []:
        if str(tag).strip().lower() == text:
            return tag
    return ""


def empty_associated_routes():
    """Stable shape for VLAN-scoped statics and BGP. Never a prune list."""
    return {
        "static_routes": [],
        "bgp_networks": [],
        "bgp_neighbors": [],
        "bgp_exports": [],
        "bgp_live": [],
        "ospf": [],
    }


def _redistribute_parts(method):
    """``direct route-map RM`` -> ``('connected', 'RM')``. NXOS says direct."""
    words = str(method or "").split()
    if not words:
        return "", ""
    source = words[0].lower()
    if source == "direct":
        source = "connected"
    route_map = ""
    for index, word in enumerate(words):
        if word.lower() == "route-map" and index + 1 < len(words):
            route_map = words[index + 1]
    return source, route_map


def _policy(device, route_map, prefix, context=None):
    return evaluate_route_map(
        route_map,
        prefix,
        device.get("route_maps") or {},
        device.get("prefix_lists") or {},
        context,
    )


def _neighbor_out_policy(peer):
    for value in peer.get("route_maps") or []:
        words = str(value).split()
        if len(words) >= 2 and words[-1].lower() == "out":
            return words[0]
    return ""


def _ipv4_value(ip_text):
    parts = str(ip_text or "").strip().split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return None
    if any(part < 0 or part > 255 for part in octets):
        return None
    return (octets[0] << 24) + (octets[1] << 16) + (octets[2] << 8) + octets[3]


def _prefix_tuple(text):
    """Return ``(network, prefix_len)`` for an IPv4 prefix, or None."""
    raw = str(text or "").strip()
    if "/" not in raw:
        return None
    normalized = network_prefix(raw)
    if "/" not in normalized:
        return None
    ip_text, plen_text = normalized.split("/", 1)
    try:
        plen = int(plen_text)
    except ValueError:
        return None
    value = _ipv4_value(ip_text)
    if value is None or plen < 0 or plen > 32:
        return None
    mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
    return value & mask, plen


def _prefix_contains(outer, inner):
    if plen_wider(outer, inner):
        net_o, plen_o = outer
        net_i, _plen_i = inner
        mask = (0xFFFFFFFF << (32 - plen_o)) & 0xFFFFFFFF if plen_o else 0
        return (net_i & mask) == (net_o & mask)
    return False


def plen_wider(outer, inner):
    return outer[1] <= inner[1]


def _ip_in_prefixes(ip_text, prefixes):
    value = _ipv4_value(str(ip_text or "").split()[0])
    if value is None:
        return ""
    for prefix in prefixes:
        parsed = _prefix_tuple(prefix)
        if not parsed:
            continue
        network, plen = parsed
        mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        if (value & mask) == network:
            return prefix
    return ""


def _prefix_relation(candidate, vlan_prefixes):
    """How *candidate* relates to the VLAN prefixes.

    ``equal`` and ``more_specific`` are VLAN routes. ``covers`` means an
    aggregate contains the VLAN prefix. Default route is never ``covers``.
    """
    parsed = _prefix_tuple(candidate)
    if not parsed:
        return "", ""
    if parsed == (0, 0):
        return "", ""
    for prefix in vlan_prefixes:
        vlan = _prefix_tuple(prefix)
        if not vlan:
            continue
        if parsed == vlan:
            return "equal", prefix
        if _prefix_contains(vlan, parsed):
            return "more_specific", prefix
        if _prefix_contains(parsed, vlan):
            return "covers", prefix
    return "", ""


def _norm_vrf_name(vrf):
    return str(vrf or "default")


def _vlan_vrfs(devices, model_vrf):
    found = set()
    if model_vrf:
        found.add(_norm_vrf_name(model_vrf))
    for device in devices or []:
        if not isinstance(device, dict) or not device.get("svi_present"):
            continue
        details = device.get("svi_details") or {}
        found.add(_norm_vrf_name(device.get("svi_vrf") or details.get("vrf") or "default"))
    return found


def _prefixes_from_svis(devices):
    prefixes = []
    seen = set()
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        details = device.get("svi_details") or {}
        for cidr in details.get("ip_addresses") or []:
            prefix = network_prefix(cidr)
            if prefix and prefix not in seen:
                seen.add(prefix)
                prefixes.append(prefix)
    return prefixes


def _update_source_is_vlan(update_source, vlan_id):
    if vlan_id in (None, ""):
        return False
    text = re_sub_interface(update_source)
    return text in ("vlan%s" % int(vlan_id), "vl%s" % int(vlan_id))


def re_sub_interface(update_source):
    return str(update_source or "").lower().replace(" ", "")


def attach_associated_routes(model, vlan_id, devices):
    """Attach statics and BGP objects that belong to this VLAN.

    A shared-VRF dump is not enough. A static counts when its next hop is
    inside a VLAN prefix, when the static itself is that prefix, or when an
    engineer tag matches the route name (case-insensitive). A BGP
    network or aggregate counts when it is, contains, or is contained by a
    VLAN prefix. ``redistribute connected`` or ``static`` on a device that
    owns the SVI is recorded because that is how the prefix is often advertised.
    Peers count when they sit on the VLAN or use its SVI as update-source.

    These objects are for review and for rebuilding the gateway. They are
    not prune actions.
    """
    if not isinstance(model, dict):
        return empty_associated_routes()
    routing = model.get("routing")
    if not isinstance(routing, dict):
        routing = {}
        model["routing"] = routing
    prefixes = []
    seen = set()
    source = list(routing.get("prefixes") or []) or _prefixes_from_svis(devices)
    for item in source:
        prefix = network_prefix(item)
        if prefix and prefix not in seen:
            seen.add(prefix)
            prefixes.append(prefix)
    vrfs = _vlan_vrfs(devices, routing.get("vrf"))
    tags = normalize_static_route_tags(routing.get("static_route_tags"))
    statics = []
    networks = []
    neighbors = []
    static_seen = set()
    network_seen = set()
    neighbor_seen = set()

    for device in devices or []:
        if not isinstance(device, dict):
            continue
        hostname = device.get("hostname") or ""
        owns_svi = bool(device.get("svi_present"))
        for route in device.get("static_routes") or []:
            if not isinstance(route, dict):
                continue
            name_tag = _matching_route_tag(route.get("name"), tags)
            vrf_ok = _norm_vrf_name(route.get("vrf")) in vrfs
            if not vrf_ok and not name_tag:
                continue
            prefix = route.get("prefix") or ""
            next_hop = route.get("next_hop") or ""
            exit_iface = route.get("interface") or ""
            hit = _ip_in_prefixes(next_hop, prefixes) if vrf_ok else ""
            relation, related = _prefix_relation(prefix, prefixes) if vrf_ok else ("", "")
            via_svi = vrf_ok and _update_source_is_vlan(exit_iface or next_hop, vlan_id)
            discard = str(exit_iface or next_hop).lower().startswith("null")
            if hit:
                reason = "next_hop %s is inside %s" % (next_hop, hit)
            elif via_svi:
                reason = "exits via %s" % (exit_iface or next_hop)
            elif relation in ("equal", "more_specific"):
                reason = "prefix %s is inside %s" % (prefix, related)
            elif relation == "covers" and discard:
                reason = "discard route %s covers %s; it often anchors an aggregate" % (
                    prefix,
                    related,
                )
            elif name_tag:
                reason = "route name %s matches tag %s" % (
                    route.get("name") or "",
                    name_tag,
                )
            else:
                continue
            key = (hostname, _norm_vrf_name(route.get("vrf")), prefix, next_hop)
            if key in static_seen:
                continue
            static_seen.add(key)
            statics.append(
                {
                    "hostname": hostname,
                    "vrf": _norm_vrf_name(route.get("vrf")),
                    "prefix": prefix,
                    "next_hop": next_hop,
                    "interface": exit_iface,
                    "distance": route.get("distance") or "",
                    "tag": route.get("tag") or "",
                    "name": route.get("name") or "",
                    "reason": reason,
                }
            )

        for block in device.get("bgp_vrfs") or []:
            if not isinstance(block, dict):
                continue
            vrf_name = _norm_vrf_name(block.get("name") or block.get("vrf"))
            if vrf_name not in vrfs:
                continue
            for kind, values in (
                ("network", block.get("networks") or []),
                ("aggregate", block.get("aggregates") or []),
            ):
                for candidate in values:
                    relation, related = _prefix_relation(candidate, prefixes)
                    if kind == "network" and relation not in ("equal", "more_specific", "covers"):
                        continue
                    if kind == "aggregate" and relation not in ("equal", "covers"):
                        continue
                    if relation == "equal":
                        reason = "%s statement advertises %s" % (kind, related)
                    elif relation == "more_specific":
                        reason = "%s %s is a more specific of %s" % (kind, candidate, related)
                    else:
                        reason = "%s %s covers VLAN prefix %s" % (kind, candidate, related)
                    key = (hostname, vrf_name, kind, candidate)
                    if key in network_seen:
                        continue
                    network_seen.add(key)
                    networks.append(
                        {
                            "hostname": hostname,
                            "vrf": vrf_name,
                            "kind": kind,
                            "prefix": candidate,
                            "method": "",
                            "reason": reason,
                        }
                    )
            if owns_svi:
                for method in block.get("redistribute") or []:
                    first, route_map = _redistribute_parts(method)
                    if first not in ("connected", "static"):
                        continue
                    key = (hostname, vrf_name, "redistribute", first)
                    if key in network_seen:
                        continue
                    network_seen.add(key)
                    if first == "connected":
                        checked = list(prefixes)
                        context = {"interface": "Vlan%s" % vlan_id}
                    else:
                        checked = [
                            item["prefix"]
                            for item in statics
                            if item["hostname"] == hostname and item["vrf"] == vrf_name
                        ]
                        context = {}
                    verdicts = [
                        dict(_policy(device, route_map, prefix, context), prefix=prefix)
                        for prefix in checked
                    ]
                    if route_map:
                        policy_text = "; ".join(
                            "%s: %s" % (item["prefix"], item["reason"]) for item in verdicts
                        ) or "route-map %s" % route_map
                    else:
                        policy_text = "no route-map, everything %s is redistributed" % first
                    networks.append(
                        {
                            "hostname": hostname,
                            "vrf": vrf_name,
                            "kind": "redistribute",
                            "prefix": "",
                            "method": method,
                            "route_map": route_map,
                            "verdicts": verdicts,
                            "reason": (
                                "BGP redistributes %s in the SVI VRF (%s). The VRF can be shared; "
                                "confirm before migration." % (first, policy_text)
                            ),
                        }
                    )

        for peer in device.get("bgp_neighbors") or []:
            if not isinstance(peer, dict):
                continue
            if _norm_vrf_name(peer.get("vrf")) not in vrfs:
                continue
            neighbor = peer.get("neighbor") or ""
            update_source = peer.get("update_source") or ""
            hit = _ip_in_prefixes(neighbor, prefixes)
            on_svi = _update_source_is_vlan(update_source, vlan_id)
            if not hit and not on_svi:
                continue
            if on_svi and hit:
                reason = "peer %s is inside %s and update-source is %s" % (
                    neighbor,
                    hit,
                    update_source,
                )
            elif on_svi:
                reason = "update-source is %s" % update_source
            else:
                reason = "peer %s is inside %s" % (neighbor, hit)
            key = (hostname, _norm_vrf_name(peer.get("vrf")), neighbor)
            if key in neighbor_seen:
                continue
            neighbor_seen.add(key)
            neighbors.append(
                {
                    "hostname": hostname,
                    "vrf": _norm_vrf_name(peer.get("vrf")),
                    "neighbor": neighbor,
                    "remote_as": str(peer.get("remote_as") or ""),
                    "update_source": update_source,
                    "description": peer.get("description") or "",
                    "reason": reason,
                }
            )

    exports, live, ospf = _advertisement_view(vlan_id, devices, prefixes, vrfs, networks)
    associated = {
        "static_routes": statics,
        "bgp_networks": networks,
        "bgp_neighbors": neighbors,
        "bgp_exports": exports,
        "bgp_live": live,
        "ospf": ospf,
    }
    routing["associated"] = associated
    return associated


def _advertisement_view(vlan_id, devices, prefixes, vrfs, networks):
    """Who advertises the VLAN prefix today, and to whom.

    ``bgp_exports``: on a switch that originates the prefix in BGP, each peer
    in the SVI VRF with the verdict of its outbound route-map.
    ``bgp_live``: ``show ip bgp <prefix>`` on the SVI owners.
    ``ospf``: OSPF networks covering the prefix, redistribution on the SVI
    owner, and OSPF lines on the SVI itself.
    """
    originators = {
        (item["hostname"], item["vrf"])
        for item in networks
        if item["kind"] in ("network", "aggregate")
        or (
            item["kind"] == "redistribute"
            and any(v.get("verdict") in ("permit", "unknown", "none") for v in item.get("verdicts") or [])
        )
        or (item["kind"] == "redistribute" and not item.get("route_map"))
    }
    exports = []
    live = []
    ospf = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        hostname = device.get("hostname") or ""
        for peer in device.get("bgp_neighbors") or []:
            if not isinstance(peer, dict):
                continue
            vrf_name = _norm_vrf_name(peer.get("vrf"))
            if (hostname, vrf_name) not in originators:
                continue
            route_map = _neighbor_out_policy(peer)
            for prefix in prefixes:
                result = _policy(device, route_map, prefix)
                exports.append(
                    {
                        "hostname": hostname,
                        "vrf": vrf_name,
                        "neighbor": peer.get("neighbor") or "",
                        "remote_as": str(peer.get("remote_as") or ""),
                        "description": peer.get("description") or "",
                        "prefix": prefix,
                        "route_map": route_map,
                        "verdict": "permit" if result["verdict"] == "none" else result["verdict"],
                        "reason": result["reason"],
                    }
                )
        for item in device.get("bgp_prefix_detail") or []:
            if isinstance(item, dict):
                live.append(
                    {
                        "hostname": hostname,
                        "vrf": item.get("vrf") or "",
                        "prefix": item.get("prefix") or "",
                        "present": bool(item.get("present")),
                        "local": bool(item.get("local")),
                        "advertised_to": list(item.get("advertised_to") or []),
                    }
                )
        owns_svi = bool(device.get("svi_present"))
        svi_ospf = (device.get("svi_details") or {}).get("ospf") or []
        if owns_svi and svi_ospf:
            ospf.append(
                {
                    "hostname": hostname,
                    "vrf": _norm_vrf_name(device.get("svi_vrf")),
                    "process": "",
                    "detail": "; ".join(svi_ospf),
                    "reason": "OSPF is enabled on Vlan%s" % vlan_id,
                }
            )
        for process in device.get("ospf") or []:
            if not isinstance(process, dict):
                continue
            vrf_name = _norm_vrf_name(process.get("vrf"))
            if vrf_name not in vrfs:
                continue
            for net in process.get("networks") or []:
                relation, related = _prefix_relation(net.get("prefix"), prefixes)
                if relation in ("equal", "covers", "more_specific"):
                    ospf.append(
                        {
                            "hostname": hostname,
                            "vrf": vrf_name,
                            "process": process.get("process") or "",
                            "detail": "network %s area %s" % (net.get("prefix"), net.get("area")),
                            "reason": "covers %s" % related,
                        }
                    )
            if owns_svi:
                for redist in process.get("redistribute") or []:
                    if redist.get("source") != "connected":
                        continue
                    verdicts = [
                        _policy(device, redist.get("route_map"), prefix, {"interface": "Vlan%s" % vlan_id})
                        for prefix in prefixes
                    ]
                    ospf.append(
                        {
                            "hostname": hostname,
                            "vrf": vrf_name,
                            "process": process.get("process") or "",
                            "detail": "redistribute connected%s"
                            % (" route-map %s" % redist["route_map"] if redist.get("route_map") else ""),
                            "reason": "; ".join(item["reason"] for item in verdicts)
                            or "SVI is connected in this VRF",
                        }
                    )
    return exports, live, ospf


def _blank_evpn_vni(value):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if value in (None, "", 0, "0"):
        return None
    return value


def _coerce_int(value):
    """YAML and Ansible facts often store a VLAN id as text."""
    if isinstance(value, bool) or value in (None, ""):
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return value


def _model_gateway(assessment):
    """Deployable gateway block: where it is today and what moves with it."""
    services = {
        key: value
        for key, value in (assessment.get("services") or {}).items()
        if value
    }
    block = {
        "location": assessment.get("location") or "",
        "mode": assessment.get("mode") or "",
        "virtual_ip": assessment.get("virtual_ip") or "",
        "subnets": list(assessment.get("subnets") or []),
        "redundancy": ", ".join(assessment.get("redundancy") or []),
        "source_devices": list(assessment.get("source_devices") or []),
    }
    if assessment.get("mtu"):
        block["mtu"] = assessment["mtu"]
    if services:
        block["services"] = services
    exits = [
        {key: item.get(key) for key in ("hostname", "interface", "neighbor", "description") if item.get(key)}
        for item in assessment.get("external_exits") or []
    ]
    if exits:
        block["external_exits"] = exits
    return block


def build_deployment_model(vlan, devices, placement):
    """Build the deployable YAML model from a VLAN record and placement."""
    vlan = vlan or {}
    devices = devices or []
    by_host = {device.get("hostname"): device for device in devices if device.get("hostname")}
    migration_type = placement["migration_type"]
    gateway_declared = placement["gateway_declared"]
    ownership = vlan.get("ownership") if isinstance(vlan.get("ownership"), dict) else {}
    evpn = vlan.get("evpn") if isinstance(vlan.get("evpn"), dict) else {}
    l2_evpn = evpn.get("l2") if isinstance(evpn.get("l2"), dict) else {}
    l3_evpn = evpn.get("l3") if isinstance(evpn.get("l3"), dict) else {}
    routing = vlan.get("routing") if isinstance(vlan.get("routing"), dict) else {}

    vlan_id = vlan.get("id")
    participating = []
    for hostname in placement["participating_order"]:
        participating.append(
            _leaf_record(
                hostname,
                gateway_declared.get(hostname) or {},
                by_host.get(hostname) or {},
                vlan_id,
            )
        )
    gateway_leafs = [
        _leaf_record(
            hostname,
            gateway_declared.get(hostname) or {},
            by_host.get(hostname) or {},
            vlan_id,
        )
        for hostname in placement["gateway_order"]
    ]
    prune_eligible = [_host_only(hostname) for hostname in placement["prune_order"]]
    source_gateways = [
        _host_only(hostname) for hostname in placement["source_gateway_order"]
    ]

    prefixes = list(routing.get("prefixes") or vlan.get("prefixes") or [])
    gateway_ip = routing.get("gateway_ip") or vlan.get("gateway") or ""
    vrf = routing.get("vrf") or vlan.get("vrf") or "default"
    static_route_tags = normalize_static_route_tags(
        list(vlan.get("static_route_tags") or [])
        + list(routing.get("static_route_tags") or [])
    )
    site_vlan = {}
    site = vlan.get("site") if isinstance(vlan.get("site"), dict) else {}
    if isinstance(site.get("vlan"), dict):
        site_vlan = site["vlan"]

    model = {
        "site": {
            "data_center": vlan.get("data_center") or site.get("data_center") or "",
            "vlan": {
                "id": _coerce_int(site_vlan.get("id", vlan.get("id"))),
                "name": site_vlan.get("name") or vlan.get("name") or "",
            },
        },
        "ownership": {
            "tenant": ownership.get("tenant") or vlan.get("tenant") or "",
            "owner": ownership.get("owner") or vlan.get("owner") or "",
        },
        "routing": {
            "vrf": vrf,
            "prefixes": prefixes,
            "gateway_ip": gateway_ip,
            "static_route_tags": static_route_tags,
            "associated": empty_associated_routes(),
        },
        "migration": {
            "type": migration_type,
            "protected_vlan": placement["protected_vlan"],
        },
        "gateway": _model_gateway(placement.get("gateway") or {}),
        "evpn": {
            "l2": {
                "vni": _blank_evpn_vni(l2_evpn.get("vni", vlan.get("vni"))),
                "rt": l2_evpn.get("rt") or vlan.get("l2_rt") or "",
            },
            "l3": {
                "vni": _blank_evpn_vni(l3_evpn.get("vni", vlan.get("l3_vni"))),
                "rt_import": l3_evpn.get("rt_import") or vlan.get("rt_import") or "",
                "rt_export": l3_evpn.get("rt_export") or vlan.get("rt_export") or "",
            },
        },
        "placement": {
            "participating_leafs": participating,
            "gateway_leafs": gateway_leafs,
            "l2_attached_switches": [
                {
                    "hostname": hostname,
                    "vtep_uplinks": [
                        ("%s %s" % (link["peer"], link["peer_if"])).strip()
                        for group in (placement.get("l2") or {}).get("groups") or []
                        if hostname in group["members"]
                        for link in group["vtep_links"]
                        if link["member"] == hostname
                    ],
                }
                for hostname in placement.get("l2_attached_order") or []
            ],
            "prune_eligible_leafs": prune_eligible,
            "source_gateway_devices": source_gateways,
        },
    }
    if not static_route_tags:
        model["routing"].pop("static_route_tags", None)
    attach_associated_routes(model, vlan.get("id"), devices)
    return model


def build_vlan_placement(vlan, devices):
    """Classify discovered switches and return the deployment model plus analysis.

    ``devices`` are the per-switch discovery dicts already normalized by
    ``build_vlan_discovery_reports``.
    """
    if vlan is None:
        vlan = {}
    if not isinstance(vlan, dict):
        raise AnsibleFilterError(
            "build_vlan_placement expects a VLAN dict, got %s" % type(vlan)
        )
    if not isinstance(devices, list):
        raise AnsibleFilterError(
            "build_vlan_placement expects a device list, got %s" % type(devices)
        )

    gateway_declared = _explicit_gateway_leaves(vlan)
    protected_vlan = _protected_vlan(vlan)
    protected_hosts = _protected_hosts(vlan)
    gateway = assess_gateway(vlan, devices)
    migration_type, migration_reason = resolve_migration_type(
        vlan, list(gateway_declared), gateway
    )
    if migration_type != gateway["recommended_migration_type"]:
        gateway["warnings"].insert(
            0,
            "The record says %s but discovery recommends %s: %s."
            % (migration_type.upper(), gateway["recommended_migration_type"].upper(), gateway["reason"]),
        )
    if migration_type == "l3":
        if not gateway["mode"]:
            gateway["warnings"].append(
                "Choose gateway_mode: distributed_anycast (every participating leaf) or "
                "centralized_anycast (a leaf pair)."
            )
        elif gateway["mode"] == "centralized_anycast" and len(gateway_declared) != 2:
            gateway["warnings"].append(
                "centralized_anycast needs exactly two gateway_leafs; %d named."
                % len(gateway_declared)
            )
    if gateway["invalid_mode"]:
        gateway["warnings"].append(
            "gateway_mode %r is not one of %s." % (gateway["invalid_mode"], ", ".join(GATEWAY_MODES))
        )

    by_hostname = {}
    participating_order = []
    l2_attached_order = []
    source_prune_order = []
    other_prune_order = []
    source_gateway_order = []
    analysis_switches = []

    def _add_participating(hostname):
        if hostname not in participating_order:
            participating_order.append(hostname)

    for hostname in gateway_declared:
        _add_participating(hostname)

    evidence_by_host = {}
    for device in devices:
        if isinstance(device, dict) and device.get("hostname"):
            evidence_by_host[device["hostname"]] = local_endpoint_evidence(device)
    l2 = analyze_l2_domains(
        vlan.get("id"),
        devices,
        {host for host, (qualifying, _non) in evidence_by_host.items() if qualifying},
    )
    l2_groups_by_host = {
        host: group for group in l2["groups"] for host in group["members"]
    }

    for device in devices:
        if not isinstance(device, dict):
            continue
        hostname = device.get("hostname") or ""
        if not hostname:
            continue
        qualifying, nonqualifying = evidence_by_host[hostname]
        qualifying = list(qualifying)
        nonqualifying = list(nonqualifying)
        switch_role = l2["roles"].get(hostname, "vtep")
        if switch_role == "vtep":
            # A VTEP that a non-VTEP switch with endpoints hangs off must keep the VLAN.
            qualifying.extend(l2["vtep_attach"].get(hostname) or [])
        elif switch_role == "mls":
            # The MLS is the only path for a non-VTEP group with no VTEP uplink.
            qualifying.extend(l2["mls_attach"].get(hostname) or [])
        is_gateway = hostname in gateway_declared
        has_endpoints = bool(qualifying)
        is_source_gateway = bool(device.get("svi_present"))
        is_protected = protected_vlan or hostname in protected_hosts
        vlan_present = bool(device.get("vlan_present"))

        unknown_ports = list(device.get("unknown_ports") or [])
        if not unknown_ports:
            unknown_ports = [
                item.get("interface")
                for item in (device.get("port_attachments") or [])
                if isinstance(item, dict)
                and item.get("role") == "unknown"
                and item.get("interface")
            ]

        silent_downstream = silent_downstream_switch_ports(device)

        if switch_role == "l2_switch" and hostname in l2["attached_hosts"]:
            # Not a VTEP: keep the VLAN as plain L2 behind a VTEP, never a VNI target.
            role = "l2_attached"
            l2_attached_order.append(hostname)
            if not qualifying:
                group = l2_groups_by_host.get(hostname) or {}
                others = [h for h in group.get("members") or [] if h != hostname]
                qualifying.append(
                    "carries the VLAN between non-VTEP switches %s" % ", ".join(others)
                )
        elif switch_role == "mls" and has_endpoints and not is_gateway:
            # Endpoints still hang off the legacy MLS. Keep the VLAN there until
            # they are re-homed; the MLS is never a VNI target.
            role = "legacy_retained"
        elif is_gateway or has_endpoints:
            role = "participating"
            _add_participating(hostname)
        elif is_protected and vlan_present:
            role = "protected"
        elif vlan_present and silent_downstream:
            role = "review"
            nonqualifying.append(
                "trunk to a switch outside discovery scope, no MACs learned: %s"
                % ", ".join(_ports_with_neighbor(device, silent_downstream))
            )
        elif is_source_gateway and vlan_present:
            # The legacy SVI is the root of the L2 domain, so it is pruned first.
            role = "prune_eligible"
            source_prune_order.append(hostname)
            if unknown_ports:
                nonqualifying.append(
                    "unclassified ports need review: %s" % ", ".join(unknown_ports)
                )
        elif vlan_present and unknown_ports:
            role = "review"
            nonqualifying.append(
                "unclassified ports need review: %s" % ", ".join(unknown_ports)
            )
        elif vlan_present and not has_endpoints:
            role = "prune_eligible"
            other_prune_order.append(hostname)
        else:
            role = "absent"

        if role == "prune_eligible" and is_source_gateway:
            nonqualifying.insert(0, "source gateway, legacy L2 root")

        if is_source_gateway and hostname not in source_gateway_order:
            source_gateway_order.append(hostname)

        evidence = list(qualifying)
        if is_gateway:
            evidence.append("engineer-supplied gateway leaf")

        record = {
            "hostname": hostname,
            "role": role,
            "switch_role": switch_role,
            "participating": role == "participating",
            "prune_eligible": role == "prune_eligible",
            "gateway_leaf": is_gateway,
            "source_gateway": is_source_gateway,
            "protected": is_protected,
            "evidence": evidence,
            "nonqualifying": nonqualifying,
        }
        by_hostname[hostname] = record
        analysis_switches.append(record)

    for hostname in gateway_declared:
        if hostname in by_hostname:
            continue
        record = {
            "hostname": hostname,
            "role": "participating",
            "participating": True,
            "prune_eligible": False,
            "gateway_leaf": True,
            "source_gateway": False,
            "protected": protected_vlan or hostname in protected_hosts,
            "evidence": ["engineer-supplied gateway leaf"],
            "nonqualifying": [],
        }
        by_hostname[hostname] = record
        analysis_switches.append(record)

    placement = {
        "migration_type": migration_type,
        "migration_reason": migration_reason,
        "gateway": gateway,
        "protected_vlan": protected_vlan,
        "gateway_declared": gateway_declared,
        "gateway_order": list(gateway_declared.keys()),
        "participating_order": participating_order,
        "l2_attached_order": l2_attached_order,
        "l2": l2,
        "prune_order": source_prune_order + other_prune_order,
        "source_gateway_order": source_gateway_order,
        "by_hostname": by_hostname,
    }
    model = build_deployment_model(vlan, devices, placement)
    analysis = {
        "migration_type": migration_type,
        "reason": migration_reason,
        "rules": list(_PLACEMENT_RULES),
        "switches": analysis_switches,
    }
    placement["deployment_model"] = model
    placement["analysis"] = analysis
    return placement


def flatten_deployment_model(document):
    """Turn a nested deployment model into the flat VLAN record playbooks load.

    Legacy flat records are returned unchanged. ``l2_l3`` is not a valid
    service type; callers validate the flattened record against the schema.
    """
    if not isinstance(document, dict):
        return document
    site = document.get("site")
    if not isinstance(site, dict) or not isinstance(site.get("vlan"), dict):
        return document

    vlan = site["vlan"]
    routing = document.get("routing") if isinstance(document.get("routing"), dict) else {}
    migration = document.get("migration") if isinstance(document.get("migration"), dict) else {}
    evpn = document.get("evpn") if isinstance(document.get("evpn"), dict) else {}
    l2_evpn = evpn.get("l2") if isinstance(evpn.get("l2"), dict) else {}
    l3_evpn = evpn.get("l3") if isinstance(evpn.get("l3"), dict) else {}
    placement = document.get("placement") if isinstance(document.get("placement"), dict) else {}
    ownership = document.get("ownership") if isinstance(document.get("ownership"), dict) else {}

    service_type = str(migration.get("type") or "l2").strip().lower()
    if service_type == "l2_l3":
        service_type = "l3" if placement.get("gateway_leafs") else "l2"

    participating = [
        hostname
        for hostname in (_hostname_of(item) for item in (placement.get("participating_leafs") or []))
        if hostname
    ]
    gateway_leafs = [
        hostname
        for hostname in (_hostname_of(item) for item in (placement.get("gateway_leafs") or []))
        if hostname
    ]
    discovery = []
    for key in (
        "participating_leafs",
        "l2_attached_switches",
        "prune_eligible_leafs",
        "source_gateway_devices",
        "gateway_leafs",
    ):
        for item in placement.get(key) or []:
            hostname = _hostname_of(item)
            if hostname and hostname not in discovery:
                discovery.append(hostname)

    # The VLAN's VNI is the L2 VNI only. The L3 VNI belongs to the VRF; a
    # missing L2 VNI is allocated from the DC's range by discovery.
    vni = _blank_evpn_vni(l2_evpn.get("vni"))
    record = {
        "id": _coerce_int(vlan.get("id")),
        "name": vlan.get("name") or "",
        "action": document.get("action") or "migrate",
        "service_type": service_type,
        "protected_vlan": bool(migration.get("protected_vlan")),
        "vrf": routing.get("vrf") or "default",
        "vni": vni,
        "gateway": routing.get("gateway_ip") or "",
        "prefixes": list(routing.get("prefixes") or []),
        "gateway_leafs": gateway_leafs,
        "target_switches": participating,
        "discovery_switches": discovery,
        "tenant": ownership.get("tenant") or "",
        "owner": ownership.get("owner") or "",
    }
    gateway = document.get("gateway") if isinstance(document.get("gateway"), dict) else {}
    if gateway.get("mode"):
        record["gateway_mode"] = gateway["mode"]
    if gateway.get("location"):
        record["gateway_location"] = gateway["location"]
    if gateway.get("svi_mtu"):
        record["svi_mtu"] = gateway["svi_mtu"]
    if routing.get("secondary_prefixes"):
        record["secondary_prefixes"] = list(routing["secondary_prefixes"])
    if isinstance(routing.get("associated"), dict):
        record["associated"] = routing["associated"]
    static_route_tags = normalize_static_route_tags(routing.get("static_route_tags"))
    if static_route_tags:
        record["static_route_tags"] = static_route_tags
    l3_vni = _blank_evpn_vni(l3_evpn.get("vni"))
    if l3_vni is not None:
        record["l3_vni"] = l3_vni
    if l2_evpn.get("rt"):
        record["l2_rt"] = l2_evpn.get("rt")
    if l3_evpn.get("rt_import"):
        record["rt_import"] = l3_evpn.get("rt_import")
    if l3_evpn.get("rt_export"):
        record["rt_export"] = l3_evpn.get("rt_export")
    leaf_bgp = _leaf_bgp_map(placement)
    if leaf_bgp:
        record["leaf_bgp"] = leaf_bgp
    data_center = site.get("data_center") or ""
    if data_center:
        record["data_center"] = data_center
    description = vlan.get("description") or vlan.get("svi_description") or ""
    if description:
        record["description"] = description
        record["svi_description"] = description
    if "id" not in vlan or vlan.get("id") is None:
        return document
    return record


PRUNE_STATUS_BY_ROLE = {
    "participating": "retained_participating",
    "l2_attached": "retained_l2_attached",
    "legacy_retained": "retained_until_rehomed",
    "protected": "protected",
    "review": "needs_review",
    "absent": "none",
    "gateway_leaf": "retained_gateway",
}
