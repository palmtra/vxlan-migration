# -*- coding: utf-8 -*-
"""VLAN DB and service-bundle loaders."""

from ansible.errors import AnsibleFilterError

from vlan_lib.common import (
    _VLAN_DB_SKIP_FILENAMES,
    coalesce_trimmed,
    sanitize_report_slug,
    vlan_db_file_prefix,
)
from vlan_lib.placement import flatten_deployment_model
from vlan_lib.schema import validate_against_schema


def _coerce_record_int(value):
    """Accept a VLAN or VNI written as text, such as id: '3000'."""
    if isinstance(value, bool) or value in (None, ""):
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return value


def _vlan_db_skip_filename(filename):
    lower = str(filename).lower()
    if lower in _VLAN_DB_SKIP_FILENAMES:
        return True
    return str(filename).startswith("_")


def parse_vlan_record(document, source_name="", validate=True):
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

    document = flatten_deployment_model(document)
    if isinstance(document, dict):
        for key in ("id", "vni", "l3_vni"):
            if key in document:
                document[key] = _coerce_record_int(document.get(key))

    if not isinstance(document, dict) or "id" not in document:
        return None

    if validate:
        validate_against_schema(document, "vlan_record", source_name)
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
        record["data_center"] = dc
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


def load_service_from_directory(service_db_dir, data_center, service_id):
    """Load one service migration bundle from vars/services/<dc>/<service_id>.yml."""
    import os

    try:
        import yaml
    except ImportError as exc:
        raise AnsibleFilterError(
            "PyYAML is required for load_service_from_directory: %s" % exc
        )

    dc = coalesce_trimmed(data_center)
    sid = coalesce_trimmed(service_id)
    if not dc:
        raise AnsibleFilterError("load_service_from_directory requires data_center")
    if not sid:
        raise AnsibleFilterError("load_service_from_directory requires service_id")
    if sid.startswith("_"):
        raise AnsibleFilterError(
            "service_id '%s' is reserved (files starting with _ are examples)" % sid
        )

    base_dir = os.path.join(str(service_db_dir), dc)
    candidates = [
        os.path.join(base_dir, "%s.yml" % sid),
        os.path.join(base_dir, "%s.yaml" % sid),
    ]
    path = next((item for item in candidates if os.path.isfile(item)), None)
    if path is None:
        raise AnsibleFilterError(
            "Service '%s' not found under %s/ (expected %s.yml)"
            % (sid, base_dir, sid)
        )

    with open(path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    if not isinstance(document, dict):
        raise AnsibleFilterError("Service file %s must be a YAML mapping" % path)
    if document.get("_meta", {}).get("example"):
        raise AnsibleFilterError(
            "Service file %s is marked as an example (_meta.example) and cannot be loaded"
            % path
        )

    record = dict(document)
    record.setdefault("id", sid)
    record.setdefault("data_center", dc)
    validate_against_schema(record, "service_bundle", path)
    record["_source_file"] = path
    return record


def service_vlan_ids(service_record):
    """Return integer VLAN IDs declared on a service bundle."""
    if not service_record:
        return []
    ids = []
    for entry in service_record.get("vlans") or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            ids.append(int(entry["id"]))
        elif entry is not None and not isinstance(entry, dict):
            ids.append(int(entry))
    return ids


def service_probe_vlan_records(service_record, existing_vlans=None):
    """Build synthetic VLAN DB records for service VLANs missing from the DC DB."""
    if not service_record:
        return []
    existing_ids = {
        int(item.get("id"))
        for item in (existing_vlans or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    records = []
    for entry in service_record.get("vlans") or []:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        vlan_id = int(entry["id"])
        if vlan_id in existing_ids:
            continue
        name = entry.get("vlan_name") or entry.get("name") or ("vlan_%s" % vlan_id)
        records.append(
            {
                "id": vlan_id,
                "name": sanitize_report_slug(name),
                "vlan_name": name,
                "action": "migrate",
                "service_type": entry.get("service_type") or "l3",
                "vrf": entry.get("vrf") or "default",
                "vni": entry.get("vni"),
                "target_switches": entry.get("target_switches") or [],
                "discovery_switches": entry.get("discovery_switches")
                or entry.get("target_switches")
                or [],
                "static_route_tags": entry.get("static_route_tags") or [],
                "_from_service": service_record.get("id", ""),
            }
        )
    return records


