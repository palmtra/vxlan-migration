# -*- coding: utf-8 -*-
"""JSON Schema validation for VLAN and service SSOT records."""

import json
from pathlib import Path

from ansible.errors import AnsibleFilterError

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
_VLAN_SCHEMA_PATH = _SCHEMA_DIR / "vlan_record.schema.json"
_SERVICE_SCHEMA_PATH = _SCHEMA_DIR / "service_bundle.schema.json"
_SCHEMA_CACHE = {}


def _load_schema(path):
    key = str(path)
    if key not in _SCHEMA_CACHE:
        if not path.is_file():
            raise AnsibleFilterError("Schema file not found: %s" % path)
        with path.open(encoding="utf-8") as handle:
            _SCHEMA_CACHE[key] = json.load(handle)
    return _SCHEMA_CACHE[key]


def validate_against_schema(document, schema_name, source_name=""):
    """Validate *document* against a named schema (vlan_record|service_bundle).

    Returns the document unchanged on success. Raises AnsibleFilterError on failure.
    Set validate_ssot_schema=false (passed as validate=False) to skip.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise AnsibleFilterError(
            "jsonschema is required for SSOT validation: %s" % exc
        ) from exc

    if schema_name == "vlan_record":
        schema = _load_schema(_VLAN_SCHEMA_PATH)
    elif schema_name == "service_bundle":
        schema = _load_schema(_SERVICE_SCHEMA_PATH)
    else:
        raise AnsibleFilterError("Unknown schema_name '%s'" % schema_name)

    try:
        jsonschema.validate(instance=document, schema=schema)
    except jsonschema.ValidationError as err:
        path = ".".join(str(p) for p in err.path) or "(root)"
        where = source_name or schema_name
        raise AnsibleFilterError(
            "SSOT schema validation failed for %s (%s): %s: %s"
            % (where, schema_name, path, err.message)
        ) from err
    return document
