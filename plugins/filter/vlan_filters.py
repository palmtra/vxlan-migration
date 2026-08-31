#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 filters for VLAN lifecycle playbooks.

Implementation lives in ``plugins/vlan_lib`` (kept outside filter_plugins so
Ansible does not load package modules as filter plugins).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from vlan_lib import (  # noqa: E402,F401
    analyze_trunk_vlan_carriage,
    arp_discovery_command,
    build_device_prune_plan,
    build_trunk_exempt_interface_names,
    build_vlan_discovery_reports,
    build_vlan_verification,
    coalesce_trimmed,
    extract_vlan_discovery,
    extract_vlan_targeted_discovery,
    load_service_from_directory,
    load_vlan_db_from_directory,
    mac_discovery_command,
    mac_table_has_learned_addresses,
    normalize_mac_address,
    normalize_prune_retain,
    normalize_target_vlan_ids,
    parse_arp_entries,
    parse_bgp_neighbors,
    parse_bgp_context,
    parse_ip_route_statics,
    parse_mac_address_table,
    parse_svi_details,
    parse_svi_vrf,
    parse_trunk_interfaces,
    parse_vlan_id_ports,
    parse_vlan_record,
    classify_mac_port_role,
    resolve_data_center,
    resolve_trunk_allowed_vlans,
    sanitize_report_slug,
    select_vlans_for_run,
    service_probe_vlan_records,
    service_vlan_ids,
    svi_command_indicates_present,
    trunk_interface_is_maintenance_exempt,
    trunk_interface_names,
    union_vlan_discovery_hosts,
    vlan_db_file_prefix,
    vlan_db_record_filename,
    vlan_id_command_indicates_present,
)


class FilterModule(object):
    def filters(self):
        return {
            "select_vlans_for_run": select_vlans_for_run,
            "extract_vlan_discovery": extract_vlan_discovery,
            "extract_vlan_targeted_discovery": extract_vlan_targeted_discovery,
            "vlan_id_command_indicates_present": vlan_id_command_indicates_present,
            "parse_vlan_id_ports": parse_vlan_id_ports,
            "trunk_interface_is_maintenance_exempt": trunk_interface_is_maintenance_exempt,
            "build_trunk_exempt_interface_names": build_trunk_exempt_interface_names,
            "trunk_interface_names": trunk_interface_names,
            "svi_command_indicates_present": svi_command_indicates_present,
            "parse_svi_vrf": parse_svi_vrf,
            "parse_svi_details": parse_svi_details,
            "parse_ip_route_statics": parse_ip_route_statics,
            "parse_bgp_neighbors": parse_bgp_neighbors,
            "parse_bgp_context": parse_bgp_context,
            "normalize_prune_retain": normalize_prune_retain,
            "build_device_prune_plan": build_device_prune_plan,
            "load_service_from_directory": load_service_from_directory,
            "service_vlan_ids": service_vlan_ids,
            "service_probe_vlan_records": service_probe_vlan_records,
            "arp_discovery_command": arp_discovery_command,
            "mac_discovery_command": mac_discovery_command,
            "mac_table_has_learned_addresses": mac_table_has_learned_addresses,
            "parse_mac_address_table": parse_mac_address_table,
            "parse_arp_entries": parse_arp_entries,
            "normalize_mac_address": normalize_mac_address,
            "classify_mac_port_role": classify_mac_port_role,
            "parse_trunk_interfaces": parse_trunk_interfaces,
            "analyze_trunk_vlan_carriage": analyze_trunk_vlan_carriage,
            "resolve_trunk_allowed_vlans": resolve_trunk_allowed_vlans,
            "build_vlan_verification": build_vlan_verification,
            "sanitize_report_slug": sanitize_report_slug,
            "build_vlan_discovery_reports": build_vlan_discovery_reports,
            "vlan_db_record_filename": vlan_db_record_filename,
            "vlan_db_file_prefix": vlan_db_file_prefix,
            "normalize_target_vlan_ids": normalize_target_vlan_ids,
            "coalesce_trimmed": coalesce_trimmed,
            "resolve_data_center": resolve_data_center,
            "load_vlan_db_from_directory": load_vlan_db_from_directory,
            "union_vlan_discovery_hosts": union_vlan_discovery_hosts,
        }
