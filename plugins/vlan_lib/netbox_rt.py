# -*- coding: utf-8 -*-
"""The L2 VNI and route target of a VLAN, from NetBox IPAM route targets.

NetBox is the register of route targets (IPAM > Route Targets). Each
migrated VLAN has one, named ``<vni>:<vlan id>``; its number is the VLAN's
L2 VNI. The numbers come from per-DC ranges (``vni_ranges``): tenant
``0000001`` (FNTS) uses the DC's internal range, every other customer the
external range. Within a range, numbers are handed out in order, whatever
the customer.

For one VLAN in one DC:

- A route target ``<n>:<vlan id>`` whose ``n`` falls in one of the DC's
  ranges is the VLAN's: reuse it. A VLAN ID repeats across DCs; the range
  tells them apart (Omaha 10000-11999, Lisle 20000-23999).
- Otherwise the VLAN gets the lowest number of its range that no route
  target, L2VPN identifier, VLAN record or discovered VXLAN map uses.

These functions only decide. ``plugins/action/netbox_vni_allocate.py``
makes the requests.
"""

import re
from collections.abc import Mapping

from vlan_lib.vni import VNI_POOLS, _range, allocate_vni

_RT_NAME_RE = re.compile(r"^\s*(\d+):(\d+)\s*$")
FNTS_TENANTS = ("0000001",)


def route_target_parts(name):
    """``(number, vlan id)`` for a ``<n>:<vid>`` route target name, else None."""
    match = _RT_NAME_RE.match(str(name or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def dc_spans(data_center, ranges):
    """``{pool: (start, end)}`` for the DC's configured ranges."""
    pools = (ranges or {}).get(data_center) or {}
    spans = {}
    if isinstance(pools, Mapping):
        for pool in VNI_POOLS:
            span = _range(pools.get(pool))
            if span:
                spans[pool] = span
    return spans


def name_prefixes(span, most=4):
    """Few name prefixes that together cover every number of *span*.

    ``(10000, 10999)`` -> ``["10"]``; ``(20000, 21999)`` -> ``["20", "21"]``.
    NetBox filters route targets by ``name__isw`` (starts with); the caller
    drops the names outside the span.
    """
    start, end = span
    best = None
    for length in range(1, len(str(end)) + 1):
        prefixes = sorted({str(number)[:length] for number in range(start, end + 1)})
        if len(prefixes) > most:
            break
        best = prefixes
    # Even one digit gives too many prefixes: use the one-digit ones anyway.
    return best or sorted({str(number)[:1] for number in range(start, end + 1)})


def tenant_name(value):
    """A NetBox tenant as its name: ``{"name": "0000001"}``, or the record's ``"0000001"``."""
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("slug") or ""
    if isinstance(value, bool) or value is None:
        return ""
    return str(value).strip()


def pool_for_tenant(tenant, internal_tenants=FNTS_TENANTS, customer_type="", default_pool="external"):
    """``{pool, source}``: internal for an FNTS tenant, external for any other tenant.

    Without a tenant, the record's ``customer_type`` decides, then
    *default_pool*.
    """
    tenant = tenant_name(tenant)
    internal = {tenant_name(item) for item in internal_tenants or []}
    if tenant:
        pool = "internal" if tenant in internal else "external"
        return {"pool": pool, "source": "tenant %s" % tenant}
    customer_type = str(customer_type or "").strip().lower()
    if customer_type in VNI_POOLS:
        return {"pool": customer_type, "source": "customer_type %s (no tenant)" % customer_type}
    default_pool = str(default_pool or "external").strip().lower()
    return {"pool": default_pool, "source": "default %s (no tenant, no customer_type)" % default_pool}


def existing_route_target(route_targets, vlan_id, data_center, ranges):
    """``{found, vni, name, tenant, pool, problems, elsewhere}``: this VLAN's route target in this DC.

    A route target ``<n>:<vlan id>`` is this DC's when ``n`` is in one of
    the DC's ranges. ``elsewhere`` lists the ones for the same VID in other
    ranges (another DC's VLAN with the same ID).
    """
    spans = dc_spans(data_center, ranges)
    mine, elsewhere = [], []
    for item in route_targets or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        parts = route_target_parts(name)
        if not parts or parts[1] != int(vlan_id):
            continue
        pool = next((p for p, (start, end) in spans.items() if start <= parts[0] <= end), "")
        entry = {
            "vni": parts[0],
            "name": "%s:%s" % parts,
            "pool": pool,
            "tenant": tenant_name(item.get("tenant")) if isinstance(item, Mapping) else "",
            "id": item.get("id") if isinstance(item, Mapping) else None,
        }
        (mine if pool else elsewhere).append(entry)
    result = {"found": False, "vni": None, "name": "", "tenant": "", "pool": "", "id": None,
              "problems": [], "elsewhere": [e["name"] for e in elsewhere]}
    if len(mine) > 1:
        result["problems"].append(
            "NetBox has %d route targets for VLAN %s in %s's ranges (%s): keep one."
            % (len(mine), vlan_id, data_center, ", ".join(e["name"] for e in mine))
        )
        return result
    if mine:
        result.update(mine[0], found=True)
    return result


def used_numbers(route_targets, span, l2vpn_identifiers=(), extra=()):
    """Numbers of *span* taken by route targets, L2VPN identifiers and *extra* (records, switches)."""
    start, end = span
    used = set()
    for item in route_targets or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        parts = route_target_parts(name)
        if parts and start <= parts[0] <= end:
            used.add(parts[0])
    for value in list(l2vpn_identifiers or []) + list(extra or []):
        if str(value).strip().isdigit() and start <= int(value) <= end:
            used.add(int(value))
    return used


def allocate_route_target(vlan_id, data_center, pool, ranges, route_targets, l2vpn_identifiers=(), extra=()):
    """The lowest free number of the DC's *pool* range, as ``{vni, route_target, pool, range, reason, used}``."""
    spans = dc_spans(data_center, ranges)
    span = spans.get(pool)
    used = used_numbers(route_targets, span, l2vpn_identifiers, extra) if span else set()
    result = allocate_vni(data_center, pool, ranges, used)
    result["used"] = len(used)
    result["route_target"] = "%s:%s" % (result["vni"], vlan_id) if result.get("vni") else ""
    if result.get("vni"):
        result["reason"] = "lowest free number in %s %s %s (%d taken)" % (data_center, pool, result["range"], len(used))
    return result


def taken_by_other_vlan(route_targets, vni, vlan_id):
    """Route target names that give *vni* to another VLAN."""
    names = []
    for item in route_targets or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        parts = route_target_parts(name)
        if parts and parts[0] == int(vni) and parts[1] != int(vlan_id):
            names.append("%s:%s" % parts)
    return names
