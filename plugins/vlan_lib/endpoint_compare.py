# -*- coding: utf-8 -*-
"""Compare the endpoints of a VLAN before and after a change.

Discovery's ``endpoint_table`` (one row per active IP, plus MAC-only rows)
is the baseline. After the change, discovery runs again and its table is
compared row by row, with the verification pings when there are any.
"""

from vlan_lib.parsers import normalize_mac_address

_INFRA_FLAGS = frozenset({"gateway", "fhrp_mac"})

# Statuses that need someone to look before the legacy side is pruned.
PROBLEM_STATUSES = ("missing", "mac_changed", "ping_failed")

CSV_COLUMNS = [
    "vlan_id",
    "ip",
    "mac",
    "status",
    "ping",
    "before_switch",
    "before_interface",
    "after_switch",
    "after_interface",
    "after_location",
    "after_mac",
    "note",
]


def _tables(reports):
    """``{vlan_id: endpoint_table}`` from discovery reports."""
    out = {}
    for report in reports or []:
        if not isinstance(report, dict) or report.get("vlan_id") in (None, ""):
            continue
        table = report.get("endpoint_table")
        if isinstance(table, dict):
            out[int(report["vlan_id"])] = table
    return out


def _host_rows(table):
    """Endpoint rows that are hosts (not the gateway or FHRP addresses)."""
    rows = []
    for row in list(table.get("endpoints") or []) + list(table.get("mac_only") or []):
        if not isinstance(row, dict) or set(row.get("flags") or []) & _INFRA_FLAGS:
            continue
        rows.append(dict(row, mac=normalize_mac_address(row.get("mac")) or ""))
    return rows


def _place(row):
    if not row or row.get("location") != "edge":
        return None
    return (row.get("hostname") or "", row.get("interface") or "")


def _ping(ip, reachability):
    if not ip or not isinstance(reachability, dict):
        return ""
    if ip in (reachability.get("reachable") or []):
        return "ok"
    if ip in (reachability.get("unreachable") or []):
        return "failed"
    return ""


def compare_endpoints(before_reports, after_reports, reachability=None):
    """Every endpoint before the change, and where it is after.

    Status per endpoint:

    - ``same_port``: learned on the same switch port.
    - ``moved``: learned on another edge port (expected when hosts are
      re-cabled to fabric leaves).
    - ``not_located``: still in ARP / the MAC tables, but only on trunks or
      not in a MAC table (``after_location`` says which).
    - ``mac_changed``: the IP answers from another MAC.
    - ``mac_only``: no ARP entry any more, the MAC is still learned.
    - ``reachable_not_learned``: in no table, but answered the ping.
    - ``ping_failed``: in the tables, but did not answer the ping.
    - ``missing``: in no table and no ping answer.

    ``new`` lists endpoints seen only after. *reachability* is the
    verification's ``{reachable, unreachable}``.
    """
    before = _tables(before_reports)
    after = _tables(after_reports)
    rows = []
    new = []
    for vlan_id in sorted(before):
        after_rows = _host_rows(after.get(vlan_id) or {})
        by_ip = {row["ip"]: row for row in after_rows if row.get("ip")}
        by_mac = {}
        for row in after_rows:
            # Prefer the row that places the MAC on an edge port.
            if row["mac"] and (row["mac"] not in by_mac or (_place(row) and not _place(by_mac[row["mac"]]))):
                by_mac[row["mac"]] = row
        seen_ips, seen_macs = set(), set()
        for old in _host_rows(before[vlan_id]):
            ip = old.get("ip") or ""
            mac = old["mac"]
            ping = _ping(ip, reachability)
            now = by_ip.get(ip) if ip else None
            note = ""
            if now and mac and now["mac"] and now["mac"] != mac:
                status = "mac_changed"
            else:
                if now is None and mac in by_mac:
                    now = by_mac[mac]
                    if ip:
                        note = "no ARP entry after the change"
                if now is None:
                    status = "reachable_not_learned" if ping == "ok" else "missing"
                elif _place(now) is None:
                    status = "not_located"
                elif _place(old) and _place(now) != _place(old):
                    status = "moved"
                elif ip and note:
                    status = "mac_only"
                else:
                    status = "same_port"
                if status not in ("missing", "reachable_not_learned") and ping == "failed":
                    status = "ping_failed"
            if status == "missing" and "intermittent" in (old.get("flags") or []):
                note = "intermittent before the change"
            if ip:
                seen_ips.add(ip)
            if mac:
                seen_macs.add(mac)
            now = now or {}
            rows.append(
                {
                    "vlan_id": vlan_id,
                    "ip": ip,
                    "mac": mac,
                    "status": status,
                    "ping": ping,
                    "before_switch": old.get("hostname") or "",
                    "before_interface": old.get("interface") or "",
                    "after_switch": now.get("hostname") or "",
                    "after_interface": now.get("interface") or "",
                    "after_location": now.get("location") or "",
                    "after_mac": now.get("mac") or "",
                    "note": note,
                }
            )
        for row in after_rows:
            if (row.get("ip") and row["ip"] in seen_ips) or (row["mac"] and row["mac"] in seen_macs):
                continue
            new.append(
                {
                    "vlan_id": vlan_id,
                    "ip": row.get("ip") or "",
                    "mac": row["mac"],
                    "switch": row.get("hostname") or "",
                    "interface": row.get("interface") or "",
                }
            )
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    problems = [row for row in rows if row["status"] in PROBLEM_STATUSES]
    return {
        "compared": bool(before) and bool(after),
        "vlans": sorted(before),
        "counts": counts,
        "counts_line": ", ".join("%d %s" % (counts[key], key) for key in sorted(counts, key=lambda k: (-counts[k], k))),
        "total": len(rows),
        "problems": len(problems),
        "problem_lines": [
            "VLAN %s %s %s: %s%s"
            % (
                row["vlan_id"],
                row["ip"] or "-",
                row["mac"] or "-",
                row["status"],
                (" (" + row["note"] + ")") if row["note"] else "",
            )
            for row in problems
        ],
        "rows": rows,
        "new": new,
    }


def endpoint_comparison_csv(comparison):
    """The comparison rows as CSV text (header included)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in (comparison or {}).get("rows") or []:
        writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})
    return buffer.getvalue()


def strip_endpoint_tables(reports):
    """Only what the comparison needs from each report (keeps facts small)."""
    return [
        {"vlan_id": report.get("vlan_id"), "endpoint_table": report.get("endpoint_table") or {}}
        for report in reports or []
        if isinstance(report, dict)
    ]
