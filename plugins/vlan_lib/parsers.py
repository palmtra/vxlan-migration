# -*- coding: utf-8 -*-
"""CLI parsers for VLAN / SVI / trunk / routing discovery."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import (
    _BGP_NEIGHBOR_ATTR_RE,
    _BGP_ROUTER_RE,
    _BGP_VRF_RE,
    _IP_ADDRESS_RE,
    _IP_ROUTE_RE,
    _MAC_ADDRESS_RE,
    _PORT_NAME_RE,
    _SVI_DESC_RE,
    _SVI_IP_RE,
    _SVI_MTU_RE,
    _SVI_VR_ADDR_RE,
    _SVI_VRF_PATTERNS,
    _TRUNK_ALLOWED_VLAN_RE,
    _TRUNK_INTERFACE_NAME_RES,
    _VLAN_NOT_FOUND_MARKERS,
    _VLAN_STATUS_RE,
    _output_indicates_missing,
    _str_equal
)

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


def normalize_mac_address(mac):
    """Normalize a MAC string to lowercase colon form (aa:bb:cc:dd:ee:ff)."""
    if mac is None:
        return ""
    hex_chars = re.sub(r"[^0-9a-fA-F]", "", str(mac))
    if len(hex_chars) != 12:
        return str(mac).strip().lower()
    pairs = [hex_chars[i : i + 2].lower() for i in range(0, 12, 2)]
    return ":".join(pairs)


def _mac_line_is_noise(line):
    lower = line.lower().strip()
    if not lower:
        return True
    if "----" in lower:
        return True
    if lower.startswith("mac address table") or lower.startswith("legend"):
        return True
    if lower.startswith("codes:"):
        return True
    if "total mac" in lower or "multicast" in lower:
        return True
    if "last move" in lower and not _MAC_ADDRESS_RE.search(line):
        return True
    if lower.startswith("vlan") and "mac address" in lower:
        return True
    if "mac address" in lower and "ports" in lower and not _MAC_ADDRESS_RE.search(line):
        return True
    return False


def parse_mac_address_table(mac_output, vlan_id=None):
    """Parse dynamic/static MAC table rows into structured entries.

    Returns a list of dicts::
        {mac, interface, entry_type, vlan_id}

    Supports EOS dotted MAC tables, NXOS tables, and simplified
    ``aa:bb:cc:dd:ee:ff Port-ChannelN`` report lines.
    """
    if not mac_output or not str(mac_output).strip():
        return []

    entries = []
    seen = set()
    target_vid = str(vlan_id) if vlan_id is not None else None

    for raw_line in str(mac_output).splitlines():
        line = raw_line.strip()
        if not line or _mac_line_is_noise(line):
            continue

        mac_match = _MAC_ADDRESS_RE.search(line)
        if not mac_match:
            continue

        mac = normalize_mac_address(mac_match.group(0))
        remainder = line[: mac_match.start()] + " " + line[mac_match.end() :]
        iface_match = _PORT_NAME_RE.search(remainder)
        if not iface_match:
            continue
        interface = iface_match.group(0)

        entry_vlan = None
        # EOS/NXOS table rows start with optional '*' + VLAN id before the MAC.
        # Do not treat MAC octets (e.g. leading "00:" ) as a VLAN id.
        prefix = line[: mac_match.start()]
        vlan_match = re.match(r"^\*?\s*(\d{1,4})\s*$", prefix.strip())
        if not vlan_match:
            vlan_match = re.match(r"^\*?\s*(\d{1,4})\b", prefix)
        if vlan_match and mac_match.start() > 0:
            entry_vlan = vlan_match.group(1)
        if target_vid is not None and entry_vlan is not None and entry_vlan != target_vid:
            continue

        lower = line.lower()
        if "static" in lower:
            entry_type = "static"
        elif "dynamic" in lower:
            entry_type = "dynamic"
        else:
            entry_type = "learned"

        key = (mac, interface.lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "mac": mac,
                "interface": interface,
                "entry_type": entry_type,
                "vlan_id": int(entry_vlan) if entry_vlan is not None else (
                    int(target_vid) if target_vid is not None else None
                ),
            }
        )

    return entries


def mac_table_has_learned_addresses(mac_output):
    """Return True when the MAC table output contains at least one learned MAC."""
    return bool(parse_mac_address_table(mac_output))


def parse_arp_entries(arp_output, vlan_id=None):
    """Parse ARP table rows into structured entries.

    Returns a list of dicts::
        {ip, mac, interface, age}

    Prefer rows bound to ``Vlan<id>`` when *vlan_id* is provided; if none match,
    fall back to all parseable host ARP rows.
    """
    if not arp_output or not str(arp_output).strip():
        return []

    entries = []
    seen = set()
    vid = str(vlan_id) if vlan_id is not None else None
    vlan_iface_re = re.compile(rf"\bvlan\s*{re.escape(vid)}\b", re.I) if vid else None

    for raw_line in str(arp_output).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("address") or "hardware addr" in lower or "----" in lower:
            continue
        if lower.startswith("total number") or lower.startswith("internet address"):
            if not _IP_ADDRESS_RE.search(line):
                continue

        ip_match = _IP_ADDRESS_RE.search(line)
        mac_match = _MAC_ADDRESS_RE.search(line)
        if not ip_match or not mac_match:
            continue

        ip_addr = ip_match.group(1) if ip_match.lastindex else ip_match.group(0)
        mac = normalize_mac_address(mac_match.group(0))
        iface_match = _PORT_NAME_RE.search(line) or re.search(
            r"\b(Vlan\d+|vlan\d+)\b", line, re.I
        )
        interface = iface_match.group(0) if iface_match else ""

        age = ""
        age_match = re.search(r"\b(\d+|-+)\b", line[ip_match.end() : mac_match.start()])
        if age_match:
            age = age_match.group(1)

        entry = {
            "ip": ip_addr,
            "mac": mac,
            "interface": interface,
            "age": age,
        }
        key = (entry["ip"], entry["mac"], entry["interface"].lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    if vlan_iface_re is not None:
        scoped = [item for item in entries if vlan_iface_re.search(item.get("interface", ""))]
        if scoped:
            return scoped
    return entries


def arp_table_has_learned_neighbors(arp_output, vlan_id=None):
    """Return True when ARP output contains host IP entries for the VLAN SVI."""
    return bool(parse_arp_entries(arp_output, vlan_id))


def classify_mac_port_role(interface, access_ports=None, trunk_ports=None, exempt_trunk_ports=None):
    """Classify a MAC-learned interface as access, trunk, exempt, or unknown."""
    access = access_ports or []
    trunks = trunk_ports or []
    exempt = exempt_trunk_ports or []
    normalized = _normalize_interface_name(interface)

    def _match(candidates):
        for candidate in candidates:
            if _normalize_interface_name(candidate) == normalized:
                return True
        return False

    if _match(exempt):
        return "exempt_trunk"
    if _match(trunks):
        return "trunk"
    if _match(access):
        return "access"
    # Heuristic: Port-Channel / Po without explicit Ports-column match → treat as trunk/uplink
    if re.match(r"^(?:po|port-channel)\d+", normalized):
        return "trunk"
    return "unknown"


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


