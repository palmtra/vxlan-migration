#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 filters for VLAN lifecycle playbooks."""

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

    target_vlan_ids = target_vlan_ids or []
    target_actions = target_actions or []

    def _keep(vlan):
        if target_vlan_ids and vlan.get("id") not in target_vlan_ids:
            return False
        if target_actions and vlan.get("action") not in target_actions:
            return False
        return True

    return [vlan for vlan in vlans if _keep(vlan)]


class FilterModule(object):
    def filters(self):
        return {
            "select_vlans_for_run": select_vlans_for_run,
        }
