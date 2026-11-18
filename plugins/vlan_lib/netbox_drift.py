# -*- coding: utf-8 -*-
"""Drift between a VLAN record (vars/vlans/<dc>/) and NetBox. Read-only.

``netbox_vlan_drift`` compares one record with what NetBox holds for it,
already fetched (plugins/action/netbox_drift.py does the requests):

    nb = {
      "vlan": {...} | {},          # the DC-scoped /ipam/vlans/ match
      "vlan_problem": "",          # several matches, no scope
      "evpn": {...},               # netbox_l2vpn_evpn() result
      "route_targets": {name: [objects]},   # /ipam/route-targets/?name=
      "prefixes": {prefix: [objects]},      # /ipam/prefixes/?prefix=
    }

Each compared field is one item ``{field, record, netbox, status}``:

    ok                 both agree
    differs            both have a value and they disagree
    missing_in_netbox  the record has it, NetBox does not
    netbox_only        NetBox has it, the record does not (NetBox fills it
                       at deploy time: not drift, but worth knowing)
    not_checked        could not be compared (no NetBox VLAN, no VNI)

A VLAN is ``in_sync`` (only ok / netbox_only / not_checked items),
``drift`` (any differs / missing_in_netbox), ``missing`` (no NetBox VLAN) or
``error`` (the lookup could not decide).
"""

from collections.abc import Mapping

from .netbox_rt import tenant_name
from .placement import _vlan_route_target

_DRIFT = ("differs", "missing_in_netbox")


def _name_of(value):
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("slug") or "").strip()
    return str(value or "").strip()


def _record_tenant(record):
    ownership = record.get("ownership") if isinstance(record.get("ownership"), Mapping) else {}
    raw = record.get("tenant") or ownership.get("tenant") or ""
    return tenant_name(raw) if raw not in (None, "") else ""


def _item(field, have, nb, status=None):
    if status is None:
        if have in ("", None, []) and nb in ("", None, []):
            status = "ok"
        elif have in ("", None, []):
            status = "netbox_only"
        elif nb in ("", None, []):
            status = "missing_in_netbox"
        else:
            status = "ok" if have == nb else "differs"
    return {"field": field, "record": have, "netbox": nb, "status": status}


def _vrf_name(vrf):
    """A record VRF as NetBox names it: ``default`` is the global table (no VRF)."""
    text = str(vrf or "").strip()
    return "" if text.lower() in ("", "default", "global") else text


