# -*- coding: utf-8 -*-
"""Mock cv_device_v3: one task per device that gets configlets."""

from ansible_collections.arista.cvp.plugins.action._mock_cvp import MockCvpAction


class ActionModule(MockCvpAction):
    MODULE = "cv_device_v3"

    def validate(self, args):
        devices = args.get("devices")
        if not isinstance(devices, list) or not devices:
            return "devices must be a non-empty list"
        for device in devices:
            # The real schema: (fqdn or serialNumber) and parentContainerName.
            if not (device.get("fqdn") or device.get("serialNumber")) or not device.get("parentContainerName"):
                return "Error, your input is not valid against current schema: %s" % device
        return ""

    def answer(self, args, task_vars):
        if self.var(task_vars, "mock_cvp_no_tasks", False) in (True, "true", "True"):
            return {"taskIds": [], "tasks": []}
        ids = [str(101 + index) for index, _ in enumerate(args["devices"])]
        return {"taskIds": ids, "tasks": [{"workOrderId": task_id} for task_id in ids]}
