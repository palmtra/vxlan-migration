#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Custom Jinja2 filters for VLAN lifecycle playbooks."""

import re

from ansible.errors import AnsibleFilterError

_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9_-]+")


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


_MAC_ADDRESS_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{2}[:.\-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F])"
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


def _output_indicates_missing(stdout):
    if not stdout or not str(stdout).strip():
        return True
    lower = str(stdout).lower()
    return any(marker in lower for marker in _VLAN_NOT_FOUND_MARKERS)


def vlan_id_command_indicates_present(stdout, vlan_id):
    """Return True when platform 'show vlan id' style output confirms the VLAN."""
    if _output_indicates_missing(stdout):
        return False
    vid = re.escape(str(vlan_id))
    patterns = (
        rf"^\s*{vid}\s+\S+",  # EOS/NXOS table row starting with VLAN ID
        rf"\bvid\s*:?\s*{vid}\b",
        rf"\bvlan\s*{vid}\b",
        rf"\bvlan{vid}\b",
    )
    text = str(stdout)
    return any(re.search(pattern, text, re.I | re.M) for pattern in patterns)


def svi_command_indicates_present(stdout, vlan_id):
    """Return True when 'show run interface VlanX' (or equivalent) shows an SVI."""
    if _output_indicates_missing(stdout):
        return False
    vid = re.escape(str(vlan_id))
    text = str(stdout)
    patterns = (
        rf"interface\s+Vlan{vid}\b",
        rf"interface\s+vlan{vid}\b",
        rf"interface\s+Vlan\s*{vid}\b",
    )
    if any(re.search(pattern, text, re.I) for pattern in patterns):
        return True
    # Brief output fallback: configured SVI line without full running-config.
    return bool(re.search(rf"\bVlan{vid}\b", text, re.I))


def mac_table_has_learned_addresses(mac_output):
    """Return True when the MAC table output contains at least one learned MAC."""
    if not mac_output or not str(mac_output).strip():
        return False
    text = str(mac_output)
    for line in text.splitlines():
        lower = line.lower()
        if any(skip in lower for skip in ("mac address", "----", "total", "multicast", "router", "cpu")):
            continue
        if _MAC_ADDRESS_RE.search(line):
            return True
    return False


def arp_table_has_learned_neighbors(arp_output, vlan_id=None):
    """Return True when ARP output contains host IP entries for the VLAN SVI."""
    if not arp_output or not str(arp_output).strip():
        return False
    text = str(arp_output)
    if vlan_id is not None and not _output_indicates_missing(text):
        vid = re.escape(str(vlan_id))
        if re.search(rf"\bVlan{vid}\b", text, re.I):
            return bool(_IP_ADDRESS_RE.search(text))
    return bool(_IP_ADDRESS_RE.search(text))


def parse_svi_vrf(svi_config, platform="eos"):
    """Extract VRF name bound to an SVI from its running-config snippet."""
    if not svi_config:
        return "default"
    text = str(svi_config)
    patterns = _SVI_VRF_PATTERNS.get(str(platform).lower(), _SVI_VRF_PATTERNS["eos"])
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return "default"


def arp_discovery_command(platform, vlan_id, svi_vrf=None, svi_present=False):
    """Build a platform-appropriate ARP command for an SVI in its VRF."""
    if not svi_present:
        return ""
    vrf = svi_vrf or "default"
    plat = str(platform).lower()
    vid = int(vlan_id)
    if plat == "eos":
        return f"show ip arp vrf {vrf} | include Vlan{vid}"
    if plat == "nxos":
        return f"show ip arp vrf {vrf} vlan {vid}"
    if plat == "ios":
        return f"show ip arp vrf {vrf} | include Vlan{vid}"
    return f"show ip arp vrf {vrf}"


