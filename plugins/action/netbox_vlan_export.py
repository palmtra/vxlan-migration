# -*- coding: utf-8 -*-
"""Action plugin: export one VLAN record to NetBox (VLAN, tenant, prefixes in their VRF).

Arguments:
  url, token, validate_certs (false), timeout (30)
  record          the VLAN record (flat)
  data_center     narrows several VID matches; named in the description
  vlan_query      /ipam/vlans/ filters for this DC (group, site), the same
                  scope netbox_check uses. A group here is where new VLANs go.
  site_id         site for a new VLAN when vlan_query has no group
  dry_run         true: look up only, write nothing (default true)
  create_missing, update_existing, export_prefixes  (default true)
  description_marker  text after the VLAN name in prefix descriptions

Every lookup runs before any write: a VLAN with a problem (ambiguous VID,
tenant or VRF missing from NetBox) gets nothing written, and the task fails
with the problems. Returns vlan_id, vlan_name, netbox_status (created |
updated | skipped | dry_run_would_create | dry_run_would_update),
netbox_vlan_id, payload, prefixes [{prefix, netbox_status, payload}]. The
token is never returned.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys

from ansible.plugins.action import ActionBase

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from vlan_lib.netbox import ROUTE_TARGET_DESCRIPTION, netbox_vlan_pick  # noqa: E402
from vlan_lib.netbox_api import Netbox, NetboxError  # noqa: E402
from vlan_lib.netbox_export import netbox_export_plan, record_tenant, record_vrf  # noqa: E402


def _merge(result, out, **extra):
    merged = dict(result)
    merged.update(out)
    merged.update(extra)
    return merged


def _bool(value, default):
    if value is None or value == "":
        return default
    return str(value).lower() in ("true", "yes", "1", "on")


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset((
        "url", "token", "validate_certs", "timeout", "record", "data_center", "vlan_query", "site_id",
        "dry_run", "create_missing", "update_existing", "export_prefixes", "description_marker",
    ))

    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        args = self._task.args
        unknown = sorted(set(args) - self._VALID_ARGS)
        if unknown:
            return dict(result, failed=True, msg="Unsupported arguments: %s" % ", ".join(unknown))
        record = dict(args.get("record") or {})
        if not args.get("url") or not args.get("token") or record.get("id") in (None, ""):
            return dict(result, failed=True, msg="url, token and a record with an id are required")
        vid = int(record["id"])
        netbox = Netbox(args["url"], args["token"], _bool(args.get("validate_certs"), False),
                        int(args.get("timeout") or 30))
        dry_run = _bool(args.get("dry_run"), True)
        export_prefixes = _bool(args.get("export_prefixes"), True)
        if not export_prefixes:
            record.pop("prefixes", None)
        out = {"vlan_id": vid, "vlan_name": record.get("name") or "", "prefixes": []}

        try:
            nb = self._lookup(netbox, record, args)
        except NetboxError as exc:
            return _merge(result, out, failed=True, netbox_status="failed",
                        msg="NetBox VLAN lookup for VID %s failed: %s" % (vid, exc.reason))
        plan = netbox_export_plan(record, args.get("data_center") or "", nb,
                                  args.get("description_marker") or ROUTE_TARGET_DESCRIPTION)
        out["payload"] = plan["vlan"]["payload"]
        if plan["problems"]:
            return _merge(result, out, failed=True, netbox_status="failed", problems=plan["problems"],
                        msg="VLAN %s not exported: %s" % (vid, " | ".join(plan["problems"])))

        vlan = plan["vlan"]
        if dry_run:
            out.update(netbox_status="dry_run_would_%s" % vlan["action"], netbox_vlan_id=vlan["id"] or "")
            out["prefixes"] = [{"prefix": p["prefix"], "netbox_status": "dry_run_would_%s" % p["action"]
                                if p["action"] != "none" else "unchanged", "payload": p["payload"]}
                               for p in plan["prefixes"]]
            return _merge(result, out, changed=False)

        vlan_pk = vlan["id"]
        try:
            if vlan["action"] == "create":
                if not _bool(args.get("create_missing"), True):
                    out.update(netbox_status="skipped", netbox_vlan_id="")
                    return _merge(result, out, changed=False)
                vlan_pk = netbox.request("ipam/vlans/", method="POST", body=vlan["payload"]).get("id")
                out["netbox_status"] = "created"
            elif _bool(args.get("update_existing"), True):
                netbox.request("ipam/vlans/%s/" % vlan_pk, method="PATCH", body=vlan["payload"])
                out["netbox_status"] = "updated"
            else:
                out["netbox_status"] = "skipped"
        except NetboxError as exc:
            return _merge(result, out, failed=True, netbox_status="failed",
                        msg="NetBox refused to %s VLAN VID %s: %s" % (vlan["action"], vid, exc.reason))
        out["netbox_vlan_id"] = vlan_pk

        for item in plan["prefixes"]:
            body = dict(item["payload"])
            if "vlan" in body and body["vlan"] is None:
                body["vlan"] = vlan_pk
            entry = {"prefix": item["prefix"], "payload": body}
            try:
                if item["action"] == "create":
                    netbox.request("ipam/prefixes/", method="POST", body=body)
                    entry["netbox_status"] = "created"
                elif item["action"] == "update" and body:
                    netbox.request("ipam/prefixes/%s/" % item["id"], method="PATCH", body=body)
                    entry["netbox_status"] = "updated"
                else:
                    entry["netbox_status"] = "unchanged"
            except NetboxError as exc:
                out["prefixes"].append(dict(entry, netbox_status="failed"))
                return _merge(result, out, failed=True, changed=True,
                            msg="NetBox refused to %s prefix %s for VLAN %s: %s"
                            % (item["action"], item["prefix"], vid, exc.reason))
            out["prefixes"].append(entry)
        return _merge(result, out, changed=True)

    @staticmethod
    def _lookup(netbox, record, args):
        vid = int(record["id"])
        query = dict(args.get("vlan_query") or {})
        pick = netbox_vlan_pick({"results": netbox.all("ipam/vlans/", dict(query, vid=vid))}, vid,
                                args.get("data_center") or "")
        nb = {"vlan": pick["vlan"], "vlan_problem": pick["problem"], "tenant": None, "vrf": None,
              "group_id": None, "site_id": args.get("site_id") or None, "prefixes": {}}
        if not pick["vlan"]:
            # A new VLAN goes where the scoped lookup will find it next time;
            # without that scope every run would create another one.
            if query.get("group"):
                groups = netbox.all("ipam/vlan-groups/", {"slug": query["group"]})
                nb["group_id"] = groups[0]["id"] if groups else None
                if not groups:
                    nb["vlan_problem"] = ("VLAN group %s (netbox_vlan_query_by_dc) is not in NetBox: create it "
                                          "before exporting VLANs into it" % query["group"])
            elif query.get("site"):
                sites = netbox.all("dcim/sites/", {"slug": query["site"]})
                nb["site_id"] = sites[0]["id"] if sites else None
                if not sites:
                    nb["vlan_problem"] = "Site %s (netbox_vlan_query_by_dc) is not in NetBox" % query["site"]
        tenant = record_tenant(record)
        if tenant:
            hits = netbox.all("tenancy/tenants/", {"name": tenant})
            nb["tenant"] = hits[0] if hits else None
        prefixes = [str(p).strip() for p in record.get("prefixes") or [] if str(p).strip()]
        vrf = record_vrf(record)
        if vrf and prefixes:
            hits = netbox.all("ipam/vrfs/", {"name": vrf})
            nb["vrf"] = hits[0] if hits else None
        for prefix in prefixes:
            nb["prefixes"][prefix] = netbox.all("ipam/prefixes/", {"prefix": prefix})
        return nb
