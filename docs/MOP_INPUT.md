# MOP input

Discovery writes one **MOP input** per VLAN and one **run index** per run. They are the input for whoever writes the MOP (Method of Procedure), whether an engineer or an LLM. The Markdown report stays the engineer's read; these files are built to be cited, checked and diffed.

| File | What it is |
|---|---|
| `reports/<dc>/<vlan>/<vlan>_mop_input_<ts>.json` | Facts, typed findings, ordered change steps with CLI and rollback, acceptance checks. Schema: `schemas/mop_input.schema.json`. About 20-40 KB per VLAN, no raw CLI output. |
| `reports/<dc>/_runs/run_<ts>.json` and `.md` | Every VLAN of the run: status, counts, suggested order, switches more than one VLAN changes, and a SHA-256 manifest of every file the run wrote. |

Turn them off with `-e discovery_write_mop_input=false` / `-e discovery_write_run_index=false`.

Nothing in these files is applied to a device. The steps carry CLI that already exists in the prune plans and configlets, plus two generated command pairs: `shutdown` / `no shutdown` on the legacy gateway SVI (a manual step in the window), and `no network` / `network` (or `aggregate-address`) for the legacy BGP origination of the VLAN prefix.

## What is in a MOP input

| Section | Contents |
|---|---|
| `run` | `run_id`, `started_at` / `finished_at` (UTC, ISO 8601), git commit and whether the tree had uncommitted changes, operator, controller, inventory, Ansible version, the discovery options in effect, and which switches answered. |
| `rules` | Standing rules for the MOP author: the VLAN record is authoritative for the VNI, fabric interconnects are the host boundary, the VRF is reused and never created, one VLAN per trunk-removal line, the legacy SVI is shut by hand and removed only after validation. |
| `vlan` | ID, name, data center, migration type (`l2` / `l3`), VRF, L2 and L3 VNI, and **status**: `blocked` (any blocker), `needs_decisions` (any decision), or `ready`. `l2_vni_source` says where the L2 VNI comes from: `record` (the VLAN record in `vars/vlans/<dc>/`, authoritative), `starter` (kept from the starter record an earlier discovery wrote under `reports/`; copy it into the VLAN DB), or `proposed` (the next free VNI; not to be used until it is in the record). `l3_vni_source` says whether the L3 VNI comes from the SSOT record or from the VRF-to-VNI map on the gateway leaves; leaves that disagree are a blocker. |
| `summary` | Finding counts by kind, high risks, host count, step count. |
| `coverage` | Switches scanned, not discovered, failed commands, MAC/ARP rows the parser could not read, how endpoint tables were read (per VLAN or bulk, samples, ARP refresh). `complete` is false when any coverage blocker exists. |
| `switches` | `SW-<hostname>`: OS, type (`vtep`, `non_vtep`, `legacy`), placement role, SVI, local endpoint MACs, and the reasons for the role. |
| `gateway` | Legacy SVIs with their HSRP/VRRP groups and **virtual MAC**, the fabric gateway mode and virtual IP, services that are not migrated (DHCP relay, ACLs, ...), and `mac_change`: the MAC hosts use today, the fabric's `ip virtual-router mac-address`, and whether it changes at cutover. |
| `routing` | Routing objects tied to the VLAN (statics, BGP peers, redistribution, advertisement). |
| `endpoints` | `EP-<vlan>-NNN`: IP, IPv6, MAC, location (`edge`, `uplink_only`, `not_in_mac_table`), switch and port, flags, samples seen, and `baseline: true` for host IPs the post-change check pings. |
| `findings` | Typed findings (see below). |
| `steps` | `S-<vlan>-NN` in change order (see below). |
| `acceptance` | `A-<vlan>-NN`: checks that say the change worked. |
| `files` | The other report files of this VLAN in this run. |

### Findings

