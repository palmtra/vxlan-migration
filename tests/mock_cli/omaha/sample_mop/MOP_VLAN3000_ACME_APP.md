# MOP: VLAN 3000 ACME_APP to VXLAN (omaha)

> **Sample.** Written from `ACME_APP_mop_input_20261102_221316_980.json` in this folder, which discovery produced from the Omaha **mock** fixtures (`tests/mock_cli/omaha`), following the contract and template in `docs/MOP_INPUT.md`. The switches, addresses and hosts are synthetic. Do not execute.

| | |
|---|---|
| Status | **Not approved for execution: waiting on decision [D-01].** `vlan.status` is `needs_decisions`; there are no blockers. |
| Discovery run | `omaha_20261102_221316_980`, started 2026-11-02T22:13:16Z, finished 2026-11-02T22:13:38Z |
| Code | `92b5f35f9004ff7ecdf3626156ab83d68f5ac8b2`, no uncommitted changes |
| MOP input | `ACME_APP_mop_input_20261102_221316_980.json`, SHA-256 `1c6e0eaf39a40058b8403f1b7b49388b52e17b6b6467f50207fccf0eb407944a` |
| Prepared by / approved by | NOT IN DISCOVERY |

## 1. Scope

| Item | Value |
|---|---|
| VLAN | 3000 ACME_APP, data center omaha |
| Migration type | L3: the gateway moves from the legacy MLS pair into the fabric as a `distributed_anycast` gateway |
| Subnet | 10.230.0.0/24, gateway 10.230.0.1 |
| VRF | v0000822a |
| L2 VNI | 11002 (proposed, see [D-01]) |
| L3 VNI | 822 (from the fabric: VRF v0000822a maps to it on fntc-aggacc-sw01, fntc-aggacc-sw02, `show interfaces Vxlan1`) |
| Fabric SVI MTU | 9214 [I-01] |

Switches that change ([SW-…] from `switches`, steps from `steps[].switch`):

| Switch | Type | Role | Change |
|---|---|---|---|
| fntc-aggacc-sw01 [SW-fntc-aggacc-sw01] | VTEP (EOS) | participating | Deploy VLAN 3000 / VNI 11002 [S-3000-04] |
| fntc-aggacc-sw02 [SW-fntc-aggacc-sw02] | VTEP (EOS) | participating | Deploy VLAN 3000 / VNI 11002 [S-3000-05] |
| fnts-fntc03-aggpe-mls01 [SW-fnts-fntc03-aggpe-mls01] | legacy (NX-OS) | prune_eligible | Shut the gateway SVI [S-3000-06], then teardown [S-3000-08] |
| fnts-fntc03-aggpe-mls02 [SW-fnts-fntc03-aggpe-mls02] | legacy (NX-OS) | prune_eligible | Shut the gateway SVI [S-3000-07], then teardown [S-3000-09] |
| fntc-aggacc-sw05 [SW-fntc-aggacc-sw05] | VTEP (EOS) | prune_eligible | Teardown [S-3000-10] |
| fntc-aggacc-sw06 [SW-fntc-aggacc-sw06] | VTEP (EOS) | prune_eligible | Teardown [S-3000-11] |

Not changed: fntc-aggacc-sw03 [SW-fntc-aggacc-sw03] keeps VLAN 3000 as a non-VTEP switch behind sw01/sw02 (`l2_attached`). oma01-blf01 and oma01-blf02 do not carry the VLAN (`absent`).

Coverage: 9 of 9 switches answered; no failed commands, no unread rows (`coverage.complete: true`). Endpoint tables were read per VLAN, one sample, no ARP refresh.

## 2. Impact and window

- **Hosts:** 4 host IPs in the baseline [EP-3000-003] [EP-3000-004] [EP-3000-005] [EP-3000-006] (section 3.3).
- **Gateway move:** 10.230.0.1 moves from HSRP group 3000 on fnts-fntc03-aggpe-mls01 (priority 110, 10.230.0.2/24) and fnts-fntc03-aggpe-mls02 (priority 100, 10.230.0.3/24) to an anycast SVI on the fabric leaves.
- **Gateway MAC change [R-01]:** hosts cache 10.230.0.1 as `00:00:0c:9f:fb:b8` today; the fabric answers with `00:1c:73:00:00:99`. Until a host refreshes its ARP entry it sends to the old MAC (section 4).
- **Deploy brings the fabric gateway up immediately** [S-3000-04] [S-3000-05]: "Vlan3000 comes up with ip address virtual 10.230.0.1 as soon as this configlet is applied, while the legacy gateway still answers. Apply it in the cutover window, or make sure the fabric and the legacy L2 are not bridged for this VLAN until the legacy SVI is shut."
- **Change window and expected interruption per phase:** NOT IN DISCOVERY.

