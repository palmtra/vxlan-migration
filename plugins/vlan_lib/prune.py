# -*- coding: utf-8 -*-
"""Human-review prune plan builders (trunk / SVI / VLAN only).

Static routes, BGP neighbors, and VRF teardown are parked: they are listed
under ``l3_review`` for operators, never as prune actions. Discovery never
applies deletes.
"""

import re

from ansible.errors import AnsibleFilterError

_L3_PARKED_NOTE = (
    "Parked: static routes, BGP neighbors, and VRF teardown are not prune "
    "candidates. Shown for human L3 review only. Automated prune apply is out "
    "of scope."
)

_DEFAULT_VRF_PARKED_NOTE = (
    "Parked: SVI VRF is default. Fabric default-route and iBGP must not be "
    "treated as VLAN prune candidates."
)


def normalize_prune_retain(retain):
    """Normalize service prune.retain into comparable sets."""
    retain = retain or {}
    if not isinstance(retain, dict):
        raise AnsibleFilterError(
            "normalize_prune_retain expects a dict, got %s" % type(retain)
        )

    vrfs = {str(item) for item in (retain.get("vrfs") or []) if item}
    interfaces = {str(item) for item in (retain.get("interfaces") or []) if item}
    neighbors = set()
    for item in retain.get("bgp_neighbors") or []:
        if not isinstance(item, dict):
            continue
        vrf = str(item.get("vrf") or "default")
        neighbor = str(item.get("neighbor") or "")
        if neighbor:
            neighbors.add((vrf, neighbor))
    static_names = {
        str(item) for item in (retain.get("static_route_names") or []) if item
    }
    return {
        "vrfs": vrfs,
        "interfaces": interfaces,
        "bgp_neighbors": neighbors,
        "static_route_names": static_names,
    }


def _prune_action(op, **fields):
    action = {"op": op, "human_required": True}
    action.update(fields)
    return action


def _os_family_from_inputs(hostname, per_vlan):
    for entry in per_vlan or []:
        if isinstance(entry, dict) and entry.get("os_family"):
            return str(entry.get("os_family")).lower()
    host = str(hostname or "").lower()
    if host.startswith("nxos"):
        return "nxos"
    if host.startswith("ios"):
        return "ios"
    return "eos"


def _trunk_remove_cli(interface, vlan_ids, os_family):
    vlan_list = ",".join(str(vid) for vid in vlan_ids)
    indent = "  " if os_family == "nxos" else "   "
    return "interface %s\n%sswitchport trunk allowed vlan remove %s" % (
        interface,
        indent,
        vlan_list,
    )


def _svi_remove_cli(vlan_id):
    return "no interface Vlan%s" % int(vlan_id)


def _vlan_remove_cli(vlan_id):
    return "no vlan %s" % int(vlan_id)


_DEFAULT_COMMIT_TIMER = "00:10:00"
_EOS_SESSION_NAME_MAX = 32


def _sanitize_session_name(vlan_ids):
    parts = ["prune"] + ["v%s" % int(vid) for vid in (vlan_ids or [])[:5]]
    name = "_".join(parts)
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)
    return (name or "prune_vlan")[:_EOS_SESSION_NAME_MAX]


def _actions_config_body(actions):
    lines = []
    for action in actions or []:
        cli = (action.get("cli") or "").strip()
        if cli:
            lines.append(cli)
    return "\n".join(lines)


def build_prune_execution(
    os_family, vlan_ids, actions, commit_timer=None, hostname=""
):
    """Build copy-paste execution text. Never applied by discovery."""
    timer = commit_timer or _DEFAULT_COMMIT_TIMER
    family = str(os_family or "eos").lower()
    vids = [int(vid) for vid in (vlan_ids or [])]
    session_name = _sanitize_session_name(vids)
    config_body = _actions_config_body(actions)
    if family == "eos":
        full_cli = "configure session %s\n" % session_name
        if config_body:
            full_cli += config_body + "\n"
        full_cli += "!\nshow session-config diffs\ncommit timer %s\n" % timer
        return {
            "platform": "eos",
            "session_name": session_name,
            "commit_timer": timer,
            "enter": "configure session %s" % session_name,
            "show_diffs": "show session-config diffs",
            "commit_timer_cmd": "commit timer %s" % timer,
            "confirm": "configure confirm",
            "abort": "configure session %s abort" % session_name,
            "config_body": config_body,
            "full_cli": full_cli,
            "notes": [
                "Paste the session, review diffs, then commit with the rollback timer.",
                "Confirm only after checks pass. If checks fail, wait: the timer rolls back.",
                "This report does not apply the session.",
            ],
        }
    if family == "nxos":
        checkpoint = session_name[:31]
        full_cli = "checkpoint %s\nconfigure terminal\n" % checkpoint
        if config_body:
            full_cli += config_body + "\n"
        full_cli += "end\n"
        return {
            "platform": "nxos",
            "checkpoint": checkpoint,
            "enter": "checkpoint %s" % checkpoint,
            "rollback": "rollback running-config checkpoint %s" % checkpoint,
            "save": "copy running-config startup-config",
            "config_body": config_body,
            "full_cli": full_cli,
            "notes": [
                "Create the checkpoint first so you can roll back.",
                "Save running-config only after verification.",
                "This report does not apply the config.",
            ],
        }
    return {
        "platform": family,
        "config_body": config_body,
        "full_cli": (config_body + "\n") if config_body else "",
        "notes": [
            "IOS prune is parked on main. Review and paste manually.",
            "This report does not apply the config.",
        ],
    }


