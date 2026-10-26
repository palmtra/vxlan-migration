# -*- coding: utf-8 -*-
"""Where does each switch attach to the VLAN after migration?

Three switch roles:

* ``vtep`` - Arista fabric leaf with a Vxlan interface. The VNI lives here.
* ``mls`` - legacy MLS / core. It is being decommissioned.
* ``l2_switch`` - a switch that is not a VTEP (an EOS switch without Vxlan1,
  or a host tagged ``migration_tier: access``). It keeps the VLAN as plain
  L2 and must reach the fabric through a VTEP.

Non-VTEP switches that are linked to each other form a group. A group with
endpoints must reach a VTEP. While it is also linked to the MLS it bridges
the EVPN and legacy L2 domains for the VLAN, which is a loop. Each of those
MLS links becomes a link prune: remove the VLAN from the trunk on both ends.
A link prune is ready only once the VLAN is VNI-mapped on the group's VTEPs;
before that the endpoints would be cut off.
"""

from vlan_lib.parsers import _normalize_interface_name

_FABRIC_TIERS = frozenset({"fabric", "leaf", "border", "border_leaf", "spine"})
_ACCESS_TIERS = frozenset({"access", "l2", "l2_switch", "non_vtep"})
_LEGACY_TIERS = frozenset({"legacy", "mls", "core", "aggregation"})


def device_tier(device):
    """``legacy`` or ``fabric``. Inventory ``migration_tier`` wins; EOS defaults to fabric.

    ``access`` (a non-VTEP L2 switch outside the fabric) counts as legacy here:
    an SVI on it is not a fabric gateway.
    """
    device = device or {}
    tier = str(device.get("migration_tier") or "").strip().lower()
    if tier in _FABRIC_TIERS:
        return "fabric"
    if tier in _LEGACY_TIERS or tier in _ACCESS_TIERS:
        return "legacy"
    return "fabric" if str(device.get("os_family") or "").lower() == "eos" else "legacy"


def device_role(device):
    """``vtep``, ``mls``, or ``l2_switch`` for one discovered switch."""
    device = device or {}
    tier = str(device.get("migration_tier") or "").strip().lower()
    if tier in _ACCESS_TIERS:
        return "l2_switch"
    if device_tier(device) == "legacy":
        return "mls"
    vxlan = device.get("vxlan_interface")
    if isinstance(vxlan, dict) and "present" in vxlan and not vxlan["present"]:
        return "l2_switch"
    return "vtep"


def vlan_live_on_vtep(device, vlan_id):
    """True when the VTEP already maps *vlan_id* to a VNI."""
    vid = str(vlan_id)
    vxlan = (device or {}).get("vxlan_interface") or {}
    for item in list(vxlan.get("vlan_vni") or []) + list((device or {}).get("vxlan_mappings") or []):
        if isinstance(item, dict) and str(item.get("vlan_id")) == vid:
            return True
    return bool((device or {}).get("vxlan_mapped"))


def _links(devices, roles):
    """Directed links between discovered switches on ports that carry the VLAN.

    ``{a, a_if, b, b_if}``: ``a_if`` carries the VLAN on ``a`` toward ``b``.
    ``b_if`` is ``b``'s port toward ``a`` when ``b`` also carries the VLAN
    toward ``a``, otherwise empty.
    """
    toward = {}
    for device in devices:
        a = device.get("hostname")
        for att in device.get("port_attachments") or []:
            if not isinstance(att, dict) or att.get("role") == "peer_link":
                continue
            for peer in att.get("peers") or []:
                b = peer.get("hostname")
                if b in roles and b != a:
                    toward.setdefault((a, b), []).append((att.get("interface") or "", peer.get("port") or ""))
    links = []
    for (a, b), ports in toward.items():
        back = toward.get((b, a)) or []
        seen = set()
        for a_if, b_port in ports:
            key = _normalize_interface_name(a_if)
            if key in seen:
                continue
            seen.add(key)
            b_if = ""
            for candidate, _port in back:
                if not b_port or _normalize_interface_name(candidate) == _normalize_interface_name(b_port):
                    b_if = candidate
                    break
            if not b_if and back:
                b_if = back[0][0]
            links.append({"a": a, "a_if": a_if, "b": b, "b_if": b_if})
    return links