## 3. Pre-checks

### 3.1 Blockers [S-3000-01]

None. Before the window, re-run `playbooks/core/discover_vlan.yml` for VLAN 3000 and confirm the report still has no blockers.

### 3.2 Decisions [S-3000-02]

| ID | Decision | Owner | Answer |
|---|---|---|---|
| [D-01] | "VNI 11002 proposed (next free in omaha external 11000-11999). Confirm it in evpn.l2.vni of the SSOT record." | Not assigned (network engineer or app owner) | **Open** |

### 3.3 Endpoint baseline [S-3000-03]

Keep `ACME_APP_endpoints_20261102_221316_980.csv`. It lists 4 host IPs; the post-change check pings these.

| ID | IP | MAC | Switch | Port | Faces | Note |
|---|---|---|---|---|---|---|
| [EP-3000-003] | 10.230.0.10 | 00:50:56:b3:00:10 | fntc-aggacc-sw01 | Et5 | esx-acme-01 | |
| [EP-3000-004] | 10.230.0.11 | 00:50:56:b3:00:11 | fntc-aggacc-sw02 | Et5 | esx-acme-02 | |
| [EP-3000-005] | 10.230.0.20 | 00:50:56:b3:00:20 | fntc-aggacc-sw03 | Et10 | acme-db-01 | |
| [EP-3000-006] | 10.230.0.40 | 00:50:56:b3:00:40 | | | | In ARP, not in any MAC table [R-02] |

Not in the baseline: [EP-3000-001] 10.230.0.2 and [EP-3000-002] 10.230.0.3 are the legacy gateway SVIs.

### 3.4 Routing objects

No `task` findings: nothing to recreate by hand. For the record, today both MLS switches originate 10.230.0.0/24 in VRF v0000822a by `redistribute direct route-map RM_CONN_ACME` and advertise it to 66.180.0.3 and 66.180.0.4; border leaves oma01-blf01 and oma01-blf02 learn it over iBGP, best path from fnts-fntc03-aggpe-mls01 (`routing`).

## 4. Risks

| ID | Severity | Risk | Mitigation |
|---|---|---|---|
| [R-01] | high | The gateway MAC changes at cutover: hosts cache 10.230.0.1 as 00:00:0c:9f:fb:b8; the fabric answers with 00:1c:73:00:00:99. Until a host updates its entry it sends to the old MAC. | "A host updates its entry when the fabric gateway ARPs for it (the post-cutover ping of every baseline endpoint does this) or when the entry ages out." Ping every endpoint from the fabric gateway right after the cutover [S-3000-06] [S-3000-07], and check hosts that do not answer. |
| [R-02] | low | 1 active IP is in ARP but not in any MAC table: 10.230.0.40 [EP-3000-006] (aged out, or behind a switch that was not discovered). | Re-run discovery or check it by hand before the window. |

Sequencing (already in the step order): teardown of fnts-fntc03-aggpe-mls01 [Q-01] and fnts-fntc03-aggpe-mls02 [Q-02] waits until the gateway has moved.

## 5. Procedure

Run the steps in order. A step starts only when every step in its `depends_on` is done and verified.

### Phase 1: deploy

#### [S-3000-04] Deploy VLAN 3000 / VNI 11002 on fntc-aggacc-sw01

- Status: ready. Depends on [S-3000-01] [S-3000-02] [S-3000-03].
- CLI: configlet `vxlan_vlan3000_fntc-aggacc-sw01.cfg` written by `playbooks/core/workflow_deploy.yml`.
- Note: Vlan3000 comes up with `ip address virtual 10.230.0.1` as soon as the configlet is applied (section 2).
- Verify:
  - `show vxlan vlan-to-vni | include 3000` (expect VNI 11002)
  - `show interfaces Vlan3000` (up, ip address virtual)
- Rollback: roll back the CVP change control (`roles/cvp_rollback`).

#### [S-3000-05] Deploy VLAN 3000 / VNI 11002 on fntc-aggacc-sw02

