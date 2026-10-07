# -*- coding: utf-8 -*-
"""Normalize discovery into participating leaves, prune-eligible leaves, and L2/L3.

Migration type is only ``l2`` or ``l3``:

* ``l3`` - the gateway is hosted inside the VXLAN fabric (``gateway_leafs``).
* ``l2`` - the gateway stays outside the fabric. The fabric provides Layer-2
  transport. Extra endpoint leaves are normal EVPN placement, not a stretch type.

``gateway_leafs`` are engineer-supplied. An SVI found in discovery is a
``source_gateway`` (legacy MLS / aggPE) and is not a fabric gateway leaf.
"""

from ansible.errors import AnsibleFilterError

from vlan_lib.parsers import normalize_mac_address

_ENDPOINT_MAC_ROLES = frozenset({"compute", "unknown", "access"})
_UPLINK_MAC_ROLES = frozenset(
    {"switch_uplink", "peer_link", "transit", "trunk", "exempt_trunk"}
)

_PLACEMENT_RULES = [
    "participating_leafs are where the VLAN/VNI must exist after migration.",
    "A switch is participating when it has local endpoint attachment, or when the engineer listed it in gateway_leafs.",
    "VLAN database presence, trunk allowance, uplink-only MAC learning, transit aggregation, and fabric interconnects do not qualify.",
    "prune_eligible_leafs carry the VLAN today, have no local endpoints, and are not gateway, participating, source-gateway, or protected.",
    "gateway_leafs are explicit. Discovery never infers fabric gateway ownership from an SVI.",
    "source_gateway_devices are existing MLS or aggPE gateway owners. Do not prune them until gateway migration completes.",
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


def _leaf_record(hostname, declared=None, device=None):
    declared = declared or {}
    device = device or {}
    bgp_as = device.get("bgp_as") or ""
    return {
        "hostname": hostname,
        "asn": declared.get("asn") or bgp_as or "",
        "router_id": declared.get("router_id") or device.get("router_id") or "",
        "rd": declared.get("rd") or "",
        "vtep": declared.get("vtep") or device.get("vtep") or "",
    }


def _host_only(hostname):
    return {"hostname": hostname}


def _blank_evpn_vni(value):
    if value in (None, "", 0, "0"):
        return None
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

    participating = []
    for hostname in placement["participating_order"]:
        participating.append(
            _leaf_record(
                hostname,
                gateway_declared.get(hostname) or {},
                by_host.get(hostname) or {},
            )
        )
    gateway_leafs = [
        _leaf_record(
            hostname,
            gateway_declared.get(hostname) or {},
            by_host.get(hostname) or {},
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
    site_vlan = {}
    site = vlan.get("site") if isinstance(vlan.get("site"), dict) else {}
    if isinstance(site.get("vlan"), dict):
        site_vlan = site["vlan"]

    return {
        "site": {
            "data_center": vlan.get("data_center") or site.get("data_center") or "",
            "vlan": {
                "id": site_vlan.get("id", vlan.get("id")),
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
    prune_order = []
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
        elif is_source_gateway:
            role = "source_gateway"
        elif is_protected and vlan_present:
            role = "protected"
        elif vlan_present and unknown_ports:
            role = "review"
            nonqualifying.append(
                "unclassified ports need review: %s" % ", ".join(unknown_ports)
            )
        elif vlan_present and not has_endpoints:
            role = "prune_eligible"
            prune_order.append(hostname)
        else:
            role = "absent"

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
        "prune_order": prune_order,
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
        "id": vlan.get("id"),
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
    l3_vni = _blank_evpn_vni(l3_evpn.get("vni"))
    if l3_vni is not None:
        record["l3_vni"] = l3_vni
    if l2_evpn.get("rt"):
        record["l2_rt"] = l2_evpn.get("rt")
    if l3_evpn.get("rt_import"):
        record["rt_import"] = l3_evpn.get("rt_import")
    if l3_evpn.get("rt_export"):
        record["rt_export"] = l3_evpn.get("rt_export")
    data_center = site.get("data_center") or ""
    if data_center:
        record["data_center"] = data_center
    if "id" not in vlan or vlan.get("id") is None:
        return document
    return record


PRUNE_STATUS_BY_ROLE = {
    "participating": "retained_participating",
    "source_gateway": "held_until_gateway_migration",
    "protected": "protected",
    "review": "needs_review",
    "absent": "none",
    "gateway_leaf": "retained_gateway",
}
