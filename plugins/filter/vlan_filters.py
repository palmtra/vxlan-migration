#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 filters for VLAN lifecycle playbooks."""

import re

from ansible.errors import AnsibleFilterError

_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9_-]+")


def select_vlans_for_run(vlans, target_vlan_ids=None, target_actions=None):
    """Filter the canonical VLAN registry for the current run.

    Args:
        vlans: list of VLAN dictionaries (must contain 'id' and 'action')
        target_vlan_ids: optional list of VLAN IDs to include
        target_actions: optional list of actions (e.g. ['migrate'])

    Returns:
        Filtered list of VLAN dictionaries.
    """
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "select_vlans_for_run expects a list, got %s" % type(vlans)
        )

    target_vlan_ids = [str(vid) for vid in (target_vlan_ids or [])]
    target_actions = target_actions or []

    def _keep(vlan):
        if target_vlan_ids and str(vlan.get("id")) not in target_vlan_ids:
            return False
        if target_actions and vlan.get("action") not in target_actions:
            return False
        return True

    return [vlan for vlan in vlans if _keep(vlan)]


_MAC_ADDRESS_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{2}[:.\-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F])"
)
_IP_ADDRESS_RE = re.compile(
    r"\b(?!(?:0\.|255\.))(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
)
_TRUNK_ALLOWED_VLAN_RE = re.compile(
    r"^\s*switchport trunk allowed vlan"
    r"(?: (?P<modifier>add|remove|except))?"
    r"(?: (?P<vlans>.+))?\s*$",
    re.I | re.M,
)
_SVI_VRF_PATTERNS = {
    "eos": (
        re.compile(r"^\s*vrf\s+forwarding\s+(\S+)", re.I | re.M),
        re.compile(r"^\s*ip\s+vrf\s+forwarding\s+(\S+)", re.I | re.M),
    ),
    "nxos": (
        re.compile(r"^\s*vrf\s+member\s+(\S+)", re.I | re.M),
        re.compile(r"^\s*ip\s+vrf\s+(\S+)", re.I | re.M),
    ),
    "ios": (
        re.compile(r"^\s*vrf\s+forwarding\s+(\S+)", re.I | re.M),
    ),
}
_TRUNK_INTERFACE_NAME_RES = {
    "eos": re.compile(
        r"^(?P<iface>(?:Eth|Ethernet|Po|Port-Channel)\S+)\s+\S+\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
    "nxos": re.compile(
        r"^(?P<iface>(?:Eth|Ethernet|po|Po|port-channel)\S+)\s+\d+\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
    "ios": re.compile(
        r"^(?P<iface>(?:Gi|GigabitEthernet|Te|TenGigabitEthernet|Po|Port-channel)\S+)\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
}
_VLAN_NOT_FOUND_MARKERS = (
    "does not exist",
    "not found",
    "invalid vlan",
    "no vlan",
    "vlan not configured",
    "invalid input",
)


def _output_indicates_missing(stdout):
    if not stdout or not str(stdout).strip():
        return True
    lower = str(stdout).lower()
    return any(marker in lower for marker in _VLAN_NOT_FOUND_MARKERS)


def vlan_id_command_indicates_present(stdout, vlan_id):
    """Return True when platform 'show vlan id' style output confirms the VLAN."""
    if _output_indicates_missing(stdout):
        return False
    vid = re.escape(str(vlan_id))
    patterns = (
        rf"^\s*{vid}\s+\S+",  # EOS/NXOS table row starting with VLAN ID
        rf"\bvid\s*:?\s*{vid}\b",
        rf"\bvlan\s*{vid}\b",
        rf"\bvlan{vid}\b",
    )
    text = str(stdout)
    return any(re.search(pattern, text, re.I | re.M) for pattern in patterns)


_VLAN_STATUS_RE = re.compile(
    r"\b(active|inactive|suspend(?:ed)?|act/unsup|unsupport(?:ed)?)\b",
    re.I,
)
_PORT_NAME_RE = re.compile(
    r"(?:"
    r"Ethernet[\w./-]+|Et[\d./-]+|"
    r"Port-Channel[\d./-]+|Po[\d./-]+|"
    r"GigabitEthernet[\w./-]+|Gi[\d./-]+|"
    r"TenGigabitEthernet[\w./-]+|Te[\d./-]+|"
    r"FastEthernet[\w./-]+|Fa[\d./-]+|"
    r"Eth[\w./-]+"
    r")",
    re.I,
)


def _normalize_interface_name(name, platform="eos"):
    """Normalize interface names so vlan-id ports can match trunk summaries."""
    normalized = re.sub(r"\s+", "", str(name).strip().lower())
    prefixes = (
        ("port-channel", "po"),
        ("portchannel", "po"),
        ("tengigabitethernet", "te"),
        ("gigabitethernet", "gi"),
        ("fastethernet", "fa"),
        ("ethernet", "et"),
    )
    for prefix, short in prefixes:
        if normalized.startswith(prefix):
            return short + normalized[len(prefix):]
    return normalized


def _parse_vlan_row_ports_fragment(row_body):
    """Return the Ports column fragment from a show vlan id table row body."""
    status_match = _VLAN_STATUS_RE.search(row_body)
    if not status_match:
        return ""
    ports_fragment = row_body[status_match.end():].strip()
    if ports_fragment.lower() in {"", "-", "none", "n/a"}:
        return ""
    return ports_fragment


def _collect_vlan_id_port_fragments(vlan_output, vlan_id):
    """Collect Ports column text for a VLAN from show vlan id output."""
    text = str(vlan_output or "")
    vid = str(int(vlan_id))
    fragments = []
    collecting = False

    for line in text.splitlines():
        row_match = re.match(rf"^\s*{re.escape(vid)}\s+(?P<body>.+)$", line)
        if row_match:
            collecting = True
            fragment = _parse_vlan_row_ports_fragment(row_match.group("body"))
            if fragment:
                fragments.append(fragment)
            continue

        if not collecting:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^\d+\s+\S", stripped):
            break
        if re.match(r"^-+$", stripped):
            continue
        if stripped.lower().startswith(("vlan", "----")):
            continue
        fragments.append(stripped)

    return fragments


def _extract_port_names_from_fragments(fragments):
    """Extract unique interface names from one or more Ports column fragments."""
    ports = []
    seen = set()
    for fragment in fragments or []:
        for match in _PORT_NAME_RE.finditer(fragment):
            port = match.group(0)
            key = port.lower()
            if key in seen:
                continue
            seen.add(key)
            ports.append(port)
    return ports


def parse_vlan_id_ports(
    vlan_output,
    vlan_id,
    platform="eos",
    trunk_interface_names=None,
    exempt_trunk_interface_names=None,
):
    """Parse show vlan id output into access vs trunk port membership.

    Uses the VLAN Ports column as the source of truth. An empty Ports field means
    the VLAN exists but has no access or trunk attachment on the switch. Trunk
    membership is determined by cross-referencing port names with show interfaces
    trunk (interface names only).

    MLAG/VPC peer trunks (EOS mlagpeer, NXOS vpc peer-link) are tracked separately
    in exempt_trunk_ports and are excluded from trunk_ports maintenance candidates.
    """
    trunk_names = trunk_interface_names or []
    trunk_set = {
        _normalize_interface_name(name, platform)
        for name in trunk_names
        if str(name).strip()
    }
    exempt_set = {
        _normalize_interface_name(name, platform)
        for name in (exempt_trunk_interface_names or [])
        if str(name).strip()
    }

    if _output_indicates_missing(vlan_output):
        return {
            "vlan_present": False,
            "has_port_membership": False,
            "ports_raw": "",
            "all_ports": [],
            "access_ports": [],
            "trunk_ports": [],
            "exempt_trunk_ports": [],
        }

    vlan_present = vlan_id_command_indicates_present(vlan_output, vlan_id)
    fragments = _collect_vlan_id_port_fragments(vlan_output, vlan_id)
    ports_raw = ", ".join(fragments)
    all_ports = _extract_port_names_from_fragments(fragments)

    access_ports = []
    trunk_ports = []
    exempt_trunk_ports = []
    for port in all_ports:
        normalized = _normalize_interface_name(port, platform)
        if normalized in trunk_set:
            if normalized in exempt_set:
                exempt_trunk_ports.append(port)
            else:
                trunk_ports.append(port)
        else:
            access_ports.append(port)

    return {
        "vlan_present": vlan_present,
        "has_port_membership": bool(all_ports),
        "ports_raw": ports_raw,
        "all_ports": all_ports,
        "access_ports": access_ports,
        "trunk_ports": trunk_ports,
        "exempt_trunk_ports": exempt_trunk_ports,
    }


def trunk_interface_is_maintenance_exempt(interface_config, platform="eos"):
    """Return True when a trunk must not be flagged for VLAN prune/maintenance."""
    text = str(interface_config or "")
    plat = str(platform).lower()
    if plat == "eos":
        return bool(
            re.search(
                r"^\s*switchport\s+trunk\s+group\s+mlagpeer\b",
                text,
                re.I | re.M,
            )
        )
    if plat == "nxos":
        return bool(re.search(r"^\s*vpc\s+peer-link\b", text, re.I | re.M))
    return False


def build_trunk_exempt_interface_names(trunk_config_results, platform="eos"):
    """Return trunk interfaces excluded from maintenance (MLAG/VPC peer-link)."""
    exempt = []
    for entry in trunk_config_results or []:
        if not isinstance(entry, dict):
            continue
        config = entry.get("stdout") or entry.get("config") or ""
        item = entry.get("item")
        if isinstance(item, dict):
            iface = item.get("interface", "")
        else:
            iface = entry.get("interface", "")
        if iface and trunk_interface_is_maintenance_exempt(config, platform):
            exempt.append(iface)
    return exempt


def trunk_interface_names(show_trunk_output, platform="eos"):
    """Return trunk interface names from show interfaces trunk output."""
    return [
        entry.get("interface", "")
        for entry in parse_trunk_interfaces(show_trunk_output, platform)
        if entry.get("interface")
    ]


def svi_command_indicates_present(stdout, vlan_id):
    """Return True when 'show run interface VlanX' (or equivalent) shows an SVI."""
    if _output_indicates_missing(stdout):
        return False
    vid = re.escape(str(vlan_id))
    text = str(stdout)
    patterns = (
        rf"interface\s+Vlan{vid}\b",
        rf"interface\s+vlan{vid}\b",
        rf"interface\s+Vlan\s*{vid}\b",
    )
    if any(re.search(pattern, text, re.I) for pattern in patterns):
        return True
    # Brief output fallback: configured SVI line without full running-config.
    return bool(re.search(rf"\bVlan{vid}\b", text, re.I))


def mac_table_has_learned_addresses(mac_output):
    """Return True when the MAC table output contains at least one learned MAC."""
    if not mac_output or not str(mac_output).strip():
        return False
    text = str(mac_output)
    for line in text.splitlines():
        lower = line.lower()
        if any(
            skip in lower
            for skip in (
                "mac address",
                "----",
                "total mac",
                "multicast",
                "router",
                "cpu",
                "last move",
                "moves",
            )
        ):
            continue
        if _MAC_ADDRESS_RE.search(line):
            return True
    return False


def arp_table_has_learned_neighbors(arp_output, vlan_id=None):
    """Return True when ARP output contains host IP entries for the VLAN SVI."""
    if not arp_output or not str(arp_output).strip():
        return False
    text = str(arp_output)
    if vlan_id is not None and not _output_indicates_missing(text):
        vid = re.escape(str(vlan_id))
        if re.search(rf"\bVlan{vid}\b", text, re.I):
            return bool(_IP_ADDRESS_RE.search(text))
    return bool(_IP_ADDRESS_RE.search(text))


def parse_svi_vrf(svi_config, platform="eos"):
    """Extract VRF name bound to an SVI from its running-config snippet."""
    if not svi_config:
        return "default"
    text = str(svi_config)
    patterns = _SVI_VRF_PATTERNS.get(str(platform).lower(), _SVI_VRF_PATTERNS["eos"])
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return "default"


_SVI_DESC_RE = re.compile(r"^\s*description\s+(.+?)\s*$", re.I | re.M)
_SVI_MTU_RE = re.compile(r"^\s*mtu\s+(\d+)\s*$", re.I | re.M)
_SVI_IP_RE = re.compile(
    r"^\s*ip\s+address\s+(\d+\.\d+\.\d+\.\d+/\d+)(?:\s+secondary)?\s*$",
    re.I | re.M,
)
_SVI_VR_ADDR_RE = re.compile(
    r"^\s*ip\s+virtual-router\s+address\s+(\S+)\s*$",
    re.I | re.M,
)
_IP_ROUTE_RE = re.compile(
    r"^\s*ip\s+route(?:\s+vrf\s+(?P<vrf>\S+))?\s+"
    r"(?P<prefix>\S+)\s+(?P<nexthop>\S+)"
    r"(?:.*?\s+name\s+(?P<name>\S+))?",
    re.I | re.M,
)
_BGP_ROUTER_RE = re.compile(r"^\s*router\s+bgp\s+(\d+)\s*$", re.I)
_BGP_VRF_RE = re.compile(r"^\s*vrf\s+(\S+)\s*$", re.I)
_BGP_NEIGHBOR_ATTR_RE = re.compile(
    r"^\s*neighbor\s+(?P<neighbor>\S+)\s+(?P<attr>remote-as|update-source|description|route-map)\s+(?P<value>.+?)\s*$",
    re.I,
)


def parse_svi_details(svi_config, platform="eos"):
    """Parse structured fields from an SVI running-config snippet."""
    if not svi_config or _output_indicates_missing(svi_config):
        return {
            "present": False,
            "description": "",
            "mtu": None,
            "vrf": "",
            "ip_addresses": [],
            "virtual_router_addresses": [],
        }

    text = str(svi_config)
    desc_match = _SVI_DESC_RE.search(text)
    mtu_match = _SVI_MTU_RE.search(text)
    return {
        "present": True,
        "description": desc_match.group(1).strip() if desc_match else "",
        "mtu": int(mtu_match.group(1)) if mtu_match else None,
        "vrf": parse_svi_vrf(text, platform),
        "ip_addresses": [match.group(1) for match in _SVI_IP_RE.finditer(text)],
        "virtual_router_addresses": [
            match.group(1) for match in _SVI_VR_ADDR_RE.finditer(text)
        ],
    }


def parse_ip_route_statics(route_config):
    """Parse static `ip route [vrf X] ...` lines from running-config output."""
    if not route_config or not str(route_config).strip():
        return []

    routes = []
    seen = set()
    for match in _IP_ROUTE_RE.finditer(str(route_config)):
        route = {
            "vrf": match.group("vrf") or "default",
            "prefix": match.group("prefix"),
            "next_hop": match.group("nexthop"),
            "name": match.group("name") or "",
        }
        key = (
            route["vrf"],
            route["prefix"],
            route["next_hop"],
            route["name"],
        )
        if key in seen:
            continue
        seen.add(key)
        routes.append(route)
    return routes


def parse_bgp_neighbors(bgp_config):
    """Parse VRF-scoped BGP neighbors from `show run section router bgp` output."""
    if not bgp_config or not str(bgp_config).strip():
        return []

    neighbors = {}
    current_vrf = "default"
    bgp_as = ""

    for raw_line in str(bgp_config).splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("!"):
            continue

        router_match = _BGP_ROUTER_RE.match(line)
        if router_match:
            bgp_as = router_match.group(1)
            current_vrf = "default"
            continue

        # Ignore address-family nesting for activation; keep last seen VRF.
        if re.match(r"^\s*address-family\b", line, re.I):
            continue

        vrf_match = _BGP_VRF_RE.match(line)
        if vrf_match and not re.match(r"^\s*neighbor\b", line, re.I):
            # Only treat top-ish `vrf NAME` stanzas (not route-target lines).
            indent = len(line) - len(line.lstrip())
            if indent <= 3:
                current_vrf = vrf_match.group(1)
            continue

        attr_match = _BGP_NEIGHBOR_ATTR_RE.match(line)
        if not attr_match:
            continue

        neighbor = attr_match.group("neighbor")
        attr = attr_match.group("attr").lower()
        value = attr_match.group("value").strip()
        key = (current_vrf, neighbor)
        entry = neighbors.setdefault(
            key,
            {
                "vrf": current_vrf,
                "neighbor": neighbor,
                "bgp_as": bgp_as,
                "remote_as": "",
                "update_source": "",
                "description": "",
                "route_maps": [],
            },
        )
        if attr == "remote-as":
            entry["remote_as"] = value
        elif attr == "update-source":
            entry["update_source"] = value
        elif attr == "description":
            entry["description"] = value
        elif attr == "route-map":
            entry["route_maps"].append(value)

    return list(neighbors.values())


def normalize_prune_retain(retain):
    """Normalize service prune.retain into comparable sets."""
    retain = retain or {}
    if not isinstance(retain, dict):
        raise AnsibleFilterError(
            "normalize_prune_retain expects a dict, got %s" % type(retain)
        )

    vrfs = {str(item) for item in (retain.get("vrfs") or []) if item}
    interfaces = {str(item) for item in (retain.get("interfaces") or []) if item}
    neighbors = set()
    for item in retain.get("bgp_neighbors") or []:
        if not isinstance(item, dict):
            continue
        vrf = str(item.get("vrf") or "default")
        neighbor = str(item.get("neighbor") or "")
        if neighbor:
            neighbors.add((vrf, neighbor))
    static_names = {
        str(item) for item in (retain.get("static_route_names") or []) if item
    }
    return {
        "vrfs": vrfs,
        "interfaces": interfaces,
        "bgp_neighbors": neighbors,
        "static_route_names": static_names,
    }


def _prune_action(op, **fields):
    action = {"op": op}
    action.update(fields)
    return action


def build_device_prune_plan(
    hostname,
    per_vlan,
    retain=None,
    static_routes=None,
    bgp_neighbors=None,
    vlan_ids=None,
):
    """Build a retain-aware prune candidate plan for one device (read-only)."""
    if not isinstance(per_vlan, list):
        raise AnsibleFilterError(
            "build_device_prune_plan expects per_vlan list, got %s" % type(per_vlan)
        )

    retain_norm = normalize_prune_retain(retain)
    wanted = None
    if vlan_ids is not None:
        wanted = {str(vid) for vid in vlan_ids}

    actions = []
    blocked = []
    svi_vrfs = set()
    vlan_id_list = []

    for entry in per_vlan:
        if not isinstance(entry, dict):
            continue
        vlan_id = entry.get("vlan_id")
        if vlan_id is None:
            continue
        if wanted is not None and str(vlan_id) not in wanted:
            continue

        vlan_id_list.append(int(vlan_id))
        svi_vrf = entry.get("svi_vrf") or (entry.get("svi_details") or {}).get("vrf") or ""
        if svi_vrf:
            svi_vrfs.add(str(svi_vrf))

        for iface in entry.get("trunk_ports") or []:
            actions.append(
                _prune_action(
                    "trunk_remove_vlans",
                    interface=iface,
                    vlans=[int(vlan_id)],
                    source="discovery",
                    confidence="high",
                )
            )

        if entry.get("vlan_present"):
            actions.append(
                _prune_action(
                    "no_vlan",
                    vlan_id=int(vlan_id),
                    source="discovery",
                    confidence="high",
                )
            )

        if entry.get("svi_present"):
            actions.append(
                _prune_action(
                    "no_interface_vlan",
                    vlan_id=int(vlan_id),
                    vrf=svi_vrf or "",
                    source="discovery",
                    confidence="high",
                )
            )

    # Consolidate trunk removals per interface.
    trunk_map = {}
    other_actions = []
    for action in actions:
        if action["op"] != "trunk_remove_vlans":
            other_actions.append(action)
            continue
        iface = action["interface"]
        trunk_map.setdefault(iface, set()).update(action.get("vlans") or [])
    consolidated = []
    for iface, vlans in sorted(trunk_map.items()):
        consolidated.append(
            _prune_action(
                "trunk_remove_vlans",
                interface=iface,
                vlans=sorted(vlans),
                source="discovery",
                confidence="high",
            )
        )
    actions = consolidated + other_actions

    for route in static_routes or []:
        vrf = str(route.get("vrf") or "default")
        if svi_vrfs and vrf not in svi_vrfs:
            continue
        action = _prune_action(
            "no_ip_route",
            vrf=vrf,
            prefix=route.get("prefix", ""),
            next_hop=route.get("next_hop", ""),
            name=route.get("name", ""),
            source="discovery",
            confidence="medium",
        )
        route_name = route.get("name") or ""
        if vrf in retain_norm["vrfs"] or (
            route_name and route_name in retain_norm["static_route_names"]
        ):
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    for neighbor in bgp_neighbors or []:
        vrf = str(neighbor.get("vrf") or "default")
        neighbor_ip = str(neighbor.get("neighbor") or "")
        if not neighbor_ip:
            continue
        if svi_vrfs and vrf not in svi_vrfs:
            continue
        action = _prune_action(
            "no_bgp_neighbor",
            vrf=vrf,
            neighbor=neighbor_ip,
            remote_as=neighbor.get("remote_as", ""),
            update_source=neighbor.get("update_source", ""),
            description=neighbor.get("description", ""),
            route_maps=neighbor.get("route_maps", []),
            source="discovery",
            confidence="medium",
        )
        if (vrf, neighbor_ip) in retain_norm["bgp_neighbors"] or vrf in retain_norm[
            "vrfs"
        ]:
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    for vrf in sorted(svi_vrfs):
        if vrf in ("", "default"):
            continue
        action = _prune_action(
            "no_vrf",
            name=vrf,
            source="discovery",
            confidence="low",
            note="Only safe after all member VLANs/SVIs/statics for this VRF are removed",
        )
        if vrf in retain_norm["vrfs"]:
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    return {
        "hostname": hostname,
        "vlan_ids": sorted(set(vlan_id_list)),
        "svi_vrfs": sorted(svi_vrfs),
        "actions": actions,
        "blocked_by_retain": blocked,
        "retain": {
            "vrfs": sorted(retain_norm["vrfs"]),
            "bgp_neighbors": [
                {"vrf": vrf, "neighbor": neighbor}
                for vrf, neighbor in sorted(retain_norm["bgp_neighbors"])
            ],
            "interfaces": sorted(retain_norm["interfaces"]),
            "static_route_names": sorted(retain_norm["static_route_names"]),
        },
        "destructive": False,
        "status": "candidate",
    }


def arp_discovery_command(platform, vlan_id, svi_vrf=None, svi_present=False):
    """Build a platform-appropriate ARP command for an SVI in its VRF."""
    if not svi_present:
        return ""
    vrf = svi_vrf or "default"
    plat = str(platform).lower()
    vid = int(vlan_id)
    if plat == "eos":
        return f"show ip arp vrf {vrf} | include Vlan{vid}"
    if plat == "nxos":
        return f"show ip arp vrf {vrf} vlan {vid}"
    if plat == "ios":
        return f"show ip arp vrf {vrf} | include Vlan{vid}"
    return f"show ip arp vrf {vrf}"


def mac_discovery_command(platform, vlan_id):
    """Build a platform-appropriate dynamic MAC table command for a VLAN."""
    vid = int(vlan_id)
    plat = str(platform).lower()
    if plat in ("eos", "nxos", "ios"):
        return f"show mac address-table dynamic vlan {vid}"
    return f"show mac address-table vlan {vid}"


def expand_vlan_spec(spec):
    """Expand a VLAN list/range token string into a set of integer VLAN IDs."""
    result = set()
    if not spec:
        return result
    for token in str(spec).split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_str, end_str = token.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            if start > end:
                start, end = end, start
            result.update(range(start, end + 1))
        else:
            result.add(int(token))
    return result


def _allowed_summary_is_all(allowed_summary):
    if not allowed_summary:
        return False
    normalized = str(allowed_summary).strip().lower()
    if normalized in {"all", "none"}:
        return normalized == "all"
    if normalized in {"1-4094", "1-4095", "1 - 4094", "1 - 4095"}:
        return True
    expanded = expand_vlan_spec(normalized)
    return len(expanded) >= 4090


def parse_trunk_interfaces(show_trunk_output, platform="eos"):
    """Parse `show interfaces trunk` into interface names and allowed summaries."""
    text = str(show_trunk_output or "")
    pattern = _TRUNK_INTERFACE_NAME_RES.get(str(platform).lower())
    if not pattern:
        return []
    interfaces = []
    for match in pattern.finditer(text):
        interfaces.append(
            {
                "interface": match.group("iface").strip(),
                "allowed_summary": match.group("allowed").strip(),
            }
        )
    return interfaces


def _trunk_entry(
    interface,
    pruning_mode,
    vlan_carried,
    cleanup_recommended,
    reason,
    allowed_summary="",
    config_line="",
):
    """Build a read-only trunk discovery record."""
    recommendation = reason
    if cleanup_recommended:
        recommendation = (
            f"{reason} Plan trunk cleanup during legacy decommission "
            f"(see decomm-vlan); discovery does not generate config commands."
        )
    return {
        "interface": interface,
        "pruning_mode": pruning_mode,
        "vlan_carried": vlan_carried,
        "cleanup_recommended": cleanup_recommended,
        "allowed_summary": allowed_summary,
        "config_line": config_line,
        "reason": reason,
        "recommendation": recommendation,
    }


def analyze_trunk_vlan_carriage(vlan_id, trunk_interfaces, platform="eos"):
    """Report whether a VLAN is carried on each trunk (read-only discovery).

    Does not generate configuration commands. When the VLAN is explicitly
    allowed on a pruned trunk, sets cleanup_recommended for decommission planning.
    """
    vid = int(vlan_id)
    results = []

    for trunk in trunk_interfaces or []:
        iface = trunk.get("interface", "")
        config = str(trunk.get("config", "") or "")
        allowed_summary = str(trunk.get("allowed_summary", "") or "").strip()
        matches = list(_TRUNK_ALLOWED_VLAN_RE.finditer(config))
        config_line = matches[-1].group(0).strip() if matches else ""

        if matches:
            modifier = None
            vlan_tokens = ""
            for match in matches:
                modifier = (match.group("modifier") or "").lower() or modifier
                vlan_tokens = match.group("vlans") or vlan_tokens

            if modifier == "except":
                except_set = expand_vlan_spec(vlan_tokens)
                if vid in except_set:
                    results.append(
                        _trunk_entry(
                            iface,
                            "except",
                            False,
                            False,
                            "VLAN is excluded on this except-style trunk.",
                            allowed_summary,
                            config_line,
                        )
                    )
                else:
                    results.append(
                        _trunk_entry(
                            iface,
                            "except",
                            True,
                            True,
                            "VLAN is allowed on an except-style trunk (not in the except list).",
                            allowed_summary,
                            config_line,
                        )
                    )
                continue

            allowed_set = expand_vlan_spec(vlan_tokens)
            if not allowed_set:
                results.append(
                    _trunk_entry(
                        iface,
                        "none",
                        False,
                        False,
                        "Trunk allowed VLAN list is empty.",
                        allowed_summary,
                        config_line,
                    )
                )
                continue

            if vid in allowed_set:
                results.append(
                    _trunk_entry(
                        iface,
                        "allow_list",
                        True,
                        True,
                        "VLAN is explicitly listed in the trunk allowed VLAN set.",
                        allowed_summary,
                        config_line,
                    )
                )
            else:
                results.append(
                    _trunk_entry(
                        iface,
                        "allow_list",
                        False,
                        False,
                        "VLAN is not in the trunk allowed VLAN set.",
                        allowed_summary,
                        config_line,
                    )
                )
            continue

        if _allowed_summary_is_all(allowed_summary):
            results.append(
                _trunk_entry(
                    iface,
                    "all",
                    True,
                    False,
                    "Trunk allows all VLANs (no explicit allowed list).",
                    allowed_summary,
                )
            )
            continue

        if not allowed_summary:
            results.append(
                _trunk_entry(
                    iface,
                    "all",
                    True,
                    False,
                    "No switchport trunk allowed vlan statement (default: all VLANs).",
                    allowed_summary,
                )
            )
            continue

        summary = allowed_summary.lower()
        if summary == "none":
            results.append(
                _trunk_entry(
                    iface,
                    "none",
                    False,
                    False,
                    "Trunk allows no VLANs.",
                    allowed_summary,
                )
            )
            continue

        explicit = expand_vlan_spec(summary)
        if vid in explicit:
            results.append(
                _trunk_entry(
                    iface,
                    "allow_list",
                    True,
                    True,
                    "VLAN appears in the trunk allowed VLAN summary from show interfaces trunk.",
                    allowed_summary,
                )
            )
        else:
            results.append(
                _trunk_entry(
                    iface,
                    "allow_list",
                    False,
                    False,
                    "VLAN is not in the trunk allowed VLAN summary.",
                    allowed_summary,
                )
            )

    return results


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
    mac_learned = mac_table_has_learned_addresses(mac_output)
    arp_learned = arp_table_has_learned_neighbors(arp_output, vlan_id) if svi_present else False

    access_ports = port_membership.get("access_ports", [])
    trunk_ports = port_membership.get("trunk_ports", [])
    exempt_trunk_ports = port_membership.get("exempt_trunk_ports", [])
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
        "all_ports": port_membership.get("all_ports", []),
        "ports_raw": port_membership.get("ports_raw", ""),
        "mac_learned": mac_learned,
        "arp_learned": arp_learned,
        "vlan_id_raw": vlan_output,
        "svi_raw": svi_output,
        "mac_table_raw": mac_output,
        "arp_raw": arp_output,
        "trunk_cleanup_recommendations": trunk_cleanup_recommendations,
    }


def _vlan_present(vlan_id, output):
    """Legacy helper — prefer vlan_id_command_indicates_present for discovery."""
    return vlan_id_command_indicates_present(output, vlan_id)


def _str_equal(a, b):
    """Compare two identifiers that may be ints or strings."""
    return str(a) == str(b)


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


def sanitize_report_slug(value):
    """Return a filesystem-safe slug for VLAN-centric report filenames."""
    if value is None:
        return "unknown_vlan"
    slug = _SLUG_INVALID.sub("_", str(value).strip())
    slug = slug.strip("_")
    return slug or "unknown_vlan"


_VLAN_DB_SKIP_FILENAMES = frozenset({"_example.yml", "example.yml"})


def vlan_db_file_prefix(vlan_id):
    """Return the required filename prefix for a VLAN ID (e.g. 100 -> '0100_')."""
    return "%04d_" % int(vlan_id)


def vlan_db_record_filename(vlan_id, name):
    """Return the canonical per-DC VLAN DB filename for a record."""
    return "%s%s.yml" % (vlan_db_file_prefix(vlan_id), sanitize_report_slug(name))


def coalesce_trimmed(*values):
    """Return the first non-empty string after trim (treats None/'' as missing).

    Accepts either variadic arguments or a single list/tuple (Jinja list pipe).
    """
    candidates = []
    for value in values:
        if isinstance(value, (list, tuple)):
            candidates.extend(value)
        else:
            candidates.append(value)
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "undefined":
            return text
    return ""


def resolve_data_center(manual_data_center="", target_data_center="", data_center=""):
    """Resolve data center from playbook/extra vars (safe for Ansible strict undefined)."""
    return coalesce_trimmed(manual_data_center, target_data_center, data_center)


def normalize_target_vlan_ids(target_vlan_ids=None, manual_vlan_id=None):
    """Normalize CLI extra vars into a list of integer VLAN IDs."""
    if manual_vlan_id is not None and str(manual_vlan_id).strip() != "":
        return [int(manual_vlan_id)]

    if target_vlan_ids is None:
        return []

    if isinstance(target_vlan_ids, list):
        return [int(vid) for vid in target_vlan_ids]

    text = str(target_vlan_ids).strip()
    if not text:
        return []

    if text.startswith("["):
        import json

        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise AnsibleFilterError(
                "target_vlan_ids JSON must be a list, got %s" % type(parsed)
            )
        return [int(vid) for vid in parsed]

    if "," in text:
        return [int(part.strip()) for part in text.split(",") if part.strip()]

    return [int(text)]


def _vlan_db_skip_filename(filename):
    lower = str(filename).lower()
    if lower in _VLAN_DB_SKIP_FILENAMES:
        return True
    return str(filename).startswith("_")


def parse_vlan_record(document, source_name=""):
    """Extract a single VLAN record dict from a loaded YAML document."""
    if not isinstance(document, dict):
        return None

    if document.get("_meta", {}).get("example"):
        return None

    if "vlans" in document:
        entries = document.get("vlans") or []
        if not isinstance(entries, list):
            raise AnsibleFilterError(
                "VLAN DB file %s: 'vlans' must be a list." % source_name
            )
        if len(entries) == 0:
            return None
        if len(entries) > 1:
            raise AnsibleFilterError(
                "VLAN DB file %s: per-VLAN files must contain one record, found %d."
                % (source_name, len(entries))
            )
        document = entries[0]

    if not isinstance(document, dict) or "id" not in document:
        return None

    return document


def load_vlan_db_from_directory(vlan_db_dir, data_center, target_vlan_ids=None):
    """Load VLAN records from vars/vlans/<dc>/*.yml."""
    import os

    try:
        import yaml
    except ImportError as exc:
        raise AnsibleFilterError(
            "PyYAML is required to load the VLAN database: %s" % exc
        ) from exc

    dc = str(data_center).strip()
    if not dc:
        raise AnsibleFilterError("data_center must be a non-empty string.")

    base_dir = os.path.join(str(vlan_db_dir), dc)
    normalized_ids = [str(int(vid)) for vid in (target_vlan_ids or [])]
    prefixes = [vlan_db_file_prefix(vid) for vid in normalized_ids] if normalized_ids else []

    records = []
    sources = []
    seen_ids = {}

    def _ingest_file(path, filename):
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}

        record = parse_vlan_record(document, filename)
        if record is None:
            return

        vlan_id = str(record.get("id"))
        if vlan_id in seen_ids:
            raise AnsibleFilterError(
                "Duplicate VLAN ID %s in %s and %s."
                % (vlan_id, filename, seen_ids[vlan_id])
            )

        seen_ids[vlan_id] = filename
        records.append(record)
        sources.append(path)

    if os.path.isdir(base_dir):
        for filename in sorted(os.listdir(base_dir)):
            if not filename.endswith((".yml", ".yaml")):
                continue
            if _vlan_db_skip_filename(filename):
                continue
            if prefixes and not any(filename.startswith(prefix) for prefix in prefixes):
                continue
            _ingest_file(os.path.join(base_dir, filename), filename)

    records.sort(key=lambda item: int(item.get("id", 0)))

    return {
        "vlans": records,
        "sources": sources,
        "data_center": dc,
        "directory": base_dir,
    }


def load_service_from_directory(service_db_dir, data_center, service_id):
    """Load one service migration bundle from vars/services/<dc>/<service_id>.yml."""
    import os

    try:
        import yaml
    except ImportError as exc:
        raise AnsibleFilterError(
            "PyYAML is required for load_service_from_directory: %s" % exc
        )

    dc = coalesce_trimmed(data_center)
    sid = coalesce_trimmed(service_id)
    if not dc:
        raise AnsibleFilterError("load_service_from_directory requires data_center")
    if not sid:
        raise AnsibleFilterError("load_service_from_directory requires service_id")
    if sid.startswith("_"):
        raise AnsibleFilterError(
            "service_id '%s' is reserved (files starting with _ are examples)" % sid
        )

    base_dir = os.path.join(str(service_db_dir), dc)
    candidates = [
        os.path.join(base_dir, "%s.yml" % sid),
        os.path.join(base_dir, "%s.yaml" % sid),
    ]
    path = next((item for item in candidates if os.path.isfile(item)), None)
    if path is None:
        raise AnsibleFilterError(
            "Service '%s' not found under %s/ (expected %s.yml)"
            % (sid, base_dir, sid)
        )

    with open(path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    if not isinstance(document, dict):
        raise AnsibleFilterError("Service file %s must be a YAML mapping" % path)
    if document.get("_meta", {}).get("example"):
        raise AnsibleFilterError(
            "Service file %s is marked as an example (_meta.example) and cannot be loaded"
            % path
        )

    record = dict(document)
    record.setdefault("id", sid)
    record.setdefault("data_center", dc)
    record["_source_file"] = path
    return record


def service_vlan_ids(service_record):
    """Return integer VLAN IDs declared on a service bundle."""
    if not service_record:
        return []
    ids = []
    for entry in service_record.get("vlans") or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            ids.append(int(entry["id"]))
        elif entry is not None and not isinstance(entry, dict):
            ids.append(int(entry))
    return ids


def service_probe_vlan_records(service_record, existing_vlans=None):
    """Build synthetic VLAN DB records for service VLANs missing from the DC DB."""
    if not service_record:
        return []
    existing_ids = {
        int(item.get("id"))
        for item in (existing_vlans or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    records = []
    for entry in service_record.get("vlans") or []:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        vlan_id = int(entry["id"])
        if vlan_id in existing_ids:
            continue
        name = entry.get("vlan_name") or entry.get("name") or ("vlan_%s" % vlan_id)
        records.append(
            {
                "id": vlan_id,
                "name": sanitize_report_slug(name),
                "vlan_name": name,
                "action": "migrate",
                "service_type": entry.get("service_type") or "l3",
                "vrf": entry.get("vrf") or "default",
                "vni": entry.get("vni"),
                "target_switches": entry.get("target_switches") or [],
                "discovery_switches": entry.get("discovery_switches")
                or entry.get("target_switches")
                or [],
                "_from_service": service_record.get("id", ""),
            }
        )
    return records


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


class FilterModule(object):
    def filters(self):
        return {
            "select_vlans_for_run": select_vlans_for_run,
            "extract_vlan_discovery": extract_vlan_discovery,
            "extract_vlan_targeted_discovery": extract_vlan_targeted_discovery,
            "vlan_id_command_indicates_present": vlan_id_command_indicates_present,
            "parse_vlan_id_ports": parse_vlan_id_ports,
            "trunk_interface_is_maintenance_exempt": trunk_interface_is_maintenance_exempt,
            "build_trunk_exempt_interface_names": build_trunk_exempt_interface_names,
            "trunk_interface_names": trunk_interface_names,
            "svi_command_indicates_present": svi_command_indicates_present,
            "parse_svi_vrf": parse_svi_vrf,
            "parse_svi_details": parse_svi_details,
            "parse_ip_route_statics": parse_ip_route_statics,
            "parse_bgp_neighbors": parse_bgp_neighbors,
            "normalize_prune_retain": normalize_prune_retain,
            "build_device_prune_plan": build_device_prune_plan,
            "load_service_from_directory": load_service_from_directory,
            "service_vlan_ids": service_vlan_ids,
            "service_probe_vlan_records": service_probe_vlan_records,
            "arp_discovery_command": arp_discovery_command,
            "mac_discovery_command": mac_discovery_command,
            "mac_table_has_learned_addresses": mac_table_has_learned_addresses,
            "parse_trunk_interfaces": parse_trunk_interfaces,
            "analyze_trunk_vlan_carriage": analyze_trunk_vlan_carriage,
            "build_vlan_verification": build_vlan_verification,
            "sanitize_report_slug": sanitize_report_slug,
            "build_vlan_discovery_reports": build_vlan_discovery_reports,
            "vlan_db_record_filename": vlan_db_record_filename,
            "vlan_db_file_prefix": vlan_db_file_prefix,
            "normalize_target_vlan_ids": normalize_target_vlan_ids,
            "coalesce_trimmed": coalesce_trimmed,
            "resolve_data_center": resolve_data_center,
            "load_vlan_db_from_directory": load_vlan_db_from_directory,
            "union_vlan_discovery_hosts": union_vlan_discovery_hosts,
        }
