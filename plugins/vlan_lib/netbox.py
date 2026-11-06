# -*- coding: utf-8 -*-
"""Turn a VLAN record into a NetBox route target (``/ipam/route-targets/``).

The route target's name is the VLAN's EVPN route target, ``<l2 vni>:<vlan
id>`` (or the record's ``l2_rt``), the same value the configlets put under
``router bgp / vlan``. Tenant and owner come from the record's ownership:

- ``tenant``: the numeric tenant id (``"0000964"``), a NetBox tenant name.
- ``owner``: the customer name (``Rewards Trust``), a NetBox owner name.

Both can sit at the top of a flat record or under ``ownership`` (the
discovery model). The description is the VLAN name followed by a fixed
marker, so every route target this repo writes reads the same.
"""

import json
from collections.abc import Mapping

from .placement import _vlan_route_target

ROUTE_TARGET_DESCRIPTION = "Created by Ansible VXLAN Automation"
# NetBox description field length.
DESCRIPTION_MAX = 200


def _ownership(record, key):
    ownership = record.get("ownership") if isinstance(record.get("ownership"), Mapping) else {}
    return record.get(key) or ownership.get(key)


def netbox_route_target(record, marker=ROUTE_TARGET_DESCRIPTION):
    """``{vlan_id, name, tenant, owner, description, problems}`` for one record.

    ``problems`` lists what stops the route target being written. The
    tenant and owner are checked against NetBox by the caller.
    """
    record = record if isinstance(record, Mapping) else {}
    vlan_id = record.get("id")
    problems = []

    name = _vlan_route_target(dict(record)).get("rt_both", "")
    if not name:
        problems.append("VLAN %s has no VNI, so it has no route target" % vlan_id)

    tenant = _ownership(record, "tenant")
    if isinstance(tenant, int) and not isinstance(tenant, bool):
        # YAML reads an unquoted id made only of the digits 0-7 as octal
        # (0000707 is 455): the id is lost and cannot be recovered here.
        problems.append(
            "VLAN %s tenant is the number %s: quote the tenant id in the record (tenant: \"0000964\")"
            % (vlan_id, tenant)
        )
        tenant = ""
    else:
        tenant = str(tenant or "").strip()
        if not tenant:
            problems.append("VLAN %s has no tenant (ownership.tenant)" % vlan_id)
        elif not tenant.isdigit():
            problems.append("VLAN %s tenant %r is not a numeric tenant id" % (vlan_id, tenant))

    owner = str(_ownership(record, "owner") or "").strip()
    if not owner:
        problems.append("VLAN %s has no owner (ownership.owner, the customer name)" % vlan_id)

    vlan_name = str(record.get("vlan_name") or record.get("name") or "").strip()
    suffix = " - %s" % marker
    description = (vlan_name[:DESCRIPTION_MAX - len(suffix)] + suffix) if vlan_name else marker

    return {
        "vlan_id": vlan_id,
        "name": name,
        "tenant": tenant,
        "owner": owner,
        "description": description,
        "problems": problems,
    }


def netbox_request_error(result, expected=(200, 201)):
    """Why a NetBox ``uri`` request failed, or ``""`` when it did not.

    For a task that keeps ``no_log`` (the request carries the token): the
    status, NetBox's own ``detail`` and the module's message hold no token,
    so they can be shown. A skipped request is not an error.
    """
    result = result if isinstance(result, Mapping) else {}
    if result.get("skipped"):
        return ""
    status = result.get("status", -1)
    expected = [expected] if isinstance(expected, int) else list(expected)
    if status in expected:
        return ""
    msg = str(result.get("msg") or "").strip()
    body = result.get("json") if isinstance(result.get("json"), Mapping) else {}
    # NetBox puts a refusal in "detail" and validation errors under each field.
    detail = str(body.get("detail") or "").strip() or (json.dumps(body, sort_keys=True)[:300] if body else "")
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return (
            "the NetBox TLS certificate is not trusted (%s). Install the CA that signed it on this host, "
            "or set netbox_validate_certs: false" % msg
        )
    if not isinstance(status, int) or status <= 0:
        return "no HTTP answer from NetBox: %s" % (msg or "unknown error")
    reason = "HTTP %s" % status
    if detail:
        reason += ": %s" % detail
    elif msg:
        reason += " (%s)" % msg
    if status in (401, 403):
        reason += ". Check netbox_token and its permissions"
    elif status == 404:
        reason += ". Check netbox_url: it should be the NetBox address without /api"
    return reason


