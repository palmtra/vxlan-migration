#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 tests for VLAN lifecycle playbooks.

select()/selectattr()/reject()/rejectattr() look up their predicate by name
in Jinja's TESTS namespace, not its FILTERS namespace, so predicates used
with those filters must be registered here rather than in plugins/filter/.
"""


def has_vlan_present(discovery_record):
    """Return True if a per-host discovery record has any VLAN present.

    Expects discovery_record to be a _vlan_discovery-shaped dict (i.e. the
    value produced by roles/vlan_discovery, with a 'per_vlan' list) --
    typically obtained via map('extract', hostvars, '_vlan_discovery').
    """
    for vlan in (discovery_record or {}).get("per_vlan", []):
        if vlan.get("vlan_present"):
            return True
    return False


class TestModule(object):
    def tests(self):
        return {
            "has_vlan_present": has_vlan_present,
        }
