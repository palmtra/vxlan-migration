# -*- coding: utf-8 -*-
"""MOP input: one compact, schema-versioned JSON per VLAN, and a run index.

The discovery reports are written for engineers. This file is written for a
MOP author, human or LLM: facts only (no raw CLI output), every item with a
stable ID to cite, findings typed as blockers / decisions / risks / tasks,
and the change steps in order with the CLI that already exists (prune plans,
configlet files) and its rollback. Nothing here is applied to a device.

Schema: schemas/mop_input.schema.json. Contract and template: docs/MOP_INPUT.md.
"""

import hashlib
import os

from vlan_lib.prune import build_prune_execution
from vlan_lib.reports import build_findings, resolve_l3_vni

MOP_SCHEMA = "vxlan-migration/mop-input"
MOP_SCHEMA_VERSION = "2.0"
RUN_INDEX_SCHEMA = "vxlan-migration/run-index"

_FINDING_PREFIX = {
    "blocker": "B",
    "decision": "D",
    "risk": "R",
    "task": "T",
    "sequencing": "Q",
    "info": "I",
}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# Standing rules for whoever writes the MOP from this input.
MOP_RULES = (
    "The VLAN record (vars/vlans/<dc>/<vid>_<name>.yml) is authoritative for the VNI. Use vlan.l2_vni only when "
    "vlan.l2_vni_source is record; never replace it with a discovery proposal.",
    "Fabric interconnects (UCS FI) are the host boundary: MACs learned on an FI port are hosts of this VLAN, even "
    "though the port is a trunk. Nothing is traced beyond the FI.",
    "The VLAN joins an existing VRF. Never create a VRF, L3 VNI or VRF route target for it; verify and reuse what "
    "is there.",
    "Each trunk-removal line names this VLAN only. Never merge it with another VLAN on the same trunk.",
    "The legacy gateway SVI is shut by hand in the window (manual step). It is removed only after the validation "
    "step passes and the legacy BGP origination is withdrawn.",
)
# Change order (docs/MOP_INPUT.md, Steps): prechecks, the manual legacy SVI
# shutdown, the fabric deploy, the link prunes that cut the EVPN-legacy loop,
# validation (a gate), the legacy BGP withdrawal, teardown, then the source
# gateways (SVI first), and final verification.
_PRUNE_PHASE_NAMES = {1: "3-link-prune-mls", 2: "4-link-prune-non-vtep", 3: "7-teardown"}


def _typed(value):
    """Ansible templating hands nested values back as text: '3' -> 3, 'True' -> True."""
    if isinstance(value, dict):
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    if isinstance(value, str):
        if value in ("True", "true"):
            return True
        if value in ("False", "false"):
            return False
        if value.isdigit() and not (len(value) > 1 and value.startswith("0")):
            return int(value)
    return value


def normalize_run(run):
    """Run metadata with typed parameters and git flags."""
    run = dict(run or {})
    for key in ("parameters", "git"):
        if isinstance(run.get(key), dict):
            run[key] = _typed(run[key])
    return run


def _hostname(item):
    return item.get("hostname") if isinstance(item, dict) else str(item or "")


