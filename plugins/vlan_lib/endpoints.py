# -*- coding: utf-8 -*-
"""Endpoint tables: bulk JSON collection, IPv6 neighbors, samples, ARP refresh.

Per-VLAN discovery reads the MAC and ARP tables one VLAN at a time with text
commands (parsers.py). For a range of VLANs, or several samples, it is cheaper
and sturdier to read each switch's whole MAC, ARP and IPv6 neighbor tables
once as JSON and split them by VLAN here. Each function returns the same entry
shapes the text parsers do, so the rest of discovery does not care which way
the data came in.
"""

import ipaddress
import json
import re

from vlan_lib.parsers import (
    _MAC_OVERLAY_RE,
    _MAC_PEER_LINK_RE,
    _MAC_SELF_RE,
    _mac_is_multicast,
    parse_svi_details,
)
from vlan_lib.common import _MAC_ADDRESS_RE, _PORT_NAME_RE
from vlan_lib.parsers import normalize_mac_address
from vlan_lib.verify import ping_succeeded

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

_BULK_COMMANDS = {
    "eos": {
        "mac": "show mac address-table | json",
        "arp": "show ip arp vrf all | json",
        "nd": "show ipv6 neighbors vrf all | json",
    },
    "nxos": {
        "mac": "show mac address-table | json",
        "arp": "show ip arp vrf all | json",
        "nd": "show ipv6 neighbor vrf all | json",
    },
}


def endpoint_bulk_command(platform, table):
    """The whole-table JSON command for *table* (mac, arp, nd), or ''."""
    return _BULK_COMMANDS.get(str(platform).lower(), {}).get(table, "")


def nd_discovery_command(platform, svi_vrf=None):
    """IPv6 neighbor command for one VRF (text); rows are split by VLAN after."""
    vrf = svi_vrf or "default"
    plat = str(platform).lower()
    if plat == "eos":
        return "show ipv6 neighbors vrf %s" % vrf
    if plat == "nxos":
        return "show ipv6 neighbor vrf %s" % vrf
    return ""


def arp_refresh_command(platform, ip_addr, vrf=None):
    """One ping that makes a silent host answer ARP (refresh, not a test)."""
    vrf = vrf or "default"
    if str(platform).lower() == "nxos":
        return "ping %s vrf %s count 1 timeout 1" % (ip_addr, vrf)
    return "ping vrf %s %s repeat 1 timeout 1" % (vrf, ip_addr)


def endpoint_collection_mode(setting, vlan_count, passes=1, refresh=False, min_vlans=5, platform="eos"):
    """``bulk`` or ``per_vlan`` for one switch.

    *setting* is ``bulk``, ``per_vlan`` or ``auto``. Auto reads the whole
    tables when the run covers *min_vlans* VLANs or more, or takes more than
    one sample. Platforms without bulk commands always go per VLAN.
    """
    if not endpoint_bulk_command(platform, "mac"):
        return "per_vlan"
    setting = str(setting or "auto").lower()
    if setting in ("bulk", "per_vlan"):
        return setting
    try:
        many = int(vlan_count) >= int(min_vlans)
    except (TypeError, ValueError):
        many = False
    return "bulk" if many or int(passes or 1) > 1 else "per_vlan"


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

