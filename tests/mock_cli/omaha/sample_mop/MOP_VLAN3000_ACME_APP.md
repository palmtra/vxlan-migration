# MOP: VLAN 3000 ACME_APP to VXLAN (omaha)

> **Sample.** Written from `ACME_APP_mop_input_20261106_152034_710.json` in this folder, which discovery produced from the Omaha **mock** fixtures (`tests/mock_cli/omaha`) with VLAN 3000 in the VLAN DB, following the contract and template in `docs/MOP_INPUT.md` (schema 2.x). The switches, addresses and hosts are synthetic. Do not execute.

| | |
|---|---|
| Status | **Not approved for execution.** `vlan.status` is `ready`: no blockers and no open decisions. Approval is NOT IN DISCOVERY. |
| Discovery run | `omaha_20261106_152034_710`, started 2026-11-06T15:20:32Z, finished 2026-11-06T15:20:51Z |
| Code | `960f1288dce621a0b6d20468815b27ca42904cbe`, no uncommitted changes |
| MOP input | `ACME_APP_mop_input_20261106_152034_710.json`, SHA-256 `7b9d18084eb9463eead133dcb51abdfc23959878fc290227e9f0acfe6bf89d1b` |
| Prepared by / approved by | NOT IN DISCOVERY |

## 1. Scope

| Item | Value |
|---|---|
| VLAN | 3000 ACME_APP, data center omaha |
| Migration type | L3: the gateway moves from the legacy MLS pair into the fabric as a `distributed_anycast` gateway |
| Subnet | 10.230.0.0/24, gateway 10.230.0.1 |
| VRF | v0000822a (existing; reused, never created [S-3000-04]) |
| L2 VNI | 11002, from the VLAN record (`l2_vni_source: record`) |
| L3 VNI | 822 (SSOT record, `evpn.l3.vni`) |
| Fabric SVI MTU | 9214 [I-01] |

Switches that change ([SW-…] from `switches`, steps from `steps[].switch`):

| Switch | Type | Role | Change |
|---|---|---|---|
| fnts-fntc03-aggpe-mls01 [SW-fnts-fntc03-aggpe-mls01] | legacy (NX-OS) | prune_eligible, source gateway | Shut the SVI by hand [S-3000-05]; later remove the SVI [S-3000-12], then trunks and VLAN [S-3000-13] |
| fnts-fntc03-aggpe-mls02 [SW-fnts-fntc03-aggpe-mls02] | legacy (NX-OS) | prune_eligible, source gateway | Shut the SVI by hand [S-3000-06]; later remove the SVI [S-3000-14], then trunks and VLAN [S-3000-15] |
| fntc-aggacc-sw01 [SW-fntc-aggacc-sw01] | VTEP (EOS) | participating | Deploy VLAN 3000 / VNI 11002 [S-3000-07] |
| fntc-aggacc-sw02 [SW-fntc-aggacc-sw02] | VTEP (EOS) | participating | Deploy VLAN 3000 / VNI 11002 [S-3000-08] |
| fntc-aggacc-sw05 [SW-fntc-aggacc-sw05] | VTEP (EOS) | prune_eligible | Teardown [S-3000-10] |
| fntc-aggacc-sw06 [SW-fntc-aggacc-sw06] | VTEP (EOS) | prune_eligible | Teardown [S-3000-11] |

Not changed: fntc-aggacc-sw03 [SW-fntc-aggacc-sw03] keeps VLAN 3000 as a non-VTEP switch behind sw01/sw02 (`l2_attached`). oma01-blf01 [SW-oma01-blf01] and oma01-blf02 [SW-oma01-blf02] do not carry the VLAN (`absent`).

Coverage: 9 of 9 switches answered; no failed commands, no unread rows (`coverage.complete: true`). Endpoint tables were read per VLAN, one sample, no ARP refresh.

## 2. Impact and window

