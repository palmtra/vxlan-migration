# -*- coding: utf-8 -*-
"""Post-change endpoint checks against the discovery baseline."""

import csv
import io
import re

from vlan_lib.common import sanitize_report_slug


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


# --- Verification report -----------------------------------------------------

_CHECK_ORDER = ("bgp_evpn", "vtep_reachability", "vni_mapping", "mac_arp_learning", "endpoint_reachability")


def _checks(results):
    if not isinstance(results, dict):
        return []
    names = [n for n in _CHECK_ORDER if n in results] + sorted(n for n in results if n not in _CHECK_ORDER)
    out = []
    for name in names:
        item = results.get(name) or {}
        passed = item.get("passed")
        if isinstance(passed, str):
            passed = passed.strip().lower() == "true"
        out.append({"check": name, "passed": bool(passed), "detail": " ".join(str(item.get("detail") or "").split())})
    return out


def build_verification_report(switches, record=None, meta=None):
    """One verification report for a VLAN, across its switches.

    *switches* is a list of ``{host, migration, rollback}``: the per-check
    results (``{check: {passed, detail}}``) of the post-change verification
    and, when one ran, of the re-verification after rollback. A switch with no
    migration results did not report (unreachable, or failed before checking).
    *meta* carries run details (data center, change control, timestamps).
    """
    record = record or {}
    meta = dict(meta or {})
    rows = []
    for item in switches or []:
        if not isinstance(item, dict) or not item.get("host"):
            continue
        migration = _checks(item.get("migration"))
        rollback = _checks(item.get("rollback")) if item.get("rollback") else []
        rows.append(
            {
                "host": str(item["host"]),
                "reported": bool(migration),
                "passed": bool(migration) and all(c["passed"] for c in migration),
                "checks": migration,
                "rollback_checks": rollback,
                "rollback_passed": (all(c["passed"] for c in rollback) if rollback else None),
            }
        )
    rows.sort(key=lambda r: r["host"])
    failed = [
        {"host": r["host"], "check": c["check"], "detail": c["detail"]}
        for r in rows
        for c in r["checks"]
        if not c["passed"]
    ]
    missing = [r["host"] for r in rows if not r["reported"]]
    rolled_back = any(r["rollback_checks"] for r in rows)
    if rolled_back:
        outcome = "rolled_back" if all(r["rollback_passed"] is not False for r in rows) else "escalated"
    else:
        outcome = "passed" if rows and not failed and not missing else "failed"
    return {
        "vlan_id": record.get("id", meta.get("vlan_id")),
        "vlan_name": record.get("name", ""),
        "vlan_slug": sanitize_report_slug(record.get("name") or "vlan%s" % record.get("id", meta.get("vlan_id", ""))),
        "data_center": meta.get("data_center", ""),
        "service_type": record.get("service_type", ""),
        "vni": record.get("vni", ""),
        "vrf": record.get("vrf", ""),
        "outcome": outcome,
        "passed": outcome == "passed",
        "switch_count": len(rows),
        "failed_checks": failed,
        "not_reported": missing,
        "switches": rows,
        "meta": meta,
    }


def verification_report_markdown(report):
    """Markdown for build_verification_report output."""
    r = report or {}
    meta = r.get("meta") or {}
    mark = {True: "pass", False: "FAIL", None: "-"}
    lines = [
        "# VLAN %s (%s) - verification" % (r.get("vlan_id", ""), r.get("vlan_name", "")),
        "",
        "**Outcome: %s**" % str(r.get("outcome", "")).upper().replace("_", " "),
        "",
        "| Field | Value |",
        "|---|---|",
        "| Data center | %s |" % r.get("data_center", ""),
        "| Service type | %s |" % r.get("service_type", ""),
        "| L2 VNI | %s |" % r.get("vni", ""),
        "| VRF | %s |" % r.get("vrf", ""),
        "| Change control | %s |" % (meta.get("change_control_id") or "-"),
        "| Deploy method | %s |" % (meta.get("deploy_method") or "-"),
        "| Verified at | %s |" % (meta.get("verified_at") or ""),
        "",
    ]
    if r.get("failed_checks") or r.get("not_reported"):
        lines += ["## Action items", ""]
        for item in r.get("failed_checks") or []:
            lines.append("- %s: %s failed. %s" % (item["host"], item["check"], item["detail"]))
        for host in r.get("not_reported") or []:
            lines.append("- %s: no verification result (unreachable, or failed before the checks)." % host)
        lines.append("")
    checks = []
    for row in r.get("switches") or []:
        for c in row.get("checks") or []:
            if c["check"] not in checks:
                checks.append(c["check"])
    lines += ["## Checks per switch", ""]
    if checks:
        lines.append("| Switch | " + " | ".join(checks) + " |")
        lines.append("|---|" + "---|" * len(checks))
        for row in r.get("switches") or []:
            by = {c["check"]: c["passed"] for c in row.get("checks") or []}
            lines.append("| %s | %s |" % (row["host"], " | ".join(mark[by.get(c)] for c in checks)))
    else:
        lines.append("No switch reported results.")
    lines += ["", "## Details", ""]
    for row in r.get("switches") or []:
        lines.append("### %s" % row["host"])
        if not row.get("checks"):
            lines.append("- No result.")
        for c in row.get("checks") or []:
            lines.append("- %s **%s**: %s" % (c["check"], mark[c["passed"]], c["detail"] or "-"))
        if row.get("rollback_checks"):
            lines.append("- After rollback:")
            for c in row["rollback_checks"]:
                lines.append("  - %s **%s**: %s" % (c["check"], mark[c["passed"]], c["detail"] or "-"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
