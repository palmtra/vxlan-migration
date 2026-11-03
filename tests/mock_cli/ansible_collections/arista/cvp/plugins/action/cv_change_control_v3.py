# -*- coding: utf-8 -*-
"""Mock cv_change_control_v3: create, approve/execute, show."""

from ansible_collections.arista.cvp.plugins.action._mock_cvp import _ONE_LINE, MockCvpAction


class ActionModule(MockCvpAction):
    MODULE = "cv_change_control_v3"

    def validate(self, args):
        state = args.get("state", "show")
        if state == "set":
            change = args.get("change") or {}
            # The real module's schema: stages required; name and notes one line;
            # each activity has an action or a task_id.
            if not change.get("stages"):
                return "Error, your input is not valid against current schema: 'stages' is a required property"
            for key in ("name", "notes"):
                if key in change and not _ONE_LINE.search(str(change[key])):
                    return "Error, your input is not valid against current schema: %s must be one line" % key
            for activity in change.get("activities") or []:
                if "action" not in activity and "task_id" not in activity:
                    return "Error, your input is not valid against current schema: activity %s" % activity
        elif state != "show" and not (args.get("change_id") or args.get("name")):
            return "Unable to approve/execute change control. Change name or change_id(s) must be specified"
        return ""

    def answer(self, args, task_vars):
        state = args.get("state", "show")
        if state == "set":
            return {"id": "mock-cc-vlan"}
        if state == "show":
            cc_id = (args.get("change_id") or ["mock-cc-vlan"])[0]
            return {"change_controls": [{"value": {
                "key": {"id": cc_id},
                "change": {
                    "name": "mock",
                    "status": self.var(task_vars, "mock_cvp_cc_status", "CHANGE_CONTROL_STATUS_COMPLETED"),
                    "error": self.var(task_vars, "mock_cvp_cc_error", ""),
                },
                "approve": {"value": True},
            }}]}
        return {"id": (args.get("change_id") or [""])[0], "state": state}
