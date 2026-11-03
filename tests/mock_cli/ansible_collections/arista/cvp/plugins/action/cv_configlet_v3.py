# -*- coding: utf-8 -*-
"""Mock cv_configlet_v3: accepts {name: text} configlets."""

from ansible_collections.arista.cvp.plugins.action._mock_cvp import MockCvpAction


class ActionModule(MockCvpAction):
    MODULE = "cv_configlet_v3"

    def validate(self, args):
        configlets = args.get("configlets")
        if not isinstance(configlets, dict) or not configlets:
            return "configlets must be a non-empty dict of name: content"
        empty = [name for name, text in configlets.items() if not str(text or "").strip()]
        if empty:
            return "configlets with no content: %s" % ", ".join(sorted(empty))
        return ""

    def answer(self, args, task_vars):
        return {"taskIds": [], "tasks": [], "configlets_updated": sorted(args["configlets"])}
