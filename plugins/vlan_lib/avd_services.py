# -*- coding: utf-8 -*-
"""Turn a VLAN record into an entry for the AVD services repo.

palmtra/avd-vxlan-repo holds, per DC, the network services the migration
adds (``group_vars/<DC>_SERVICES.yml``, AVD ``tenants`` format). A VLAN
whose gateway moves to the fabric becomes an SVI; one whose gateway stays
external (firewall) becomes an ``l2vlan``. Centralized anycast is both: the
SVI on the gateway leaf pair, the same VLAN as an ``l2vlan`` on the others.

Placement is by leaf pair: ``tags`` are node group names from
``group_vars/<DC>_FABRIC.yml``. A pair carries the VLAN when any of its
leaves does (MLAG peers carry the same VLANs). A VLAN whose gateway stays
external also goes on the pair the gateway (firewall) attaches to: the DC's
``_external_gateway_pair`` (the border leaves), unless the record names
other leaves in ``external_gateway_leafs`` (a manual override).

The fabric advertises subnets with ``network`` statements under the VRF and
never redistributes connected, so an anycast gateway in VRF default would
not be advertised and is refused.

RD and RT follow the fabric's scheme in both DCs (see ``bgp_vlan_evpn``):
``rd_override`` is the VLAN id, so the RD is ``<router-id>:<vlan id>``, and
``rt_override`` is ``<l2 vni>:<vlan id>`` (or the record's ``l2_rt``).
"""

import copy
from collections.abc import Mapping

from .placement import _vlan_route_target, virtual_gateway_address

GATEWAY_MODES = ("distributed_anycast", "centralized_anycast")


def avd_leaf_pairs(fabric_vars):
    """``{leaf: node group}`` from an AVD repo ``<DC>_FABRIC.yml``."""
    pairs = {}
    for group in ((fabric_vars or {}).get("l3leaf") or {}).get("node_groups") or []:
        for node in group.get("nodes") or []:
            pairs[str(node.get("name"))] = str(group.get("group"))
    return pairs


def _fabric_nodes(fabric_vars):
    nodes = {}
    for group in ((fabric_vars or {}).get("l3leaf") or {}).get("node_groups") or []:
        for node in group.get("nodes") or []:
            nodes[str(node.get("name"))] = {
                "router_id": str(node.get("loopback_ipv4_address") or "").split("/")[0],
                "asn": str(node.get("bgp_as") or group.get("bgp_as") or ""),
            }
    return nodes


def asn_value(asn):
    """ASN as an int, from asplain (``4200000203``) or asdot (``64086.60107``)."""
    text = str(asn or "").strip()
    if "." in text:
        high, _, low = text.partition(".")
        if high.isdigit() and low.isdigit():
            return int(high) * 65536 + int(low)
        return None
    return int(text) if text.isdigit() else None


def _pairs_for(leaves, pairs, problems, label):
    tags = []
    for leaf in leaves or []:
        group = pairs.get(str(leaf))
        if not group:
            problems.append("%s %s is not in any leaf pair in the AVD repo's fabric file" % (label, leaf))
        elif group not in tags:
            tags.append(group)
    return tags


def _fabric_drift(record, fabric_vars):
    """Discovery's router-id/ASN per leaf against the AVD repo's fabric facts."""
    problems = []
    nodes = _fabric_nodes(fabric_vars)
    for leaf, bgp in sorted((record.get("leaf_bgp") or {}).items()):
        node = nodes.get(str(leaf))
        if not node or not isinstance(bgp, dict):
            continue
        router_id = str(bgp.get("router_id") or "").strip()
        asn = str(bgp.get("asn") or "").strip()
        if router_id and node["router_id"] and router_id != node["router_id"]:
            problems.append("%s router-id is %s on the switch but %s in the AVD repo" % (leaf, router_id, node["router_id"]))
        if asn and node["asn"] and asn_value(asn) != asn_value(node["asn"]):
            problems.append("%s ASN is %s on the switch but %s in the AVD repo" % (leaf, asn, node["asn"]))
    return problems


