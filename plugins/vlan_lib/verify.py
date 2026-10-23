# -*- coding: utf-8 -*-
"""Post-change endpoint checks against the discovery baseline."""

import csv
import io
import re


def parse_endpoint_baseline(text, vlan_id):
    """Host IPs for *vlan_id* from a ``<vlan>_endpoints_<ts>.csv`` baseline.

    Gateway and FHRP rows are left out: they are the old gateway, not hosts.
    MAC-only rows have no IP and cannot be pinged.
    """
    ips = []
    for row in csv.DictReader(io.StringIO(str(text or ""))):
        if str(row.get("vlan_id") or "").strip() != str(vlan_id):
            continue
        ip_addr = str(row.get("ip") or "").strip()
        flags = str(row.get("flags") or "")
        if not ip_addr or "gateway" in flags or "fhrp" in flags:
            continue
        if ip_addr not in ips:
            ips.append(ip_addr)
    return ips


_RECEIVED_RE = re.compile(r"(\d+)\s+(?:packets\s+)?received", re.I)


def ping_succeeded(stdout):
    """True when an EOS/NX-OS ping got at least one reply."""
    match = _RECEIVED_RE.search(str(stdout or ""))
    return bool(match and int(match.group(1)) > 0)


def endpoint_reachability(ping_results):
    """Summarize looped ``cli_command`` ping results: ``{reachable, unreachable}``."""
    reachable = []
    unreachable = []
    for item in ping_results or []:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        target = str(item.get("item") or "")
        (reachable if ping_succeeded(item.get("stdout")) else unreachable).append(target)
    return {"reachable": reachable, "unreachable": unreachable}
