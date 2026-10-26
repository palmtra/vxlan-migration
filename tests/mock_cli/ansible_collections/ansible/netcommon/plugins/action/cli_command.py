# -*- coding: utf-8 -*-
"""Mock ``ansible.netcommon.cli_command`` for discovery end-to-end tests.

Output comes from ``<mock_cli_root>/devices/<inventory_hostname>/<slug>.txt``
where ``slug`` is the command lowercased with every run of non-alphanumeric
characters replaced by ``_``. A ``<slug>.error`` file makes the command fail
with its content as the message. A missing file returns empty output, like a
command that prints nothing. ``mock_unreachable: true`` on a host makes every
command unreachable.
"""

import os
import re

from ansible.plugins.action import ActionBase


def command_slug(command):
    return re.sub(r"[^a-z0-9]+", "_", str(command).lower()).strip("_")


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        task_vars = task_vars or {}
        command = self._task.args.get("command", "")
        result["changed"] = False
        if self._templar.template(task_vars.get("mock_unreachable", False)):
            result["unreachable"] = True
            result["msg"] = "mock: host is unreachable"
            return result
        # task_vars arrive untemplated; mock_cli_root is "{{ inventory_dir }}/..".
        mock_root = self._templar.template(task_vars["mock_cli_root"])
        base = os.path.join(
            mock_root, "devices", task_vars["inventory_hostname"], command_slug(command)
        )
        result["invocation"] = {"module_args": {"command": command}}
        if os.path.exists(base + ".error"):
            with open(base + ".error", encoding="utf-8") as handle:
                result["failed"] = True
                result["msg"] = handle.read().strip()
            return result
        text = ""
        if os.path.exists(base + ".txt"):
            with open(base + ".txt", encoding="utf-8") as handle:
                text = handle.read()
        result["stdout"] = text
        result["stdout_lines"] = text.splitlines()
        return result