# --------------------------------------------------------------------------
# VNI and route targets from NetBox (L2VPN, type VXLAN-EVPN)
# --------------------------------------------------------------------------

_EVPN_TYPES = ("vxlan-evpn", "vxlan")


def _choice(value):
    """A NetBox choice field: ``{"value": "vxlan-evpn", "label": ...}`` or a plain string."""
    if isinstance(value, Mapping):
        return str(value.get("value") or "").lower()
    return str(value or "").lower()


def _target_names(targets):
    names = []
    for target in targets or []:
        name = target.get("name") if isinstance(target, Mapping) else target
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def netbox_vlan_pick(lookup_json, vlan_id, data_center=""):
    """``{vlan, matches, problem}`` from a ``/ipam/vlans/?vid=`` answer.

    VIDs repeat across sites and VLAN groups (one group per DC). Several
    matches are narrowed to the one whose group or site names *data_center*;
    if that does not leave one, the query needs a scope
    (netbox_vlan_query_by_dc) and the VLAN is not used.
    """
    results = [r for r in ((lookup_json or {}).get("results") or []) if isinstance(r, Mapping)]
    if len(results) > 1 and data_center:
        dc = str(data_center).strip().lower()
        mine = [
            r for r in results
            if any(
                dc in str((r.get(key) or {}).get(field) or "").lower()
                for key in ("group", "site")
                if isinstance(r.get(key), Mapping)
                for field in ("slug", "name")
            )
        ]
        if len(mine) == 1:
            results = mine
    matches = [
        "%s (id %s, %s)" % (
            r.get("name") or "?",
            r.get("id"),
            ", ".join(
                "%s %s" % (key, (r.get(key) or {}).get("slug") or (r.get(key) or {}).get("name"))
                for key in ("site", "group")
                if isinstance(r.get(key), Mapping)
            ) or "no site or group",
        )
        for r in results
    ]
    if len(results) > 1:
        return {
            "vlan": {},
            "matches": matches,
            "problem": "NetBox has %d VLANs with VID %s (%s). Set netbox_vlan_query_by_dc to pick the site or "
            "VLAN group of this data center." % (len(results), vlan_id, "; ".join(matches)),
        }
    return {"vlan": dict(results[0]) if results else {}, "matches": matches, "problem": ""}


def netbox_l2vpn_evpn(terminations_json, l2vpns):
    """The VXLAN-EVPN L2VPN terminated on a VLAN: ``{found, vni, import, export, name, problems}``.

    *terminations_json* is the ``l2vpn-terminations/?vlan_id=`` answer;
    *l2vpns* the full L2VPN objects it names (a list, or looped uri results
    with ``json``). The L2VPN identifier is the VNI; its import and export
    route targets are the VLAN's.
    """
    problems = []
    objects = []
    for item in l2vpns or []:
        body = item.get("json") if isinstance(item, Mapping) and "json" in item else item
        if isinstance(body, Mapping) and body.get("id") is not None:
            objects.append(body)
    if not objects:
        for term in (terminations_json or {}).get("results") or []:
            if isinstance(term, Mapping) and isinstance(term.get("l2vpn"), Mapping):
                objects.append(term["l2vpn"])
    evpn = [o for o in objects if _choice(o.get("type")) in _EVPN_TYPES]
    if not evpn:
        others = ["%s (%s)" % (o.get("name"), _choice(o.get("type")) or "no type") for o in objects]
        return {
            "found": False, "vni": None, "import": [], "export": [], "name": "",
            "problems": [],
            "note": ("NetBox L2VPNs on this VLAN are not VXLAN-EVPN: %s" % ", ".join(others)) if others
            else "NetBox has no L2VPN terminated on this VLAN",
        }
    if len(evpn) > 1:
        problems.append(
            "The VLAN is terminated on %d VXLAN-EVPN L2VPNs in NetBox (%s); it must be one"
            % (len(evpn), ", ".join(str(o.get("name")) for o in evpn))
        )
    l2vpn = evpn[0]
    vni = l2vpn.get("identifier")
    try:
        vni = int(vni) if vni not in (None, "") else None
    except (TypeError, ValueError):
        problems.append("NetBox L2VPN %s identifier %r is not a VNI" % (l2vpn.get("name"), vni))
        vni = None
    if vni is None and not problems:
        problems.append("NetBox L2VPN %s has no identifier (the VNI)" % l2vpn.get("name"))
    return {
        "found": True,
        "vni": vni,
        "import": _target_names(l2vpn.get("import_targets")),
        "export": _target_names(l2vpn.get("export_targets")),
        "name": str(l2vpn.get("name") or ""),
        "problems": problems,
        "note": "",
    }