| Kind | ID | Meaning for the MOP |
|---|---|---|
| `blocker` | `B-NN` | Fix before the change window. The pre-check step is `blocked` until a re-run of discovery shows none. |
| `decision` | `D-NN` | An engineer or app owner must choose or confirm (VNI, VLAN name, idle ports, review switches). Record the answer in the MOP. |
| `risk` | `R-NN` | Proceed with the mitigation in the text. `severity: high` belongs in the MOP's risk section with its mitigation. |
| `task` | `T-NN` | Work the generated config does not do (routing objects to recreate in the fabric). Becomes a MOP step written by the engineer. |
| `sequencing` | `Q-NN` | Waits on an earlier step (teardown waits for the gateway move). Already reflected in the step order. |
| `info` | `I-NN` | Context only. |

A VLAN that is not in the VLAN DB (a discovery probe) is always a `scope` blocker, listed first: the VLAN record is the authority for the VNI and the change, and nothing in the report replaces it.

Each finding has a `category` (coverage, scope, gateway, fabric, routing, l2, prune, placement, naming, vni, endpoints) and a `severity` (high, medium, low). IDs are numbered per kind in a fixed order, so the same network state gives the same IDs. L2 VNI findings also carry `topic` (`proposed`, `missing`, `existing_mapping`) and `vni`: `migrate_vlan.yml` uses them to tell which VNI findings a VNI from NetBox settles.

### Steps

| Phase | What |
|---|---|
| `0-prechecks` | Resolve blockers (lists their IDs), record decisions, keep the endpoint baseline, verify the VRF exists on the gateway leaves and reuse it (L3 in a non-default VRF; the configlet never creates a VRF, L3 VNI or VRF route target), recreate routing objects (`task` findings). |
| `1-gateway-shutdown` | L3 with a legacy gateway: `shutdown` on each legacy gateway SVI, rollback `no shutdown`. `manual: true`: the engineer runs it by hand in the window, outside the automation, right before the fabric change control. |
| `2-deploy` | One step per fabric leaf: the configlet `vxlan_vlan<id>_<leaf>.cfg` written by `playbooks/core/workflow_deploy.yml`, in one CloudVision change control executed in the window after the shutdown. The fabric SVI is not pre-staged: it comes up with the change control. Rollback: remove the configlet (`roles/cvp_rollback` renders the reversing one). |
| `3-link-prune-mls` | MLS side of links to non-VTEP switches. Right after the deploy: it cuts the EVPN-legacy L2 loop (EVPN does not carry BPDUs). Re-run discovery after it. |
| `4-link-prune-non-vtep` | Non-VTEP side of those links. |
| `5-validate` | `gate: true`. `playbooks/core/workflow_verify.yml`, ping every baseline endpoint, ARP on a fabric gateway leaf, the endpoint comparison. Nothing after it runs until it passes. Carries the gateway MAC change note. |
| `6-routing-withdrawal` | Per legacy switch that originates the VLAN prefix with a BGP `network` or `aggregate-address`: first check `show ip bgp vrf <vrf> <prefix>` on the border leaves for a path the fabric originates, then `no network` (rollback `network`). No CLI when the switch's BGP ASN was not discovered. `redistribute` is VRF-wide and is not withdrawn here. |
| `7-teardown` | Trunks and VLAN removal on prune-eligible switches that are not source gateways. NX-OS: checkpoint, `save` once checks pass, rollback to the checkpoint. EOS: `configure session` ending in `commit timer`; `confirm` (`configure session <name> commit`, before the timer expires) makes it permanent once checks pass, and the rollback is to let the timer expire or abort the session. |
| `8-source-gateway-teardown` | Each source gateway in two steps: the SVI on its own (`no interface Vlan<id>`), then trunks and the VLAN. Each has its own checkpoint or session (`prune_v<id>_svi`, `prune_v<id>_l2`). Status `blocked_until_gateway_validated` until the new gateway is validated (see below). |
| `9-verify` | `playbooks/core/workflow_verify.yml` against the baseline, then a discovery re-run. |

An L2 VLAN, or one whose gateway is not on the legacy switches, has no `1-gateway-shutdown` and no `6-routing-withdrawal` step.