_VLAN_IFACE_RE = re.compile(r"(?<![A-Za-z0-9])vl(?:an)?\s*(\d{1,4})(?!\d)", re.I)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _ipv4(text):
    try:
        return str(ipaddress.IPv4Address(str(text).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return ""


def _ipv6(text):
    candidate = str(text or "").strip().split("%")[0]
    if ":" not in candidate:
        return ""
    try:
        return str(ipaddress.IPv6Address(candidate))
    except (ipaddress.AddressValueError, ValueError):
        return ""


def ipv6_is_link_local(address):
    try:
        return ipaddress.IPv6Address(address).is_link_local
    except (ipaddress.AddressValueError, ValueError):
        return False


def _split_interface(text):
    """``"Vlan3000, Ethernet5"`` -> (vlan_id or None, svi, port)."""
    text = str(text or "")
    vlan = _VLAN_IFACE_RE.search(text)
    port = ""
    for part in [p.strip() for p in text.split(",")]:
        if vlan and _VLAN_IFACE_RE.fullmatch(part):
            continue
        match = _PORT_NAME_RE.search(part)
        if match:
            port = match.group(0)
            break
    vid = int(vlan.group(1)) if vlan else None
    return vid, ("Vlan%d" % vid if vid is not None else ""), port


def _load_json(text):
    """Parsed JSON, or None when *text* is empty or not JSON."""
    if isinstance(text, (dict, list)):
        return text
    text = str(text or "").strip()
    if not text:
        return None
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def _unreadable(table, output):
    if not str(output or "").strip():
        return "no %s output" % table
    return "%s output is not JSON" % table


def _walk_rows(data, vrf=""):
    """Yield ``(row, vrf)`` for every dict in *data*, tracking the VRF it sits in.

    EOS nests rows under ``{"vrfs": {"<name>": {...}}}``; NX-OS puts the VRF
    name in the row's parent (``vrf-name-out``).
    """
    if isinstance(data, list):
        for item in data:
            for found in _walk_rows(item, vrf):
                yield found
        return
    if not isinstance(data, dict):
        return
    name = _first(data, ("vrf-name-out", "vrf_name", "vrf"))
    if isinstance(name, str) and name:
        vrf = name
    yield data, vrf
    for key, value in data.items():
        if key == "vrfs" and isinstance(value, dict):
            for name, inner in value.items():
                for found in _walk_rows(inner, name):
                    yield found
        elif isinstance(value, (dict, list)):
            for found in _walk_rows(value, vrf):
                yield found


def _mac_valued(row):
    """The MAC in a table row, from known keys or any MAC-shaped value."""
    value = _first(row, ("macAddress", "disp_mac_addr", "mac", "hwAddress", "mac-addr", "mac_address"))
    if value is not None and _MAC_ADDRESS_RE.fullmatch(str(value).strip()):
        return normalize_mac_address(value)
    return ""


# --------------------------------------------------------------------------
# MAC table (JSON)
# --------------------------------------------------------------------------


def _mac_port_kind(port, flag=""):
    if str(flag).strip().upper().startswith("G"):
        return "self"
    if _MAC_PEER_LINK_RE.search(port):
        return "peer_link"
    if _MAC_OVERLAY_RE.search(port):
        return "overlay"
    if _MAC_SELF_RE.search(port) or str(port).strip().lower() in ("router", "cpu", "switch"):
        return "self"
    if _PORT_NAME_RE.search(port):
        return "port"
    return ""


def parse_mac_table_json(output, vlan_ids=None):
    """Split a whole-switch ``show mac address-table | json`` by VLAN.

    Returns ``{ok, entries: {vlan_id: [entry]}, rows, unparsed, problems}``.
    Entries match parse_mac_table_detail: ``mac, interface, entry_type,
    vlan_id, port_kind`` (+ ``remote_vtep``, ``moves``). EOS rows live under
    ``unicastTable.tableEntries``; NX-OS under ``TABLE_mac_address.ROW_mac_address``.
    *ok* is false when the output is not JSON or rows could not be read.
    """
    wanted = {int(v) for v in vlan_ids} if vlan_ids else None
    result = {"ok": False, "entries": {}, "rows": 0, "multicast": 0, "unparsed": [], "problems": []}
    data = _load_json(output)
    if data is None:
        result["problems"].append(_unreadable("MAC table", output))
        return result
    seen = set()
    for row, _vrf in _walk_rows(data):
        mac = _mac_valued(row)
        if not mac:
            continue
        result["rows"] += 1
        vlan = _first(row, ("vlanId", "disp_vlan", "vlan", "vlan_id"))
        try:
            vid = int(str(vlan).strip())
        except (TypeError, ValueError):
            result["unparsed"].append(json.dumps(row, sort_keys=True))
            continue
        if _mac_is_multicast(mac):
            result["multicast"] += 1
            continue
        if wanted is not None and vid not in wanted:
            continue
        port = str(_first(row, ("interface", "disp_port", "port", "ports")) or "").strip()
        flag = str(_first(row, ("disp_type",)) or "")
        kind = _mac_port_kind(port, flag)
        if not kind:
            result["unparsed"].append(json.dumps(row, sort_keys=True))
            continue
        etype = str(_first(row, ("entryType", "type")) or "").lower()
        static = str(_first(row, ("disp_is_static",)) or "").lower() in ("enabled", "true", "yes")
        if "static" in etype or "configured" in etype or static or flag.strip().upper().startswith("G"):
            entry_type = "static"
        else:
            entry_type = "dynamic"
        interface = port
        if kind == "port":
            interface = _PORT_NAME_RE.search(port).group(0)
        elif kind == "peer_link":
            interface = "vPC Peer-Link"
        key = (vid, mac, interface.lower())
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "mac": mac,
            "interface": interface,
            "entry_type": entry_type,
            "vlan_id": vid,
            "port_kind": kind,
        }
        overlay = _MAC_OVERLAY_RE.search(port) if kind == "overlay" else None
        if overlay and overlay.group("vtep"):
            entry["remote_vtep"] = overlay.group("vtep")
        moves = _first(row, ("moves",))
        if moves is not None:
            try:
                entry["moves"] = int(moves)
            except (TypeError, ValueError):
                pass
        result["entries"].setdefault(vid, []).append(entry)
    if result["unparsed"]:
        result["problems"].append(
            "%d MAC table row(s) not understood, e.g. %s" % (len(result["unparsed"]), result["unparsed"][0])
        )
    result["ok"] = not result["unparsed"]
    return result


# --------------------------------------------------------------------------
# ARP and IPv6 neighbors (JSON)
# --------------------------------------------------------------------------


def _neighbor_rows(data, family):
    """Yield ``(row, vrf, address)`` for neighbor rows of *family* (4 or 6)."""
    keys = (
        ("address", "ip-addr-out", "ip_addr", "ipAddress", "ip")
        if family == 4
        else ("address", "ipv6-addr", "ipv6_addr", "ip-addr-out", "addr", "ipv6Address")
    )
    convert = _ipv4 if family == 4 else _ipv6
    for row, vrf in _walk_rows(data):
        value = _first(row, keys)
        address = convert(value) if isinstance(value, str) else ""
        if not address and _mac_valued(row):
            # Key names differ between releases: any address-shaped value will do.
            address = next((convert(v) for v in row.values() if isinstance(v, str) and convert(v)), "")
        if address:
            yield row, vrf, address


def parse_neighbors_json(output, family=4, vlan_ids=None):
    """Split a whole-switch ARP (family 4) or IPv6 neighbor (6) JSON by VLAN.

    Returns ``{ok, entries: {vlan_id: [entry]}, rows, incomplete, problems}``.
    Entries match parse_arp_entries: ``ip, mac, interface, port, age, vrf``.
    EOS: ``{"vrfs": {vrf: {"ipV4Neighbors": [{address, hwAddress,
    interface: "Vlan100, Ethernet3", age}]}}}``. NX-OS: ``TABLE_vrf.ROW_vrf
    [{vrf-name-out, TABLE_adj.ROW_adj [{ip-addr-out, mac, intf-out}]}]``.
    Rows on routed ports (no VLAN) are counted and left out.
    """
    wanted = {int(v) for v in vlan_ids} if vlan_ids else None
    result = {"ok": False, "entries": {}, "rows": 0, "incomplete": 0, "not_vlan": 0, "problems": []}
    data = _load_json(output)
    if data is None:
        result["problems"].append(_unreadable("ARP" if family == 4 else "IPv6 neighbor", output))
        return result
    seen = set()
    for row, vrf, address in _neighbor_rows(data, family):
        result["rows"] += 1
        mac = _mac_valued(row)
        if not mac:
            result["incomplete"] += 1
            continue
        interface = str(_first(row, ("interface", "intf-out", "intf", "interface_name")) or "")
        vid, svi, port = _split_interface(interface)
        if vid is None:
            result["not_vlan"] += 1
            continue
        if wanted is not None and vid not in wanted:
            continue
        key = (vid, address, mac)
        if key in seen:
            continue
        seen.add(key)
        age = _first(row, ("age", "time-stamp", "time_stamp"))
        result["entries"].setdefault(vid, []).append(
            {
                "ip": address,
                "mac": mac,
                "interface": svi,
                "port": port,
                "age": "" if age is None else str(age),
                "vrf": vrf or "",
            }
        )
    result["ok"] = True
    return result


def parse_nd_entries(nd_output, vlan_id=None):
    """Parse ``show ipv6 neighbor(s)`` text into ``[{ip, mac, interface, port}]``.

    Handles EOS (``Vl100, Et5``) and NX-OS, which wraps a long address onto
    its own line and prints the rest of the row on the next one.
    """
    entries = []
    seen = set()
    pending = ""
    want = int(vlan_id) if vlan_id is not None else None
    for raw_line in str(nd_output or "").splitlines():
        tokens = [t for t in re.split(r"[\s,]+", raw_line.strip()) if t]
        if not tokens:
            continue
        address = ""
        for token in tokens:
            address = _ipv6(token)
            if address:
                break
        mac = next((normalize_mac_address(t) for t in tokens if _MAC_ADDRESS_RE.fullmatch(t)), "")
        if address and not mac:
            pending = address
            continue
        if not mac:
            continue
        address = address or pending
        pending = ""
        if not address:
            continue
        vid, svi, port = _split_interface(raw_line)
        if vid is None or (want is not None and vid != want):
            continue
        key = (address, mac)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"ip": address, "mac": mac, "interface": svi, "port": port})
    return entries


