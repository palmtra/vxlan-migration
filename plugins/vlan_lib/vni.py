# -*- coding: utf-8 -*-
"""L2 VNI allocation from per-DC ranges.

Each data center has its own VNI ranges, one per pool (``internal`` and
``customer``), so the same VLAN ID in two DCs never shares a VNI. A VLAN
record without a VNI gets the next free one in its DC and pool. Free means
not in any VLAN record of that DC, not already mapped on a discovered
switch, and not handed to another VLAN earlier in the same run.

The ranges are hard-coded in ``inventory/group_vars/all/vni_ranges.yml`` for
now; NetBox becomes the allocator later.
"""

VNI_POOLS = ("internal", "customer")


def _range(entry):
    if not isinstance(entry, dict):
        return None
    try:
        start = int(entry.get("start"))
        end = int(entry.get("end"))
    except (TypeError, ValueError):
        return None
    if start < 1 or end > 16777215 or start > end:
        return None
    return start, end


def vni_range_problems(ranges):
    """Missing, malformed, or overlapping ranges, as readable lines."""
    problems = []
    spans = []
    for dc, pools in sorted((ranges or {}).items()):
        for pool in VNI_POOLS:
            entry = (pools or {}).get(pool) if isinstance(pools, dict) else None
            span = _range(entry)
            unset = entry is None or (
                isinstance(entry, dict) and entry.get("start") is None and entry.get("end") is None
            )
            if not unset and span is None:
                problems.append("VNI range %s/%s is malformed: %r" % (dc, pool, entry))
            if span:
                spans.append((span, "%s/%s" % (dc, pool)))
    for index, (span_a, name_a) in enumerate(spans):
        for span_b, name_b in spans[index + 1:]:
            if span_a[0] <= span_b[1] and span_b[0] <= span_a[1]:
                problems.append("VNI ranges %s and %s overlap" % (name_a, name_b))
    return problems


def used_vnis(records=None, devices=None):
    """VNIs already taken: VLAN records (L2 and L3) and discovered VXLAN maps."""
    used = set()
    for record in records or []:
        if not isinstance(record, dict):
            continue
        for key in ("vni", "l3_vni"):
            value = record.get(key)
            if str(value or "").strip().isdigit():
                used.add(int(value))
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        vxlan = device.get("vxlan_interface") or {}
        for item in list(device.get("vxlan_mappings") or []) + list(vxlan.get("vlan_vni") or []):
            if isinstance(item, dict) and str(item.get("vni") or "").isdigit():
                used.add(int(item["vni"]))
        for vni in (vxlan.get("vrf_vni") or {}).values():
            if str(vni).isdigit():
                used.add(int(vni))
    return used


def allocate_vni(data_center, pool, ranges, used):
    """Return ``{vni, pool, range, reason}``. ``vni`` is None when none is free."""
    pool = str(pool or "").strip().lower() or "customer"
    pools = (ranges or {}).get(data_center) or {}
    span = _range(pools.get(pool)) if isinstance(pools, dict) else None
    if pool not in VNI_POOLS:
        return {"vni": None, "pool": pool, "range": "", "reason": "vni_pool must be internal or customer"}
    if span is None:
        return {
            "vni": None,
            "pool": pool,
            "range": "",
            "reason": "no %s VNI range configured for %s in vni_ranges" % (pool, data_center),
        }
    label = "%s-%s" % span
    for vni in range(span[0], span[1] + 1):
        if vni not in used:
            return {"vni": vni, "pool": pool, "range": label, "reason": "next free in %s %s %s" % (data_center, pool, label)}
    return {"vni": None, "pool": pool, "range": label, "reason": "%s %s range %s is full" % (data_center, pool, label)}
