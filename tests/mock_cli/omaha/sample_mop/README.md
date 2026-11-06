# Sample MOP for VLAN 3000 (Omaha mock)

| File | What it is |
|---|---|
| `ACME_APP_mop_input_20261106_152034_710.json` | The MOP input (schema 2.0) discovery wrote for VLAN 3000 from the Omaha mock fixtures (`tests/mock_cli/omaha`) with the VLAN in the VLAN DB: the deploy run of `tests/run_omaha_vlan3000_mock.sh`. Synthetic network. |
| `MOP_VLAN3000_ACME_APP.md` | A MOP written from that file only, following the contract and template in `docs/MOP_INPUT.md`: CLI copied verbatim from `steps[].cli`, every statement cites an ID, gaps marked NOT IN DISCOVERY. |

`tests/test_sample_mop.py` checks that the MOP keeps to the contract:
- the input still validates against `schemas/mop_input.schema.json`;
- the SHA-256 in the MOP matches the input file;
- every ID the MOP cites exists in the input;
- every step, high risk and decision is covered;
- every CLI block is copied verbatim;
- manual steps and the validation gate are marked.

Run the same checks on any MOP an LLM writes from a real MOP input.

To regenerate the input (the timestamp in the file name changes; update the MOP and the test to match):

```bash
MOCK_OUT=/tmp/omaha tests/run_omaha_vlan3000_mock.sh
ls /tmp/omaha/deploy/reports/omaha/ACME_APP/ACME_APP_mop_input_*.json
```