# --------------------------------------------------------------------------
# Samples
# --------------------------------------------------------------------------


def parse_endpoint_tables_json(platform, mac_output, arp_output, nd_output=None, vlan_ids=None):
    """One sample of a switch's endpoint tables, kept for *vlan_ids* only.

    Returns ``{ok, mac, arp, nd, problems}`` where each table maps
    ``str(vlan_id)`` to entries. *ok* is false when the MAC or ARP table could
    not be read; discovery then falls back to per-VLAN commands.
    """
    mac = parse_mac_table_json(mac_output, vlan_ids)
    arp = parse_neighbors_json(arp_output, 4, vlan_ids)
    nd = parse_neighbors_json(nd_output, 6, vlan_ids) if str(nd_output or "").strip() else None
    problems = list(mac["problems"]) + list(arp["problems"])
    if nd is not None:
        problems.extend(nd["problems"])
    return {
        "ok": bool(mac["ok"] and arp["ok"]),
        "platform": str(platform).lower(),
        "mac": {str(k): v for k, v in mac["entries"].items()},
        "arp": {str(k): v for k, v in arp["entries"].items()},
        "nd": {str(k): v for k, v in (nd["entries"] if nd and nd["ok"] else {}).items()},
        "counts": {
            "mac_rows": mac["rows"],
            "arp_rows": arp["rows"],
            "arp_incomplete": arp["incomplete"],
            "nd_rows": nd["rows"] if nd else 0,
        },
        "problems": problems,
    }