### Source gateway prune waits for validation

A prune-eligible switch that owns the VLAN's SVI (a source gateway) is pruned only after the new gateway is validated. Discovery and `playbooks/core/prune_vlan.yml` keep its CLI under `blocked_actions`:

- `blocked_until_gateway_moved` while the SVI is up;
- `blocked_until_gateway_validated` once it is shut, until either the newest `reports/<dc>/_runs/migrate_vlan<id>_<ts>.json` has outcome `deployed_and_verified` and an endpoint comparison with no problems, or the engineer runs with `-e new_gateway_validated=true` (or `=3039,3040` for some VLANs) after doing the validation step by hand.

### One VLAN per trunk line

Discovery of several VLANs at once (`target_vlan_ids=3039,3040`) never merges them into one `switchport trunk allowed vlan remove 3039,3040` line. Each VLAN's report, prune plan and MOP input carry that VLAN's lines only.

Every step has `switch`, `status` (`ready`, `blocked`, `needs_decision`, or the prune plan status such as `blocked_until_gateway_moved`), `manual` (run by hand) or `gate` (validation) where they apply, `cli` (exact text, or `null` when the CLI is in the file named by `cli_source`), `rollback`, `verify`, `depends_on` (step IDs), and `notes`.

## Writing the MOP with an LLM

Give the model the MOP input of each VLAN in the change (and the run index when there are several), the template below, and this contract:

```text
You are writing a Method of Procedure for a network change from the attached
MOP input JSON (schema vxlan-migration/mop-input 2.x).

Rules:
1. Use only facts in the JSON. If something the template asks for is not
   there, write "NOT IN DISCOVERY" and list it under Open items.
2. Use CLI only from steps[].cli, verbatim. Where cli is null, name the file
   in steps[].cli_source; do not write that CLI yourself.
3. Keep the step order and depends_on. Do not merge, drop or reorder steps.
4. Cite IDs in brackets for every statement: [S-3000-06], [R-01], [EP-3000-004].
5. If vlan.status is "blocked", put the blockers first and mark the MOP NOT
   READY FOR EXECUTION.
6. Every high-severity risk goes in the Risks section with its mitigation.
7. Every decision becomes an Open item with an owner, unless the engineer's
   answer is given below.
8. Follow every line of rules[]. Mark steps with manual: true as done by
   hand in the window, and steps with gate: true as a hold point.

Engineer's answers to decisions: <paste, or "none">
```

### MOP template

```markdown
# MOP: VLAN <vlan.id> <vlan.name> to VXLAN (<vlan.data_center>)

Status: <vlan.status>. Discovery run <run.run_id> (<run.finished_at>, code <run.git.commit>).

## 1. Scope
VLAN, subnet(s), VRF, VNI(s), migration type, switches that change (from steps[].switch).

## 2. Impact and window
Hosts affected (endpoints.summary.active_ips), gateway move (gateway.*),
gateway MAC change (gateway.mac_change), expected interruption per phase.

## 3. Pre-checks
Blockers [B-..], decisions [D-..] with owners, tasks [T-..], baseline file.

## 4. Risks
High risks [R-..] with mitigation; coverage gaps (coverage.*).

## 5. Procedure
One subsection per step in order: ID, switch, status, CLI (verbatim),
verification, rollback.

## 6. Verification and acceptance
acceptance[] and the 6-verify step.

## 7. Rollback
Per step, reverse order: steps[].rollback.

## 8. Open items
Everything marked NOT IN DISCOVERY, and unanswered decisions.
```

## Checking a MOP input

```bash
python3 -c "import json,jsonschema,sys; jsonschema.validate(json.load(open(sys.argv[1])), json.load(open('schemas/mop_input.schema.json'))); print('ok')" reports/<dc>/<vlan>/<vlan>_mop_input_<ts>.json
```

Compare the files a MOP cites against the run index's SHA-256 manifest to confirm the MOP was built from that run.