def _components(members, links):
    graph = {host: set() for host in members}
    for link in links:
        if link["a"] in graph and link["b"] in graph:
            graph[link["a"]].add(link["b"])
            graph[link["b"]].add(link["a"])
    seen = set()
    groups = []
    for host in members:
        if host in seen:
            continue
        stack = [host]
        group = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            group.append(node)
            stack.extend(sorted(graph[node] - seen))
        groups.append(sorted(group))
    return groups


def _edge_links(group, links, roles, role):
    """Links between *group* members and switches of *role*, seen from the member."""
    found = {}
    for link in links:
        if link["a"] in group and roles.get(link["b"]) == role:
            key = (link["a"], link["b"])
            found.setdefault(key, {"member": link["a"], "member_if": link["a_if"],
                                   "peer": link["b"], "peer_if": link["b_if"]})
    for link in links:
        if link["b"] in group and roles.get(link["a"]) == role:
            key = (link["b"], link["a"])
            entry = found.setdefault(key, {"member": link["b"], "member_if": link["b_if"],
                                           "peer": link["a"], "peer_if": link["a_if"]})
            if not entry["peer_if"]:
                entry["peer_if"] = link["a_if"]
            if not entry["member_if"]:
                entry["member_if"] = link["b_if"]
    return list(found.values())


