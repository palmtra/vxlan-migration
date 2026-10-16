# -*- coding: utf-8 -*-
"""Physical topology parsers: LLDP/CDP neighbors, port-channels, STP, VXLAN.

Neighbors are the most reliable way to tell a switch-to-switch trunk from a
server, firewall, or a switch outside the discovery scope. Port descriptions
are the fallback when a port has no neighbor.
"""

import re

from vlan_lib.parsers import _normalize_interface_name, normalize_mac_address

_CAPABILITY_LETTERS = {
    "b": "bridge",
    "r": "router",
    "s": "station",
    "w": "wlan",
    "t": "telephone",
    "p": "repeater",
    "o": "other",
    "c": "docsis",
}
_CAPABILITY_WORDS = {
    "bridge": "bridge",
    "switch": "bridge",
    "router": "router",
    "station": "station",
    "host": "station",
    "wlan": "wlan",
    "telephone": "telephone",
    "phone": "telephone",
    "repeater": "repeater",
    "other": "other",
}

# Firewalls, load balancers, and routers: an external first hop may sit here.
_NETWORK_SERVICE_PLATFORM_RE = re.compile(
    r"(?i)(?:palo\s*alto|pan-os|fortigate|fortinet|fortios|adaptive\s+security|"
    r"\basa\b|firepower|check\s*point|gaia|\bf5\b|big-?ip|netscaler|citrix\s+adc|"
    r"\bsrx\d*|sophos|sonicwall|\ba10\b|nsx\s+edge|ios[\s-]?xr|\basr\d*|\bisr\d*|"
    r"juniper\s+networks.*\bmx\d*)"
)
_FABRIC_INTERCONNECT_RE = re.compile(r"(?i)(?:fabric\s+interconnect|ucs-fi|\bfi-[ab]\b)")
_SWITCH_PLATFORM_RE = re.compile(
    r"(?i)(?:arista\s+networks|\beos\b|nx-os|nexus|cisco\s+ios|catalyst|\bn[3579]k|"
    r"\bws-c|\bc9[0-9]{3}|juniper.*\b(?:ex|qfx)\d*|dell.*os10|aruba|procurve|"
    r"hpe?\s+comware|cumulus|sonic|extreme\s*networks|brocade|mellanox|onyx)"
)
_SERVER_PLATFORM_RE = re.compile(
    r"(?i)(?:vmware|\besxi?\b|linux|ubuntu|red\s*hat|rhel|centos|debian|suse|"
    r"windows|hyper-v|nutanix|\bahv\b|ucs\s+[bc]-series|netapp|pure\s*storage|"
    r"isilon|poweredge|idrac|\bilo\b|z/os|zvm|z/vm|ibm\s+z|osa)"
)