- **Hosts:** 4 host IPs in the baseline [EP-3000-003] [EP-3000-004] [EP-3000-005] [EP-3000-006] (section 3.3).
- **Gateway move:** 10.230.0.1 moves from HSRP group 3000 on fnts-fntc03-aggpe-mls01 (priority 110, 10.230.0.2/24) and fnts-fntc03-aggpe-mls02 (priority 100, 10.230.0.3/24) to an anycast SVI on the fabric leaves.
- **Window sequence:** the engineer shuts the legacy SVIs by hand [S-3000-05] [S-3000-06], then the fabric change control runs [S-3000-07] [S-3000-08]; the anycast gateway comes up with it. The fabric SVI is not pre-staged.
- **Interruption:** the gateway is unreachable between the shutdown and the change control. Its expected length is NOT IN DISCOVERY.
- **Gateway MAC change [R-01]:** hosts cache 10.230.0.1 as `00:00:0c:9f:fb:b8` today; the fabric answers with `00:1c:73:00:00:99` (section 4).
- **Change window:** NOT IN DISCOVERY.

## 3. Pre-checks

### 3.1 Blockers [S-3000-01]

None. Before the window, re-run `playbooks/core/discover_vlan.yml` for VLAN 3000 and confirm the report still has no blockers.

### 3.2 Decisions [S-3000-02]

None open.

### 3.3 Endpoint baseline [S-3000-03]

Keep `ACME_APP_endpoints_20261106_152034_710.csv`. It lists 4 host IPs; the validation [S-3000-09] and the final check [S-3000-16] ping these.

| ID | IP | MAC | Switch | Port | Faces | Note |
|---|---|---|---|---|---|---|
| [EP-3000-003] | 10.230.0.10 | 00:50:56:b3:00:10 | fntc-aggacc-sw01 | Et5 | esx-acme-01 | |
| [EP-3000-004] | 10.230.0.11 | 00:50:56:b3:00:11 | fntc-aggacc-sw02 | Et5 | esx-acme-02 | |
| [EP-3000-005] | 10.230.0.20 | 00:50:56:b3:00:20 | fntc-aggacc-sw03 | Et10 | acme-db-01 | |
| [EP-3000-006] | 10.230.0.40 | 00:50:56:b3:00:40 | | | | In ARP, not in any MAC table [R-02] |

Not in the baseline: [EP-3000-001] 10.230.0.2 and [EP-3000-002] 10.230.0.3 are the legacy gateway SVIs.

### 3.4 VRF [S-3000-04]

- Status: ready.
- Verify:
  - `show vrf v0000822a` on fntc-aggacc-sw01, fntc-aggacc-sw02: the VRF exists.
  - `show running-config interfaces Vxlan1 | include vrf v0000822a`: the VRF has its L3 VNI.
  - `show running-config section router bgp | include vrf v0000822a`: the VRF is in BGP with its RD and route targets.
- The VLAN joins the existing VRF. The configlet carries no VRF, L3 VNI or VRF route target: do not create them for this VLAN. If the VRF is missing on a gateway leaf, stop.

### 3.5 Routing objects

No `task` findings and no BGP `network` or `aggregate-address` for 10.230.0.0/24 on the legacy switches, so there is no routing-withdrawal step. Both MLS switches originate 10.230.0.0/24 in VRF v0000822a by `redistribute direct route-map RM_CONN_ACME` and advertise it to 66.180.0.3 and 66.180.0.4 (`routing`). That redistribution is VRF-wide and stays; the route goes when the legacy SVI is shut.

## 4. Risks

| ID | Severity | Risk | Mitigation |
|---|---|---|---|
| [R-01] | high | The gateway MAC changes at cutover: hosts cache 10.230.0.1 as 00:00:0c:9f:fb:b8; the fabric answers with 00:1c:73:00:00:99. Until a host updates its entry it sends to the old MAC. | "A host updates its entry when the fabric gateway ARPs for it (the post-cutover ping of every baseline endpoint does this) or when the entry ages out." Ping every endpoint from the fabric gateway in the validation step [S-3000-09], and check hosts that do not answer. |
| [R-02] | low | 1 active IP is in ARP but not in any MAC table: 10.230.0.40 [EP-3000-006] (aged out, or behind a switch that was not discovered). | Re-run discovery or check it by hand before the window. |

