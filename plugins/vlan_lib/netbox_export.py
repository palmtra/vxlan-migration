# -*- coding: utf-8 -*-
"""What the NetBox export writes for one VLAN record (no requests here).

``netbox_export_plan(record, data_center, nb)`` takes what NetBox already
holds (fetched by plugins/action/netbox_vlan_export.py):

    nb = {
      "vlan": {...} | {},       # the DC-scoped VLAN match (netbox_vlan_pick)
      "vlan_problem": "",       # several matches and no scope
      "tenant": {...} | None,   # tenancy/tenants ?name=<record tenant>; None: not looked up
      "vrf": {...} | None,      # ipam/vrfs ?name=<record vrf>; None: global table or not looked up
      "group_id": 7 | None,     # the DC's VLAN group (netbox_vlan_query_by_dc group)
      "site_id": 1 | None,      # the DC's site, when there is no group
      "prefixes": {prefix: [objects]},
    }

and returns ``{vlan, prefixes, problems}``:

    vlan:     {action: create | update, id, payload}
    prefixes: [{prefix, action: create | update | none, id, payload}]

A problem (an ambiguous VLAN, a tenant or VRF missing from NetBox) means
nothing is written for the VLAN. Tenants and VRFs are never created here:
they are managed in NetBox.

Scope: a new VLAN goes in the DC's VLAN group when the lookup is scoped by
group, else on its site. An existing VLAN keeps its group and site.
Prefixes: one per record prefix, in the record's VRF (``default`` = global),
assigned to the VLAN and its tenant. An existing prefix in that VRF is
updated (VLAN, tenant); one in another VRF is left alone and a new one is
created in the right VRF.
"""

from collections.abc import Mapping

from .netbox import DESCRIPTION_MAX, ROUTE_TARGET_DESCRIPTION
from .netbox_rt import tenant_name


def record_tenant(record):
    record = record if isinstance(record, Mapping) else {}
    ownership = record.get("ownership") if isinstance(record.get("ownership"), Mapping) else {}
    raw = record.get("tenant") or ownership.get("tenant")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return None  # unquoted id read as a number: unusable (see netbox_route_target)
    return tenant_name(raw) if raw not in (None, "") else ""


def record_vrf(record):
    """The VRF name NetBox uses, ``""`` for the global table (``default``)."""
    text = str((record or {}).get("vrf") or "").strip()
    return "" if text.lower() in ("", "default", "global") else text


def _id(value):
    if isinstance(value, Mapping):
        return value.get("id")
    return value


def _name(value):
    if isinstance(value, Mapping):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def _description(record, data_center):
    text = "Exported from vxlan-migration local VLAN DB (%s). service_type=%s, vni=%s, vrf=%s" % (
        data_center,
        record.get("service_type") or "unknown",
        record.get("vni") if record.get("vni") not in (None, "") else "n/a",
        record.get("vrf") or "default",
    )
    return text[:DESCRIPTION_MAX]


def netbox_export_plan(record, data_center, nb, marker=ROUTE_TARGET_DESCRIPTION):
    record = record if isinstance(record, Mapping) else {}
    nb = nb if isinstance(nb, Mapping) else {}
    vid = record.get("id")
    problems = []

    if nb.get("vlan_problem"):
        problems.append(str(nb["vlan_problem"]))

    tenant = record_tenant(record)
    tenant_id = None
    if tenant is None:
        problems.append('VLAN %s tenant is a number: quote the tenant id in the record (tenant: "0000964")' % vid)
    elif tenant:
        tenant_id = _id(nb.get("tenant"))
        if tenant_id is None:
            problems.append("VLAN %s tenant %s is not in NetBox (Tenancy > Tenants)" % (vid, tenant))

    vrf = record_vrf(record)
    prefixes = [str(p).strip() for p in record.get("prefixes") or [] if str(p).strip()]
    vrf_id = None
    if vrf and prefixes:
        vrf_id = _id(nb.get("vrf"))
        if vrf_id is None:
            problems.append("VLAN %s VRF %s is not in NetBox (IPAM > VRFs): its prefixes cannot be exported" % (vid, vrf))

    existing = nb.get("vlan") if isinstance(nb.get("vlan"), Mapping) else {}
    payload = {
        "vid": int(vid),
        "name": str(record.get("vlan_name") or record.get("name") or ""),
        "status": "active",
        "description": _description(record, data_center),
    }
    if tenant_id is not None:
        payload["tenant"] = tenant_id
    if not existing:
        if nb.get("group_id") is not None:
            payload["group"] = nb["group_id"]
        elif nb.get("site_id") is not None:
            payload["site"] = nb["site_id"]
    vlan = {"action": "update" if existing else "create", "id": existing.get("id"), "payload": payload}

    vlan_name = payload["name"]
    suffix = " - %s" % marker
    prefix_description = (vlan_name[:DESCRIPTION_MAX - len(suffix)] + suffix) if vlan_name else marker
    planned = []
    for prefix in prefixes:
        hits = [h for h in (nb.get("prefixes") or {}).get(prefix) or [] if isinstance(h, Mapping)]
        mine = [h for h in hits if _name(h.get("vrf")) == vrf]
        body = {"prefix": prefix, "vrf": vrf_id, "status": "active", "description": prefix_description}
        if tenant_id is not None:
            body["tenant"] = tenant_id
        if mine:
            current = mine[0]
            update = {}
            if existing and _id(current.get("vlan")) != existing.get("id"):
                update["vlan"] = existing.get("id")
            elif not existing:
                update["vlan"] = None  # filled with the new VLAN's id once it is created
            if tenant_id is not None and _id(current.get("tenant")) != tenant_id:
                update["tenant"] = tenant_id
            planned.append({"prefix": prefix, "action": "update" if update else "none",
                            "id": current.get("id"), "payload": update})
        else:
            body["vlan"] = existing.get("id") if existing else None
            planned.append({"prefix": prefix, "action": "create", "id": None, "payload": body})

    return {"vlan": vlan, "prefixes": planned, "problems": problems}