def build_device_prune_plan(
    hostname,
    per_vlan,
    retain=None,
    static_routes=None,
    bgp_neighbors=None,
    vlan_ids=None,
    commit_timer=None,
):
    """Build a human-review prune candidate plan for one device (read-only).

    Actions are limited to trunk VLAN remove, SVI delete, and VLAN delete.
    Routing objects are parked in ``l3_review``.
    """
    if not isinstance(per_vlan, list):
        raise AnsibleFilterError(
            "build_device_prune_plan expects per_vlan list, got %s" % type(per_vlan)
        )

    retain_norm = normalize_prune_retain(retain)
    wanted = None
    if vlan_ids is not None:
        wanted = {str(vid) for vid in vlan_ids}

    os_family = _os_family_from_inputs(hostname, per_vlan)
    actions = []
    l3_review = []
    blocked = []
    svi_vrfs = set()
    vlan_id_list = []
    endpoint_ports = []
    svi_inventory = []

    for entry in per_vlan:
        if not isinstance(entry, dict):
            continue
        vlan_id = entry.get("vlan_id")
        if vlan_id is None:
            continue
        if wanted is not None and str(vlan_id) not in wanted:
            continue

        vlan_id_list.append(int(vlan_id))
        svi_vrf = entry.get("svi_vrf") or (entry.get("svi_details") or {}).get("vrf") or ""
        if svi_vrf:
            svi_vrfs.add(str(svi_vrf))

        compute_ports = entry.get("compute_ports")
        unknown_ports = entry.get("unknown_ports")
        if compute_ports is None and unknown_ports is None:
            endpoint_candidates = list(entry.get("access_ports") or [])
        else:
            endpoint_candidates = list(compute_ports or []) + list(unknown_ports or [])
        for iface in endpoint_candidates:
            if iface not in endpoint_ports:
                endpoint_ports.append(iface)

        details = entry.get("svi_details") or {}
        if entry.get("svi_present"):
            svi_inventory.append(
                {
                    "vlan_id": int(vlan_id),
                    "vrf": svi_vrf or "default",
                    "ip_addresses": details.get("ip_addresses") or [],
                    "virtual_router_addresses": details.get("virtual_router_addresses")
                    or [],
                    "hsrp_addresses": details.get("hsrp_addresses") or [],
                    "hsrp_groups": details.get("hsrp_groups") or [],
                    "description": details.get("description") or "",
                    "note": (
                        "Human must remove this SVI after endpoints have moved. "
                        "Deleting it drops the gateway."
                    ),
                }
            )

        prune_trunks = entry.get("switch_uplink_ports")
        if prune_trunks is None:
            prune_trunks = entry.get("trunk_ports") or []
        for iface in prune_trunks:
            actions.append(
                _prune_action(
                    "trunk_remove_vlans",
                    interface=iface,
                    vlans=[int(vlan_id)],
                    cli=_trunk_remove_cli(iface, [int(vlan_id)], os_family),
                    source="discovery",
                    confidence="high",
                )
            )

        if entry.get("svi_present"):
            actions.append(
                _prune_action(
                    "no_interface_vlan",
                    vlan_id=int(vlan_id),
                    vrf=svi_vrf or "",
                    cli=_svi_remove_cli(vlan_id),
                    source="discovery",
                    confidence="high",
                    note="Removes the SVI/gateway. Confirm overlay is live first.",
                )
            )

        if entry.get("vlan_present"):
            actions.append(
                _prune_action(
                    "no_vlan",
                    vlan_id=int(vlan_id),
                    cli=_vlan_remove_cli(vlan_id),
                    source="discovery",
                    confidence="high",
                    note="Last step after trunks and SVI are gone.",
                )
            )

    # Consolidate trunk removals per interface; keep SVI then VLAN after trunks.
    trunk_map = {}
    svi_actions = []
    vlan_actions = []
    for action in actions:
        if action["op"] == "trunk_remove_vlans":
            iface = action["interface"]
            trunk_map.setdefault(iface, set()).update(action.get("vlans") or [])
        elif action["op"] == "no_interface_vlan":
            svi_actions.append(action)
        else:
            vlan_actions.append(action)

    consolidated = []
    for iface, vlans in sorted(trunk_map.items()):
        vlan_list = sorted(vlans)
        consolidated.append(
            _prune_action(
                "trunk_remove_vlans",
                interface=iface,
                vlans=vlan_list,
                cli=_trunk_remove_cli(iface, vlan_list, os_family),
                source="discovery",
                confidence="high",
            )
        )
    actions = consolidated + svi_actions + vlan_actions

    default_vrf_in_scope = "default" in svi_vrfs

    # Routing objects are only in scope when this device has an SVI for the
    # VLAN(s). Empty svi_vrfs means L2-only - do not attach fabric default-VRF
    # statics/iBGP as if they belonged to the VLAN.
    if svi_vrfs:
        for route in static_routes or []:
            vrf = str(route.get("vrf") or "default")
            if vrf not in svi_vrfs:
                continue
            action = _prune_action(
                "no_ip_route",
                vrf=vrf,
                prefix=route.get("prefix", ""),
                next_hop=route.get("next_hop", ""),
                name=route.get("name", ""),
                source="discovery",
                parked=True,
                note=_DEFAULT_VRF_PARKED_NOTE if vrf == "default" else _L3_PARKED_NOTE,
            )
            route_name = route.get("name") or ""
            if vrf in retain_norm["vrfs"] or (
                route_name and route_name in retain_norm["static_route_names"]
            ):
                action["reason"] = "matched prune.retain"
                blocked.append(action)
            else:
                l3_review.append(action)

        for neighbor in bgp_neighbors or []:
            vrf = str(neighbor.get("vrf") or "default")
            neighbor_ip = str(neighbor.get("neighbor") or "")
            if not neighbor_ip:
                continue
            if vrf not in svi_vrfs:
                continue
            action = _prune_action(
                "no_bgp_neighbor",
                vrf=vrf,
                neighbor=neighbor_ip,
                remote_as=neighbor.get("remote_as", ""),
                update_source=neighbor.get("update_source", ""),
                description=neighbor.get("description", ""),
                route_maps=neighbor.get("route_maps", []),
                source="discovery",
                parked=True,
                note=_DEFAULT_VRF_PARKED_NOTE if vrf == "default" else _L3_PARKED_NOTE,
            )
            if (vrf, neighbor_ip) in retain_norm["bgp_neighbors"] or vrf in retain_norm[
                "vrfs"
            ]:
                action["reason"] = "matched prune.retain"
                blocked.append(action)
            else:
                l3_review.append(action)

        for vrf in sorted(svi_vrfs):
            if vrf in ("", "default"):
                continue
            action = _prune_action(
                "no_vrf",
                name=vrf,
                source="discovery",
                parked=True,
                note=_L3_PARKED_NOTE,
            )
            if vrf in retain_norm["vrfs"]:
                action["reason"] = "matched prune.retain"
                blocked.append(action)
            else:
                l3_review.append(action)

    status = "candidate"
    if endpoint_ports:
        status = "blocked_by_compute_endpoints"
    elif not actions:
        status = "none"

    return {
        "hostname": hostname,
        "os_family": os_family,
        "vlan_ids": sorted(set(vlan_id_list)),
        "svi_vrfs": sorted(svi_vrfs),
        "endpoint_ports": endpoint_ports,
        "svi_inventory": svi_inventory,
        "actions": actions,
        "l3_review": l3_review,
        "blocked_by_retain": blocked,
        "retain": {
            "vrfs": sorted(retain_norm["vrfs"]),
            "bgp_neighbors": [
                {"vrf": vrf, "neighbor": neighbor}
                for vrf, neighbor in sorted(retain_norm["bgp_neighbors"])
            ],
            "interfaces": sorted(retain_norm["interfaces"]),
            "static_route_names": sorted(retain_norm["static_route_names"]),
        },
        "destructive": False,
        "human_required": True,
        "apply_automated": False,
        "status": status,
        "default_vrf_routing_parked": default_vrf_in_scope,
        "order": [
            "Rehome or shut compute endpoints (servers / IBM Z / Nutanix / UCS / HCI)",
            "Remove VLAN from switch-to-switch trunks (EOS session + commit timer)",
            "no interface Vlan<id> (gateway)",
            "no vlan <id>",
        ],
        "execution": build_prune_execution(
            os_family, vlan_id_list, actions, commit_timer, hostname
        ),
    }