def expand_vlan_spec(spec):
    """Expand a VLAN list/range token string into a set of integer VLAN IDs."""
    result = set()
    if not spec:
        return result
    for token in str(spec).split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_str, end_str = token.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            if start > end:
                start, end = end, start
            result.update(range(start, end + 1))
        else:
            result.add(int(token))
    return result


def _allowed_summary_is_all(allowed_summary):
    if not allowed_summary:
        return False
    normalized = str(allowed_summary).strip().lower()
    if normalized in {"all", "none"}:
        return normalized == "all"
    if normalized in {"1-4094", "1-4095", "1 - 4094", "1 - 4095"}:
        return True
    expanded = expand_vlan_spec(normalized)
    return len(expanded) >= 4090


def parse_trunk_interfaces(show_trunk_output, platform="eos"):
    """Parse `show interfaces trunk` into interface names and allowed summaries."""
    text = str(show_trunk_output or "")
    pattern = _TRUNK_INTERFACE_NAME_RES.get(str(platform).lower())
    if not pattern:
        return []
    interfaces = []
    for match in pattern.finditer(text):
        interfaces.append(
            {
                "interface": match.group("iface").strip(),
                "allowed_summary": match.group("allowed").strip(),
            }
        )
    return interfaces


def _trunk_entry(
    interface,
    pruning_mode,
    vlan_carried,
    cleanup_recommended,
    reason,
    allowed_summary="",
    config_line="",
):
    """Build a read-only trunk discovery record."""
    recommendation = reason
    if cleanup_recommended:
        recommendation = (
            f"{reason} Plan trunk cleanup during legacy decommission "
            f"(see cvg-decomm-vlan); discovery does not generate config commands."
        )
    return {
        "interface": interface,
        "pruning_mode": pruning_mode,
        "vlan_carried": vlan_carried,
        "cleanup_recommended": cleanup_recommended,
        "allowed_summary": allowed_summary,
        "config_line": config_line,
        "reason": reason,
        "recommendation": recommendation,
    }


def analyze_trunk_vlan_carriage(vlan_id, trunk_interfaces, platform="eos"):
    """Report whether a VLAN is carried on each trunk (read-only discovery).

    Does not generate configuration commands. When the VLAN is explicitly
    allowed on a pruned trunk, sets cleanup_recommended for decommission planning.
    """
    vid = int(vlan_id)
    results = []

    for trunk in trunk_interfaces or []:
        iface = trunk.get("interface", "")
        config = str(trunk.get("config", "") or "")
        allowed_summary = str(trunk.get("allowed_summary", "") or "").strip()
        matches = list(_TRUNK_ALLOWED_VLAN_RE.finditer(config))
        config_line = matches[-1].group(0).strip() if matches else ""

        if matches:
            modifier = None
            vlan_tokens = ""
            for match in matches:
                modifier = (match.group("modifier") or "").lower() or modifier
                vlan_tokens = match.group("vlans") or vlan_tokens

            if modifier == "except":
                except_set = expand_vlan_spec(vlan_tokens)
                if vid in except_set:
                    results.append(
                        _trunk_entry(
                            iface,
                            "except",
                            False,
                            False,
                            "VLAN is excluded on this except-style trunk.",
                            allowed_summary,
                            config_line,
                        )
                    )
                else:
                    results.append(
                        _trunk_entry(
                            iface,
                            "except",
                            True,
                            True,
                            "VLAN is allowed on an except-style trunk (not in the except list).",
                            allowed_summary,
                            config_line,
                        )
                    )
                continue

            allowed_set = expand_vlan_spec(vlan_tokens)
            if not allowed_set:
                results.append(
                    _trunk_entry(
                        iface,
                        "none",
                        False,
                        False,
                        "Trunk allowed VLAN list is empty.",
                        allowed_summary,
                        config_line,
                    )
                )
                continue

            if vid in allowed_set:
                results.append(
                    _trunk_entry(
                        iface,
                        "allow_list",
                        True,
                        True,
                        "VLAN is explicitly listed in the trunk allowed VLAN set.",
                        allowed_summary,
                        config_line,
                    )
                )
            else:
                results.append(
                    _trunk_entry(
                        iface,
                        "allow_list",
                        False,
                        False,
                        "VLAN is not in the trunk allowed VLAN set.",
                        allowed_summary,
                        config_line,
                    )
                )
            continue

        if _allowed_summary_is_all(allowed_summary):
            results.append(
                _trunk_entry(
                    iface,
                    "all",
                    True,
                    False,
                    "Trunk allows all VLANs (no explicit allowed list).",
                    allowed_summary,
                )
            )
            continue

        if not allowed_summary:
            results.append(
                _trunk_entry(
                    iface,
                    "all",
                    True,
                    False,
                    "No switchport trunk allowed vlan statement (default: all VLANs).",
                    allowed_summary,
                )
            )
            continue

        summary = allowed_summary.lower()
        if summary == "none":
            results.append(
                _trunk_entry(
                    iface,
                    "none",
                    False,
                    False,
                    "Trunk allows no VLANs.",
                    allowed_summary,
                )
            )
            continue

        explicit = expand_vlan_spec(summary)
        if vid in explicit:
            results.append(
                _trunk_entry(
                    iface,
                    "allow_list",
                    True,
                    True,
                    "VLAN appears in the trunk allowed VLAN summary from show interfaces trunk.",
                    allowed_summary,
                )
            )
        else:
            results.append(
                _trunk_entry(
                    iface,
                    "allow_list",
                    False,
                    False,
                    "VLAN is not in the trunk allowed VLAN summary.",
                    allowed_summary,
                )
            )

    return results


