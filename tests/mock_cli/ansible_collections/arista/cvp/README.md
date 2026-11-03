# Mock arista.cvp (tests only)

Answers `cv_facts_v3`, `cv_configlet_v3`, `cv_device_v3` and `cv_change_control_v3`
without CloudVision. Arguments are checked against the real arista.cvp 3.x
argument specs (and the change-control schema rules the real module enforces),
so a call the real module would reject fails here too.

Every call is appended as one JSON line to `$MOCK_CVP_LOG` when it is set.

Task variables that shape the answers:

| Variable | Default | Effect |
|---|---|---|
| `mock_cvp_devices` | every inventory host in container `Leaves` | `cv_facts_v3` device list |
| `mock_cvp_cc_status` | `CHANGE_CONTROL_STATUS_COMPLETED` | status `cv_change_control_v3 state=show` returns |
| `mock_cvp_cc_error` | `""` | error text in that change control (non-empty means failed) |
| `mock_cvp_no_tasks` | `false` | `cv_device_v3` creates no task (configlets already in place) |
