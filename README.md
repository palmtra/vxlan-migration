# vxlan-migration: test branch

Test code and synthetic data for [vxlan-migration](../../tree/main). Nothing here is
production data, and none of it is ever merged into `main`. This branch shares no
history with `main` (it is an orphan branch), so a merge cannot carry it across by
accident.

| Path | What |
|---|---|
| `tests/` | Unit tests (`python -m unittest discover -s tests`) and the end-to-end mock run (`tests/run_discovery_mock.sh`) |
| `tests/fixtures/cli/` | Canned EOS / NXOS command output for parser tests |
| `tests/mock_netbox/` | Mock NetBox API (`server.py`), its seed objects and the VLAN records for `tests/run_netbox_rt_mock.sh` (route-target export) |
| `tests/mock_cli/` | Mock fabric: inventory, per-device command output, a shadow `ansible.netcommon` that answers `cli_command` from it, and a VLAN record for the record-mode run |
| `tests/fixtures/examples/` | The old `_example.yml` VLAN and service templates (reference only) |
| `molecule/` | Localhost scenarios for `vlan_db` and `service_db`, with their own fixture VLAN DB and service bundle |

## How it is used

- **CI on `main`** checks out the code, then copies `tests/` and `molecule/` from
  `origin/test` with `git archive` (untracked) and runs the suite. `main` gitignores
  both directories and fails any PR that tracks them.
- **Locally**, from a `main` checkout: `make tests-fetch`, then `make test`, `make mock`,
  `make molecule`.
- **Changes here** run `.github/workflows/tests.yml`: this branch's tests against
  `main`'s code. Use *Run workflow* with `code_ref` to test against another branch,
  for example a feature branch whose tests change with it.

When a code change needs new or different tests, open the test change as a PR to this
branch. Land it together with the code PR to `main`: CI on `main` always uses the tip
of this branch.