def avd_service_entries(record, fabric_vars, default_mtu=9214):
    """``{vlan_id, vrf, svi, l2vlan, problems, notes}`` for one VLAN record.

    ``vrf`` is ``{name, vrf_vni|vrf_id}`` when there is an SVI. ``svi`` and
    ``l2vlan`` are the AVD entries, or None. Nothing should be written while
    ``problems`` is not empty.
    """
    record = record or {}
    problems = []
    notes = []
    vlan_id = record.get("id")
    pairs = avd_leaf_pairs(fabric_vars)
    if not pairs:
        problems.append("the AVD repo's fabric file has no l3leaf node_groups")

    vni = record.get("vni")
    if not str(vni or "").strip().isdigit():
        problems.append("VLAN %s has no VNI; run discovery with vni_ranges set, or set vni in the record" % vlan_id)
        vni = None
    else:
        vni = int(vni)

    name = str(record.get("vlan_name") or record.get("name") or "VLAN%s" % vlan_id)
    common = {"id": int(vlan_id), "name": name} if str(vlan_id).isdigit() else {"id": vlan_id, "name": name}
    if vni is not None:
        common["vni_override"] = vni
    common["rd_override"] = common["id"]
    route_target = _vlan_route_target(record).get("rt_both")
    if route_target:
        common["rt_override"] = route_target

    migration = {"source": "vxlan-migration %s VLAN %s" % (record.get("data_center") or "", vlan_id)}
    if record.get("customer_type"):
        migration["customer_type"] = str(record["customer_type"])

    participating = list(record.get("target_switches") or record.get("participating_leafs") or [])
    gateway_leafs = list(record.get("gateway_leafs") or [])
    service_type = str(record.get("service_type") or "l2").lower()

    svi = None
    l2vlan = None
    vrf = None
    if service_type == "l3":
        mode = str(record.get("gateway_mode") or "distributed_anycast")
        if mode not in GATEWAY_MODES:
            problems.append("gateway_mode %r must be one of %s" % (mode, ", ".join(GATEWAY_MODES)))
        address = virtual_gateway_address(record.get("gateway"), record.get("prefixes"))
        if "/" not in str(address):
            problems.append("VLAN %s needs gateway and prefixes for the anycast address (got %r)" % (vlan_id, address))
        if record.get("secondary_prefixes"):
            notes.append("secondary prefixes %s are not moved (anycast primary only)" % ", ".join(record["secondary_prefixes"]))

        if mode == "centralized_anycast":
            if not gateway_leafs:
                problems.append("centralized_anycast needs gateway_leafs")
            svi_tags = _pairs_for(gateway_leafs, pairs, problems, "gateway leaf")
            l2_tags = [tag for tag in _pairs_for(participating, pairs, problems, "leaf") if tag not in svi_tags]
        else:
            svi_tags = _pairs_for(participating + [leaf for leaf in gateway_leafs if leaf not in participating], pairs, problems, "leaf")
            l2_tags = []

        svi = dict(common, enabled=True, tags=svi_tags, ip_address_virtual=address)
        svi["mtu"] = int(record.get("svi_mtu") or default_mtu)
        description = str(record.get("svi_description") or record.get("description") or "").strip()
        if description:
            svi["description"] = description
        svi["_migration"] = dict(migration, gateway_mode=mode)
        if l2_tags:
            l2vlan = dict(common, tags=l2_tags, _migration=dict(migration))

        vrf_name = str(record.get("vrf") or "default")
        vrf = {"name": vrf_name}
        if vrf_name == "default":
            problems.append(
                "VLAN %s gateway is in VRF default; the fabric advertises subnets with network statements "
                "under a VRF (no redistribute connected), so set the record's vrf" % vlan_id
            )
        elif str(record.get("l3_vni") or "").strip().isdigit():
            vrf["vrf_vni"] = int(record["l3_vni"])
        else:
            problems.append("VRF %s needs l3_vni (the fabric's L3 VNI) in the record" % vrf_name)
    else:
        tags = _pairs_for(participating, pairs, problems, "leaf")
        override = list(record.get("external_gateway_leafs") or [])
        if override:
            gateway_pairs = _pairs_for(override, pairs, problems, "external gateway leaf")
            if len(gateway_pairs) > 1:
                problems.append("external_gateway_leafs %s span more than one leaf pair" % override)
            elif gateway_pairs:
                migration["external_gateway_pair"] = gateway_pairs[0]
        else:
            gateway_pairs = [str(fabric_vars.get("_external_gateway_pair"))] if (fabric_vars or {}).get("_external_gateway_pair") else []
            if not gateway_pairs:
                problems.append("the AVD repo's fabric file has no _external_gateway_pair; set external_gateway_leafs on the record")
        tags += [pair for pair in gateway_pairs if pair not in tags]
        l2vlan = dict(common, tags=tags, _migration=dict(migration, gateway_mode="external"))

    for entry in (svi, l2vlan):
        if entry is not None and not entry["tags"]:
            problems.append("VLAN %s has no leaf pair to go on" % vlan_id)
    problems.extend(_fabric_drift(record, fabric_vars))
    return {"vlan_id": common["id"], "vrf": vrf, "svi": svi, "l2vlan": l2vlan, "problems": problems, "notes": notes}