def merge_netbox_evpn(record, evpn):
    """Apply a NetBox L2VPN's VNI and route targets to a VLAN record.

    Returns ``{record, vni_source, rt_source, problems, notes}``. NetBox
    fills what the record leaves empty. A value both have must agree: a
    different VNI, or an explicit ``l2_rt`` that differs, is a problem and
    the record is returned unchanged. More than one import or export target
    is a problem (one route-target line each is generated).
    """
    record = dict(record or {})
    evpn = evpn or {}
    notes = []
    problems = list(evpn.get("problems") or [])
    if not evpn.get("found"):
        if evpn.get("note"):
            notes.append(evpn["note"])
        return {"record": record, "vni_source": "record" if record.get("vni") else "", "rt_source": "",
                "problems": problems, "notes": notes}
    if problems:
        return {"record": record, "vni_source": "", "rt_source": "", "problems": problems, "notes": notes}

    merged = dict(record)
    vni = evpn.get("vni")
    have = record.get("vni")
    vni_source = "netbox"
    if have not in (None, "") and str(have).strip().isdigit():
        if int(have) != vni:
            problems.append(
                "VNI conflict for VLAN %s: the record says %s, NetBox L2VPN %s says %s"
                % (record.get("id"), have, evpn.get("name"), vni)
            )
        vni_source = "record and netbox"
    merged["vni"] = vni

    imports, exports = evpn.get("import") or [], evpn.get("export") or []
    rt_source = ""
    if len(imports) > 1 or len(exports) > 1:
        problems.append(
            "NetBox L2VPN %s has more than one import or export route target (%s / %s); one each is supported"
            % (evpn.get("name"), ", ".join(imports) or "-", ", ".join(exports) or "-")
        )
    elif imports or exports:
        rt_import = imports[0] if imports else (exports[0] if exports else "")
        rt_export = exports[0] if exports else rt_import
        explicit = str(record.get("l2_rt") or "").strip()
        if rt_import == rt_export:
            if explicit and explicit != rt_import:
                problems.append(
                    "Route-target conflict for VLAN %s: the record says %s, NetBox L2VPN %s says %s"
                    % (record.get("id"), explicit, evpn.get("name"), rt_import)
                )
            merged["l2_rt"] = rt_import
            merged.pop("l2_rt_import", None)
            merged.pop("l2_rt_export", None)
        else:
            if explicit:
                problems.append(
                    "Route-target conflict for VLAN %s: the record says both %s, NetBox L2VPN %s says import %s, "
                    "export %s" % (record.get("id"), explicit, evpn.get("name"), rt_import, rt_export)
                )
            merged.pop("l2_rt", None)
            merged["l2_rt_import"] = rt_import
            merged["l2_rt_export"] = rt_export
        rt_source = "netbox"
    else:
        notes.append(
            "NetBox L2VPN %s has no route targets; using %s"
            % (evpn.get("name"), record.get("l2_rt") or "<vni>:<vlan id>")
        )
    if problems:
        return {"record": record, "vni_source": "", "rt_source": "", "problems": problems, "notes": notes}
    return {"record": merged, "vni_source": vni_source, "rt_source": rt_source, "problems": [], "notes": notes}
