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

from vlan_lib.parsers import network_prefix, normalize_mac_address

_ENDPOINT_MAC_ROLES = frozenset({"compute", "unknown", "access"})
_UPLINK_MAC_ROLES = frozenset(
    {"switch_uplink", "peer_link", "transit", "trunk", "exempt_trunk"}
)

_PLACEMENT_RULES = [
    "participating_leafs are where the VLAN/VNI must exist after migration.",
    "A switch is participating when it has local endpoint attachment, or when the engineer listed it in gateway_leafs.",
    "VLAN database presence, trunk allowance, uplink-only MAC learning, transit aggregation, and fabric interconnects do not qualify.",
    "prune_eligible_leafs carry the VLAN today and have no local endpoints. Source gateways are included and listed first: they are the root of the legacy L2.",
    "A source gateway that also has local endpoints, or that the engineer named in gateway_leafs, stays participating and is not pruned.",
    "gateway_leafs are explicit. Discovery never infers fabric gateway ownership from an SVI.",
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


def _explicit_migration_type(vlan):
    vlan = vlan or {}
    migration = vlan.get("migration") if isinstance(vlan.get("migration"), dict) else {}
    raw = migration.get("type") or vlan.get("service_type") or ""
    return str(raw).strip().lower().replace("-", "_")


def resolve_migration_type(vlan, gateway_hosts):
    """Return ``(type, reason)`` where type is ``l2`` or ``l3``.

    ``l2_l3`` is deprecated. Gateway-in-fabric is ``l3``. Gateway-outside is
    ``l2``. Discovered SVIs do not change the type.
    """
    explicit = _explicit_migration_type(vlan)
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
    for key in ("compute_ports", "access_ports"):
        for iface in device.get(key) or []:
            if iface and iface not in interfaces:
                interfaces.append(iface)
    if interfaces:
        qualifying.append(
            "endpoint-facing interfaces: %s" % ", ".join(interfaces)
        )

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

    netbox = device.get("netbox_endpoints") or device.get("endpoint_mappings") or []
    if netbox:
        qualifying.append("NetBox endpoint mapping")

    nonqualifying = []
    if _uplink_only_macs(device, endpoint_macs):
        nonqualifying.append("MAC learned only on an uplink")
    transit = [
        item.get("interface")
        for item in (device.get("port_attachments") or [])
        if isinstance(item, dict) and item.get("role") == "transit"
    ]
    if transit and not interfaces and not endpoint_macs:
        nonqualifying.append(
            "fabric interconnect attachment: %s" % ", ".join(transit)
        )
    if not qualifying:
        if device.get("switch_uplink_ports") or device.get("trunk_ports"):
            nonqualifying.append("VLAN allowed on a trunk")
        elif device.get("vlan_present"):
            nonqualifying.append("VLAN present in the VLAN database")
    return qualifying, nonqualifying


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
    """Route-target for ``router bgp / vlan``.

    Prefer the explicit L3 import and export targets. When those are absent,
    use ``<l3_vni>:<vlan id>``. An L2 target is the last fallback.
    """
    if not isinstance(record, dict):
        return {}
    rt_import = str(record.get("rt_import") or "").strip()
    rt_export = str(record.get("rt_export") or "").strip()
    if rt_import and rt_export:
        if rt_import == rt_export:
            return {"rt_both": rt_import}
        return {"rt_import": rt_import, "rt_export": rt_export}
    single = rt_import or rt_export
    if single:
        return {"rt_both": single}
    l3_vni = record.get("l3_vni")
    vlan_id = record.get("id")
    if l3_vni not in (None, "") and vlan_id not in (None, ""):
        return {"rt_both": "%s:%s" % (l3_vni, vlan_id)}
    l2_rt = str(record.get("l2_rt") or "").strip()
    if l2_rt:
        return {"rt_both": l2_rt}
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


def bgp_vlan_evpn(record, hostname, host_asn="", host_router_id=""):
    """EOS ``router bgp / vlan`` values for one VTEP.

    RD is the leaf route distinguisher, or ``<router-id>:<vlan id>``.
    An empty result means this VLAN has no route-target to publish.
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
    }


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
            hit = _ip_in_prefixes(next_hop, prefixes) if vrf_ok else ""
            relation, related = _prefix_relation(prefix, prefixes) if vrf_ok else ("", "")
            if hit:
                reason = "next_hop %s is inside %s" % (next_hop, hit)
            elif relation in ("equal", "more_specific"):
                reason = "prefix %s is inside %s" % (prefix, related)
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
                    first = str(method).split()[0].lower()
                    if first not in ("connected", "static"):
                        continue
                    key = (hostname, vrf_name, "redistribute", first)
                    if key in network_seen:
                        continue
                    network_seen.add(key)
                    networks.append(
                        {
                            "hostname": hostname,
                            "vrf": vrf_name,
                            "kind": "redistribute",
                            "prefix": "",
                            "method": method,
                            "reason": (
                                "SVI is connected in this VRF and BGP redistributes %s. "
                                "The VLAN prefix may be advertised. The VRF can be shared; confirm before migration."
                                % first
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

    associated = {
        "static_routes": statics,
        "bgp_networks": networks,
        "bgp_neighbors": neighbors,
    }
    routing["associated"] = associated
    return associated


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
    migration_type, migration_reason = resolve_migration_type(
        vlan, list(gateway_declared)
    )

    by_hostname = {}
    participating_order = []
    source_prune_order = []
    other_prune_order = []
    source_gateway_order = []
    analysis_switches = []

    def _add_participating(hostname):
        if hostname not in participating_order:
            participating_order.append(hostname)

    for hostname in gateway_declared:
        _add_participating(hostname)

    for device in devices:
        if not isinstance(device, dict):
            continue
        hostname = device.get("hostname") or ""
        if not hostname:
            continue
        qualifying, nonqualifying = local_endpoint_evidence(device)
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

        if is_gateway or has_endpoints:
            role = "participating"
            _add_participating(hostname)
        elif is_protected and vlan_present:
            role = "protected"
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
        "protected_vlan": protected_vlan,
        "gateway_declared": gateway_declared,
        "gateway_order": list(gateway_declared.keys()),
        "participating_order": participating_order,
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
        "prune_eligible_leafs",
        "source_gateway_devices",
        "gateway_leafs",
    ):
        for item in placement.get(key) or []:
            hostname = _hostname_of(item)
            if hostname and hostname not in discovery:
                discovery.append(hostname)

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
    if "id" not in vlan or vlan.get("id") is None:
        return document
    return record


PRUNE_STATUS_BY_ROLE = {
    "participating": "retained_participating",
    "protected": "protected",
    "review": "needs_review",
    "absent": "none",
    "gateway_leaf": "retained_gateway",
}
