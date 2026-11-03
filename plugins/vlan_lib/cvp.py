# -*- coding: utf-8 -*-
"""CloudVision helpers for the configlet push and its change control.

The ``arista.cvp`` v3 modules run on the CloudVision inventory host (httpapi
connection; ``ansible_user: cvaas`` with a token for CVaaS, ``svc_account``
for on-prem). These functions shape their inputs and read their results:

- the container each target device already sits in (cv_device_v3 moves a
  device when ``parentContainerName`` differs, so it must be the current one);
- the task IDs that applying configlets creates;
- the change control that runs those tasks;
- the state of that change control while the playbook waits for it.
"""

import re

from ansible.errors import AnsibleFilterError

# Status values CloudVision reports for a change control (resource API).
_CC_STATUS = {
    "CHANGE_CONTROL_STATUS_COMPLETED": "completed",
    "CHANGE_CONTROL_STATUS_RUNNING": "running",
    "CHANGE_CONTROL_STATUS_SCHEDULED": "scheduled",
    "CHANGE_CONTROL_STATUS_NOT_STARTED": "not_started",
    "CHANGE_CONTROL_STATUS_UNSPECIFIED": "unknown",
}
# Placeholders cv_device_v3 returns instead of task IDs in check mode.
_NOT_TASKS = {"check_mode", "unsupported_in_check_mode"}


def _short(name):
    return str(name or "").split(".")[0].lower()


def _find_list(data, key):
    """The first list stored under *key* anywhere in *data*."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get(key), list):
                return item[key]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return []


def cvp_device_containers(facts_result, hostnames):
    """Map each target switch to its CloudVision device record.

    *facts_result* is a registered ``cv_facts_v3`` result (``facts:
    [devices]``). A switch matches on hostname, or on the short name of its
    FQDN. Returns ``{found: {host: {container, serial, fqdn}}, missing: [...]}``.
    """
    by_name = {}
    for device in _find_list(facts_result, "cvp_devices"):
        if not isinstance(device, dict):
            continue
        record = {
            "container": device.get("parentContainerName") or "",
            "serial": device.get("serialNumber") or "",
            "fqdn": device.get("fqdn") or "",
            "hostname": device.get("hostname") or "",
        }
        for name in (device.get("hostname"), device.get("fqdn")):
            if name:
                by_name.setdefault(_short(name), record)
    found = {}
    missing = []
    for host in hostnames or []:
        record = by_name.get(_short(host))
        if record and record["container"] and record["container"] != "Undefined":
            found[host] = record
        else:
            missing.append(host)
    return {"found": found, "missing": missing}


def cvp_device_assignments(assignments, containers):
    """cv_device_v3 ``devices`` input: add configlets, keep each device where it is.

    *assignments* maps a switch to the configlet names it gets. *containers*
    is the ``found`` map from cvp_device_containers. Used with ``apply_mode:
    loose`` (configlets are added, none removed) and ``search_key: hostname``.
    """
    devices = []
    for host in sorted(assignments or {}):
        record = (containers or {}).get(host)
        if not record:
            raise AnsibleFilterError("No CloudVision container known for %s" % host)
        devices.append(
            {
                "fqdn": record.get("fqdn") or host,
                "hostname": record.get("hostname") or host,
                "parentContainerName": record["container"],
                "configlets": list(assignments[host]),
            }
        )
    return devices


def cvp_task_ids(device_results):
    """Task IDs cv_device_v3 created (one registered result, or a list of them)."""
    results = list(device_results) if isinstance(device_results, list) else [device_results]
    ids = []
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            results.extend(result["results"])
            continue
        for task_id in _find_list(result, "taskIds"):
            text = str(task_id)
            if text and text not in _NOT_TASKS and text not in ids:
                ids.append(text)
    return ids


def _one_line(text, limit=480):
    """Change-control text: CloudVision's schema takes a single line."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit]


def cvp_change_control(name, notes, task_ids, stage="Configlet push", mode="parallel"):
    """cv_change_control_v3 ``change`` input that runs *task_ids* in one stage."""
    tasks = [str(t) for t in task_ids or [] if str(t)]
    if not tasks:
        raise AnsibleFilterError("A change control needs at least one task ID")
    return {
        "name": _one_line(name, 120),
        "notes": _one_line(notes),
        "activities": [{"task_id": task_id, "stage": stage} for task_id in tasks],
        "stages": [{"name": stage, "mode": mode}],
    }


def _find_value(data, key):
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if key in item and not isinstance(item[key], (dict, list)):
                return item[key]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def cvp_change_control_state(show_result):
    """``{status, approved, error, done}`` from a ``cv_change_control_v3`` show.

    *status* is completed, failed, running, scheduled, not_started (created,
    not yet run), not_found or unknown. *done* is true for completed and
    failed: the wait can stop.
    """
    controls = _find_list(show_result, "change_controls")
    control = next((c for c in controls if c), None)
    if not control:
        return {"status": "not_found", "approved": False, "error": "", "done": False}
    raw = _find_value(control, "status") or ""
    error = _find_value(control, "error") or ""
    approve = control.get("value", control).get("approve") if isinstance(control, dict) else None
    approved = bool(approve.get("value")) if isinstance(approve, dict) else False
    status = _CC_STATUS.get(str(raw), "unknown")
    if error and str(error).strip():
        status = "failed"
    return {
        "status": status,
        "approved": approved,
        "error": str(error or "").strip(),
        "done": status in ("completed", "failed"),
    }