def _unique(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def report_file_names(report, timestamp):
    """The report files discovery writes for one VLAN, by purpose."""
    slug = report.get("vlan_slug") or "vlan_%s" % report.get("vlan_id")
    return {
        "discovery_markdown": "%s_discovery_%s.md" % (slug, timestamp),
        "discovery_json": "%s_discovery_%s.json" % (slug, timestamp),
        "deployable_model": "%s_discovery_%s.yml" % (slug, timestamp),
        "endpoint_baseline": "%s_endpoints_%s.csv" % (slug, timestamp),
        "prune_markdown": "%s_prune_%s.md" % (slug, timestamp),
        "prune_json": "%s_prune_%s.json" % (slug, timestamp),
        "mop_input": "%s_mop_input_%s.json" % (slug, timestamp),
    }


def _findings(report):
    """Typed findings with IDs (B-01, D-01, ...) numbered per kind, in report order."""
    counters = {}
    out = []
    for item in build_findings(report):
        prefix = _FINDING_PREFIX.get(item["kind"], "I")
        counters[prefix] = counters.get(prefix, 0) + 1
        out.append(dict(item, id="%s-%02d" % (prefix, counters[prefix])))
    return out


def _switches(report):
    decisions = {
        item.get("hostname"): item for item in (report.get("placement_analysis") or {}).get("switches") or []
    }
    rows = []
    for device in report.get("devices") or []:
        host = device.get("hostname") or ""
        decision = decisions.get(host) or {}
        details = device.get("svi_details") or {}
        rows.append(
            {
                "id": "SW-%s" % host,
                "hostname": host,
                "os": device.get("os_family") or "",
                "type": {"vtep": "vtep", "l2_switch": "non_vtep", "mls": "legacy"}.get(
                    decision.get("switch_role") or "", device.get("migration_tier") or ""
                ),
                "role": device.get("placement_role") or decision.get("role") or "",
                "vlan_present": bool(device.get("vlan_present")),
                "svi": details.get("primary_ip") or "" if device.get("svi_present") else "",
                "svi_shutdown": bool(details.get("shutdown")) if device.get("svi_present") else False,
                "local_endpoint_macs": len(device.get("endpoint_mac_entries") or []),
                "reasons": list(
                    decision.get("evidence") or []
                    if decision.get("role") in ("participating", "l2_attached", "legacy_retained")
                    else decision.get("nonqualifying") or []
                ),
            }
        )
    return sorted(rows, key=lambda row: row["hostname"])


def _endpoints(report):
    table = report.get("endpoint_table") or {}
    rows = list(table.get("endpoints") or []) + list(table.get("mac_only") or [])
    out = []
    for index, row in enumerate(rows, start=1):
        out.append(
            {
                "id": "EP-%s-%03d" % (report.get("vlan_id"), index),
                "ip": row.get("ip") or "",
                "ipv6": list(row.get("ipv6") or []),
                "mac": row.get("mac") or "",
                "location": row.get("location") or "",
                "switch": row.get("hostname") or "",
                "port": row.get("interface") or "",
                "port_faces": row.get("port_label") or "",
                "role": row.get("role") or "",
                "flags": list(row.get("flags") or []),
                "seen_by_arp_on": list(row.get("arp_from") or []),
                "also_on": list(row.get("also_on") or []),
                "seen": row.get("seen"),
                "baseline": bool(row.get("ip")) and not {"gateway", "fhrp_mac"} & set(row.get("flags") or []),
            }
        )
    return {"summary": dict(table.get("summary") or {}), "rows": out}


def _gateway(report):
    gw = report.get("gateway_assessment") or {}
    model_gw = (report.get("deployment_model") or {}).get("gateway") or {}
    gmac = report.get("gateway_mac") or {}
    legacy = []
    for svi in gw.get("svis") or []:
        legacy.append(
            {
                "switch": svi.get("hostname") or "",
                "interface": "Vlan%s" % report.get("vlan_id"),
                "vrf": svi.get("vrf") or "",
                "addresses": list(svi.get("ip_addresses") or []),
                "fhrp": [
                    {
                        "protocol": group.get("protocol"),
                        "group": group.get("group"),
                        "family": group.get("family"),
                        "vip": group.get("ip"),
                        "priority": group.get("priority"),
                        "virtual_mac": group.get("virtual_mac") or "",
                        "mac_source": group.get("mac_source") or "",
                    }
                    for group in svi.get("fhrp_groups") or []
                ],
            }
        )
    return {
        "location": gw.get("location") or "",
        "mode": gw.get("mode") or model_gw.get("mode") or "",
        "virtual_ip": model_gw.get("virtual_ip") or "",
        "subnets": list(gw.get("subnets") or []),
        "svi_mtu": model_gw.get("svi_mtu"),
        "legacy_svis": legacy,
        "services_not_migrated": {
            key: value for key, value in (gw.get("services") or {}).items() if value
        },
        "mac_change": {
            "applies": bool(gmac.get("applies")),
            "legacy_macs": list(gmac.get("legacy_macs") or []),
            "fabric_mac": gmac.get("fabric_mac") or "",
            "fabric_missing": list(gmac.get("fabric_missing") or []),
            "changes": bool(gmac.get("changes")),
        },
    }


def _coverage(report, findings):
    devices = report.get("devices") or []
    return {
        "switches_scanned": sorted(report.get("switches_scanned") or []),
        "not_discovered": sorted(report.get("not_discovered") or []),
        "failed_commands": [
            {"switch": d.get("hostname"), "command": e.get("command"), "error": e.get("error")}
            for d in devices
            for e in d.get("errors") or []
        ],
        "unread_rows": [
            {"switch": d.get("hostname"), "table": table, "problem": problem}
            for d in devices
            for table, check in sorted((d.get("parse_checks") or {}).items())
            for problem in (check or {}).get("problems") or []
        ],
        "endpoint_collection": dict(report.get("endpoint_collection") or {}),
        "complete": not [f for f in findings if f["kind"] == "blocker" and f["category"] == "coverage"],
    }


def _step(steps, vlan_id, **fields):
    step = {
        "id": "S-%s-%02d" % (vlan_id, len(steps) + 1),
        "phase": fields.pop("phase"),
        "title": fields.pop("title"),
        "switch": fields.pop("switch", ""),
        "status": fields.pop("status", "ready"),
        "cli": fields.pop("cli", None),
        "cli_source": fields.pop("cli_source", ""),
        "rollback": fields.pop("rollback", None),
        "verify": fields.pop("verify", []),
        "depends_on": fields.pop("depends_on", []),
        "notes": fields.pop("notes", []),
    }
    step.update(fields)
    steps.append(step)
    return step["id"]


def _plan_cli(plan):
    """The plan's session/checkpoint-wrapped CLI, also while it waits on an earlier step."""
    execution = plan.get("execution") or {}
    if execution.get("full_cli"):
        return execution["full_cli"]
    actions = plan.get("actions") or plan.get("blocked_actions") or []
    return "\n".join(a.get("cli", "") for a in actions) or None


def _source_gateway_hosts(report):
    """Legacy switches that own the VLAN's SVI today (the source gateways)."""
    model = report.get("deployment_model") or {}
    hosts = _unique(
        [svi.get("hostname") for svi in (report.get("gateway_assessment") or {}).get("svis") or []]
        + [_hostname(item) for item in (model.get("placement") or {}).get("source_gateway_devices") or []]
    )
    return hosts


def _split_source_gateway_plan(plan, os_family="", vlan_id=None):
    """A source gateway's teardown, as two changes: the SVI, then trunks and VLAN.

    Returns ``[(title, actions, execution)]``. The SVI comes out on its own
    so the destructive gateway removal is a separate, later step.
    """
    actions = list(plan.get("actions") or plan.get("blocked_actions") or [])
    svi = [a for a in actions if a.get("op") == "no_interface_vlan"]
    rest = [a for a in actions if a.get("op") != "no_interface_vlan"]
    execution = plan.get("execution") or {}
    platform = execution.get("platform") or plan.get("os_family") or os_family or "eos"
    timer = execution.get("commit_timer") or None
    vlan_ids = plan.get("vlan_ids") or ([vlan_id] if vlan_id is not None else [])
    parts = []
    for title, chunk, suffix in (
        ("Remove the legacy gateway SVI", svi, "svi"),
        ("Remove trunks and the VLAN", rest, "l2"),
    ):
        if chunk:
            execution = build_prune_execution(platform, vlan_ids, chunk, timer, plan.get("hostname") or "", suffix)
            parts.append((title, chunk, execution))
    return parts


def _bgp_withdraw_cli(item, asn, os_family):
    """``no network`` / ``no aggregate-address`` for one legacy BGP origination."""
    if not asn:
        return None
    vrf = item.get("vrf") or "default"
    statement = "network" if item.get("kind") == "network" else "aggregate-address"
    if (os_family or "").lower() == "nxos":
        lines = ["router bgp %s" % asn]
        indent = "  "
        if vrf != "default":
            lines.append("%svrf %s" % (indent, vrf))
            indent += "  "
        lines.append("%saddress-family ipv4 unicast" % indent)
        lines.append("%s  no %s %s" % (indent, statement, item.get("prefix")))
        return "\n".join(lines)
    lines = ["router bgp %s" % asn]
    if vrf != "default":
        lines.append("   vrf %s" % vrf)
        lines.append("      no %s %s" % (statement, item.get("prefix")))
    else:
        lines.append("   no %s %s" % (statement, item.get("prefix")))
    return "\n".join(lines)


def _steps(report, findings, files):
    """The change steps in order (docs/MOP_INPUT.md, Steps).

    Prechecks; the manual shutdown of the legacy gateway SVI in the window
    (L3 with a legacy gateway); the fabric deploy, which brings the gateway
    up; the link prunes that cut the EVPN-legacy loop; a validation step that
    gates everything after it; the legacy BGP origination withdrawal; the
    teardown of the other switches; the source gateways (SVI first, then
    trunks and VLAN); final verification.
    """
    vlan_id = report.get("vlan_id")
    model = report.get("deployment_model") or {}
    placement = model.get("placement") or {}
    vni = ((model.get("evpn") or {}).get("l2") or {}).get("vni")
    gw = report.get("gateway_assessment") or {}
    gmac = report.get("gateway_mac") or {}
    l3 = report.get("migration_type") == "l3"
    legacy_gateway = l3 and gw.get("location") == "legacy"
    devices = {d.get("hostname"): d for d in report.get("devices") or [] if d.get("hostname")}
    os_by_host = {host: (d.get("os_family") or "").lower() for host, d in devices.items()}
    vrf = gw.get("vrf") or report.get("discovered_vrf") or ""
    virtual_ip = gw.get("virtual_ip") or (model.get("gateway") or {}).get("virtual_ip", "")
    source_gateways = _source_gateway_hosts(report)
    steps = []

    blockers = [f["id"] for f in findings if f["kind"] == "blocker"]
    decisions = [f["id"] for f in findings if f["kind"] == "decision"]
    tasks = [f["id"] for f in findings if f["kind"] == "task"]

    gateway_hosts = _unique(_hostname(item) for item in placement.get("gateway_leafs") or [])
    leaves = _unique(
        [_hostname(item) for item in placement.get("participating_leafs") or []] + gateway_hosts
    )
    if l3 and not gateway_hosts:
        gateway_hosts = list(leaves)

    # 0 - prechecks
    pre = []
    pre.append(
        _step(
            steps, vlan_id, phase="0-prechecks", title="Resolve every blocker and re-run discovery",
            status="blocked" if blockers else "ready", findings=blockers,
            verify=["Re-run playbooks/core/discover_vlan.yml for VLAN %s: no blockers in the report" % vlan_id],
        )
    )
    pre.append(
        _step(
            steps, vlan_id, phase="0-prechecks", title="Record the engineer's and app owner's decisions",
            status="needs_decision" if decisions else "ready", findings=decisions,
        )
    )
    pre.append(
        _step(
            steps, vlan_id, phase="0-prechecks", title="Keep the endpoint baseline for post-change checks",
            files=[files["endpoint_baseline"]],
            verify=["The baseline lists %s host IPs" % (report.get("endpoint_table") or {}).get("summary", {}).get("active_ips", 0)],
        )
    )
    if l3 and vrf and vrf != "default":
        vrf_blockers = [f["id"] for f in findings if f["kind"] == "blocker" and f["category"] == "fabric"]
        pre.append(
            _step(
                steps, vlan_id, phase="0-prechecks",
                title="Verify VRF %s exists on the gateway leaves and reuse it" % vrf,
                status="blocked" if vrf_blockers else "ready", findings=vrf_blockers,
                verify=[
                    "show vrf %s on %s: the VRF exists" % (vrf, ", ".join(gateway_hosts) or "each gateway leaf"),
                    "show running-config interfaces Vxlan1 | include vrf %s: the VRF has its L3 VNI" % vrf,
                    "show running-config section router bgp | include vrf %s: the VRF is in BGP with its RD and route targets" % vrf,
                ],
                notes=[
                    "The VLAN joins the existing VRF. The configlet carries no VRF, L3 VNI or VRF route target: "
                    "do not create them for this VLAN. If the VRF is missing on a gateway leaf, stop."
                ],
            )
        )
    if tasks:
        pre.append(
            _step(
                steps, vlan_id, phase="0-prechecks", title="Recreate the routing objects the configlet does not carry",
                status="ready", findings=tasks, cli_source="engineer (not generated)",
            )
        )

    # 1 - manual shutdown of the legacy gateway, in the window
    shutdown = []
    if legacy_gateway:
        for svi in gw.get("svis") or []:
            host = svi.get("hostname") or ""
            indent = "  " if os_by_host.get(host) == "nxos" else "   "
            shutdown.append(
                _step(
                    steps, vlan_id, phase="1-gateway-shutdown",
                    title="Shut the legacy gateway SVI (manual, in the window)", switch=host,
                    manual=True,
                    cli="interface Vlan%s\n%sshutdown" % (vlan_id, indent),
                    cli_source="mop_input (manual; the SVI is removed later, after validation)",
                    rollback="interface Vlan%s\n%sno shutdown" % (vlan_id, indent),
                    verify=["show interface Vlan%s: administratively down" % vlan_id],
                    notes=[
                        "The engineer shuts the SVI by hand in the maintenance window; the automation does not. "
                        "Execute the fabric change control (deploy steps) right after."
                    ],
                    depends_on=list(pre),
                )
            )

    # 2 - fabric deploy (one CloudVision change control, executed in the window)
    deploy = []
    for host in leaves:
        notes = []
        if l3 and host in gateway_hosts and legacy_gateway:
            notes.append(
                "Vlan%s comes up with ip address virtual %s as soon as this configlet is applied. Execute the "
                "change control only after the legacy SVI is shut (%s)." % (vlan_id, virtual_ip, ", ".join(shutdown))
            )
        verify = ["show vxlan vlan-to-vni | include %s  (expect VNI %s)" % (vlan_id, vni or "?")]
        if l3 and host in gateway_hosts:
            verify.append("show interfaces Vlan%s  (up, ip address virtual)" % vlan_id)
        deploy.append(
            _step(
                steps, vlan_id, phase="2-deploy", title="Deploy VLAN %s / VNI %s" % (vlan_id, vni or "?"), switch=host,
                status="blocked" if blockers else "ready", depends_on=list(shutdown or pre),
                cli_source="configlet vxlan_vlan%s_%s.cfg written by playbooks/core/workflow_deploy.yml" % (vlan_id, host),
                rollback=(
                    "Remove configlet vxlan_vlan%s_%s from %s in CloudVision (roles/cvp_rollback renders the "
                    "reversing configlet from leaf_vxlan_rollback_configlet.j2)" % (vlan_id, host, host)
                ),
                verify=verify, notes=notes,
            )
        )

    # Switches that keep the VLAN have a plan with nothing to do: not a step.
    plans = [
        plan
        for plan in report.get("prune_plans") or []
        if plan.get("placement_role") in ("prune_eligible", "link_prune")
    ]
    by_phase = {}
    for plan in plans:
        by_phase.setdefault(plan.get("phase") or 3, []).append(plan)

    def prune_step(phase_name, title, plan, cli, execution, depends):
        rollback = execution.get("rollback") or None
        confirm = None
        notes = []
        if execution.get("platform") == "eos" and execution.get("commit_timer_cmd"):
            timer = execution.get("commit_timer") or ""
            confirm = execution.get("confirm") or None
            rollback = (
                "Do not commit the session again: it rolls back when the commit timer (%s) expires. "
                "To roll back at once: %s" % (timer, execution.get("abort") or "")
            ).strip()
            notes.append(
                "The change is live but provisional for %s after `commit timer`. Run the checks, then keep it "
                "with `%s` before the timer expires, or it rolls back." % (timer, confirm or "")
            )
        return _step(
            steps, vlan_id, phase=phase_name, title=title,
            switch=plan.get("hostname") or "",
            status="ready" if plan.get("actions") else (plan.get("status") or "blocked"),
            blocked_reason=plan.get("blocked_reason") or "",
            cli=cli, cli_source=files["prune_markdown"],
            rollback=rollback,
            confirm=confirm,
            save=execution.get("save") or "",
            notes=notes,
            verify=["Re-run discovery for VLAN %s: %s shows the change" % (vlan_id, plan.get("hostname"))],
            depends_on=list(depends),
        )

    def prune_steps(phase, depends, keep=None):
        ids = []
        for plan in by_phase.get(phase, []):
            if keep is not None and not keep(plan):
                continue
            ids.append(
                prune_step(
                    _PRUNE_PHASE_NAMES.get(phase, "7-teardown"),
                    "%s: %s" % (plan.get("phase_label") or "prune", plan.get("hostname")),
                    plan, _plan_cli(plan), plan.get("execution") or {}, depends,
                )
            )
        return ids

    # 3, 4 - link prunes right after the deploy: they cut the EVPN-legacy loop.
    phase1 = prune_steps(1, deploy or pre)
    phase2 = prune_steps(2, phase1 or deploy or pre)

    # 5 - validation gates everything after it
    validate_verify = [
        "playbooks/core/workflow_verify.yml for VLAN %s passes on every fabric leaf" % vlan_id,
        "Every baseline host answers ping from the fabric (%s)" % files["endpoint_baseline"],
        "Endpoint comparison (playbooks/core/migrate_vlan.yml summary): no endpoint missing after the change",
    ]
    validate_notes = [
        "Gate: nothing after this step runs until it passes. playbooks/core/prune_vlan.yml refuses to prune a "
        "source gateway until the new gateway is validated (a migrate_vlan.yml summary with verification passed "
        "and no endpoint problems, or -e new_gateway_validated=true once this step is done by hand)."
    ]
    if l3:
        validate_verify.insert(
            1,
            "show ip arp vrf %s | include Vlan%s on a fabric gateway leaf: the endpoints are there"
            % (vrf or "default", vlan_id),
        )
    if gmac.get("changes"):
        validate_notes.append(
            "Gateway MAC changes from %s to %s: hosts update their ARP entry for %s when the fabric "
            "gateway ARPs for them (the ping above) or when it ages out."
            % (", ".join(gmac.get("legacy_macs") or []), gmac.get("fabric_mac"), virtual_ip)
        )
    validate = [
        _step(
            steps, vlan_id, phase="5-validate", title="Validate the new gateway and the endpoints",
            gate=True,
            cli_source="playbooks/core/workflow_verify.yml; endpoint comparison in playbooks/core/migrate_vlan.yml",
            verify=validate_verify, notes=validate_notes,
            depends_on=list(phase2 or phase1 or deploy or pre),
        )
    ]

    # 6 - withdraw the legacy BGP origination of the VLAN prefix
    withdraw = []
    routing = report.get("routing_detail") or {}
    border = _unique(row.get("hostname") for row in routing.get("bgp_fabric") or [])
    for item in routing.get("bgp_networks") or []:
        host = item.get("hostname") or ""
        if item.get("kind") not in ("network", "aggregate") or not host or host in leaves:
            continue
        device = devices.get(host) or {}
        os_family = os_by_host.get(host, "")
        cli = _bgp_withdraw_cli(item, device.get("bgp_as") or "", os_family)
        rollback = None
        if cli:
            rollback = cli.replace("no network ", "network ").replace("no aggregate-address ", "aggregate-address ")
        statement = "network" if item.get("kind") == "network" else "aggregate-address"
        withdraw.append(
            _step(
                steps, vlan_id, phase="6-routing-withdrawal",
                title="Withdraw the legacy BGP %s %s" % (statement, item.get("prefix")), switch=host,
                cli=cli,
                cli_source="mop_input (from routing.bgp_networks)" if cli else "engineer (BGP ASN not discovered on %s)" % host,
                rollback=rollback,
                verify=[
                    "Before the change: show ip bgp vrf %s %s on %s: a path originated by the fabric is present, "
                    "not only the one from %s. Stop if it is not."
                    % (item.get("vrf") or "default", item.get("prefix"), ", ".join(border) or "the border leaves", host),
                    "After the change: the same command shows the fabric path as best and no path from %s" % host,
                ],
                notes=["Keeps the subnet reachable: the fabric must originate it before the legacy %s goes." % statement],
                depends_on=list(validate),
            )
        )

    # 7 - teardown of the other prune-eligible switches; 8 - the source gateways
    teardown = prune_steps(3, withdraw or validate, keep=lambda plan: plan.get("hostname") not in source_gateways)
    source_teardown = []  # the last step of each source gateway
    depends = list(teardown or withdraw or validate)
    for plan in by_phase.get(3, []):
        if plan.get("hostname") not in source_gateways:
            continue
        previous = depends
        split = _split_source_gateway_plan(plan, os_by_host.get(plan.get("hostname"), ""), vlan_id)
        for title, _actions, execution in split:
            previous = [
                prune_step(
                    "8-source-gateway-teardown", "%s: %s" % (title, plan.get("hostname")),
                    plan, execution.get("full_cli") or None, execution, previous,
                )
            ]
        if previous is not depends:
            source_teardown.extend(previous)

    _step(
        steps, vlan_id, phase="9-verify", title="Post-change verification",
        cli_source="playbooks/core/workflow_verify.yml",
        verify=[
            "Every baseline host answers ping from the fabric (%s)" % files["endpoint_baseline"],
            "Re-run discovery: VLAN %s is VNI-mapped on %s and nothing is left to prune" % (vlan_id, ", ".join(leaves) or "the fabric leaves"),
        ],
        depends_on=list(source_teardown or teardown or withdraw or validate),
    )
    return steps


def _acceptance(report, steps):
    vlan_id = report.get("vlan_id")
    model = report.get("deployment_model") or {}
    vni = ((model.get("evpn") or {}).get("l2") or {}).get("vni")
    summary = (report.get("endpoint_table") or {}).get("summary") or {}
    gmac = report.get("gateway_mac") or {}
    checks = [
        {"check": "VLAN %s is mapped to VNI %s on every fabric leaf in the deploy steps" % (vlan_id, vni or "?")},
        {"check": "All %s baseline host IPs answer ping from the fabric gateway" % summary.get("active_ips", 0)},
        {"check": "A re-run of discovery for VLAN %s lists no prune-eligible switch and no blockers" % vlan_id},
    ]
    if gmac.get("applies") and gmac.get("fabric_mac"):
        checks.append(
            {"check": "Endpoints resolve %s to the fabric MAC %s" % (
                (model.get("gateway") or {}).get("virtual_ip") or "the gateway", gmac["fabric_mac"])}
        )
    return [dict(item, id="A-%s-%02d" % (vlan_id, index)) for index, item in enumerate(checks, start=1)]


def build_mop_input(report, run=None, timestamp=""):
    """The MOP input document for one VLAN report (schemas/mop_input.schema.json)."""
    run = normalize_run(run)
    findings = _findings(report)
    files = report_file_names(report, timestamp or run.get("timestamp") or "")
    model = report.get("deployment_model") or {}
    evpn = model.get("evpn") or {}
    steps = _steps(report, findings, files)
    l3_vni = resolve_l3_vni(report)
    counts = {kind: len([f for f in findings if f["kind"] == kind]) for kind in _FINDING_PREFIX}
    status = "blocked" if counts["blocker"] else ("needs_decisions" if counts["decision"] else "ready")
    return {
        "schema": MOP_SCHEMA,
        "schema_version": MOP_SCHEMA_VERSION,
        "purpose": (
            "Input for writing the MOP for this VLAN. Facts come from read-only discovery. Use only the CLI in "
            "steps[].cli and the configlet files named in steps[].cli_source; cite IDs; do not invent commands."
        ),
        "rules": list(MOP_RULES),
        "run": run,
        "vlan": {
            "id": report.get("vlan_id"),
            "name": report.get("vlan_name") or "",
            "data_center": report.get("data_center") or "",
            "migration_type": report.get("migration_type") or "",
            "vrf": (report.get("gateway_assessment") or {}).get("vrf") or report.get("discovered_vrf") or "",
            "l2_vni": (evpn.get("l2") or {}).get("vni"),
            "l2_vni_source": (evpn.get("l2") or {}).get("vni_source") or "",
            "l3_vni": l3_vni["vni"],
            "l3_vni_source": l3_vni["source"],
            "status": status,
        },
        "summary": {
            "findings": counts,
            "high_risks": len([f for f in findings if f["kind"] == "risk" and f["severity"] == "high"]),
            "endpoints": ((report.get("endpoint_table") or {}).get("summary") or {}).get("active_ips", 0),
            "steps": len(steps),
            "steps_ready": len([s for s in steps if s["status"] == "ready"]),
        },
        "coverage": _coverage(report, findings),
        "switches": _switches(report),
        "gateway": _gateway(report),
        "routing": report.get("routing_detail") or {},
        "endpoints": _endpoints(report),
        "findings": sorted(findings, key=lambda f: (list(_FINDING_PREFIX).index(f["kind"]), _SEVERITY_RANK.get(f["severity"], 9), f["id"])),
        "steps": steps,
        "acceptance": _acceptance(report, steps),
        "files": files,
    }


def build_run_index(mop_inputs, run=None, manifest=None):
    """One index for a run that covered one or more VLANs.

    ``{schema, run, vlans: [...], order, shared_switches, totals, files}``.
    *order* puts ready VLANs first, then those waiting on decisions, then
    blocked ones. *shared_switches* names switches more than one VLAN of the
    run changes, so their steps can be batched or sequenced.
    """
    vlans = []
    touched = {}
    for doc in mop_inputs or []:
        vlan = doc.get("vlan") or {}
        hosts = _unique(step.get("switch") for step in doc.get("steps") or [] if step.get("switch"))
        for host in hosts:
            touched.setdefault(host, []).append(vlan.get("id"))
        vlans.append(
            {
                "id": vlan.get("id"),
                "name": vlan.get("name"),
                "migration_type": vlan.get("migration_type"),
                "status": vlan.get("status"),
                "findings": (doc.get("summary") or {}).get("findings") or {},
                "high_risks": (doc.get("summary") or {}).get("high_risks", 0),
                "endpoints": (doc.get("summary") or {}).get("endpoints", 0),
                "switches_changed": hosts,
                "mop_input": (doc.get("files") or {}).get("mop_input", ""),
            }
        )
    rank = {"ready": 0, "needs_decisions": 1, "blocked": 2}
    order = [v["id"] for v in sorted(vlans, key=lambda v: (rank.get(v["status"], 3), v["id"] or 0))]
    return {
        "schema": RUN_INDEX_SCHEMA,
        "schema_version": MOP_SCHEMA_VERSION,
        "run": normalize_run(run),
        "vlans": sorted(vlans, key=lambda v: v["id"] or 0),
        "order": order,
        "shared_switches": {host: ids for host, ids in sorted(touched.items()) if len(ids) > 1},
        "totals": {
            "vlans": len(vlans),
            "ready": len([v for v in vlans if v["status"] == "ready"]),
            "needs_decisions": len([v for v in vlans if v["status"] == "needs_decisions"]),
            "blocked": len([v for v in vlans if v["status"] == "blocked"]),
            "endpoints": sum(v["endpoints"] or 0 for v in vlans),
            "switches_changed": len(touched),
        },
        "files": list(manifest or []),
    }


def file_manifest(paths, base=""):
    """``[{path, bytes, sha256}]`` for files that exist; *path* is relative to *base*."""
    out = []
    for path in sorted(set(paths or [])):
        if not path or not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        out.append(
            {
                "path": os.path.relpath(path, base) if base else path,
                "bytes": os.path.getsize(path),
                "sha256": digest.hexdigest(),
            }
        )
    return out


def _settled_by_netbox(finding, netbox_vni):
    if finding.get("category") != "vni":
        return False
    topic = finding.get("topic")
    if topic in ("proposed", "missing"):
        return True
    return topic == "existing_mapping" and finding.get("vni") == netbox_vni


def migrate_readiness(mop_inputs, record=None, decisions_reviewed=False, netbox_vni=None):
    """Can the migration go on from discovery to the deploy? ``{ready, reasons, ...}``.

    Stops on: no discovery report; a VLAN that is not in the VLAN DB (a
    discovery probe: an engineer writes the record from the discovery
    snippet first); any blocker; any open decision unless
    *decisions_reviewed*. High risks never stop it; they are listed. A VNI from NetBox
    (*netbox_vni*) settles discovery's VNI proposal and its "no VNI" blocker,
    and an existing VXLAN mapping to that same VNI; a mapping to another VNI
    stays a decision.
    """
    docs = [d for d in mop_inputs or [] if isinstance(d, dict)]
    record = record if isinstance(record, dict) else {}
    try:
        netbox_vni = int(netbox_vni) if netbox_vni not in (None, "", False) else None
    except (TypeError, ValueError):
        netbox_vni = None
    reasons = []
    blockers, decisions, risks = [], [], []
    for doc in docs:
        vlan = (doc.get("vlan") or {}).get("id")
        for finding in doc.get("findings") or []:
            line = "VLAN %s [%s] %s" % (vlan, finding.get("id"), finding.get("text"))
            if netbox_vni is not None and _settled_by_netbox(finding, netbox_vni):
                continue
            if finding.get("kind") == "blocker":
                blockers.append(line)
            elif finding.get("kind") == "decision":
                decisions.append(line)
            elif finding.get("kind") == "risk" and finding.get("severity") == "high":
                risks.append(line)
    if not docs:
        reasons.append("Discovery wrote no report for the VLAN, so there is nothing to check.")
    if record.get("_probe"):
        reasons.append(
            "VLAN %s is not in the VLAN DB (vars/vlans/<dc>/). Review the discovery report, put the VLAN DB "
            "snippet discovery wrote there, decide what it leaves open (gateway mode and leaves, VNI), and run "
            "again." % record.get("id")
        )
    if blockers:
        reasons.append("%d blocker(s) must be fixed first (see below)." % len(blockers))
    if decisions and not decisions_reviewed:
        reasons.append(
            "%d decision(s) are open. Answer them in the VLAN record, then run again with "
            "-e migrate_decisions_reviewed=true." % len(decisions)
        )
    return {
        "ready": not reasons,
        "reasons": reasons,
        "blockers": blockers,
        "decisions": decisions,
        "high_risks": risks,
        "decisions_reviewed": bool(decisions_reviewed),
    }


def prune_readiness(reports):
    """What the legacy prune can do now, per switch: ``{plans, ready, blocked}``.

    *reports* are discovery reports (``prune_plans`` and
    ``link_prune_plans``). A plan with CLI in ``actions`` is ready; one whose
    CLI sits in ``blocked_actions`` keeps its status (VNI not live yet,
    gateway not in the fabric, SVI still the active gateway). Plans with
    nothing to remove are left out. Ordered by phase, then switch.
    """
    rows = []
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        for plan in report.get("prune_plans") or []:
            if not isinstance(plan, dict):
                continue
            if plan.get("placement_role") not in ("prune_eligible", "link_prune"):
                continue
            if not plan.get("actions") and not plan.get("blocked_actions"):
                continue
            ready = bool(plan.get("actions"))
            rows.append({
                "hostname": plan.get("hostname") or "",
                "vlan_ids": list(plan.get("vlan_ids") or []),
                "kind": "link" if plan.get("placement_role") == "link_prune" else "teardown",
                "phase": plan.get("phase") or 3,
                "status": "ready" if ready else (plan.get("status") or "blocked"),
                "reason": "" if ready else (plan.get("blocked_reason") or ""),
            })
    rows.sort(key=lambda row: (row["phase"], row["hostname"]))
    return {
        "plans": rows,
        "ready": [row["hostname"] for row in rows if row["status"] == "ready"],
        "blocked": [row["hostname"] for row in rows if row["status"] != "ready"],
    }


def gateway_validated_vlans(reports_dir, data_center, vlan_ids, override=None):
    """VLAN IDs whose new gateway is validated, which releases the source gateways' prune.

    *override* (``-e new_gateway_validated``): true validates every VLAN in
    *vlan_ids*, a list (or ``3039,3040``) names the VLANs, and false or empty
    reads the record:
    the newest ``reports/<dc>/_runs/migrate_vlan<id>_<ts>.json`` of each VLAN
    must have outcome ``deployed_and_verified`` and an endpoint comparison
    with no problems.
    """
    import glob
    import json

    ids = [str(int(vid)) for vid in vlan_ids or [] if str(vid).strip().isdigit()]
    if isinstance(override, str) and override.replace(",", "").replace(" ", "").isdigit():
        override = [item for item in override.split(",") if item.strip()]  # -e new_gateway_validated=3039,3040
    if isinstance(override, (list, tuple, set)):
        return sorted({int(vid) for vid in override if str(vid).strip().isdigit()})
    if str(override).strip().lower() in ("true", "yes", "1"):
        return sorted(int(vid) for vid in ids)
    validated = []
    for vid in ids:
        pattern = os.path.join(str(reports_dir or ""), str(data_center or ""), "_runs", "migrate_vlan%s_*.json" % vid)
        paths = sorted(glob.glob(pattern))
        if not paths:
            continue
        try:
            with open(paths[-1], encoding="utf-8") as handle:
                summary = json.load(handle)
        except (OSError, ValueError):
            continue
        endpoints = summary.get("endpoints") or {}
        if (
            summary.get("outcome") == "deployed_and_verified"
            and endpoints.get("compared")
            and int(endpoints.get("problems") or 0) == 0
        ):
            validated.append(int(vid))
    return validated
