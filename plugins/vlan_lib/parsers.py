# -*- coding: utf-8 -*-
"""CLI parsers for VLAN / SVI / trunk / routing discovery."""

import re

from ansible.errors import AnsibleFilterError

from vlan_lib.common import (
    _BGP_AGGREGATE_RE,
    _BGP_INDENTED_ATTR_RE,
    _BGP_NETWORK_RE,
    _BGP_NEIGHBOR_ATTR_RE,
    _BGP_NEIGHBOR_STANZA_RE,
    _BGP_RD_RE,
    _BGP_REDIST_RE,
    _BGP_ROUTER_ID_RE,
    _BGP_ROUTER_RE,
    _BGP_RT_RE,
    _BGP_VLAN_RE,
    _BGP_VRF_RE,
    _IP_ADDRESS_RE,
    _MAC_ADDRESS_RE,
    _PORT_NAME_RE,
    _VRF_CONTEXT_RE,
    _SVI_DESC_RE,
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
        # NXOS show vlan / show interface trunk print Eth1/5; show run prints Ethernet1/5.
        ("eth", "et"),
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
            # NXOS follows the ports table with VLAN Type / Remote SPAN tables.
            break
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


_MLAG_PEER_PORT_RE = re.compile(r"(?<![A-Za-z0-9])P(?:Et|Po)[\d./-]+", re.I)
_VXLAN_MEMBER_RE = re.compile(r"(?<![A-Za-z0-9])Vx\d+\b", re.I)


def parse_vlan_id_name(vlan_output, vlan_id):
    """Return the on-box VLAN name from `show vlan id` when present."""
    vid = str(int(vlan_id))
    for line in str(vlan_output or "").splitlines():
        match = re.match(
            rf"^\s*{re.escape(vid)}\s+(\S+)\s+"
            r"(?:active|inactive|suspend(?:ed)?|act/unsup|unsupport(?:ed)?)\b",
            line,
            re.I,
        )
        if match:
            return match.group(1)
    return ""


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
            "vlan_name_on_box": "",
            "has_port_membership": False,
            "ports_raw": "",
            "all_ports": [],
            "access_ports": [],
            "trunk_ports": [],
            "exempt_trunk_ports": [],
            "mlag_peer_ports": [],
            "vxlan_mapped": False,
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
        "vlan_name_on_box": parse_vlan_id_name(vlan_output, vlan_id) if vlan_present else "",
        "has_port_membership": bool(all_ports),
        "ports_raw": ports_raw,
        "all_ports": all_ports,
        "access_ports": access_ports,
        "trunk_ports": trunk_ports,
        "exempt_trunk_ports": exempt_trunk_ports,
        "mlag_peer_ports": [
            match.group(0) for match in _MLAG_PEER_PORT_RE.finditer(ports_raw)
        ],
        "vxlan_mapped": bool(_VXLAN_MEMBER_RE.search(ports_raw)),
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
    vid = re.escape(str(int(vlan_id)))
    # Anchor on the interface line. NXOS echoes "!Command: show running-config
    # interface Vlan100" even when the SVI does not exist.
    return bool(
        re.search(rf"^\s*interface\s+vlan\s*{vid}\s*$", str(stdout), re.I | re.M)
    )


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


_ARP_VLAN_IFACE_RE = re.compile(r"(?<![A-Za-z0-9])vlan\s*(\d+)(?!\d)", re.I)


def parse_arp_entries(arp_output, vlan_id=None):
    """Parse ARP table rows into structured entries.

    Returns a list of dicts::
        {ip, mac, interface, port, age}

    ``interface`` is the SVI (``Vlan100``) when the row names one, otherwise the
    port. ``port`` is the physical or port-channel member EOS prints after the
    SVI (``Vlan100, Ethernet3``). With *vlan_id*, rows bound to another SVI are
    dropped: ``include Vlan100`` on EOS also returns Vlan1000-Vlan1009.
    Incomplete entries have no MAC and are skipped.
    """
    if not arp_output or not str(arp_output).strip():
        return []

    entries = []
    seen = set()
    vid = str(int(vlan_id)) if vlan_id is not None else None

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
        tail = line[mac_match.end():]
        vlan_match = _ARP_VLAN_IFACE_RE.search(tail) or _ARP_VLAN_IFACE_RE.search(line)
        port_match = _PORT_NAME_RE.search(tail)
        svi = "Vlan%s" % vlan_match.group(1) if vlan_match else ""
        port = port_match.group(0) if port_match else ""

        if vid is not None and vlan_match and vlan_match.group(1) != vid:
            continue

        age = ""
        age_match = re.search(
            r"(\d+:\d+:\d+|\d+|-+|static)", line[ip_match.end(): mac_match.start()], re.I
        )
        if age_match:
            age = age_match.group(1)

        entry = {
            "ip": ip_addr,
            "mac": mac,
            "interface": svi or port,
            "port": port,
            "age": age,
        }
        key = (entry["ip"], entry["mac"], entry["interface"].lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

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


# Fabric interconnects are transit. They do not make a switch a participating leaf.
_TRANSIT_DESC_RE = re.compile(
    r"(?i)(?:\bfabric\s+interconnects?\b|\bfi-[ab]\b)"
)

# Endpoint-facing: servers, mainframe OSA, storage, hypervisors, appliances.
# Checked before switch-uplink. Fabric interconnects are excluded above.
_COMPUTE_DESC_RE = re.compile(
    r"(?i)(?:"
    r"\bz1[3-9]\b|\bz16\b|\bosa\b|\bmainframe\b|"
    r"\bnutanix\b|\bahv\b|\bacropolis\b|\bucs\b|"
    r"\bstorage\b|\bappliance\b|"
    r"\besxi\b|\bvmware\b|\bvsphere\b|\bhyper-?v\b|"
    r"\bhci\b|\bhyperconverged\b|\bvxrail\b|"
    r"\bblade\b|\bc2[24]0\b|\bc480\b|"
    r"\bserver\b|\bhypervisor\b|\bkvm\b|\bproxmox\b|"
    r"\bcompute\b|\bvmnic\b|\bvnic\b"
    r")"
)

# Firewalls, load balancers, and routers. These are endpoints of the VLAN and,
# when no SVI exists, usually the external gateway. Checked before the switch
# rule: "to-fw01" must never make a switch prune-eligible.
_NETWORK_SERVICE_DESC_RE = re.compile(
    r"(?i)(?<![a-z])(?:"
    r"fw\d*|firewall|palo(?:alto)?|pan|asa\d*|forti\w*|checkpoint|chkp|"
    r"f5|big-?ip|ltm|lb\d*|load-?balancer|netscaler|adc|srx\d*|"
    r"router|rtr\d*|wan|isp|inet|internet"
    r")(?![a-z])"
)

# Neighbor (LLDP/CDP) role -> attachment role. "unknown" falls through.
_NEIGHBOR_ATTACHMENT_ROLES = {
    "switch": "switch_uplink",
    "downstream_switch": "downstream_switch",
    "network_service": "network_service",
    "transit": "transit",
    "compute": "compute",
}

# Switch-to-switch: uplinks, peer links, and descriptions pointing at other switches.
_SWITCH_DESC_RE = re.compile(
    r"(?i)(?:"
    r"\buplink\b|\bpeer-?link\b|\bmlagpeer\b|"
    r"\bto\s+\S*(?:sw|leaf|spine|agg|core|oob|router|fw)\S*|"
    r"[-_](?:sw|leaf|spine|agg|core|oob)\d+|"
    r"\b(?:spine|leaf|aggacc|aggpe|core|oob)[-_]?\d*\b"
    r")"
)

_INTERFACE_LINE_RE = re.compile(r"^interface\s+(\S+)\s*$", re.I | re.M)
_SWITCHPORT_MODE_RE = re.compile(r"^\s*switchport\s+mode\s+(\S+)", re.I | re.M)


def _interface_stanza(text):
    """Return the first interface stanza from a show-run snippet."""
    lines = str(text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^interface\s+\S+", line, re.I):
            start = index
            break
    if start is None:
        return "\n".join(line.rstrip() for line in lines).strip()
    collected = []
    for line in lines[start:]:
        if collected and re.match(r"^interface\s+\S+", line, re.I):
            break
        collected.append(line.rstrip())
    return "\n".join(collected).strip()


def parse_l2_interface_config(text, interface_hint="", platform="eos"):
    """Parse description / mode / raw stanza from ``show run interface``."""
    raw_text = str(text or "")
    stanza = _interface_stanza(raw_text)
    iface_match = _INTERFACE_LINE_RE.search(stanza) or _INTERFACE_LINE_RE.search(raw_text)
    interface = interface_hint or ""
    if iface_match:
        interface = iface_match.group(1)
    desc_match = _SVI_DESC_RE.search(stanza)
    mode_match = _SWITCHPORT_MODE_RE.search(stanza)
    mode = (mode_match.group(1).lower() if mode_match else "")
    if mode not in ("access", "trunk"):
        mode = "unknown"
    return {
        "interface": interface,
        "description": desc_match.group(1).strip() if desc_match else "",
        "mode": mode,
        "peer_link": trunk_interface_is_maintenance_exempt(stanza, platform),
        "raw": stanza,
    }


def interface_config_entries_from_cli_results(results, platform="eos"):
    """Normalize Ansible cli_command loop results into interface config dicts."""
    entries = []
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        stdout = entry.get("stdout") or entry.get("config") or entry.get("raw") or ""
        item = entry.get("item")
        if isinstance(item, dict):
            iface = item.get("interface") or ""
        elif item not in (None, ""):
            iface = str(item)
        else:
            iface = entry.get("interface") or ""
        parsed = parse_l2_interface_config(stdout, iface, platform)
        if parsed.get("interface") or parsed.get("raw"):
            entries.append(parsed)
    return entries


def ports_needing_interface_config(
    ports, cached_configs=None, mac_entries=None, platform="eos"
):
    """Return VLAN/MAC ports that do not already have a cached interface config."""
    cached_norm = set()
    for item in cached_configs or []:
        if isinstance(item, dict):
            iface = item.get("interface") or ""
        else:
            iface = str(item or "")
        if iface:
            cached_norm.add(_normalize_interface_name(iface, platform))

    mac_ifaces = []
    for entry in mac_entries or []:
        if isinstance(entry, dict):
            mac_ifaces.append(entry.get("interface") or "")
        else:
            mac_ifaces.append(str(entry or ""))

    needed = []
    seen = set()
    for port in list(ports or []) + mac_ifaces:
        if not port:
            continue
        norm = _normalize_interface_name(port, platform)
        if not norm or norm in cached_norm or norm in seen:
            continue
        seen.add(norm)
        needed.append(port)
    return needed


def classify_attachment_role(
    interface,
    parsed=None,
    access_ports=None,
    trunk_ports=None,
    exempt_trunk_ports=None,
    switch_hostnames=None,
    neighbor_role="",
):
    """Classify a VLAN member port.

    Roles: ``compute``, ``network_service`` (firewall / LB / router),
    ``switch_uplink`` (switch in the inventory), ``downstream_switch`` (switch
    outside the inventory), ``peer_link``, ``transit``, or ``unknown``.

    An LLDP/CDP neighbor decides first. Then descriptions, which win over
    access-vs-trunk: compute trunks (z14 OSA, UCS, Nutanix) are still endpoints.
    """
    parsed = parsed or {}
    description = parsed.get("description") or ""
    port_role = classify_mac_port_role(
        interface, access_ports, trunk_ports, exempt_trunk_ports
    )
    if parsed.get("peer_link") or port_role == "exempt_trunk":
        return "peer_link"
    if neighbor_role in _NEIGHBOR_ATTACHMENT_ROLES:
        return _NEIGHBOR_ATTACHMENT_ROLES[neighbor_role]
    if description and _TRANSIT_DESC_RE.search(description):
        return "transit"
    if description and _NETWORK_SERVICE_DESC_RE.search(description):
        return "network_service"
    if description and _COMPUTE_DESC_RE.search(description):
        return "compute"
    desc_l = description.lower()
    for host in switch_hostnames or []:
        host_l = str(host).strip().lower()
        if host_l and len(host_l) >= 4 and host_l in desc_l:
            return "switch_uplink"
    if description and _SWITCH_DESC_RE.search(description):
        return "switch_uplink"
    if port_role == "access":
        return "compute"
    if port_role == "trunk":
        return "switch_uplink"
    if parsed.get("mode") == "access":
        return "compute"
    if parsed.get("mode") == "trunk":
        return "switch_uplink"
    return "unknown"


def index_interface_configs(interface_configs, platform="eos"):
    """Map normalized interface name -> parsed L2 config dict."""
    by_norm = {}
    for item in interface_configs or []:
        if not isinstance(item, dict):
            continue
        if item.get("raw") or item.get("description") is not None:
            parsed = dict(item)
            if not parsed.get("interface") and item.get("config"):
                parsed = parse_l2_interface_config(
                    item.get("config"), item.get("interface") or "", platform
                )
        else:
            parsed = parse_l2_interface_config(
                item.get("config") or "", item.get("interface") or "", platform
            )
        iface = parsed.get("interface") or item.get("interface") or ""
        if not iface:
            continue
        parsed["interface"] = iface
        by_norm[_normalize_interface_name(iface, platform)] = parsed
    return by_norm


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


def _dotted_mask_to_prefix_len(mask):
    """Convert a dotted IPv4 mask to a prefix length."""
    octets = [int(part) for part in str(mask).split(".")]
    if len(octets) != 4 or any(part < 0 or part > 255 for part in octets):
        return None
    bits = "".join(bin(part)[2:].zfill(8) for part in octets)
    if "01" in bits:
        return None
    return bits.count("1")


def network_prefix(cidr):
    """Return the network prefix for a host CIDR (10.10.100.2/24 → 10.10.100.0/24)."""
    text = str(cidr or "").strip()
    if "/" not in text:
        return text
    ip_text, plen_text = text.split("/", 1)
    try:
        plen = int(plen_text)
        octets = [int(part) for part in ip_text.split(".")]
    except ValueError:
        return text
    if len(octets) != 4 or plen < 0 or plen > 32:
        return text
    value = (octets[0] << 24) + (octets[1] << 16) + (octets[2] << 8) + octets[3]
    mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
    value &= mask
    return "%d.%d.%d.%d/%d" % (
        (value >> 24) & 255,
        (value >> 16) & 255,
        (value >> 8) & 255,
        value & 255,
        plen,
    )


def parse_svi_ip_addresses(svi_config):
    """Parse SVI IPv4 addresses (CIDR or dotted-mask NXOS/IOS form)."""
    addresses = []
    seen = set()
    pattern = re.compile(
        r"^\s*ip\s+address\s+(\d+\.\d+\.\d+\.\d+)"
        r"(?:/(\d+)|(?:\s+(\d+\.\d+\.\d+\.\d+)))?"
        r"(?:\s+secondary)?\s*$",
        re.I | re.M,
    )
    for match in pattern.finditer(str(svi_config or "")):
        ip_addr = match.group(1)
        if match.group(2):
            cidr = "%s/%s" % (ip_addr, match.group(2))
        elif match.group(3):
            plen = _dotted_mask_to_prefix_len(match.group(3))
            cidr = "%s/%s" % (ip_addr, plen) if plen is not None else ip_addr
        else:
            cidr = ip_addr
        if cidr in seen:
            continue
        seen.add(cidr)
        addresses.append(cidr)
    return addresses


def parse_hsrp_groups(svi_config):
    """Parse HSRP groups from an SVI snippet as ``{group, ip, ...}`` dicts.

    Handles NXOS ``hsrp N`` blocks and IOS ``standby N ...`` one-liners.
    Groups without a virtual IPv4 address (``hsrp 1 ipv6``) are dropped.
    """
    return [
        group
        for group in _parse_svi_stanza(svi_config)["fhrp_groups"]
        if group["protocol"] == "hsrp" and group.get("ip")
    ]


def parse_hsrp_addresses(svi_config):
    """Parse HSRP VIP addresses from an SVI snippet."""
    addresses = []
    for group in parse_hsrp_groups(svi_config):
        for vip in [group.get("ip")] + list(group.get("secondary_ips") or []):
            if vip and vip not in addresses:
                addresses.append(vip)
    return addresses


_SVI_BENIGN_LINES = (
    re.compile(r"^no\s+shutdown$", re.I),
    re.compile(r"^no\s+ipv?6?\s*redirects$", re.I),
    re.compile(r"^no\s+ip\s+redirects$", re.I),
    re.compile(r"^no\s+ipv6\s+redirects$", re.I),
    re.compile(r"^load-interval\b", re.I),
    re.compile(r"^no\s+autostate$", re.I),
    re.compile(r"^management$", re.I),
)
_HOST_IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)(?:/\d+)?")


def _host_ip(text):
    match = _HOST_IP_RE.match(str(text or "").strip())
    return match.group(1) if match else ""


def _cidr_from_address(address, mask=None):
    """Return ``A.B.C.D/len`` from CIDR text or an address plus dotted mask."""
    text = str(address or "").strip()
    if "/" in text or not mask:
        return text
    plen = _dotted_mask_to_prefix_len(mask)
    return "%s/%s" % (text, plen) if plen is not None else text


def _empty_svi_stanza():
    return {
        "description": "",
        "mtu": None,
        "vrf": "",
        "shutdown": False,
        "addresses": [],
        "virtual_router_addresses": [],
        "fhrp_groups": [],
        "ipv6_addresses": [],
        "dhcp_relay": [],
        "acls": [],
        "pbr": [],
        "ospf": [],
        "multicast": [],
        "nat": [],
        "urpf": [],
        "other_lines": [],
    }


def _fhrp_group(groups, protocol, number, family="ipv4"):
    for group in groups:
        if (
            group["protocol"] == protocol
            and group["group"] == number
            and group["family"] == family
        ):
            return group
    group = {
        "protocol": protocol,
        "group": number,
        "family": family,
        "ip": "",
        "secondary_ips": [],
        "priority": None,
        "preempt": False,
    }
    groups.append(group)
    return group


def _fhrp_add_ip(group, ip_text, secondary=False):
    ip_addr = _host_ip(ip_text)
    if not ip_addr:
        return
    if secondary or group["ip"]:
        if ip_addr != group["ip"] and ip_addr not in group["secondary_ips"]:
            group["secondary_ips"].append(ip_addr)
    else:
        group["ip"] = ip_addr


def _parse_fhrp_child(group, line):
    """Apply one line from inside an NXOS ``hsrp N`` / ``vrrp N`` block."""
    match = re.match(
        r"^(?:ip|address)\s+(\d+\.\d+\.\d+\.\d+)(?:\s+(secondary|primary))?$",
        line,
        re.I,
    )
    if match:
        _fhrp_add_ip(group, match.group(1), (match.group(2) or "").lower() == "secondary")
        return
    match = re.match(r"^priority(?:-level)?\s+(\d+)", line, re.I)
    if match:
        group["priority"] = int(match.group(1))
        return
    if re.match(r"^preempt\b", line, re.I):
        group["preempt"] = True


def _parse_svi_stanza(svi_config):
    """Line-by-line parse of an SVI stanza (EOS, NXOS, IOS).

    Every line that is not understood lands in ``other_lines`` so nothing that
    must move with the gateway is dropped silently.
    """
    result = _empty_svi_stanza()
    stanza = _interface_stanza(svi_config)
    if not re.match(r"^interface\s+\S+", stanza, re.I):
        return result
    lines = stanza.splitlines()[1:]
    block = None
    block_indent = None

    for raw_line in lines:
        if not raw_line.strip() or raw_line.strip().startswith("!"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()

        if block is not None and indent > block_indent:
            _parse_fhrp_child(block, line)
            continue
        block = None
        block_indent = None

        if any(pattern.match(line) for pattern in _SVI_BENIGN_LINES):
            continue

        match = re.match(r"^description\s+(.+)$", line, re.I)
        if match:
            result["description"] = match.group(1).strip()
            continue
        match = re.match(r"^mtu\s+(\d+)$", line, re.I)
        if match:
            result["mtu"] = int(match.group(1))
            continue
        match = re.match(
            r"^(?:vrf\s+member|vrf\s+forwarding|ip\s+vrf\s+forwarding|vrf)\s+(\S+)$",
            line,
            re.I,
        )
        if match:
            result["vrf"] = match.group(1)
            continue
        if re.match(r"^shutdown$", line, re.I):
            result["shutdown"] = True
            continue

        match = re.match(
            r"^ip\s+address\s+virtual\s+(\d+\.\d+\.\d+\.\d+/\d+)(\s+secondary)?$",
            line,
            re.I,
        )
        if match:
            result["addresses"].append(
                {
                    "cidr": match.group(1),
                    "secondary": bool(match.group(2)),
                    "virtual": True,
                }
            )
            continue
        match = re.match(
            r"^ip\s+address\s+(\d+\.\d+\.\d+\.\d+)"
            r"(?:/(\d+)|\s+(\d+\.\d+\.\d+\.\d+))?(\s+secondary)?$",
            line,
            re.I,
        )
        if match:
            if match.group(2):
                cidr = "%s/%s" % (match.group(1), match.group(2))
            else:
                cidr = _cidr_from_address(match.group(1), match.group(3))
            result["addresses"].append(
                {"cidr": cidr, "secondary": bool(match.group(4)), "virtual": False}
            )
            continue
        match = re.match(r"^ip\s+virtual-router\s+address\s+(\S+)", line, re.I)
        if match:
            vip = _host_ip(match.group(1))
            if vip and vip not in result["virtual_router_addresses"]:
                result["virtual_router_addresses"].append(vip)
            continue
        match = re.match(r"^ipv6\s+address\s+(\S+)", line, re.I)
        if match:
            result["ipv6_addresses"].append(match.group(1))
            continue

        match = re.match(
            r"^ip\s+helper-address\s+(?:vrf\s+(\S+)\s+)?(\S+)(?:\s+vrf\s+(\S+))?",
            line,
            re.I,
        )
        if match:
            result["dhcp_relay"].append(
                {"server": match.group(2), "vrf": match.group(1) or match.group(3) or ""}
            )
            continue
        match = re.match(
            r"^ip\s+dhcp\s+relay\s+address\s+(\S+)(?:\s+use-vrf\s+(\S+))?",
            line,
            re.I,
        )
        if match:
            result["dhcp_relay"].append(
                {"server": match.group(1), "vrf": match.group(2) or ""}
            )
            continue
        match = re.match(r"^ip\s+access-group\s+(\S+)\s+(in|out)\b", line, re.I)
        if match:
            result["acls"].append(
                {"name": match.group(1), "direction": match.group(2).lower()}
            )
            continue
        match = re.match(
            r"^(?:ip\s+policy\s+route-map|service-policy\s+type\s+pbr\s+input)\s+(\S+)",
            line,
            re.I,
        )
        if match:
            result["pbr"].append(match.group(1))
            continue
        if re.match(r"^(?:ip\s+router\s+ospf|ip\s+ospf)\b", line, re.I):
            result["ospf"].append(line)
            continue
        if re.match(r"^(?:ip\s+pim|ip\s+igmp|pim\s+ipv4|ip\s+multicast)\b", line, re.I):
            result["multicast"].append(line)
            continue
        if re.match(r"^ip\s+nat\b", line, re.I):
            result["nat"].append(line)
            continue
        if re.match(r"^ip\s+verify\s+unicast\b", line, re.I):
            result["urpf"].append(line)
            continue

        if re.match(r"^hsrp\s+version\b", line, re.I):
            continue
        match = re.match(r"^hsrp\s+(\d+)(?:\s+(ipv4|ipv6))?$", line, re.I)
        if match:
            block = _fhrp_group(
                result["fhrp_groups"],
                "hsrp",
                int(match.group(1)),
                (match.group(2) or "ipv4").lower(),
            )
            block_indent = indent
            continue
        match = re.match(r"^vrrp(?:v3)?\s+(\d+)(?:\s+address-family\s+(ipv4|ipv6))?$", line, re.I)
        if match:
            block = _fhrp_group(
                result["fhrp_groups"],
                "vrrp",
                int(match.group(1)),
                (match.group(2) or "ipv4").lower(),
            )
            block_indent = indent
            continue
        match = re.match(r"^vrrp\s+(\d+)\s+(.+)$", line, re.I)
        if match:
            group = _fhrp_group(result["fhrp_groups"], "vrrp", int(match.group(1)))
            rest = match.group(2)
            addr = re.match(r"^(?:ipv4|ip)\s+(\S+)(\s+secondary)?$", rest, re.I)
            if addr:
                _fhrp_add_ip(group, addr.group(1), bool(addr.group(2)))
            else:
                _parse_fhrp_child(group, rest)
            continue
        match = re.match(r"^standby\s+(\d+)\s+(.+)$", line, re.I)
        if match:
            group = _fhrp_group(result["fhrp_groups"], "hsrp", int(match.group(1)))
            _parse_fhrp_child(group, match.group(2))
            continue

        result["other_lines"].append(line)

    return result


def svi_prefixes(svi_config, platform="eos"):
    """Network prefixes of an SVI (primary and secondary), for per-prefix lookups."""
    if not svi_config or _output_indicates_missing(svi_config):
        return []
    found = []
    for cidr in parse_svi_details(svi_config, platform).get("ip_addresses") or []:
        prefix = network_prefix(cidr)
        if "/" in prefix and prefix not in found:
            found.append(prefix)
    return found


def parse_svi_details(svi_config, platform="eos"):
    """Parse structured fields from an SVI running-config snippet.

    ``gateway_addresses`` is every first-hop address hosts may point at:
    VARP, HSRP, VRRP, and EOS anycast ``ip address virtual``.
    """
    if not svi_config or _output_indicates_missing(svi_config):
        details = _empty_svi_stanza()
        details.pop("addresses")
        details.update(
            {
                "present": False,
                "ip_addresses": [],
                "primary_ip": "",
                "secondary_ips": [],
                "anycast_addresses": [],
                "hsrp_addresses": [],
                "hsrp_groups": [],
                "vrrp_addresses": [],
                "gateway_addresses": [],
                "redundancy": "",
            }
        )
        return details

    parsed = _parse_svi_stanza(svi_config)
    addresses = parsed.pop("addresses")
    ip_addresses = []
    for item in addresses:
        if item["cidr"] not in ip_addresses:
            ip_addresses.append(item["cidr"])
    primary = [item["cidr"] for item in addresses if not item["secondary"]]
    anycast = [_host_ip(item["cidr"]) for item in addresses if item["virtual"]]
    hsrp_groups = [
        group
        for group in parsed["fhrp_groups"]
        if group["protocol"] == "hsrp" and group.get("ip")
    ]
    hsrp_addrs = []
    vrrp_addrs = []
    for group in parsed["fhrp_groups"]:
        bucket = hsrp_addrs if group["protocol"] == "hsrp" else vrrp_addrs
        for vip in [group.get("ip")] + list(group.get("secondary_ips") or []):
            if vip and vip not in bucket:
                bucket.append(vip)

    gateways = []
    for vip in parsed["virtual_router_addresses"] + hsrp_addrs + vrrp_addrs + anycast:
        if vip and vip not in gateways:
            gateways.append(vip)
    if anycast:
        redundancy = "anycast"
    elif parsed["virtual_router_addresses"]:
        redundancy = "varp"
    elif hsrp_addrs:
        redundancy = "hsrp"
    elif vrrp_addrs:
        redundancy = "vrrp"
    elif ip_addresses:
        redundancy = "none"
    else:
        redundancy = ""

    parsed.update(
        {
            "present": True,
            "vrf": parsed["vrf"] or parse_svi_vrf(svi_config, platform),
            "ip_addresses": ip_addresses,
            "primary_ip": primary[0] if primary else "",
            "secondary_ips": [item["cidr"] for item in addresses if item["secondary"]],
            "anycast_addresses": anycast,
            "hsrp_addresses": hsrp_addrs,
            "hsrp_groups": hsrp_groups,
            "vrrp_addresses": vrrp_addrs,
            "gateway_addresses": gateways,
            "redundancy": redundancy,
        }
    )
    return parsed


_ROUTE_IFACE_TOKEN_RE = re.compile(
    r"^(?:vlan|ethernet|eth|et|port-channel|po|null|loopback|lo|tunnel|"
    r"management|mgmt|gigabitethernet|gi|tengigabitethernet|te)[\d/.:-]*\d$",
    re.I,
)
_ROUTE_HEAD_RE = re.compile(
    r"^\s*ip\s+route(?:\s+vrf\s+(?P<vrf>\S+))?\s+(?P<rest>\S.*)$", re.I
)
_IPV4_TOKEN_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def _parse_route_tokens(rest):
    """Split the text after ``ip route [vrf X]`` into route fields.

    EOS:  PREFIX [INTF] [NEXTHOP] [DISTANCE] [tag T] [name N] [metric M]
    NXOS: PREFIX [INTF] NEXTHOP [vrf V] [tag T] [name N] [DISTANCE]
    Either may use ``NETWORK MASK`` instead of ``NETWORK/LEN``.
    """
    tokens = rest.split()
    if not tokens:
        return None
    prefix = tokens[0]
    index = 1
    if (
        "/" not in prefix
        and len(tokens) > 1
        and _IPV4_TOKEN_RE.match(prefix)
        and _IPV4_TOKEN_RE.match(tokens[1])
    ):
        plen = _dotted_mask_to_prefix_len(tokens[1])
        if plen is not None:
            prefix = "%s/%s" % (prefix, plen)
            index = 2
    route = {
        "prefix": prefix,
        "next_hop_ip": "",
        "interface": "",
        "distance": "",
        "tag": "",
        "name": "",
        "nexthop_vrf": "",
    }
    keyed = {"name": "name", "tag": "tag", "vrf": "nexthop_vrf"}
    skip_value = {"metric", "track"}
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower in keyed and index + 1 < len(tokens):
            route[keyed[lower]] = tokens[index + 1]
            index += 2
            continue
        if lower in skip_value:
            index += 2
            continue
        if _IPV4_TOKEN_RE.match(token) and not route["next_hop_ip"]:
            route["next_hop_ip"] = token
        elif _ROUTE_IFACE_TOKEN_RE.match(token) and not route["interface"]:
            route["interface"] = token
        elif token.isdigit() and not route["distance"]:
            route["distance"] = token
        index += 1
    return route


def parse_ip_route_statics(route_config):
    """Parse static routes from EOS ``ip route [vrf X]`` and NXOS vrf-context.

    ``next_hop`` is the next-hop address, or the exit interface for an
    interface-only route (``Null0``). ``interface`` keeps the exit interface
    when both are given (``ip route 10.0.0.0/8 Vlan100 10.10.100.5``).
    """
    if not route_config or not str(route_config).strip():
        return []

    routes = []
    seen = set()
    current_vrf = None
    for raw_line in str(route_config).splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue

        ctx_match = _VRF_CONTEXT_RE.match(line)
        if ctx_match:
            current_vrf = ctx_match.group(1)
            continue

        if current_vrf and raw_line[:1] not in (" ", "\t"):
            current_vrf = None

        match = _ROUTE_HEAD_RE.match(line)
        if not match:
            continue
        parsed = _parse_route_tokens(match.group("rest"))
        if not parsed:
            continue
        route = {
            "vrf": match.group("vrf") or current_vrf or "default",
            "prefix": parsed["prefix"],
            "next_hop": parsed["next_hop_ip"] or parsed["interface"],
            "interface": parsed["interface"],
            "distance": parsed["distance"],
            "tag": parsed["tag"],
            "name": parsed["name"],
            "nexthop_vrf": parsed["nexthop_vrf"],
        }
        key = (
            route["vrf"],
            route["prefix"],
            route["next_hop"],
            route["interface"],
            route["name"],
        )
        if key in seen:
            continue
        seen.add(key)
        routes.append(route)
    return routes


def _bgp_apply_neighbor_attr(entry, attr, value):
    attr = str(attr or "").lower()
    value = str(value or "").strip()
    if attr == "remote-as":
        entry["remote_as"] = value
    elif attr == "update-source":
        entry["update_source"] = value
    elif attr == "description":
        entry["description"] = value
    elif attr == "route-map":
        entry["route_maps"].append(value)


def parse_bgp_context(bgp_config):
    """Parse BGP running-config: ASN, VRF RD/RT, VLAN EVPN blocks, neighbors.

    Handles EOS same-line ``neighbor X remote-as`` and NXOS split stanzas
    (``neighbor X`` then indented ``remote-as``).
    """
    empty = {
        "bgp_as": "",
        "router_id": "",
        "neighbors": [],
        "vlan_blocks": [],
        "vrfs": [],
    }
    if not bgp_config or not str(bgp_config).strip():
        return empty

    neighbors = {}
    vlan_blocks = {}
    vrf_blocks = {}
    current_vrf = "default"
    vrf_indent = None
    current_vlan = None
    vlan_indent = None
    current_neighbor = None
    neighbor_indent = None
    bgp_as = ""
    router_id = ""

    def ensure_neighbor(vrf, neighbor):
        key = (vrf, neighbor)
        return neighbors.setdefault(
            key,
            {
                "vrf": vrf,
                "neighbor": neighbor,
                "bgp_as": bgp_as,
                "remote_as": "",
                "update_source": "",
                "description": "",
                "route_maps": [],
            },
        )

    def ensure_vlan(vlan_id):
        vid = int(vlan_id)
        return vlan_blocks.setdefault(
            vid,
            {
                "vlan_id": vid,
                "rd": "",
                "route_targets": [],
                "redistribute": [],
            },
        )

    def ensure_vrf(name):
        entry = vrf_blocks.setdefault(
            name,
            {
                "name": name,
                "rd": "",
                "route_targets": [],
                "networks": [],
                "aggregates": [],
                "redistribute": [],
            },
        )
        entry.setdefault("networks", [])
        entry.setdefault("aggregates", [])
        entry.setdefault("redistribute", [])
        return entry

    def remember_prefix(bucket, address, plen, mask):
        if plen:
            prefix = network_prefix("%s/%s" % (address, int(plen)))
        elif mask:
            length = _dotted_mask_to_prefix_len(mask)
            if length is None:
                return
            prefix = network_prefix("%s/%s" % (address, length))
        else:
            return
        if prefix and prefix not in bucket:
            bucket.append(prefix)

    for raw_line in str(bgp_config).splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue

        indent = len(line) - len(line.lstrip())

        router_match = _BGP_ROUTER_RE.match(line)
        if router_match:
            bgp_as = router_match.group(1)
            current_vrf = "default"
            vrf_indent = None
            current_vlan = None
            vlan_indent = None
            current_neighbor = None
            neighbor_indent = None
            continue

        if current_neighbor is not None and neighbor_indent is not None:
            if indent <= neighbor_indent:
                current_neighbor = None
                neighbor_indent = None
        if current_vlan is not None and vlan_indent is not None:
            if indent <= vlan_indent:
                current_vlan = None
                vlan_indent = None
        if current_vrf != "default" and vrf_indent is not None:
            if indent <= vrf_indent:
                current_vrf = "default"
                vrf_indent = None

        if re.match(r"^\s*address-family\b", line, re.I):
            continue

        if (
            current_vlan is None
            and current_neighbor is None
            and current_vrf == "default"
        ):
            router_id_match = _BGP_ROUTER_ID_RE.match(line)
            if router_id_match:
                router_id = router_id_match.group(1)
                continue

        vlan_match = _BGP_VLAN_RE.match(line)
        if vlan_match:
            current_vlan = int(vlan_match.group(1))
            vlan_indent = indent
            current_neighbor = None
            neighbor_indent = None
            ensure_vlan(current_vlan)
            continue

        if current_vlan is not None:
            block = ensure_vlan(current_vlan)
            rd_match = _BGP_RD_RE.match(line)
            if rd_match:
                block["rd"] = rd_match.group(1)
                continue
            rt_match = _BGP_RT_RE.match(line)
            if rt_match:
                block["route_targets"].append(rt_match.group(1).strip())
                continue
            redist_match = _BGP_REDIST_RE.match(line)
            if redist_match:
                block["redistribute"].append(redist_match.group(1).strip())
                continue
            continue

        if current_neighbor is None and current_vlan is None:
            network_match = _BGP_NETWORK_RE.match(line)
            if network_match:
                block = ensure_vrf(current_vrf)
                remember_prefix(
                    block["networks"],
                    network_match.group(1),
                    network_match.group(2),
                    network_match.group(3),
                )
                continue
            aggregate_match = _BGP_AGGREGATE_RE.match(line)
            if aggregate_match:
                block = ensure_vrf(current_vrf)
                remember_prefix(
                    block["aggregates"],
                    aggregate_match.group(1),
                    aggregate_match.group(2),
                    aggregate_match.group(3),
                )
                continue
            redist_match = _BGP_REDIST_RE.match(line)
            if redist_match:
                block = ensure_vrf(current_vrf)
                method = redist_match.group(1).strip()
                if method and method not in block["redistribute"]:
                    block["redistribute"].append(method)
                continue

        vrf_match = _BGP_VRF_RE.match(line)
        if vrf_match and not re.match(r"^\s*neighbor\b", line, re.I):
            name = vrf_match.group(1)
            if name.lower() not in ("context", "forwarding", "member"):
                if indent <= 3:
                    current_vrf = name
                    vrf_indent = indent
                    current_neighbor = None
                    neighbor_indent = None
                    ensure_vrf(name)
                    continue

        attr_match = _BGP_NEIGHBOR_ATTR_RE.match(line)
        if attr_match:
            neighbor = attr_match.group("neighbor")
            current_neighbor = neighbor
            neighbor_indent = indent
            entry = ensure_neighbor(current_vrf, neighbor)
            _bgp_apply_neighbor_attr(
                entry, attr_match.group("attr"), attr_match.group("value")
            )
            continue

        stanza_match = _BGP_NEIGHBOR_STANZA_RE.match(line)
        if stanza_match:
            current_neighbor = stanza_match.group(1)
            neighbor_indent = indent
            ensure_neighbor(current_vrf, current_neighbor)
            continue

        if current_neighbor is not None:
            indented = _BGP_INDENTED_ATTR_RE.match(line)
            if indented:
                entry = ensure_neighbor(current_vrf, current_neighbor)
                _bgp_apply_neighbor_attr(entry, indented.group(1), indented.group(2))
                continue

        if current_vrf != "default":
            vrf_entry = ensure_vrf(current_vrf)
            rd_match = _BGP_RD_RE.match(line)
            if rd_match:
                vrf_entry["rd"] = rd_match.group(1)
                continue
            rt_match = _BGP_RT_RE.match(line)
            if rt_match:
                vrf_entry["route_targets"].append(rt_match.group(1).strip())
                continue

    return {
        "bgp_as": bgp_as,
        "router_id": router_id,
        "neighbors": list(neighbors.values()),
        "vlan_blocks": [vlan_blocks[key] for key in sorted(vlan_blocks)],
        "vrfs": [vrf_blocks[key] for key in sorted(vrf_blocks)],
    }


_INTERFACE_IPV4_RE = re.compile(
    r"Internet address is\s+(\d+\.\d+\.\d+\.\d+)",
    re.I,
)


def parse_interface_ipv4(interface_text):
    """First IPv4 address from ``show ip interface``."""
    match = _INTERFACE_IPV4_RE.search(str(interface_text or ""))
    if not match:
        return ""
    return match.group(1)


_BGP_SUMMARY_AS_RE = re.compile(
    r"local\s+AS(?:\s+number)?\s*[:=]?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_BGP_SUMMARY_RID_RE = re.compile(
    r"router\s+identifier\s+(\d+\.\d+\.\d+\.\d+)",
    re.I,
)


def _bgp_summary_from_json(blob):
    import json

    try:
        data = json.loads(blob)
    except ValueError:
        return {"bgp_as": "", "router_id": ""}
    vrfs = data.get("vrfs") if isinstance(data, dict) else None
    if not isinstance(vrfs, dict):
        return {"bgp_as": "", "router_id": ""}
    ordered = []
    if isinstance(vrfs.get("default"), dict):
        ordered.append(vrfs["default"])
    ordered.extend(
        value for key, value in vrfs.items() if key != "default" and isinstance(value, dict)
    )
    for vrf in ordered:
        asn = vrf.get("asn")
        if asn in (None, "", 0, "0"):
            asn = vrf.get("localAsn") or vrf.get("localAs")
        if asn in (None, "", 0, "0"):
            continue
        router_id = vrf.get("routerId") or vrf.get("router_id") or ""
        return {"bgp_as": str(asn).strip(), "router_id": str(router_id).strip()}
    return {"bgp_as": "", "router_id": ""}


def parse_bgp_summary(summary_text):
    """ASN and router-id from ``show ip bgp summary``.

    Accepts the JSON form (``vrfs.default.asn``) and the text header.
    A 4-byte ASN may be asplain (``4200000106``) or asdot (``64086.60010``).
    """
    blob = str(summary_text or "").strip()
    if blob.startswith("{"):
        parsed = _bgp_summary_from_json(blob)
        if parsed.get("bgp_as") or parsed.get("router_id"):
            return parsed
    asn_match = _BGP_SUMMARY_AS_RE.search(blob)
    rid_match = _BGP_SUMMARY_RID_RE.search(blob)
    return {
        "bgp_as": asn_match.group(1) if asn_match else "",
        "router_id": rid_match.group(1) if rid_match else "",
    }


def parse_bgp_neighbors(bgp_config):
    """Parse VRF-scoped BGP neighbors from ``show run`` BGP output."""
    return parse_bgp_context(bgp_config).get("neighbors") or []


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
        return f"show ip arp vlan {vid} vrf {vrf}"
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
    plat = str(platform).lower()
    if plat == "nxos":
        return _parse_nxos_trunk_interfaces(text)
    pattern = _TRUNK_INTERFACE_NAME_RES.get(plat)
    if not pattern:
        return []
    interfaces = []
    seen = set()
    for match in pattern.finditer(text):
        iface = match.group("iface").strip()
        key = iface.lower()
        if key in seen:
            continue
        seen.add(key)
        allowed = match.group("allowed").strip()
        # EOS prints Allowed then Forwarding; keep the first token group.
        allowed = re.split(r"\s{2,}", allowed, maxsplit=1)[0].strip()
        interfaces.append(
            {
                "interface": iface,
                "allowed_summary": allowed,
            }
        )
    return interfaces


def _parse_nxos_trunk_interfaces(text):
    """Parse NXOS `show interface trunk`, preferring the Allowed-on-Trunk table."""
    body = text
    section = re.search(
        r"Vlans Allowed on Trunk\s*\n[^\n]*\n(.*?)(?=\n\s*Port\s+Vlans |\n\s*Port\s+STP|\Z)",
        text,
        re.I | re.S,
    )
    if section:
        body = section.group(1)
    pattern = re.compile(
        r"^(?P<iface>(?:Eth|Ethernet|po|Po|port-channel)\S+)\s+(?P<allowed>\S.*)$",
        re.I | re.M,
    )
    interfaces = []
    seen = set()
    for match in pattern.finditer(body):
        iface = match.group("iface").strip()
        allowed = match.group("allowed").strip()
        if allowed.lower() in {"trunking", "--"}:
            continue
        key = iface.lower()
        if key in seen:
            continue
        seen.add(key)
        interfaces.append({"interface": iface, "allowed_summary": allowed})
    return interfaces


def resolve_trunk_allowed_vlans(config):
    """Resolve the effective allowed VLAN set from cumulative trunk config lines.

    NXOS (and some EOS) running-config accumulates::

        switchport trunk allowed vlan 1-100
        switchport trunk allowed vlan add 200
        switchport trunk allowed vlan remove 50

    Returns a dict ``{mode, vlans}`` or ``None`` when no allowed-vlan statement
    exists (platform default: all VLANs).
    """
    matches = list(_TRUNK_ALLOWED_VLAN_RE.finditer(str(config or "")))
    if not matches:
        return None

    allowed = set()
    initialized = False
    for match in matches:
        modifier = (match.group("modifier") or "").lower()
        tokens = match.group("vlans") or ""
        vlans = expand_vlan_spec(tokens)
        if modifier == "except":
            return {"mode": "except", "vlans": vlans}
        if modifier == "add":
            allowed.update(vlans)
            initialized = True
        elif modifier == "remove":
            allowed.difference_update(vlans)
            initialized = True
        else:
            allowed = set(vlans)
            initialized = True
    if not initialized:
        return None
    return {"mode": "allow_list", "vlans": allowed}


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
        resolved = resolve_trunk_allowed_vlans(config)

        if resolved and resolved.get("mode") == "except":
            except_set = resolved.get("vlans") or set()
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

        if resolved and resolved.get("mode") == "allow_list":
            allowed_set = resolved.get("vlans") or set()
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