def netbox_vlan_drift(record, nb):
    record = record if isinstance(record, Mapping) else {}
    nb = nb if isinstance(nb, Mapping) else {}
    vlan_id = record.get("id")
    report = {
        "vlan_id": vlan_id,
        "name": str(record.get("vlan_name") or record.get("name") or ""),
        "netbox_vlan_id": None,
        "status": "in_sync",
        "items": [],
        "notes": [],
    }
    if nb.get("error"):
        report.update(status="error", notes=[str(nb["error"])])
        return report
    if nb.get("vlan_problem"):
        report.update(status="error", notes=[str(nb["vlan_problem"])])
        return report
    vlan = nb.get("vlan") if isinstance(nb.get("vlan"), Mapping) else {}
    if not vlan:
        report["status"] = "missing"
        report["items"].append(_item("vlan", vlan_id, None, "missing_in_netbox"))
        report["notes"].append("NetBox has no VLAN %s in this data center's scope" % vlan_id)
        return report
    report["netbox_vlan_id"] = vlan.get("id")
    items = report["items"]

    items.append(_item("name", report["name"], _name_of(vlan.get("name"))))
    items.append(_item("tenant", _record_tenant(record), _name_of(vlan.get("tenant"))))

    evpn = nb.get("evpn") if isinstance(nb.get("evpn"), Mapping) else {}
    nb_vni = evpn.get("vni") if evpn.get("found") else None
    rec_vni = record.get("vni")
    rec_vni = int(rec_vni) if str(rec_vni or "").strip().isdigit() else None
    items.append(_item("vni", rec_vni, nb_vni))
    for problem in evpn.get("problems") or []:
        report["notes"].append(problem)
    if evpn.get("note"):
        report["notes"].append(evpn["note"])

    # The route target the configlets use, from the record (or its VNI from NetBox).
    effective = dict(record)
    if rec_vni is None and nb_vni is not None:
        effective["vni"] = nb_vni
    rt = _vlan_route_target(effective)
    want_import = rt.get("rt_both") or rt.get("rt_import") or ""
    want_export = rt.get("rt_both") or rt.get("rt_export") or ""
    if evpn.get("found"):
        items.append(_item("l2vpn_import_target", want_import, ", ".join(evpn.get("import") or [])))
        items.append(_item("l2vpn_export_target", want_export, ", ".join(evpn.get("export") or [])))
    else:
        items.append(_item("l2vpn", want_import or None, None, "missing_in_netbox" if want_import else "not_checked"))

    # IPAM route target <vni>:<vid> (written by export_vlan_to_netbox.yml).
    if want_import:
        hits = (nb.get("route_targets") or {}).get(want_import) or []
        if not hits:
            items.append(_item("ipam_route_target", want_import, None, "missing_in_netbox"))
        else:
            items.append(_item("ipam_route_target", want_import, _name_of(hits[0].get("name")) or want_import))
            items.append(_item("route_target_tenant", _record_tenant(record), _name_of(hits[0].get("tenant"))))
    else:
        items.append(_item("ipam_route_target", None, None, "not_checked"))

    # Prefixes: in the record's VRF and assigned to this VLAN.
    want_vrf = _vrf_name(record.get("vrf"))
    for prefix in record.get("prefixes") or []:
        prefix = str(prefix).strip()
        hits = (nb.get("prefixes") or {}).get(prefix) or []
        mine = [h for h in hits if _name_of(h.get("vrf")) == want_vrf]
        if not hits:
            items.append(_item("prefix %s" % prefix, prefix, None, "missing_in_netbox"))
            continue
        if not mine:
            items.append(_item("prefix %s vrf" % prefix, want_vrf or "global",
                               ", ".join(_name_of(h.get("vrf")) or "global" for h in hits), "differs"))
            continue
        nb_vlan = mine[0].get("vlan")
        nb_vlan_id = nb_vlan.get("id") if isinstance(nb_vlan, Mapping) else nb_vlan
        items.append(_item("prefix %s vlan" % prefix, vlan.get("id"), nb_vlan_id))

    if any(i["status"] in _DRIFT for i in items):
        report["status"] = "drift"
    return report


def netbox_drift_summary(reports):
    counts = {}
    for r in reports or []:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    return counts


def netbox_drift_markdown(result):
    """Markdown for the drift playbook: ``{data_center, checked_at, reports}``."""
    result = result or {}
    reports = result.get("reports") or []
    counts = netbox_drift_summary(reports)
    lines = [
        "# NetBox drift - %s" % (result.get("data_center") or ""),
        "",
        "Checked %s. Read-only: nothing was written to NetBox." % (result.get("checked_at") or ""),
        "",
        "| Status | VLANs |",
        "|---|---|",
    ]
    for status in ("in_sync", "drift", "missing", "error"):
        lines.append("| %s | %s |" % (status, counts.get(status, 0)))
    lines += ["", "## VLANs", "", "| VLAN | Name | Status | Differences |", "|---|---|---|---|"]
    for r in reports:
        diffs = [i["field"] for i in r.get("items") or [] if i["status"] in _DRIFT]
        lines.append("| %s | %s | %s | %s |" % (r.get("vlan_id"), r.get("name"), r.get("status"), ", ".join(diffs) or "-"))
    lines.append("")
    for r in reports:
        if r.get("status") == "in_sync" and not r.get("notes"):
            continue
        lines += ["### VLAN %s (%s): %s" % (r.get("vlan_id"), r.get("name"), r.get("status")), ""]
        rows = [i for i in r.get("items") or [] if i["status"] != "ok"]
        if rows:
            lines += ["| Field | Record | NetBox | Status |", "|---|---|---|---|"]
            for i in rows:
                lines.append("| %s | %s | %s | %s |" % (
                    i["field"], "-" if i["record"] in (None, "") else i["record"],
                    "-" if i["netbox"] in (None, "") else i["netbox"], i["status"]))
            lines.append("")
        for note in r.get("notes") or []:
            lines.append("- %s" % note)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