Sequencing (already in the step order): the teardown of fnts-fntc03-aggpe-mls01 [Q-01] and fnts-fntc03-aggpe-mls02 [Q-02] waits until the gateway has moved and the new gateway is validated.

## 5. Procedure

Run the steps in order. A step starts only when every step in its `depends_on` is done and verified.

### Phase 1: legacy gateway shutdown (manual, in the window)

#### [S-3000-05] Shut the legacy gateway SVI on fnts-fntc03-aggpe-mls01

- Status: ready. **Manual**: the engineer runs it by hand; the automation does not. Depends on [S-3000-01] [S-3000-02] [S-3000-03] [S-3000-04].
- CLI:

```
interface Vlan3000
  shutdown
```

- Verify: `show interface Vlan3000`: administratively down.
- Then execute the fabric change control (Phase 2) right away.
- Rollback:

```
interface Vlan3000
  no shutdown
```

#### [S-3000-06] Shut the legacy gateway SVI on fnts-fntc03-aggpe-mls02

- Status: ready. **Manual**. Depends on [S-3000-01] [S-3000-02] [S-3000-03] [S-3000-04].
- CLI:

```
interface Vlan3000
  shutdown
```

- Verify: `show interface Vlan3000`: administratively down.
- Rollback:

```
interface Vlan3000
  no shutdown
```

### Phase 2: fabric deploy (one CloudVision change control)

#### [S-3000-07] Deploy VLAN 3000 / VNI 11002 on fntc-aggacc-sw01

- Status: ready. Depends on [S-3000-05] [S-3000-06].
- CLI: configlet `vxlan_vlan3000_fntc-aggacc-sw01.cfg` written by `playbooks/core/workflow_deploy.yml`.
- Note: Vlan3000 comes up with `ip address virtual 10.230.0.1` as soon as the configlet is applied. Execute the change control only after the legacy SVI is shut.
- Verify:
  - `show vxlan vlan-to-vni | include 3000` (expect VNI 11002)
  - `show interfaces Vlan3000` (up, ip address virtual)
- Rollback: remove configlet `vxlan_vlan3000_fntc-aggacc-sw01` from fntc-aggacc-sw01 in CloudVision (`roles/cvp_rollback` renders the reversing configlet from `leaf_vxlan_rollback_configlet.j2`).

#### [S-3000-08] Deploy VLAN 3000 / VNI 11002 on fntc-aggacc-sw02

- Status: ready. Depends on [S-3000-05] [S-3000-06].
- CLI: configlet `vxlan_vlan3000_fntc-aggacc-sw02.cfg` written by `playbooks/core/workflow_deploy.yml`.
- Note: as in [S-3000-07].
- Verify:
  - `show vxlan vlan-to-vni | include 3000` (expect VNI 11002)
  - `show interfaces Vlan3000` (up, ip address virtual)
- Rollback: remove configlet `vxlan_vlan3000_fntc-aggacc-sw02` from fntc-aggacc-sw02 in CloudVision (`roles/cvp_rollback`).

### Phases 3 and 4: link prunes

None for this VLAN: the MOP input has no `3-link-prune-mls` or `4-link-prune-non-vtep` steps.

### Phase 5: validation (hold point)

#### [S-3000-09] Validate the new gateway and the endpoints

- Status: ready. **Gate**: nothing after this step runs until it passes. Depends on [S-3000-07] [S-3000-08].
- Run `playbooks/core/workflow_verify.yml` and the endpoint comparison in `playbooks/core/migrate_vlan.yml`.
- Verify:
  - `playbooks/core/workflow_verify.yml` for VLAN 3000 passes on every fabric leaf.
  - `show ip arp vrf v0000822a | include Vlan3000` on a fabric gateway leaf: the endpoints are there.
  - Every baseline host answers ping from the fabric (`ACME_APP_endpoints_20261106_152034_710.csv`).
  - Endpoint comparison: no endpoint missing after the change.
