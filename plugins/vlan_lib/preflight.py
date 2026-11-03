# -*- coding: utf-8 -*-
"""Preflight: can a migration run reach everything it needs?

Each check is ``{check, target, status, detail}``; status is ``ok``,
``warn`` or ``fail``. ``preflight_report`` rolls them up.
"""

import re

_EOS_MODEL = re.compile(r"^\s*Arista\s+(\S+)", re.M)
_EOS_VERSION = re.compile(r"^\s*Software image version:\s*(\S+)", re.M)
_NXOS_VERSION = re.compile(r"^\s*(?:NXOS|system):\s+version\s+(\S+)", re.M)
# "cisco Nexus9000 C93180YC-EX chassis", "cisco Nexus 3048 Chassis"; not the
# "Cisco Nexus Operating System (NX-OS) Software" banner.
_NXOS_MODEL = re.compile(r"^\s*[Cc]isco\s+(Nexus\s?\d\w*(?:\s+(?![Cc]hassis\b)[A-Z0-9][\w-]*)?)", re.M)


def switch_identity(output, expected_os=""):
    """Platform, model and version from ``show version`` text.

    ``matches`` is False when the switch is not the OS the inventory says
    (``network_os_family``: eos / nxos), which would send the wrong
    commands to it.
    """
    text = output if isinstance(output, str) else ""
    if _EOS_MODEL.search(text) or _EOS_VERSION.search(text):
        found = "eos"
        model = (_EOS_MODEL.search(text) or [None, ""])[1]
        version = (_EOS_VERSION.search(text) or [None, ""])[1]
    elif "NX-OS" in text or _NXOS_VERSION.search(text):
        found = "nxos"
        model = (_NXOS_MODEL.search(text) or [None, ""])[1]
        version = (_NXOS_VERSION.search(text) or [None, ""])[1]
    else:
        found, model, version = "", "", ""
    expected = str(expected_os or "").lower()
    return {
        "os_family": found,
        "model": model,
        "version": version,
        "matches": bool(found) and (not expected or expected == found),
    }


def preflight_report(checks):
    """``{status, failed, warnings, checks, lines}`` for a list of checks."""
    rows = [c for c in checks or [] if isinstance(c, dict) and c.get("check")]
    failed = [c for c in rows if c.get("status") == "fail"]
    warnings = [c for c in rows if c.get("status") == "warn"]
    order = {"fail": 0, "warn": 1, "ok": 2}
    rows = sorted(rows, key=lambda c: (order.get(c.get("status"), 3), c["check"], str(c.get("target") or "")))
    return {
        "status": "fail" if failed else ("warn" if warnings else "ok"),
        "failed": len(failed),
        "warnings": len(warnings),
        "checks": rows,
        "lines": [
            "%-4s %-12s %-28s %s"
            % (str(c.get("status", "")).upper(), c["check"], c.get("target") or "-", c.get("detail") or "")
            for c in rows
        ],
    }