def _merge_table(samples, table, key_fields):
    merged = {}
    order = {}
    for sample in samples:
        for vid, entries in (sample.get(table) or {}).items():
            bucket = merged.setdefault(vid, {})
            keys = order.setdefault(vid, [])
            for entry in entries:
                key = tuple(str(entry.get(field, "")).lower() for field in key_fields)
                if key not in bucket:
                    keys.append(key)
                    bucket[key] = dict(entry, seen=0)
                else:
                    seen = bucket[key]["seen"]
                    bucket[key].update(entry)
                    bucket[key]["seen"] = seen
                bucket[key]["seen"] += 1
    return {vid: [merged[vid][key] for key in order[vid]] for vid in merged}


def merge_endpoint_samples(samples):
    """Union of several samples; every entry records how many samples saw it.

    Returns ``{ok, passes, mac, arp, nd, problems}``. *ok* needs every sample
    readable; a switch whose bulk tables could not be read falls back to
    per-VLAN commands.
    """
    samples = [s for s in samples or [] if isinstance(s, dict)]
    problems = []
    for sample in samples:
        for problem in sample.get("problems") or []:
            if problem not in problems:
                problems.append(problem)
    return {
        "ok": bool(samples) and all(s.get("ok") for s in samples),
        "passes": len(samples),
        "mac": _merge_table(samples, "mac", ("mac", "interface", "port_kind")),
        "arp": _merge_table(samples, "arp", ("ip", "mac")),
        "nd": _merge_table(samples, "nd", ("ip", "mac")),
        "problems": problems,
    }


