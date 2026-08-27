# -*- coding: utf-8 -*-
"""Shared helpers and constants for VLAN filter plugins."""

import re

from ansible.errors import AnsibleFilterError

_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9_-]+")

_MAC_ADDRESS_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"(?:"
    r"(?:[0-9a-fA-F]{2}[:.\-]){5}[0-9a-fA-F]{2}"  # aa:bb:cc:dd:ee:ff / aa-bb-... / aa.bb...
    r"|"
    r"(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}"  # Arista/Cisco xxxx.xxxx.xxxx
    r")"
    r"(?![0-9a-fA-F])"
)

_IP_ADDRESS_RE = re.compile(
    r"\b(?!(?:0\.|255\.))(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
)

_TRUNK_ALLOWED_VLAN_RE = re.compile(
    r"^\s*switchport trunk allowed vlan"
    r"(?: (?P<modifier>add|remove|except))?"
    r"(?: (?P<vlans>.+))?\s*$",
    re.I | re.M,
)

_SVI_VRF_PATTERNS = {
    "eos": (
        re.compile(r"^\s*vrf\s+forwarding\s+(\S+)", re.I | re.M),
        re.compile(r"^\s*ip\s+vrf\s+forwarding\s+(\S+)", re.I | re.M),
    ),
    "nxos": (
        re.compile(r"^\s*vrf\s+member\s+(\S+)", re.I | re.M),
        re.compile(r"^\s*ip\s+vrf\s+(\S+)", re.I | re.M),
    ),
    "ios": (
        re.compile(r"^\s*vrf\s+forwarding\s+(\S+)", re.I | re.M),
    ),
}

_TRUNK_INTERFACE_NAME_RES = {
    "eos": re.compile(
        r"^(?P<iface>(?:Eth|Ethernet|Po|Port-Channel)\S+)\s+\S+\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
    "nxos": re.compile(
        r"^(?P<iface>(?:Eth|Ethernet|po|Po|port-channel)\S+)\s+\d+\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
    "ios": re.compile(
        r"^(?P<iface>(?:Gi|GigabitEthernet|Te|TenGigabitEthernet|Po|Port-channel)\S+)\s+\S+\s+(?P<allowed>.+)$",
        re.M,
    ),
}

_VLAN_NOT_FOUND_MARKERS = (
    "does not exist",
    "not found",
    "invalid vlan",
    "no vlan",
    "vlan not configured",
    "invalid input",
)

_VLAN_STATUS_RE = re.compile(
    r"\b(active|inactive|suspend(?:ed)?|act/unsup|unsupport(?:ed)?)\b",
    re.I,
)

_PORT_NAME_RE = re.compile(
    r"(?:"
    r"Ethernet[\w./-]+|Et[\d./-]+|"
    r"Port-Channel[\d./-]+|Po[\d./-]+|"
    r"GigabitEthernet[\w./-]+|Gi[\d./-]+|"
    r"TenGigabitEthernet[\w./-]+|Te[\d./-]+|"
    r"FastEthernet[\w./-]+|Fa[\d./-]+|"
    r"Eth[\w./-]+"
    r")",
    re.I,
)

_SVI_DESC_RE = re.compile(r"^\s*description\s+(.+?)\s*$", re.I | re.M)

_SVI_MTU_RE = re.compile(r"^\s*mtu\s+(\d+)\s*$", re.I | re.M)

_SVI_IP_RE = re.compile(
    r"^\s*ip\s+address\s+(\d+\.\d+\.\d+\.\d+/\d+)(?:\s+secondary)?\s*$",
    re.I | re.M,
)

_SVI_VR_ADDR_RE = re.compile(
    r"^\s*ip\s+virtual-router\s+address\s+(\S+)\s*$",
    re.I | re.M,
)

_IP_ROUTE_RE = re.compile(
    r"^\s*ip\s+route(?:\s+vrf\s+(?P<vrf>\S+))?\s+"
    r"(?P<prefix>\S+)\s+(?P<nexthop>\S+)"
    r"(?:.*?\s+name\s+(?P<name>\S+))?",
    re.I | re.M,
)

_BGP_ROUTER_RE = re.compile(r"^\s*router\s+bgp\s+(\d+)\s*$", re.I)

_BGP_VRF_RE = re.compile(r"^\s*vrf\s+(\S+)\s*$", re.I)

_BGP_NEIGHBOR_ATTR_RE = re.compile(
    r"^\s*neighbor\s+(?P<neighbor>\S+)\s+(?P<attr>remote-as|update-source|description|route-map)\s+(?P<value>.+?)\s*$",
    re.I,
)

_VLAN_DB_SKIP_FILENAMES = frozenset({"_example.yml", "example.yml"})


def _output_indicates_missing(stdout):
    if not stdout or not str(stdout).strip():
        return True
    lower = str(stdout).lower()
    return any(marker in lower for marker in _VLAN_NOT_FOUND_MARKERS)


def _str_equal(a, b):
    """Compare two identifiers that may be ints or strings."""
    return str(a) == str(b)


def _vlan_present(vlan_id, output):
    """Legacy helper — prefer vlan_id_command_indicates_present for discovery."""
    return vlan_id_command_indicates_present(output, vlan_id)


def sanitize_report_slug(value):
    """Return a filesystem-safe slug for VLAN-centric report filenames."""
    if value is None:
        return "unknown_vlan"
    slug = _SLUG_INVALID.sub("_", str(value).strip())
    slug = slug.strip("_")
    return slug or "unknown_vlan"


def coalesce_trimmed(*values):
    """Return the first non-empty string after trim (treats None/'' as missing).

    Accepts either variadic arguments or a single list/tuple (Jinja list pipe).
    """
    candidates = []
    for value in values:
        if isinstance(value, (list, tuple)):
            candidates.extend(value)
        else:
            candidates.append(value)
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "undefined":
            return text
    return ""


def resolve_data_center(manual_data_center="", target_data_center="", data_center=""):
    """Resolve data center from playbook/extra vars (safe for Ansible strict undefined)."""
    return coalesce_trimmed(manual_data_center, target_data_center, data_center)


def normalize_target_vlan_ids(target_vlan_ids=None, manual_vlan_id=None):
    """Normalize CLI extra vars into a list of integer VLAN IDs."""
    if manual_vlan_id is not None and str(manual_vlan_id).strip() != "":
        return [int(manual_vlan_id)]

    if target_vlan_ids is None:
        return []

    if isinstance(target_vlan_ids, list):
        return [int(vid) for vid in target_vlan_ids]

    text = str(target_vlan_ids).strip()
    if not text:
        return []

    if text.startswith("["):
        import json

        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise AnsibleFilterError(
                "target_vlan_ids JSON must be a list, got %s" % type(parsed)
            )
        return [int(vid) for vid in parsed]

    if "," in text:
        return [int(part.strip()) for part in text.split(",") if part.strip()]

    return [int(text)]


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


def vlan_db_file_prefix(vlan_id):
    """Return the required filename prefix for a VLAN ID (e.g. 100 -> '0100_')."""
    return "%04d_" % int(vlan_id)


def vlan_db_record_filename(vlan_id, name):
    """Return the canonical per-DC VLAN DB filename for a record."""
    return "%s%s.yml" % (vlan_db_file_prefix(vlan_id), sanitize_report_slug(name))