def _drop_vlan(tenants, vlan_id):
    for tenant in tenants:
        for vrf in tenant.get("vrfs") or []:
            vrf["svis"] = [svi for svi in vrf.get("svis") or [] if svi.get("id") != vlan_id]
        tenant["l2vlans"] = [item for item in tenant.get("l2vlans") or [] if item.get("id") != vlan_id]
        if not tenant["l2vlans"]:
            tenant.pop("l2vlans")


def merge_avd_services(services, data_center, entries):
    """Return the services document with *entries* written in.

    Re-exporting a VLAN replaces its previous entries, in whichever tenant
    they were. New entries go to tenant ``<DC>_MIGRATION``. Returns
    ``{services, problems}``; ``services`` is unchanged when there are
    problems.
    """
    original = services if isinstance(services, dict) else {}
    document = copy.deepcopy(original)
    tenants = document.setdefault("tenants", []) or []
    document["tenants"] = tenants
    problems = []
    for entry in entries or []:
        problems.extend(entry.get("problems") or [])
    if problems:
        return {"services": original, "problems": problems}

    tenant_name = "%s_MIGRATION" % str(data_center).upper()
    for entry in entries:
        _drop_vlan(tenants, entry["vlan_id"])
        tenant = next((t for t in tenants if t.get("name") == tenant_name), None)
        if tenant is None:
            tenant = {"name": tenant_name}
            tenants.append(tenant)
        if entry.get("svi"):
            vrfs = tenant.setdefault("vrfs", [])
            vrf = next((v for v in vrfs if v.get("name") == entry["vrf"]["name"]), None)
            if vrf is None:
                vrf = dict(entry["vrf"], svis=[])
                vrfs.append(vrf)
            for key in ("vrf_vni", "vrf_id"):
                if key in entry["vrf"] and vrf.get(key) not in (None, entry["vrf"][key]):
                    problems.append("VRF %s has %s %s in the AVD repo but %s in the record" % (vrf["name"], key, vrf[key], entry["vrf"][key]))
            vrf.setdefault("svis", []).append(entry["svi"])
            vrf["svis"].sort(key=lambda item: item["id"])
        if entry.get("l2vlan"):
            tenant.setdefault("l2vlans", []).append(entry["l2vlan"])
            tenant["l2vlans"].sort(key=lambda item: item["id"])
    for tenant in tenants:
        for vrf in tenant.get("vrfs") or []:
            if not vrf.get("svis"):
                vrf.pop("svis", None)
        tenant["vrfs"] = [vrf for vrf in tenant.get("vrfs") or [] if vrf.get("svis")]
        if not tenant["vrfs"]:
            tenant.pop("vrfs")
    document["tenants"] = [t for t in tenants if t.get("vrfs") or t.get("l2vlans")]
    if problems:
        return {"services": original, "problems": problems}
    return {"services": document, "problems": []}


def _plain(value):
    """Plain dict/list/str/int copies. Ansible hands filters str and dict
    subclasses (unsafe text, hostvars) that yaml.safe_dump refuses."""
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        # AnsibleUnsafeText survives str() and encode/decode; str.__str__
        # returns a plain str copy of any str subclass.
        return str.__str__(value)
    return value


def avd_services_file_text(original_text, services):
    """The new services file: the original header comment, then the YAML."""
    import yaml

    header = []
    for line in str(original_text or "").splitlines():
        if line.startswith("#") or line.strip() in ("", "---"):
            header.append(line)
        else:
            break
    while header and not header[-1].strip():
        header.pop()
    if not header or header[0].strip() != "---":
        header.insert(0, "---")
    body = yaml.safe_dump(_plain(services), sort_keys=False, default_flow_style=False, width=1000)
    return "\n".join(header) + "\n" + body
