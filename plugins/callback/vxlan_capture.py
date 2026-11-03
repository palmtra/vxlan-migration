# -*- coding: utf-8 -*-
"""Record what a run read from the switches and CloudVision.

Off unless the run sets a capture directory:

    ansible-playbook ... -e vxlan_capture_dir=captures/vlan3000

(or ``VXLAN_CAPTURE_DIR``). Works with any playbook in this repository.

- Every ``cli_command`` output goes to ``devices/<host>/<slug>.txt`` (a
  failed command to ``<slug>.error``), the layout of the mock CLI, so a
  capture replays through tests/mock_cli as is. A command that runs again
  with different output is kept as ``<slug>~2.txt``, ``~3`` and so on.
- Every ``arista.cvp`` module result goes to
  ``cloudvision/<nnn>_<module>.json``.
- ``capture_index.json`` lists the hosts (inventory groups, OS), the files,
  and the commands per host.

Secrets are removed before writing (vlan_lib.capture). Results of tasks
marked ``no_log`` never reach a callback, so nothing hidden is written.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = """
name: vxlan_capture
type: aggregate
short_description: Save switch outputs and CloudVision results for validation
description:
  - Writes cli_command outputs and arista.cvp results under the directory in the
    vxlan_capture_dir extra var (or VXLAN_CAPTURE_DIR), with secrets removed.
  - Does nothing when neither is set.
"""

import datetime
import json
import os
import sys

from ansible.plugins.callback import CallbackBase

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from vlan_lib.capture import command_slug, redact_data, redact_text  # noqa: E402

# Inventory variables worth keeping per host (never credentials).
_HOST_VARS = ("ansible_network_os", "network_os_family")


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "vxlan_capture"
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self):
        super(CallbackModule, self).__init__()
        self._dir = None
        self._hosts = {}
        self._outputs = {}
        self._cvp_count = 0

    # --- setup ---------------------------------------------------------------
    def v2_playbook_on_play_start(self, play):
        if self._dir is not None:
            return
        target = os.environ.get("VXLAN_CAPTURE_DIR") or ""
        manager = play.get_variable_manager() if hasattr(play, "get_variable_manager") else None
        extra = getattr(manager, "extra_vars", None) or {}
        target = str(extra.get("vxlan_capture_dir") or target or "")
        if not target or "{{" in target:
            self._dir = ""
            return
        self._dir = os.path.abspath(target)
        os.makedirs(self._dir, exist_ok=True)
        self._load_index()

    def _load_index(self):
        path = os.path.join(self._dir, "capture_index.json")
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                index = json.load(handle)
        except (OSError, ValueError):
            return
        self._hosts = index.get("hosts") or {}
        self._cvp_count = int(index.get("cloudvision_results") or 0)
        for host, info in self._hosts.items():
            for name in info.get("files") or []:
                self._outputs.setdefault(host, {})[name] = None

    # --- results -------------------------------------------------------------
    def v2_runner_on_ok(self, result):
        self._record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._record(result, failed=True)

    def v2_runner_on_unreachable(self, result):
        self._record(result, failed=True)

    def v2_runner_item_on_ok(self, result):
        self._record(result)

    def v2_runner_item_on_failed(self, result):
        self._record(result, failed=True)

    def _record(self, result, failed=False):
        if not self._dir:
            return
        res = getattr(result, "_result", None) or {}
        if "results" in res or res.get("censored") or res.get("skipped"):
            return
        action = str(getattr(result._task, "action", "") or "")
        try:
            if action.split(".")[-1] == "cli_command":
                self._record_command(result, res, failed)
            elif action.startswith("arista.cvp."):
                self._record_cloudvision(action, res)
        except (OSError, ValueError, TypeError) as exc:  # never break the run
            self._display.warning("vxlan_capture: %s" % exc)

    def _host(self, result):
        host = result._host
        name = host.get_name()
        info = self._hosts.setdefault(name, {"groups": [], "vars": {}, "files": [], "commands": {}})
        info["groups"] = sorted({g.name for g in host.get_groups() if g.name != "all"} | set(info["groups"]))
        for key in _HOST_VARS:
            value = (host.vars or {}).get(key)
            if isinstance(value, str) and "{{" not in value:
                info["vars"][key] = value
        return name, info

    def _record_command(self, result, res, failed):
        command = ((res.get("invocation") or {}).get("module_args") or {}).get("command")
        if not command:
            command = (result._task.args or {}).get("command")
        if not command or "{{" in str(command):
            return
        name, info = self._host(result)
        unreachable = bool(res.get("unreachable"))
        is_error = failed or unreachable or ("stdout" not in res and bool(res.get("msg")))
        text = redact_text(str(res.get("msg") or "") if is_error else str(res.get("stdout") or ""))
        ext = ".error" if is_error else ".txt"
        slug = command_slug(command)
        known = self._outputs.setdefault(name, {})
        file_name = slug + ext
        folder = os.path.join(self._dir, "devices", name)
        if file_name in known:
            if known[file_name] is None:  # from an earlier run into this directory
                known[file_name] = _read(os.path.join(folder, file_name))
            if known[file_name] == text:
                return
            n = 2
            while "%s~%d%s" % (slug, n, ext) in known:
                n += 1
            file_name = "%s~%d%s" % (slug, n, ext)
        known[file_name] = text
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, file_name), "w", encoding="utf-8") as handle:
            handle.write(text)
        info["files"].append(file_name)
        info["commands"][file_name] = str(command)

    def _record_cloudvision(self, action, res):
        self._cvp_count += 1
        folder = os.path.join(self._dir, "cloudvision")
        os.makedirs(folder, exist_ok=True)
        data = redact_data({key: value for key, value in res.items() if not key.startswith("_ansible")})
        path = os.path.join(folder, "%03d_%s.json" % (self._cvp_count, action.split(".")[-1]))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, default=str)

    # --- index ---------------------------------------------------------------
    def v2_playbook_on_stats(self, stats):
        if not self._dir:
            return
        index = {
            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hosts": self._hosts,
            "cloudvision_results": self._cvp_count,
        }
        with open(os.path.join(self._dir, "capture_index.json"), "w", encoding="utf-8") as handle:
            json.dump(index, handle, indent=2, sort_keys=True)
