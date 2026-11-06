# -*- coding: utf-8 -*-
"""Action plugin: a VLAN's L2 VNI and route target from NetBox IPAM route targets.

Runs on the control node. Reads NetBox, decides with
``plugins/vlan_lib/netbox_rt.py``, and writes one route target only when
``reserve`` is true (the role asks for an approval word first).

Arguments:
  url, token, validate_certs (false), timeout (30)
  vlan_id, data_center, ranges (vni_ranges)
  record            the VLAN record (vni, l2_rt, tenant / ownership.tenant,
                    customer_type, name); may be empty for a VLAN not in the DB
  vlan_query        extra /ipam/vlans/ filters for this DC (group, site)
  internal_tenants  tenants that use the internal range (["0000001"])
  default_pool      pool without a tenant or customer_type (external)
  extra_used        VNIs taken outside NetBox (VLAN records, discovered maps)
  l2vpn_apps        where L2VPNs live (["vpn", "ipam"])
  reserve           true: create the route target when NetBox has none
  description_marker  text after the VLAN name in the description

Returns: vni, route_target, source (netbox | record | allocated | ""),
pool, pool_source, range, tenant, existing, reserved, would_reserve,
problems (stop the run), notes. The token is never returned.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import sys
from urllib.parse import urlencode, urlsplit

from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.urls import open_url
from ansible.plugins.action import ActionBase

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from vlan_lib.netbox import ROUTE_TARGET_DESCRIPTION, DESCRIPTION_MAX, netbox_vlan_pick  # noqa: E402
from vlan_lib.netbox_rt import (  # noqa: E402
    FNTS_TENANTS,
    allocate_route_target,
    dc_spans,
    existing_route_target,
    name_prefixes,
    pool_for_tenant,
    taken_by_other_vlan,
    tenant_name,
)

_PAGE = 1000


class NetboxError(Exception):
    pass


class _Netbox(object):
    def __init__(self, url, token, validate_certs, timeout):
        self.base = str(url or "").rstrip("/")
        self.headers = {"Authorization": "Token %s" % token, "Accept": "application/json"}
        self.validate_certs = validate_certs
        self.timeout = timeout

    def request(self, path, params=None, method="GET", body=None, missing_ok=False):
        url = "%s/api/%s" % (self.base, path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        headers = dict(self.headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        try:
            response = open_url(
                url, data=data, headers=headers, method=method,
                validate_certs=self.validate_certs, timeout=self.timeout,
            )
            return json.loads(response.read() or b"{}")
        except HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            detail = ""
            try:
                payload = json.loads(exc.read() or b"{}")
                detail = payload.get("detail") or json.dumps(payload, sort_keys=True)[:300]
            except (ValueError, AttributeError):
                pass
            hint = ""
            if exc.code in (401, 403):
                hint = ". Check netbox_token and its permissions"
            elif exc.code == 404:
                hint = ". Check netbox_url: it should be the NetBox address without /api"
            raise NetboxError("%s /api/%s: HTTP %s%s%s" % (method, path, exc.code, (": %s" % detail) if detail else "", hint))
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                raise NetboxError(
                    "the NetBox TLS certificate is not trusted (%s). Install the CA that signed it on this host, "
                    "or set netbox_validate_certs: false" % reason
                )
            raise NetboxError("no HTTP answer from NetBox for /api/%s: %s" % (path, reason))

    def all(self, path, params, missing_ok=False):
        """Every result of a list query, following ``next``."""
        params = dict(params, limit=_PAGE)
        page = self.request(path, params, missing_ok=missing_ok)
        if page is None:
            return None
        results = list(page.get("results") or [])
        while page.get("next"):
            nxt = urlsplit(page["next"])
            # NetBox may answer with its own host name; keep ours.
            page = self.request(nxt.path.split("/api/", 1)[-1], dict(_query(nxt.query)))
            results.extend(page.get("results") or [])
        return results


def _query(text):
    from urllib.parse import parse_qsl

    return parse_qsl(text, keep_blank_values=True)


def _record_tenant(record):
    ownership = record.get("ownership") if isinstance(record.get("ownership"), dict) else {}
    return tenant_name(record.get("tenant") or ownership.get("tenant") or "")


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset((
        "url", "token", "validate_certs", "timeout", "vlan_id", "data_center", "ranges", "record",
        "vlan_query", "internal_tenants", "default_pool", "extra_used", "l2vpn_apps", "reserve",
        "description_marker",
    ))

    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        args = self._task.args
        unknown = sorted(set(args) - self._VALID_ARGS)
        if unknown:
            result.update(failed=True, msg="netbox_vni_allocate: unknown arguments %s" % ", ".join(unknown))
            return result
        netbox = _Netbox(
            args.get("url"), args.get("token"),
            self._bool(args.get("validate_certs", False)), int(args.get("timeout") or 30),
        )
        try:
            result.update(self._decide(netbox, args))
        except NetboxError as exc:
            result.update(failed=True, msg=str(exc))
        return result

    @staticmethod
    def _bool(value):
        return str(value).strip().lower() in ("true", "yes", "1", "on")

    def _decide(self, netbox, args):
        vlan_id = int(args["vlan_id"])
        dc = str(args.get("data_center") or "").strip()
        ranges = args.get("ranges") or {}
        record = args.get("record") if isinstance(args.get("record"), dict) else {}
        notes, problems = [], []
        out = {
            "changed": False, "vlan_id": vlan_id, "data_center": dc, "vni": None, "route_target": "",
            "source": "", "pool": "", "pool_source": "", "range": "", "tenant": "", "existing": {},
            "reserved": False, "would_reserve": {}, "problems": problems, "notes": notes,
        }
        if not dc_spans(dc, ranges):
            problems.append("No VNI range for data center %r in vni_ranges." % dc)
            return out

        # The VLAN's tenant: NetBox first (the VLAN group of this DC), then the record.
        params = {"vid": vlan_id}
        params.update(args.get("vlan_query") or {})
        picked = netbox_vlan_pick({"results": netbox.all("ipam/vlans/", params)}, vlan_id, dc)
        tenant = tenant_name((picked["vlan"] or {}).get("tenant"))
        if picked["problem"]:
            notes.append(picked["problem"])
        if tenant:
            tenant_source = "NetBox VLAN %s" % picked["vlan"].get("id")
        else:
            tenant = _record_tenant(record)
            tenant_source = "VLAN record" if tenant else ""
            if not picked["vlan"] and not picked["problem"]:
                notes.append("VLAN %s is not in NetBox for %s." % (vlan_id, dc))
        pool = pool_for_tenant(
            tenant, args.get("internal_tenants") or FNTS_TENANTS,
            record.get("customer_type") or "", args.get("default_pool") or "external",
        )
        out.update(tenant=tenant, tenant_source=tenant_source, pool=pool["pool"], pool_source=pool["source"])
        span = dc_spans(dc, ranges).get(pool["pool"])
        out["range"] = "%s-%s" % span if span else ""

        # A route target this VLAN already has in this DC's ranges wins.
        existing = existing_route_target(
            netbox.all("ipam/route-targets/", {"name__iew": ":%s" % vlan_id}), vlan_id, dc, ranges
        )
        out["existing"] = existing
        problems.extend(existing["problems"])
        if existing["elsewhere"]:
            notes.append("Route targets for VLAN %s in other data centers' ranges: %s." % (vlan_id, ", ".join(existing["elsewhere"])))
        if problems:
            return out

        record_vni = record.get("vni")
        record_vni = int(record_vni) if str(record_vni or "").strip().isdigit() and int(record_vni) > 0 else None
        if existing["found"]:
            out.update(vni=existing["vni"], route_target=existing["name"], source="netbox")
            if record_vni is not None and record_vni != existing["vni"]:
                problems.append(
                    "VNI conflict for VLAN %s: the record says %s, NetBox route target %s says %s."
                    % (vlan_id, record_vni, existing["name"], existing["vni"])
                )
            if existing["pool"] != pool["pool"]:
                notes.append(
                    "Route target %s is in the %s range, but %s points to %s."
                    % (existing["name"], existing["pool"], pool["source"], pool["pool"])
                )
            return out

        if record_vni is not None:
            others = taken_by_other_vlan(netbox.all("ipam/route-targets/", {"name__isw": "%s:" % record_vni}), record_vni, vlan_id)
            out.update(vni=record_vni, route_target="%s:%s" % (record_vni, vlan_id), source="record")
            if others:
                problems.append(
                    "VNI %s of VLAN %s is already in NetBox for another VLAN: %s." % (record_vni, vlan_id, ", ".join(others))
                )
                return out
        else:
            if not span:
                problems.append("No %s VNI range for %s in vni_ranges." % (pool["pool"], dc))
                return out
            route_targets = []
            for prefix in name_prefixes(span):
                route_targets.extend(netbox.all("ipam/route-targets/", {"name__isw": prefix}))
            identifiers = []
            for app in args.get("l2vpn_apps") or ["vpn", "ipam"]:
                l2vpns = netbox.all(
                    "%s/l2vpns/" % app, {"identifier__gte": span[0], "identifier__lte": span[1]}, missing_ok=True
                )
                if l2vpns is not None:
                    identifiers = [item.get("identifier") for item in l2vpns]
                    break
            allocation = allocate_route_target(
                vlan_id, dc, pool["pool"], ranges, route_targets, identifiers, args.get("extra_used") or []
            )
            if not allocation.get("vni"):
                problems.append("No free VNI for VLAN %s: %s." % (vlan_id, allocation.get("reason")))
                return out
            out.update(vni=allocation["vni"], route_target=allocation["route_target"], source="allocated",
                       reason=allocation["reason"])

        # The VLAN has no route target in NetBox yet: write it, or say what would be written.
        marker = args.get("description_marker") or ROUTE_TARGET_DESCRIPTION
        vlan_name = str(record.get("vlan_name") or record.get("name") or (picked["vlan"] or {}).get("name") or "").strip()
        suffix = " - %s" % marker
        payload = {
            "name": out["route_target"],
            "description": (vlan_name[:DESCRIPTION_MAX - len(suffix)] + suffix) if vlan_name else marker,
        }
        if tenant:
            tenants = netbox.all("tenancy/tenants/", {"name": tenant})
            if tenants:
                payload["tenant"] = tenants[0].get("id")
            else:
                notes.append("Tenant %s is not in NetBox (Tenancy > Tenants): the route target is written without it." % tenant)
        out["would_reserve"] = payload
        if self._bool(args.get("reserve", False)) and not self._task.check_mode:
            try:
                created = netbox.request("ipam/route-targets/", method="POST", body=payload)
            except NetboxError as exc:
                problems.append(
                    "Could not write route target %s (another run may have taken it; run again): %s"
                    % (payload["name"], exc)
                )
                return out
            out.update(reserved=True, changed=True, reserved_id=created.get("id"))
        return out