def extract_vlan_targeted_discovery(vlan, outputs):
    """Build a per-VLAN discovery record from targeted CLI commands."""
    vlan_id = vlan.get("id")
    if vlan_id is None:
        raise AnsibleFilterError("extract_vlan_targeted_discovery requires vlan['id']")

    vlan_output = outputs.get("vlan_id_output", "")
    svi_output = outputs.get("svi_output", "")
    mac_output = outputs.get("mac_output", "")
    stp_output = outputs.get("stp_output", "")
    arp_output = outputs.get("arp_output", "")
    platform = outputs.get("platform", "eos")
    trunk_interfaces = outputs.get("trunk_interfaces", []) or []

    vlan_present = vlan_id_command_indicates_present(vlan_output, vlan_id)
    svi_present = svi_command_indicates_present(svi_output, vlan_id)
    svi_vrf = parse_svi_vrf(svi_output, platform) if svi_present else ""
    mac_learned = mac_table_has_learned_addresses(mac_output)
    arp_learned = arp_table_has_learned_neighbors(arp_output, vlan_id) if svi_present else False

    trunk_vlan_status = analyze_trunk_vlan_carriage(vlan_id, trunk_interfaces, platform)
    trunk_cleanup_recommendations = [
        entry for entry in trunk_vlan_status if entry.get("cleanup_recommended")
    ]

    trunks_present = bool(trunk_cleanup_recommendations) or bool(
        trunk_vlan_status
        and any(entry.get("vlan_carried") for entry in trunk_vlan_status)
    )
    if not trunks_present and vlan_present and vlan_output:
        trunks_present = bool(
            re.search(r"(Et|Eth|Ethernet|Po|Port-Channel|Gi|Te|Fa)\d", vlan_output, re.I)
        )

    return {
        "vlan_id": vlan_id,
        "vlan_name": vlan.get("name", ""),
        "target_vrf": svi_vrf or outputs.get("target_vrf", vlan.get("vrf", "")),
        "svi_vrf": svi_vrf,
        "vlan_present": vlan_present,
        "svi_present": svi_present,
        "trunks_present": trunks_present,
        "mac_learned": mac_learned,
        "arp_learned": arp_learned,
        "vlan_id_raw": vlan_output,
        "svi_raw": svi_output,
        "mac_table_raw": mac_output,
        "stp_raw": stp_output,
        "arp_raw": arp_output,
        "trunk_vlan_status": trunk_vlan_status,
        "trunk_cleanup_recommendations": trunk_cleanup_recommendations,
    }