- Note [R-01]: the gateway MAC changes from 00:00:0c:9f:fb:b8 to 00:1c:73:00:00:99; hosts update their ARP entry for 10.230.0.1 when the fabric gateway ARPs for them (the ping above) or when it ages out.
- `playbooks/core/prune_vlan.yml` prunes the source gateways only after this passes: a `migrate_vlan.yml` summary with verification passed and no endpoint problems, or `-e new_gateway_validated=true` when the step is done by hand.
- On failure: roll back in reverse order (section 7).

### Phase 6: routing withdrawal

None for this VLAN: no `6-routing-withdrawal` steps (section 3.5).

### Phase 7: teardown of the other switches

#### [S-3000-10] Teardown on fntc-aggacc-sw05 (trunks, VLAN)

- Status: ready. Depends on [S-3000-09].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
configure session prune_v3000
interface Port-Channel1
   switchport trunk allowed vlan remove 3000
no vlan 3000
!
show session-config diffs
commit timer 00:20:00
```

- The change is live but provisional for 00:20:00 after `commit timer`. Run the checks, then keep it with `configure session prune_v3000 commit` before the timer expires, or it rolls back.
- Verify: re-run discovery for VLAN 3000: fntc-aggacc-sw05 shows the change.
- Keep it after verification, before the timer expires: `configure session prune_v3000 commit`
- Rollback: do not commit the session again: it rolls back when the commit timer (00:20:00) expires. To roll back at once: `configure session prune_v3000 abort`

#### [S-3000-11] Teardown on fntc-aggacc-sw06 (trunks, VLAN)

- Status: ready. Depends on [S-3000-09].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
configure session prune_v3000
interface Port-Channel1
   switchport trunk allowed vlan remove 3000
no vlan 3000
!
show session-config diffs
commit timer 00:20:00
```

- The change is live but provisional for 00:20:00 after `commit timer`. Run the checks, then keep it with `configure session prune_v3000 commit` before the timer expires, or it rolls back.
- Verify: re-run discovery for VLAN 3000: fntc-aggacc-sw06 shows the change.
- Keep it after verification, before the timer expires: `configure session prune_v3000 commit`
- Rollback: do not commit the session again: it rolls back when the commit timer (00:20:00) expires. To roll back at once: `configure session prune_v3000 abort`

### Phase 8: source gateways (SVI first, then trunks and VLAN)

These four steps show `blocked_until_gateway_moved` in this discovery run because the legacy SVIs were still up [Q-01] [Q-02]. After the shutdown, discovery shows `blocked_until_gateway_validated` until the validation [S-3000-09] is recorded. Re-run discovery and confirm the plans show ready before running them.

#### [S-3000-12] Remove the legacy gateway SVI on fnts-fntc03-aggpe-mls01

- Status: blocked_until_gateway_moved ("Vlan SVI on fnts-fntc03-aggpe-mls01 is still the active gateway; move the gateway to the fabric first"). Depends on [S-3000-10] [S-3000-11].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
checkpoint prune_v3000_svi
configure terminal
no interface Vlan3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls01 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000_svi`

#### [S-3000-13] Remove trunks and VLAN 3000 on fnts-fntc03-aggpe-mls01

- Status: blocked_until_gateway_moved (as [S-3000-12]). Depends on [S-3000-12].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
checkpoint prune_v3000_l2
configure terminal
interface port-channel101
  switchport trunk allowed vlan remove 3000
interface port-channel105
  switchport trunk allowed vlan remove 3000
no vlan 3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls01 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000_l2`

#### [S-3000-14] Remove the legacy gateway SVI on fnts-fntc03-aggpe-mls02

