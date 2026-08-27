# -*- coding: utf-8 -*-
"""Retain-aware prune plan builders."""

from ansible.errors import AnsibleFilterError

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
    action = {"op": op}
    action.update(fields)
    return action


def build_device_prune_plan(
    hostname,
    per_vlan,
    retain=None,
    static_routes=None,
    bgp_neighbors=None,
    vlan_ids=None,
):
    """Build a retain-aware prune candidate plan for one device (read-only)."""
    if not isinstance(per_vlan, list):
        raise AnsibleFilterError(
            "build_device_prune_plan expects per_vlan list, got %s" % type(per_vlan)
        )

    retain_norm = normalize_prune_retain(retain)
    wanted = None
    if vlan_ids is not None:
        wanted = {str(vid) for vid in vlan_ids}

    actions = []
    blocked = []
    svi_vrfs = set()
    vlan_id_list = []

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

        for iface in entry.get("trunk_ports") or []:
            actions.append(
                _prune_action(
                    "trunk_remove_vlans",
                    interface=iface,
                    vlans=[int(vlan_id)],
                    cli=(
                        "switchport trunk allowed vlan remove %s"
                        % int(vlan_id)
                    ),
                    source="discovery",
                    confidence="high",
                )
            )

        if entry.get("vlan_present"):
            actions.append(
                _prune_action(
                    "no_vlan",
                    vlan_id=int(vlan_id),
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
                    source="discovery",
                    confidence="high",
                )
            )

    # Consolidate trunk removals per interface.
    trunk_map = {}
    other_actions = []
    for action in actions:
        if action["op"] != "trunk_remove_vlans":
            other_actions.append(action)
            continue
        iface = action["interface"]
        trunk_map.setdefault(iface, set()).update(action.get("vlans") or [])
    consolidated = []
    for iface, vlans in sorted(trunk_map.items()):
        vlan_list = sorted(vlans)
        consolidated.append(
            _prune_action(
                "trunk_remove_vlans",
                interface=iface,
                vlans=vlan_list,
                cli=(
                    "switchport trunk allowed vlan remove %s"
                    % ",".join(str(vid) for vid in vlan_list)
                ),
                source="discovery",
                confidence="high",
            )
        )
    actions = consolidated + other_actions

    for route in static_routes or []:
        vrf = str(route.get("vrf") or "default")
        if svi_vrfs and vrf not in svi_vrfs:
            continue
        action = _prune_action(
            "no_ip_route",
            vrf=vrf,
            prefix=route.get("prefix", ""),
            next_hop=route.get("next_hop", ""),
            name=route.get("name", ""),
            source="discovery",
            confidence="medium",
        )
        route_name = route.get("name") or ""
        if vrf in retain_norm["vrfs"] or (
            route_name and route_name in retain_norm["static_route_names"]
        ):
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    for neighbor in bgp_neighbors or []:
        vrf = str(neighbor.get("vrf") or "default")
        neighbor_ip = str(neighbor.get("neighbor") or "")
        if not neighbor_ip:
            continue
        if svi_vrfs and vrf not in svi_vrfs:
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
            confidence="medium",
        )
        if (vrf, neighbor_ip) in retain_norm["bgp_neighbors"] or vrf in retain_norm[
            "vrfs"
        ]:
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    for vrf in sorted(svi_vrfs):
        if vrf in ("", "default"):
            continue
        action = _prune_action(
            "no_vrf",
            name=vrf,
            source="discovery",
            confidence="low",
            note="Only safe after all member VLANs/SVIs/statics for this VRF are removed",
        )
        if vrf in retain_norm["vrfs"]:
            action["reason"] = "matched prune.retain"
            blocked.append(action)
        else:
            actions.append(action)

    return {
        "hostname": hostname,
        "vlan_ids": sorted(set(vlan_id_list)),
        "svi_vrfs": sorted(svi_vrfs),
        "actions": actions,
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
        "status": "candidate",
    }