def _vlan_present(vlan_id, output):
    """Legacy helper — prefer vlan_id_command_indicates_present for discovery."""
    return vlan_id_command_indicates_present(output, vlan_id)


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


def sanitize_report_slug(value):
    """Return a filesystem-safe slug for VLAN-centric report filenames."""
    if value is None:
        return "unknown_vlan"
    slug = _SLUG_INVALID.sub("_", str(value).strip())
    slug = slug.strip("_")
    return slug or "unknown_vlan"


_VLAN_DB_SKIP_FILENAMES = frozenset({"_example.yml", "example.yml"})


def vlan_db_file_prefix(vlan_id):
    """Return the required filename prefix for a VLAN ID (e.g. 100 -> '0100_')."""
    return "%04d_" % int(vlan_id)


def vlan_db_record_filename(vlan_id, name):
    """Return the canonical per-DC VLAN DB filename for a record."""
    return "%s%s.yml" % (vlan_db_file_prefix(vlan_id), sanitize_report_slug(name))


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


def _vlan_db_skip_filename(filename):
    lower = str(filename).lower()
    if lower in _VLAN_DB_SKIP_FILENAMES:
        return True
    return str(filename).startswith("_")


def parse_vlan_record(document, source_name=""):
    """Extract a single VLAN record dict from a loaded YAML document."""
    if not isinstance(document, dict):
        return None

    if document.get("_meta", {}).get("example"):
        return None

    if "vlans" in document:
        entries = document.get("vlans") or []
        if not isinstance(entries, list):
            raise AnsibleFilterError(
                "VLAN DB file %s: 'vlans' must be a list." % source_name
            )
        if len(entries) == 0:
            return None
        if len(entries) > 1:
            raise AnsibleFilterError(
                "VLAN DB file %s: per-VLAN files must contain one record, found %d."
                % (source_name, len(entries))
            )
        document = entries[0]

    if not isinstance(document, dict) or "id" not in document:
        return None

    return document


def load_vlan_db_from_directory(vlan_db_dir, data_center, target_vlan_ids=None):
    """Load VLAN records from vars/vlans/<dc>/*.yml."""
    import os

    try:
        import yaml
    except ImportError as exc:
        raise AnsibleFilterError(
            "PyYAML is required to load the VLAN database: %s" % exc
        ) from exc

    dc = str(data_center).strip()
    if not dc:
        raise AnsibleFilterError("data_center must be a non-empty string.")

    base_dir = os.path.join(str(vlan_db_dir), dc)
    normalized_ids = [str(int(vid)) for vid in (target_vlan_ids or [])]
    prefixes = [vlan_db_file_prefix(vid) for vid in normalized_ids] if normalized_ids else []

    records = []
    sources = []
    seen_ids = {}

    def _ingest_file(path, filename):
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}

        record = parse_vlan_record(document, filename)
        if record is None:
            return

        vlan_id = str(record.get("id"))
        if vlan_id in seen_ids:
            raise AnsibleFilterError(
                "Duplicate VLAN ID %s in %s and %s."
                % (vlan_id, filename, seen_ids[vlan_id])
            )

        seen_ids[vlan_id] = filename
        records.append(record)
        sources.append(path)

    if os.path.isdir(base_dir):
        for filename in sorted(os.listdir(base_dir)):
            if not filename.endswith((".yml", ".yaml")):
                continue
            if _vlan_db_skip_filename(filename):
                continue
            if prefixes and not any(filename.startswith(prefix) for prefix in prefixes):
                continue
            _ingest_file(os.path.join(base_dir, filename), filename)

    records.sort(key=lambda item: int(item.get("id", 0)))

    return {
        "vlans": records,
        "sources": sources,
        "data_center": dc,
        "directory": base_dir,
    }


