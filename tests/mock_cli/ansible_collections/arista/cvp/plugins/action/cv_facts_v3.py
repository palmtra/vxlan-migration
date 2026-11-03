# -*- coding: utf-8 -*-
"""Mock cv_facts_v3: the devices CloudVision manages."""

from ansible_collections.arista.cvp.plugins.action._mock_cvp import MockCvpAction


class ActionModule(MockCvpAction):
    MODULE = "cv_facts_v3"

    def answer(self, args, task_vars):
        devices = task_vars.get("mock_cvp_devices")
        if devices is None:
            hosts = (task_vars.get("groups") or {}).get("all") or []
            devices = [
                {"hostname": host, "fqdn": host, "parentContainerName": "Leaves",
                 "serialNumber": "MOCK%04d" % index}
                for index, host in enumerate(sorted(hosts))
            ]
        return {"cvp_devices": self._templar.template(devices)}
