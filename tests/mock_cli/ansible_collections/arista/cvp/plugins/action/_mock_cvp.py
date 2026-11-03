# -*- coding: utf-8 -*-
"""Shared logic for the mock arista.cvp action plugins (tests only)."""

import json
import os
import re

from ansible.plugins.action import ActionBase

# Real arista.cvp 3.12 argument specs: name -> allowed values (None = any).
SPECS = {
    "cv_facts_v3": {"facts": None, "regexp_filter": None, "verbose": None},
    "cv_configlet_v3": {"configlets": None, "state": ("present", "absent"), "configlets_notes": None},
    "cv_device_v3": {
        "devices": None,
        "state": ("present", "factory_reset", "provisioning_reset", "absent"),
        "apply_mode": ("loose", "strict"),
        "inventory_mode": ("loose", "strict"),
        "search_key": ("fqdn", "hostname", "serialNumber"),
    },
    "cv_change_control_v3": {
        "name": None,
        "change": None,
        "state": ("show", "set", "remove", "approve", "unapprove", "execute",
                  "schedule", "approve_and_execute", "schedule_and_approve"),
        "change_id": None,
        "schedule_time": None,
    },
}
_ONE_LINE = re.compile(r"^.*$")


class MockCvpAction(ActionBase):
    MODULE = ""

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        task_vars = task_vars or {}
        args = dict(self._task.args)
        spec = SPECS[self.MODULE]
        unknown = sorted(set(args) - set(spec))
        if unknown:
            return self._fail(result, "Unsupported parameters for (arista.cvp.%s) module: %s"
                              % (self.MODULE, ", ".join(unknown)))
        for key, allowed in spec.items():
            if allowed and key in args and args[key] not in allowed:
                return self._fail(result, "value of %s must be one of: %s, got: %s"
                                  % (key, ", ".join(allowed), args[key]))
        self._log({"module": self.MODULE, "args": args, "host": task_vars.get("inventory_hostname")})
        problem = self.validate(args)
        if problem:
            return self._fail(result, problem)
        result.update({"changed": self.MODULE != "cv_facts_v3", "failed": False})
        result["data"] = self.answer(args, task_vars)
        return result

    @staticmethod
    def _fail(result, msg):
        result.update({"failed": True, "msg": msg})
        return result

    @staticmethod
    def _log(entry):
        path = os.environ.get("MOCK_CVP_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def var(self, task_vars, name, default):
        value = task_vars.get(name, default)
        return self._templar.template(value) if isinstance(value, str) else value

    def validate(self, args):  # pragma: no cover - overridden
        return ""

    def answer(self, args, task_vars):  # pragma: no cover - overridden
        return {}