def analyze_l2_domains(vlan_id, devices, endpoint_hosts):
    """Attachment of non-VTEP switches for one VLAN.

    ``endpoint_hosts`` are switches with local endpoint evidence. Returns
    roles, non-VTEP groups, VTEP attachment evidence, link prunes, fabric to
    legacy interconnects, and issues.
    """
    devices = [d for d in devices or [] if isinstance(d, dict) and d.get("hostname")]
    by_host = {d["hostname"]: d for d in devices}
    roles = {host: device_role(device) for host, device in by_host.items()}
    carriers = {
        host
        for host, device in by_host.items()
        if device.get("vlan_present") or device.get("port_attachments")
    }
    links = [link for link in _links(devices, roles) if link["a"] in carriers]
    l2_hosts = [host for host in by_host if roles[host] == "l2_switch" and host in carriers]

    groups = []
    attached = []
    vtep_attach = {}
    mls_attach = {}
    link_prunes = []
    issues = []
    for members in _components(l2_hosts, links):
        vtep_links = _edge_links(members, links, roles, "vtep")
        mls_links = _edge_links(members, links, roles, "mls")
        has_endpoints = any(host in endpoint_hosts for host in members)
        names = ", ".join(members)
        vteps = sorted({link["peer"] for link in vtep_links})
        live = [host for host in vteps if vlan_live_on_vtep(by_host.get(host), vlan_id)]
        not_live = [host for host in vteps if host not in live]
        if not has_endpoints:
            status = "no_endpoints"
        elif not vtep_links:
            status = "no_vtep_uplink"
            issues.append(
                "%s carries endpoints but has no link to a VTEP. Connect it to a VTEP before "
                "the MLS is pruned; its MLS links are kept until then." % names
            )
            for link in mls_links:
                mls_attach.setdefault(link["peer"], []).append(
                    "only path for non-VTEP switch %s, which has no VTEP uplink (%s)"
                    % (link["member"], link["peer_if"] or "?")
                )
        elif mls_links:
            status = "dual_attached"
            issues.append(
                "%s bridges EVPN and legacy L2 for VLAN %s (VTEP: %s; MLS: %s). Prune VLAN %s "
                "from the MLS links at cutover; see link prunes."
                % (
                    names,
                    vlan_id,
                    ", ".join("%s via %s" % (l["peer"], l["member_if"]) for l in vtep_links),
                    ", ".join("%s via %s" % (l["peer"], l["member_if"]) for l in mls_links),
                    vlan_id,
                )
            )
        else:
            status = "vtep_only"
        if has_endpoints:
            attached.extend(members)
            for link in vtep_links:
                vtep_attach.setdefault(link["peer"], []).append(
                    "attachment point for non-VTEP switch %s (%s -> %s)"
                    % (link["member"], link["peer_if"] or "?", link["member_if"] or "?")
                )
            for member in members:
                ports = sorted(
                    {l["member_if"] for l in vtep_links if l["member"] == member and l["member_if"]},
                    key=_normalize_interface_name,
                )
                peers = sorted({l["peer"] for l in vtep_links if l["member"] == member})
                if len(ports) > 1:
                    issues.append(
                        "%s reaches VTEPs %s over separate links (%s). Bundle them into one "
                        "port-channel (MLAG or EVPN multihoming), or STP is the only loop protection."
                        % (member, ", ".join(peers), ", ".join(ports))
                    )
        if status == "dual_attached":
            ready = not not_live
            blocked = (
                ""
                if ready
                else "VLAN %s is not VNI-mapped yet on %s; pruning now would cut the endpoints off"
                % (vlan_id, ", ".join(not_live))
            )
            for link in mls_links:
                reason = "%s reaches the fabric through %s; this MLS link bridges EVPN and legacy L2" % (
                    link["member"],
                    ", ".join(vteps),
                )
                link_prunes.append(
                    {
                        "hostname": link["member"],
                        "interface": link["member_if"],
                        "peer": link["peer"],
                        "peer_interface": link["peer_if"],
                        "side": "non_vtep",
                        "ready": ready,
                        "blocked_reason": blocked,
                        "reason": reason,
                    }
                )
                if link["peer_if"]:
                    link_prunes.append(
                        {
                            "hostname": link["peer"],
                            "interface": link["peer_if"],
                            "peer": link["member"],
                            "peer_interface": link["member_if"],
                            "side": "mls",
                            "ready": ready,
                            "blocked_reason": blocked,
                            "reason": reason,
                        }
                    )
        groups.append(
            {
                "members": members,
                "has_endpoints": has_endpoints,
                "status": status,
                "vtep_links": vtep_links,
                "mls_links": mls_links,
                "vteps_live": live,
                "vteps_not_live": not_live,
            }
        )

    interconnects = []
    seen = set()
    for link in links:
        pair = (roles.get(link["a"]), roles.get(link["b"]))
        if pair == ("vtep", "mls"):
            entry = (link["a"], link["a_if"], link["b"], link["b_if"])
        elif pair == ("mls", "vtep"):
            entry = (link["b"], link["b_if"], link["a"], link["a_if"])
        else:
            continue
        key = (entry[0], _normalize_interface_name(entry[1]), entry[2])
        if key in seen:
            continue
        seen.add(key)
        interconnects.append(
            {"vtep": entry[0], "vtep_if": entry[1], "mls": entry[2], "mls_if": entry[3]}
        )
    dual = [group for group in groups if group["status"] == "dual_attached"]
    if interconnects and dual:
        issues.append(
            "Confirm STP blocks one path for VLAN %s until cutover: it has a direct fabric-legacy "
            "interconnect (%s) and non-VTEP switches attached to both domains (%s), which is an L2 "
            "loop between EVPN and legacy otherwise."
            % (
                vlan_id,
                ", ".join("%s %s - %s" % (i["vtep"], i["vtep_if"], i["mls"]) for i in interconnects),
                "; ".join(", ".join(group["members"]) for group in dual),
            )
        )
    return {
        "roles": roles,
        "groups": groups,
        "attached_hosts": attached,
        "vtep_attach": vtep_attach,
        "mls_attach": mls_attach,
        "link_prunes": link_prunes,
        "interconnects": interconnects,
        "issues": issues,
    }