- Status: blocked_until_gateway_moved ("Vlan SVI on fnts-fntc03-aggpe-mls02 is still the active gateway; move the gateway to the fabric first"). Depends on [S-3000-10] [S-3000-11].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
checkpoint prune_v3000_svi
configure terminal
no interface Vlan3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls02 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000_svi`

#### [S-3000-15] Remove trunks and VLAN 3000 on fnts-fntc03-aggpe-mls02

- Status: blocked_until_gateway_moved (as [S-3000-14]). Depends on [S-3000-14].
- CLI (from `ACME_APP_prune_20261106_152034_710.md`):

```
checkpoint prune_v3000_l2
configure terminal
interface port-channel101
  switchport trunk allowed vlan remove 3000
interface port-channel105
  switchport trunk allowed vlan remove 3000
no vlan 3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls02 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000_l2`

### Phase 9: verify

#### [S-3000-16] Post-change verification

- Depends on [S-3000-13] [S-3000-15].
- Run `playbooks/core/workflow_verify.yml`.
- Verify:
  - Every baseline host answers ping from the fabric (`ACME_APP_endpoints_20261106_152034_710.csv`).
  - Re-run discovery: VLAN 3000 is VNI-mapped on fntc-aggacc-sw01, fntc-aggacc-sw02 and nothing is left to prune.

## 6. Verification and acceptance

| ID | Check | Result |
|---|---|---|
| [A-3000-01] | VLAN 3000 is mapped to VNI 11002 on every fabric leaf in the deploy steps | |
| [A-3000-02] | All 4 baseline host IPs answer ping from the fabric gateway | |
| [A-3000-03] | A re-run of discovery for VLAN 3000 lists no prune-eligible switch and no blockers | |
| [A-3000-04] | Endpoints resolve 10.230.0.1 to the fabric MAC 00:1c:73:00:00:99 | |

## 7. Rollback

Reverse order, per device. Roll back only as far as needed.

| Undo | Switch | Rollback |
|---|---|---|
| [S-3000-15] | fnts-fntc03-aggpe-mls02 | `rollback running-config checkpoint prune_v3000_l2` |
| [S-3000-14] | fnts-fntc03-aggpe-mls02 | `rollback running-config checkpoint prune_v3000_svi` |
| [S-3000-13] | fnts-fntc03-aggpe-mls01 | `rollback running-config checkpoint prune_v3000_l2` |
| [S-3000-12] | fnts-fntc03-aggpe-mls01 | `rollback running-config checkpoint prune_v3000_svi` |
| [S-3000-11] | fntc-aggacc-sw06 | Before `configure session prune_v3000 commit`: let the commit timer expire, or `configure session prune_v3000 abort` |
| [S-3000-10] | fntc-aggacc-sw05 | Before `configure session prune_v3000 commit`: let the commit timer expire, or `configure session prune_v3000 abort` |
| [S-3000-08] | fntc-aggacc-sw02 | Remove configlet `vxlan_vlan3000_fntc-aggacc-sw02` in CloudVision (`roles/cvp_rollback`) |
| [S-3000-07] | fntc-aggacc-sw01 | Remove configlet `vxlan_vlan3000_fntc-aggacc-sw01` in CloudVision (`roles/cvp_rollback`) |
| [S-3000-06] | fnts-fntc03-aggpe-mls02 | `interface Vlan3000` / `  no shutdown` |
| [S-3000-05] | fnts-fntc03-aggpe-mls01 | `interface Vlan3000` / `  no shutdown` |

Before Phase 8, undoing [S-3000-07] [S-3000-08] then [S-3000-05] [S-3000-06] puts the gateway back on the MLS pair.

## 8. Open items

1. **[EP-3000-006]** 10.230.0.40 is not in any MAC table [R-02]. Locate it before the window.
2. **Change window, expected interruption between the shutdown and the change control, prepared by / approved by:** NOT IN DISCOVERY.
