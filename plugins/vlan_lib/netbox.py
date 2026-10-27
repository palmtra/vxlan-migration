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