def build_vlan_discovery_reports(vlans, play_hosts, hostvars):
    """Build one aggregated discovery report dict per VLAN in *vlans*."""
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects a list, got %s" % type(vlans)
        )
    if not isinstance(play_hosts, list):
        raise AnsibleFilterError(
            "build_vlan_discovery_reports expects play_hosts list, got %s"
            % type(play_hosts)
        )

    reports = []
    for vlan in vlans:
        vlan_id = vlan.get("id")
        devices = []
        for host in play_hosts:
            host_data = hostvars.get(host, {})
            discovery = host_data.get("_vlan_discovery", {})
            per_vlan = {}
            for entry in discovery.get("per_vlan", []) or []:
                if _str_equal(entry.get("vlan_id"), vlan_id):
                    per_vlan = entry
                    break
            devices.append(
                {
                    "hostname": discovery.get("hostname", host),
                    "data_center": discovery.get(
                        "data_center", host_data.get("data_center", "unknown")
                    ),
                    "os_family": discovery.get("os_family", ""),
                    "timestamp": discovery.get("timestamp", ""),
                    "vlan_present": per_vlan.get("vlan_present", False),
                    "svi_present": per_vlan.get("svi_present", False),
                    "trunks_present": per_vlan.get("trunks_present", False),
                    "mac_learned": per_vlan.get("mac_learned", False),
                    "arp_learned": per_vlan.get("arp_learned", False),
                    "svi_vrf": per_vlan.get("svi_vrf", ""),
                    "vlan_id_raw": per_vlan.get("vlan_id_raw", ""),
                    "svi_raw": per_vlan.get("svi_raw", ""),
                    "target_vrf": per_vlan.get("svi_vrf")
                    or per_vlan.get("target_vrf", vlan.get("vrf", "default")),
                    "mac_table_raw": per_vlan.get("mac_table_raw", ""),
                    "stp_raw": per_vlan.get("stp_raw", ""),
                    "arp_raw": per_vlan.get("arp_raw", ""),
                    "trunk_vlan_status": per_vlan.get("trunk_vlan_status", []),
                    "trunk_cleanup_recommendations": per_vlan.get(
                        "trunk_cleanup_recommendations", []
                    ),
                    "vxlan_raw": discovery.get("vxlan_raw", ""),
                }
            )

        trunk_cleanup_candidates = []
        for device in devices:
            for entry in device.get("trunk_cleanup_recommendations", []) or []:
                trunk_cleanup_candidates.append(
                    {
                        "hostname": device["hostname"],
                        "interface": entry.get("interface"),
                        "pruning_mode": entry.get("pruning_mode"),
                        "recommendation": entry.get("recommendation"),
                    }
                )

        switches_found = [
            device["hostname"]
            for device in devices
            if device.get("vlan_present")
        ]
        switches_with_mac_learning = [
            device["hostname"]
            for device in devices
            if device.get("mac_learned")
        ]
        switches_with_svi = [
            device["hostname"]
            for device in devices
            if device.get("svi_present")
        ]
        switches_with_arp = [
            device["hostname"]
            for device in devices
            if device.get("arp_learned")
        ]

        reports.append(
            {
                "vlan_id": vlan_id,
                "vlan_name": vlan.get("name", ""),
                "vlan_slug": sanitize_report_slug(vlan.get("name")),
                "service_type": vlan.get("service_type", ""),
                "target_switches": vlan.get("target_switches", []),
                "switches_found": switches_found,
                "switches_with_mac_learning": switches_with_mac_learning,
                "switches_with_svi": switches_with_svi,
                "switches_with_arp": switches_with_arp,
                "trunk_cleanup_candidates": trunk_cleanup_candidates,
                "vni": vlan.get("vni"),
                "vrf": vlan.get("vrf", "default"),
                "data_center": vlan.get("data_center", ""),
                "devices": devices,
            }
        )
    return reports


