# -*- coding: utf-8 -*-
"""Action plugin: compare VLAN records with NetBox. Read-only (GET only).

Arguments:
  url, token, validate_certs (false), timeout (30)
  records      VLAN records (vars/vlans/<dc>/), flat
  vlan_query   extra /ipam/vlans/ filters for this DC (group, site)
  data_center  narrows several VID matches (netbox_vlan_pick)
  l2vpn_apps   where L2VPNs live (["vpn", "ipam"])

Returns: reports (one per record, see vlan_lib/netbox_drift.py), summary
{status: count}. Fails only when NetBox cannot be reached at all; a lookup
that fails for one VLAN marks that VLAN ``error``. The token is never returned.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys

from ansible.plugins.action import ActionBase

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from vlan_lib.netbox import netbox_l2vpn_evpn, netbox_vlan_pick  # noqa: E402
from vlan_lib.netbox_api import Netbox, NetboxError  # noqa: E402
from vlan_lib.netbox_drift import netbox_drift_summary, netbox_vlan_drift  # noqa: E402
from vlan_lib.placement import _vlan_route_target  # noqa: E402


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset((
        "url", "token", "validate_certs", "timeout", "records", "vlan_query", "data_center", "l2vpn_apps",
    ))

    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        args = self._task.args
        unknown = sorted(set(args) - self._VALID_ARGS)
        if unknown:
            return dict(result, failed=True, msg="Unsupported arguments: %s" % ", ".join(unknown))
        if not args.get("url") or not args.get("token"):
            return dict(result, failed=True, msg="netbox_url and netbox_token are required")
        netbox = Netbox(
            args["url"], args["token"],
            str(args.get("validate_certs", False)).lower() in ("true", "yes", "1"),
            int(args.get("timeout") or 30),
        )
        # One cheap request first: a bad URL or token stops the run with the reason.
        try:
            netbox.request("ipam/vlans/", {"limit": 1})
        except NetboxError as exc:
            return dict(result, failed=True, msg="NetBox drift check could not reach NetBox: %s" % exc)

        apps = list(args.get("l2vpn_apps") or ["vpn", "ipam"])
        reports = []
        for record in args.get("records") or []:
            if not isinstance(record, dict) or record.get("_probe"):
                continue
            try:
                nb = self._fetch(netbox, record, args, apps)
            except NetboxError as exc:
                nb = {"error": "VLAN %s: %s" % (record.get("id"), exc)}
            reports.append(netbox_vlan_drift(record, nb))
        result.update(changed=False, reports=reports, summary=netbox_drift_summary(reports))
        return result

    @staticmethod
    def _fetch(netbox, record, args, apps):
        vid = int(record.get("id"))
        params = {"vid": vid}
        params.update(args.get("vlan_query") or {})
        pick = netbox_vlan_pick({"results": netbox.all("ipam/vlans/", params)}, vid, args.get("data_center") or "")
        nb = {"vlan": pick["vlan"], "vlan_problem": pick["problem"], "evpn": {}, "route_targets": {}, "prefixes": {}}
        if not pick["vlan"]:
            return nb
        vlan_pk = pick["vlan"].get("id")

        terms = None
        for app in apps:
            terms = netbox.all("%s/l2vpn-terminations/" % app, {"vlan_id": vlan_pk}, missing_ok=True)
            if terms is not None:
                l2vpn_app = app
                break
        objects = []
        for l2vpn_pk in sorted({t["l2vpn"]["id"] for t in terms or [] if isinstance(t.get("l2vpn"), dict)}):
            objects.append(netbox.request("%s/l2vpns/%s/" % (l2vpn_app, l2vpn_pk)))
        nb["evpn"] = netbox_l2vpn_evpn({"results": terms or []}, objects)

        effective = dict(record)
        if not str(effective.get("vni") or "").strip().isdigit() and nb["evpn"].get("vni") is not None:
            effective["vni"] = nb["evpn"]["vni"]
        rt = _vlan_route_target(effective)
        name = rt.get("rt_both") or rt.get("rt_import")
        if name:
            nb["route_targets"][name] = netbox.all("ipam/route-targets/", {"name": name})
        for prefix in record.get("prefixes") or []:
            prefix = str(prefix).strip()
            nb["prefixes"][prefix] = netbox.all("ipam/prefixes/", {"prefix": prefix})
        return nb
