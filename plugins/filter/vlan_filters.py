#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 filters for VLAN lifecycle playbooks."""

import re

from ansible.errors import AnsibleFilterError


def select_vlans_for_run(vlans, target_vlan_ids=None, target_actions=None):
    """Filter the canonical VLAN registry for the current run.

    Args:
        vlans: list of VLAN dictionaries (must contain 'id' and 'action')
        target_vlan_ids: optional list of VLAN IDs to include
        target_actions: optional list of actions (e.g. ['migrate'])

    Returns:
        Filtered list of VLAN dictionaries.
    """
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "select_vlans_for_run expects a list, got %s" % type(vlans)
        )

    target_vlan_ids = [str(vid) for vid in (target_vlan_ids or [])]
    target_actions = target_actions or []

    def _keep(vlan):
        if target_vlan_ids and str(vlan.get("id")) not in target_vlan_ids:
            return False
        if target_actions and vlan.get("action") not in target_actions:
            return False
        return True

    return [vlan for vlan in vlans if _keep(vlan)]


def _vlan_present(vlan_id, output):
    """Return True if vlan_id appears as a standalone VLAN in output."""
    if not output:
        return False
    pattern = r"\b%s\b" % re.escape(str(vlan_id))
    return bool(re.search(pattern, output))


def _str_equal(a, b):
    """Compare two identifiers that may be ints or strings."""
    return str(a) == str(b)


def extract_vlan_discovery(vlan, outputs):
    """Build a structured per-VLAN discovery record from raw CLI outputs.

    Args:
        vlan: VLAN dictionary from the registry (must contain 'id')
        outputs: dict with keys:
            vlan_output, svi_output, trunk_output,
            mac_outputs (list of result dicts from looped commands),
            stp_outputs (list of result dicts from looped commands),
            arp_output

    Returns:
        Dictionary summarizing presence and raw output excerpts.
    """
    vlan_id = vlan.get("id")
    if vlan_id is None:
        raise AnsibleFilterError("extract_vlan_discovery requires vlan['id']")

    vlan_output = outputs.get("vlan_output", "")
    svi_output = outputs.get("svi_output", "")
    trunk_output = outputs.get("trunk_output", "")
    arp_output = outputs.get("arp_output", "")

    # Locate the mac/stp outputs that belong to this VLAN ID.
    mac_outputs = outputs.get("mac_outputs", []) or []
    stp_outputs = outputs.get("stp_outputs", []) or []
    mac_output = ""
    stp_output = ""
    for entry in mac_outputs:
        if _str_equal(entry.get("item"), vlan_id):
            mac_output = entry.get("stdout", "")
            break
    for entry in stp_outputs:
        if _str_equal(entry.get("item"), vlan_id):
            stp_output = entry.get("stdout", "")
            break

    return {
        "vlan_id": vlan_id,
        "vlan_name": vlan.get("name", ""),
        "target_vrf": outputs.get("target_vrf", vlan.get("vrf", "")),
        "vlan_present": _vlan_present(vlan_id, vlan_output),
        "svi_present": _vlan_present(vlan_id, svi_output),
        "trunks_present": _vlan_present(vlan_id, trunk_output),
        "mac_table_raw": mac_output,
        "stp_raw": stp_output,
        "arp_raw": arp_output,
    }


def has_vlan_present(discovery_record):
    """Return True if a per-host discovery record has any VLAN present."""
    for vlan in discovery_record.get("per_vlan", []):
        if vlan.get("vlan_present"):
            return True
    return False


class FilterModule(object):
    def filters(self):
        return {
            "select_vlans_for_run": select_vlans_for_run,
            "extract_vlan_discovery": extract_vlan_discovery,
            "has_vlan_present": has_vlan_present,
        }