def union_vlan_discovery_hosts(vlans):
    """Return a de-duplicated list of discovery switches across VLAN records."""
    if not isinstance(vlans, list):
        raise AnsibleFilterError(
            "union_vlan_discovery_hosts expects a list, got %s" % type(vlans)
        )

    hosts = []
    seen = set()
    for vlan in vlans:
        if not isinstance(vlan, dict):
            continue
        candidates = vlan.get("discovery_switches") or vlan.get("target_switches") or []
        for host in candidates:
            host_key = str(host)
            if host_key in seen:
                continue
            seen.add(host_key)
            hosts.append(host)
    return hosts


def build_vlan_verification(vlan_id, outputs):
    """Build a post-change verification summary for a single VLAN.

    Args:
        vlan_id: the VLAN ID being verified
        outputs: dict with keys:
            vni_output: raw 'show vxlan vlan-to-vni' output
            mac_outputs: list of result dicts from the looped MAC table commands
            arp_output: raw ARP table output for the target VRF
            registry: list of VLAN dictionaries from the canonical registry
                      (used to look up the expected VNI for this VLAN)

    Returns:
        Dictionary with vlan_id, vni, vni_mapped, and mac_or_arp_learned.
    """
    vni_output = outputs.get("vni_output", "")
    arp_output = outputs.get("arp_output", "")
    registry = outputs.get("registry", []) or []

    vni = None
    for entry in registry:
        if _str_equal(entry.get("id"), vlan_id):
            vni = entry.get("vni")
            break

    vni_mapped = False
    if vni is not None:
        vni_mapped = _vlan_present(vlan_id, vni_output) and _vlan_present(vni, vni_output)

    mac_outputs = outputs.get("mac_outputs", []) or []
    mac_output = ""
    for entry in mac_outputs:
        if _str_equal(entry.get("item"), vlan_id):
            mac_output = entry.get("stdout", "")
            break

    mac_pattern = r"([0-9a-fA-F]{2}[:.\-]){5}[0-9a-fA-F]{2}"
    mac_learned = bool(re.search(mac_pattern, mac_output))
    arp_learned = bool(re.search(r"\d+\.\d+\.\d+\.\d+", arp_output))

    return {
        "vlan_id": vlan_id,
        "vni": vni,
        "vni_mapped": vni_mapped,
        "mac_or_arp_learned": mac_learned or arp_learned,
    }


class FilterModule(object):
    def filters(self):
        return {
            "select_vlans_for_run": select_vlans_for_run,
            "extract_vlan_discovery": extract_vlan_discovery,
            "extract_vlan_targeted_discovery": extract_vlan_targeted_discovery,
            "vlan_id_command_indicates_present": vlan_id_command_indicates_present,
            "svi_command_indicates_present": svi_command_indicates_present,
            "parse_svi_vrf": parse_svi_vrf,
            "arp_discovery_command": arp_discovery_command,
            "mac_table_has_learned_addresses": mac_table_has_learned_addresses,
            "parse_trunk_interfaces": parse_trunk_interfaces,
            "analyze_trunk_vlan_carriage": analyze_trunk_vlan_carriage,
            "build_vlan_verification": build_vlan_verification,
            "sanitize_report_slug": sanitize_report_slug,
            "build_vlan_discovery_reports": build_vlan_discovery_reports,
            "vlan_db_record_filename": vlan_db_record_filename,
            "vlan_db_file_prefix": vlan_db_file_prefix,
            "normalize_target_vlan_ids": normalize_target_vlan_ids,
            "load_vlan_db_from_directory": load_vlan_db_from_directory,
            "union_vlan_discovery_hosts": union_vlan_discovery_hosts,
        }