- Status: ready. Depends on [S-3000-01] [S-3000-02] [S-3000-03].
- CLI: configlet `vxlan_vlan3000_fntc-aggacc-sw02.cfg` written by `playbooks/core/workflow_deploy.yml`.
- Note: Vlan3000 comes up with `ip address virtual 10.230.0.1` as soon as the configlet is applied (section 2).
- Verify:
  - `show vxlan vlan-to-vni | include 3000` (expect VNI 11002)
  - `show interfaces Vlan3000` (up, ip address virtual)
- Rollback: roll back the CVP change control (`roles/cvp_rollback`).

### Phases 2 and 3: link prunes

None for this VLAN: the MOP input has no `2-link-prune-mls` or `3-link-prune-non-vtep` steps.

### Phase 4: gateway cutover

#### [S-3000-06] Shut the legacy gateway SVI on fnts-fntc03-aggpe-mls01

- Status: ready. Depends on [S-3000-04] [S-3000-05].
- CLI:

```
interface Vlan3000
  shutdown
```

- Verify:
  - Ping every baseline endpoint from a fabric gateway leaf (`playbooks/core/workflow_verify.yml`).
  - `show ip arp vrf v0000822a | include Vlan3000` on a fabric gateway leaf: the endpoints are there.
- Note [R-01]: the gateway MAC changes from 00:00:0c:9f:fb:b8 to 00:1c:73:00:00:99; hosts update their ARP entry for 10.230.0.1 when the fabric gateway ARPs for them (the ping above) or when it ages out.
- Rollback:

```
interface Vlan3000
  no shutdown
```

#### [S-3000-07] Shut the legacy gateway SVI on fnts-fntc03-aggpe-mls02

- Status: ready. Depends on [S-3000-04] [S-3000-05].
- CLI:

```
interface Vlan3000
  shutdown
```

- Verify:
  - Ping every baseline endpoint from a fabric gateway leaf (`playbooks/core/workflow_verify.yml`).
  - `show ip arp vrf v0000822a | include Vlan3000` on a fabric gateway leaf: the endpoints are there.
- Note [R-01]: as in [S-3000-06].
- Rollback:

```
interface Vlan3000
  no shutdown
```

### Phase 5: teardown

The two MLS teardowns show `blocked_until_gateway_moved` in this discovery run because their SVIs were still active [Q-01] [Q-02]. After [S-3000-06] and [S-3000-07], re-run discovery: the plans should show ready before you run them.

#### [S-3000-08] Teardown on fnts-fntc03-aggpe-mls01 (trunks, SVI, VLAN)

- Status: blocked_until_gateway_moved ("Vlan SVI on fnts-fntc03-aggpe-mls01 is still the active gateway; move the gateway to the fabric first"). Depends on [S-3000-06] [S-3000-07].
- CLI (from `ACME_APP_prune_20261102_221316_980.md`):

```
checkpoint prune_v3000
configure terminal
interface port-channel101
  switchport trunk allowed vlan remove 3000
interface port-channel105
  switchport trunk allowed vlan remove 3000
no interface Vlan3000
no vlan 3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls01 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000`

#### [S-3000-09] Teardown on fnts-fntc03-aggpe-mls02 (trunks, SVI, VLAN)

- Status: blocked_until_gateway_moved ("Vlan SVI on fnts-fntc03-aggpe-mls02 is still the active gateway; move the gateway to the fabric first"). Depends on [S-3000-06] [S-3000-07].
- CLI (from `ACME_APP_prune_20261102_221316_980.md`):

```
checkpoint prune_v3000
configure terminal
interface port-channel101
  switchport trunk allowed vlan remove 3000
interface port-channel105
  switchport trunk allowed vlan remove 3000
no interface Vlan3000
no vlan 3000
end
```

- Verify: re-run discovery for VLAN 3000: fnts-fntc03-aggpe-mls02 shows the change.
- Save after verification: `copy running-config startup-config`
- Rollback: `rollback running-config checkpoint prune_v3000`

#### [S-3000-10] Teardown on fntc-aggacc-sw05 (trunks, VLAN)

- Status: ready. Depends on [S-3000-06] [S-3000-07].
- CLI (from `ACME_APP_prune_20261102_221316_980.md`):

```
configure session prune_v3000
interface Port-Channel1
   switchport trunk allowed vlan remove 3000
no vlan 3000
!
show session-config diffs
commit timer 00:10:00
```