def endpoint_tables_for_vlan(tables, vlan_id):
    """The slice of merged bulk tables for one VLAN, shaped for discovery."""
    tables = tables or {}
    key = str(int(vlan_id))
    macs = list((tables.get("mac") or {}).get(key) or [])
    return {
        "passes": tables.get("passes") or 1,
        "mac_entries": [e for e in macs if e.get("port_kind") == "port"],
        "peer_link_mac_entries": [e for e in macs if e.get("port_kind") == "peer_link"],
        "overlay_mac_entries": [e for e in macs if e.get("port_kind") == "overlay"],
        "arp_entries": list((tables.get("arp") or {}).get(key) or []),
        "nd_entries": list((tables.get("nd") or {}).get(key) or []),
        "problems": list(tables.get("problems") or []),
    }


# --------------------------------------------------------------------------
# ARP refresh (ping sweep)
# --------------------------------------------------------------------------


def arp_refresh_targets(svi_results, platform="eos", max_hosts=1024):
    """Addresses to ping from this switch's SVIs, before tables are read.

    *svi_results* are looped ``show running-config interface VlanN`` results
    (``item`` is the VLAN record). Every host address in each SVI subnet is a
    target, except the switch's own and gateway addresses. Shut SVIs are
    skipped. Returns ``{targets: [{vlan_id, vrf, ip}], skipped: [reason]}``;
    once *max_hosts* is reached, further subnets are skipped and named.
    """
    targets = []
    skipped = []
    budget = int(max_hosts)
    for result in svi_results or []:
        if not isinstance(result, dict) or result.get("skipped"):
            continue
        vlan = result.get("item") or {}
        vlan_id = vlan.get("id") if isinstance(vlan, dict) else vlan
        details = parse_svi_details(result.get("stdout") or "", platform)
        if not details.get("present") or details.get("shutdown"):
            continue
        vrf = details.get("vrf") or "default"
        own = {str(cidr).split("/")[0] for cidr in details.get("ip_addresses") or []}
        own.update(details.get("gateway_addresses") or [])
        for cidr in details.get("ip_addresses") or []:
            try:
                network = ipaddress.ip_interface(cidr).network
            except ValueError:
                continue
            if network.version != 4 or network.prefixlen >= 31:
                continue
            hosts = [str(h) for h in network.hosts() if str(h) not in own]
            if len(hosts) > budget:
                skipped.append(
                    "Vlan%s %s: %d addresses, over the %d-address budget" % (vlan_id, network, len(hosts), budget)
                )
                continue
            budget -= len(hosts)
            targets.extend({"vlan_id": vlan_id, "vrf": vrf, "ip": ip_addr} for ip_addr in hosts)
    return {"targets": targets, "skipped": skipped}


def arp_refresh_owner_targets(targets_by_host, hostname):
    """Targets *hostname* pings: one SVI owner per VLAN (the first by name).

    Both gateways of an HSRP pair own the subnet; one sweep refreshes the
    MAC tables on the path, so the second would only double the probes.
    """
    owners = {}
    for host in sorted(targets_by_host or {}):
        for target in (targets_by_host.get(host) or {}).get("targets") or []:
            owners.setdefault(str(target.get("vlan_id")), host)
    mine = (targets_by_host or {}).get(hostname) or {}
    return [t for t in mine.get("targets") or [] if owners.get(str(t.get("vlan_id"))) == hostname]


def arp_refresh_summary(ping_results):
    """``{sent, answered}`` from looped ping results."""
    sent = 0
    answered = 0
    for item in ping_results or []:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        sent += 1
        if ping_succeeded(item.get("stdout")):
            answered += 1
    return {"sent": sent, "answered": answered}