def _clean(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.strip()


def short_hostname(name):
    """``leaf2.example.com(FOX123)`` -> ``leaf2``. Lowercase, no domain or serial."""
    text = _clean(name).lower()
    text = re.sub(r"\(.*\)$", "", text).strip()
    return text.split(".")[0] if text else ""


def _capabilities(text):
    found = []
    for token in re.split(r"[\s,]+", str(text or "")):
        token = token.strip().lower()
        if not token:
            continue
        word = _CAPABILITY_WORDS.get(token)
        if word is None and len(token) == 1:
            word = _CAPABILITY_LETTERS.get(token)
        if word and word not in found:
            found.append(word)
    return found


def _neighbor(local, name="", port="", description="", platform="", capabilities="",
              mgmt="", protocol="lldp"):
    return {
        "local_interface": _clean(local),
        "neighbor_name": _clean(name),
        "neighbor_port": _clean(port),
        "system_description": _clean(description),
        "platform": _clean(platform),
        "capabilities": _capabilities(capabilities),
        "mgmt_address": _clean(mgmt),
        "protocol": protocol,
    }


def _field(block, pattern):
    match = re.search(pattern, block, re.I | re.M)
    return match.group(1).strip() if match else ""


def _parse_eos_lldp_detail(text):
    neighbors = []
    sections = re.split(r"(?m)^Interface\s+", text)
    for section in sections[1:]:
        head = re.match(r"(\S+)\s+detected\s+(\d+)", section)
        if not head or int(head.group(2)) == 0:
            continue
        local = head.group(1)
        for block in re.split(r"(?m)^\s+Neighbor\s+", section)[1:]:
            neighbors.append(
                _neighbor(
                    local,
                    name=_field(block, r"System Name\s*:\s*(.+)$"),
                    port=_field(block, r"Port ID\s*:\s*(.+)$"),
                    description=_field(block, r"System Description\s*:\s*(.+)$"),
                    capabilities=_field(block, r"Enabled Capabilities\s*:\s*(.+)$")
                    or _field(block, r"System Capabilities\s*:\s*(.+)$"),
                    mgmt=_field(block, r"Management Address\s*:\s*(\S+)"),
                )
            )
    return neighbors


def _parse_nxos_lldp_detail(text):
    neighbors = []
    for block in re.split(r"(?mi)^(?=Chassis id:)", text)[1:]:
        local = _field(block, r"^Local Port id:\s*(\S+)")
        if not local:
            continue
        neighbors.append(
            _neighbor(
                local,
                name=_field(block, r"^System Name:\s*(.+)$"),
                port=_field(block, r"^Port id:\s*(\S+)"),
                description=_field(block, r"^System Description:\s*(.+)$"),
                capabilities=_field(block, r"^Enabled Capabilities:\s*(.+)$")
                or _field(block, r"^System Capabilities:\s*(.+)$"),
                mgmt=_field(block, r"^Management Address:\s*(\S+)"),
            )
        )
    return neighbors


def parse_lldp_neighbors(text, platform="eos"):
    """Parse ``show lldp neighbors detail`` (EOS or NXOS)."""
    body = str(text or "")
    if not body.strip():
        return []
    if re.search(r"(?mi)^Local Port id:", body):
        return _parse_nxos_lldp_detail(body)
    if str(platform).lower() == "nxos":
        return _parse_nxos_lldp_detail(body)
    return _parse_eos_lldp_detail(body)


def parse_cdp_neighbors(text):
    """Parse NXOS/IOS ``show cdp neighbors detail``."""
    neighbors = []
    for block in re.split(r"(?m)^(?=Device ID:)", str(text or ""))[1:]:
        local = _field(block, r"^Interface:\s*([^,\s]+)")
        if not local:
            continue
        version = _field(block, r"^Version\s*:\s*\n\s*(.+)$")
        neighbors.append(
            _neighbor(
                local,
                name=_field(block, r"^System Name:\s*(.+)$")
                or _field(block, r"^Device ID:\s*(\S+)"),
                port=_field(block, r"Port ID \(outgoing port\):\s*(\S+)"),
                description=version,
                platform=_field(block, r"^Platform:\s*([^,]+)"),
                capabilities=_field(block, r"Capabilities:\s*(.+)$"),
                mgmt=_field(block, r"IPv4 Address:\s*(\S+)"),
                protocol="cdp",
            )
        )
    return neighbors


def parse_port_channel_members(text, platform="eos"):
    """Map a port-channel to its members from ``show run | section channel-group``.

    Returns ``{normalized_po: [member, ...]}`` (``po10``).
    """
    members = {}
    current = None
    for raw_line in str(text or "").splitlines():
        iface = re.match(r"^interface\s+(\S+)", raw_line, re.I)
        if iface:
            current = iface.group(1)
            continue
        group = re.match(r"^\s+channel-group\s+(\d+)\b", raw_line, re.I)
        if group and current:
            key = "po%s" % group.group(1)
            members.setdefault(key, [])
            if current not in members[key]:
                members[key].append(current)
    return members


def neighbors_for_interface(interface, neighbors, port_channels=None, platform="eos"):
    """Neighbors seen on *interface*, or on its members when it is a port-channel."""
    norm = _normalize_interface_name(interface, platform)
    wanted = {norm}
    for member in (port_channels or {}).get(norm, []):
        wanted.add(_normalize_interface_name(member, platform))
    found = []
    seen = set()
    for item in neighbors or []:
        if not isinstance(item, dict):
            continue
        local = _normalize_interface_name(item.get("local_interface", ""), platform)
        if local not in wanted:
            continue
        key = (short_hostname(item.get("neighbor_name")), item.get("neighbor_port"))
        if key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found


def classify_neighbor(neighbor, inventory_hostnames=None):
    """Return the role a neighbor gives the local port.

    ``switch`` - another switch in the inventory (switch-to-switch trunk).
    ``downstream_switch`` - a switch outside the inventory; endpoints may sit behind it.
    ``network_service`` - firewall, load balancer, or router; often the external gateway.
    ``transit`` - UCS fabric interconnect (kept as transit, as before).
    ``compute`` - a server or hypervisor.
    ``unknown`` - not enough information; fall back to the port description.
    """
    if not isinstance(neighbor, dict):
        return "unknown"
    inventory = {short_hostname(name) for name in (inventory_hostnames or []) if name}
    inventory.discard("")
    name = short_hostname(neighbor.get("neighbor_name"))
    if name and name in inventory:
        return "switch"
    text = " ".join(
        str(neighbor.get(key) or "")
        for key in ("neighbor_name", "system_description", "platform")
    )
    if _FABRIC_INTERCONNECT_RE.search(text):
        return "transit"
    if _NETWORK_SERVICE_PLATFORM_RE.search(text):
        return "network_service"
    if _SERVER_PLATFORM_RE.search(text):
        return "compute"
    if _SWITCH_PLATFORM_RE.search(text):
        return "downstream_switch"
    caps = neighbor.get("capabilities") or []
    if caps and set(caps) <= {"station"}:
        return "compute"
    if "router" in caps and "bridge" not in caps:
        return "network_service"
    return "unknown"


def parse_spanning_tree_vlan(text):
    """Parse ``show spanning-tree vlan <id>`` (EOS / NXOS, PVST or MST)."""
    body = str(text or "")
    result = {
        "present": False,
        "protocol": "",
        "root_priority": None,
        "root_address": "",
        "is_root": False,
        "root_port": "",
        "bridge_priority": None,
        "bridge_address": "",
        "blocked_ports": [],
    }
    if not body.strip() or re.search(r"(?i)does not exist|no spanning tree|not enabled", body):
        return result
    proto = re.search(r"(?i)spanning tree enabled protocol\s+(\S+)", body)
    if proto:
        result["protocol"] = proto.group(1)
    root = re.search(
        r"(?is)Root ID\s+Priority\s+(\d+)\s+Address\s+(\S+)(.*?)(?:Bridge ID|\Z)", body
    )
    if root:
        result["present"] = True
        result["root_priority"] = int(root.group(1))
        result["root_address"] = normalize_mac_address(root.group(2))
        rest = root.group(3)
        result["is_root"] = bool(re.search(r"(?i)this bridge is the root", rest))
        port = re.search(r"(?im)^\s*Port\s+\d+\s+\(([^)]+)\)", rest)
        if port:
            result["root_port"] = port.group(1).strip()
    bridge = re.search(r"(?is)Bridge ID\s+Priority\s+(\d+).*?Address\s+(\S+)", body)
    if bridge:
        result["present"] = True
        result["bridge_priority"] = int(bridge.group(1))
        result["bridge_address"] = normalize_mac_address(bridge.group(2))
    for match in re.finditer(
        r"(?im)^(\S+)\s+(root|desg|designated|altn|alternate|back|backup|mstr|master|disa|disabled)\s+(\S+)",
        body,
    ):
        role = match.group(2).lower()
        state = match.group(3).lower()
        if role in ("altn", "alternate", "back", "backup") or state in (
            "blk",
            "blocking",
            "discarding",
            "bkn*",
        ):
            result["blocked_ports"].append(match.group(1))
    return result


def parse_vxlan_vlan_to_vni(text):
    """Parse EOS ``show vxlan vlan-to-vni`` into ``[{vlan_id, vni}]``."""
    mappings = []
    seen = set()
    for match in re.finditer(r"(?m)^\s*(\d{1,4})\*?\s+(\d+)\b", str(text or "")):
        vlan_id = int(match.group(1))
        vni = int(match.group(2))
        if not 1 <= vlan_id <= 4094 or (vlan_id, vni) in seen:
            continue
        seen.add((vlan_id, vni))
        mappings.append({"vlan_id": vlan_id, "vni": vni})
    return mappings


def _bracket_pairs(text):
    """``[10, 10010]  [20, 10020]`` -> ``[('10', '10010'), ('20', '10020')]``."""
    return re.findall(r"\[\s*([^,\]]+?)\s*,\s*(\d+)\s*\]", text or "")


def parse_vxlan_interface(text):
    """Parse EOS ``show interfaces Vxlan1``: is this switch a ready HER VTEP?

    Returns source interface and address, UDP port, replication mode
    (``headend`` or ``multicast``), flood list source (``EVPN``, static,
    CVX), static VLAN-to-VNI and VRF-to-VNI maps, and the static headend
    flood list.
    """
    body = str(text or "")
    result = {
        "present": False,
        "source_interface": "",
        "source_ip": "",
        "udp_port": None,
        "replication_mode": "",
        "flood_list_source": "",
        "vlan_vni": [],
        "vrf_vni": {},
        "flood_vteps": [],
    }
    if not re.search(r"(?im)^\s*Vxlan\d+ is ", body):
        return result
    result["present"] = True
    source = re.search(
        r"(?i)Source interface is\s+(\S+)(?:\s+and is \S+\s+with\s+(\d+\.\d+\.\d+\.\d+))?", body
    )
    if source:
        result["source_interface"] = source.group(1)
        result["source_ip"] = source.group(2) or ""
    port = re.search(r"(?i)Listening on UDP port\s+(\d+)", body)
    if port:
        result["udp_port"] = int(port.group(1))
    mode = re.search(
        r"(?i)Replication/Flood Mode is\s+(\S+)(?:\s+with Flood List Source:\s*(.+?))?\s*$",
        body,
        re.M,
    )
    if mode:
        result["replication_mode"] = mode.group(1).strip().lower()
        result["flood_list_source"] = (mode.group(2) or "").strip()
    section = None
    for line in body.splitlines():
        lower = line.strip().lower()
        if lower.startswith("static vlan to vni mapping") or lower.startswith("dynamic vlan to vni"):
            section = "vlan"
            continue
        if lower.startswith("static vrf to vni mapping") or lower.startswith("dynamic vrf to vni"):
            section = "vrf"
            continue
        if lower.startswith("headend replication flood vtep list"):
            section = "flood"
            continue
        if section in ("vlan", "vrf") and "[" not in line:
            section = None
        if section == "vlan":
            for vlan, vni in _bracket_pairs(line):
                if vlan.strip().isdigit():
                    result["vlan_vni"].append({"vlan_id": int(vlan), "vni": int(vni)})
        elif section == "vrf":
            for vrf, vni in _bracket_pairs(line):
                result["vrf_vni"][vrf.strip()] = int(vni)
        elif section == "flood":
            addresses = re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", line)
            if not addresses:
                section = None
                continue
            for address in addresses:
                if address not in result["flood_vteps"]:
                    result["flood_vteps"].append(address)
    return result
