# -*- coding: utf-8 -*-
"""Mock ``ansible.netcommon.cli_command`` for discovery end-to-end tests.

Output comes from ``<mock_cli_root>/devices/<inventory_hostname>/<slug>.txt``
where ``slug`` is the command lowercased with every run of non-alphanumeric
characters replaced by ``_``. A ``<slug>.error`` file makes the command fail
with its content as the message. A missing file returns empty output, like a
command that prints nothing. ``mock_unreachable: true`` on a host makes every
command unreachable.

After a change: when ``$MOCK_CLI_STATE_FILE`` holds ``post`` (the mock
CloudVision writes it when a change control completes), a file under
``devices/<host>/post/`` answers first, so verification sees the switches as
they are after the change.
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
        host_dir = os.path.join(mock_root, "devices", task_vars["inventory_hostname"])
        base = os.path.join(host_dir, command_slug(command))
        state_file = os.environ.get("MOCK_CLI_STATE_FILE")
        if state_file and os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as handle:
                phase = handle.read().strip()
            post = os.path.join(host_dir, phase, command_slug(command))
            if phase and (os.path.exists(post + ".txt") or os.path.exists(post + ".error")):
                base = post
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