- The change is live but provisional for 00:10:00 after `commit timer`. Run the checks, then keep it with `configure session prune_v3000 commit` before the timer expires, or it rolls back.
- Verify: re-run discovery for VLAN 3000: fntc-aggacc-sw05 shows the change.
- Keep it after verification, before the timer expires: `configure session prune_v3000 commit`
- Rollback: do not commit the session again: it rolls back when the commit timer (00:10:00) expires. To roll back at once: `configure session prune_v3000 abort`

#### [S-3000-11] Teardown on fntc-aggacc-sw06 (trunks, VLAN)

- Status: ready. Depends on [S-3000-06] [S-3000-07].
- CLI (from `ACME_APP_prune_20261102_221316_980.md`):

```
configure session prune_v3000
interface Port-Channel1
   switchport trunk allowed vlan remove 3000
no vlan 3000
!
show session-config diffs
commit timer 00:10:00
```

- The change is live but provisional for 00:10:00 after `commit timer`. Run the checks, then keep it with `configure session prune_v3000 commit` before the timer expires, or it rolls back.
- Verify: re-run discovery for VLAN 3000: fntc-aggacc-sw06 shows the change.
- Keep it after verification, before the timer expires: `configure session prune_v3000 commit`
- Rollback: do not commit the session again: it rolls back when the commit timer (00:10:00) expires. To roll back at once: `configure session prune_v3000 abort`

### Phase 6: verify

#### [S-3000-12] Post-change verification

- Depends on [S-3000-08] [S-3000-09] [S-3000-10] [S-3000-11].
- Run `playbooks/core/workflow_verify.yml`.
- Verify:
  - Every baseline host answers ping from the fabric (`ACME_APP_endpoints_20261102_221316_980.csv`).
  - Re-run discovery: VLAN 3000 is VNI-mapped on fntc-aggacc-sw01, fntc-aggacc-sw02 and nothing is left to prune.

## 6. Verification and acceptance

| ID | Check | Result |
|---|---|---|
| [A-3000-01] | VLAN 3000 is mapped to VNI 11002 on every fabric leaf in the deploy steps | |
| [A-3000-02] | All 4 baseline host IPs answer ping from the fabric gateway | |
| [A-3000-03] | A re-run of discovery for VLAN 3000 lists no prune-eligible switch and no blockers | |
| [A-3000-04] | Endpoints resolve 10.230.0.1 to the fabric MAC 00:1c:73:00:00:99 | |

## 7. Rollback

Reverse order. Roll back only as far as needed.

| Undo | Switch | Rollback |
|---|---|---|
| [S-3000-11] | fntc-aggacc-sw06 | Before `configure session prune_v3000 commit`: let the commit timer expire, or `configure session prune_v3000 abort` |
| [S-3000-10] | fntc-aggacc-sw05 | Before `configure session prune_v3000 commit`: let the commit timer expire, or `configure session prune_v3000 abort` |
| [S-3000-09] | fnts-fntc03-aggpe-mls02 | `rollback running-config checkpoint prune_v3000` |
| [S-3000-08] | fnts-fntc03-aggpe-mls01 | `rollback running-config checkpoint prune_v3000` |
| [S-3000-07] | fnts-fntc03-aggpe-mls02 | `interface Vlan3000` / `  no shutdown` |
| [S-3000-06] | fnts-fntc03-aggpe-mls01 | `interface Vlan3000` / `  no shutdown` |
| [S-3000-05] | fntc-aggacc-sw02 | Roll back the CVP change control (`roles/cvp_rollback`) |
| [S-3000-04] | fntc-aggacc-sw01 | Roll back the CVP change control (`roles/cvp_rollback`) |

## 8. Open items

1. **[D-01]** Confirm VNI 11002 in `evpn.l2.vni` of the SSOT record. Owner not assigned.
2. **Bridging during deploy** [S-3000-04] [S-3000-05]: whether the fabric and the legacy L2 are bridged for VLAN 3000 before the legacy SVI is shut is NOT IN DISCOVERY. Pick one: apply the configlets in the cutover window, or confirm they are not bridged.
3. **[EP-3000-006]** 10.230.0.40 is not in any MAC table [R-02]. Locate it before the window.
4. **Change window, expected interruption per phase, prepared by / approved by:** NOT IN DISCOVERY.
