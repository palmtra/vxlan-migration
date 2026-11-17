# -*- coding: utf-8 -*-
"""Render EOS CLI from an AVD structured config with pyavd, on the control node.

Used by the Studios deploy (roles/avd_vxlan_config/tasks/studios.yml), which
renders every leaf in one run_once task, so a rollback started from a rescue
on one host still renders all of the VLAN's leaves.
"""

from ansible.errors import AnsibleFilterError


def avd_eos_config(structured_config, hostname=""):
    """Validate *structured_config* against AVD's eos_config schema and render it.

    An empty structured config renders to an empty string (a leaf with no
    migrated VLAN left after a rollback).
    """
    try:
        import pyavd
    except ImportError as err:  # pragma: no cover - pyavd is in requirements.txt
        raise AnsibleFilterError("pyavd is required to render AVD configs: %s" % err)
    result = pyavd.validate_structured_config(dict(structured_config or {}))
    violations = list(getattr(result.validation_result, "violations", []) or [])
    if violations:
        detail = "; ".join(
            "%s: %s" % (".".join(str(p) for p in (getattr(v, "path", None) or [])) or "<root>", getattr(v, "message", v))
            for v in violations
        )
        raise AnsibleFilterError("AVD structured config for %s is invalid: %s" % (hostname or "device", detail))
    return pyavd.get_device_config(result.validated_data)
