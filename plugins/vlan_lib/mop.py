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

from vlan_lib.reports import build_findings

MOP_SCHEMA = "vxlan-migration/mop-input"
MOP_SCHEMA_VERSION = "1.0"
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
# Change order: deploy the VNI, prune the MLS links (re-run discovery), the
# non-VTEP side, move the gateway, then tear down the legacy VLAN.
_PRUNE_PHASE_NAMES = {1: "2-link-prune-mls", 2: "3-link-prune-non-vtep", 3: "5-teardown"}


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


def _steps(report, findings, files):
    vlan_id = report.get("vlan_id")
    model = report.get("deployment_model") or {}
    placement = model.get("placement") or {}
    vni = ((model.get("evpn") or {}).get("l2") or {}).get("vni")
    gw = report.get("gateway_assessment") or {}
    gmac = report.get("gateway_mac") or {}
    l3 = report.get("migration_type") == "l3"
    os_by_host = {d.get("hostname"): (d.get("os_family") or "").lower() for d in report.get("devices") or []}
    steps = []

    blockers = [f["id"] for f in findings if f["kind"] == "blocker"]
    decisions = [f["id"] for f in findings if f["kind"] == "decision"]
    tasks = [f["id"] for f in findings if f["kind"] == "task"]
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
    if tasks:
        pre.append(
            _step(
                steps, vlan_id, phase="0-prechecks", title="Recreate the routing objects the configlet does not carry",
                status="ready", findings=tasks, cli_source="engineer (not generated)",
            )
        )

    gateway_hosts = _unique(_hostname(item) for item in placement.get("gateway_leafs") or [])
    leaves = _unique(
        [_hostname(item) for item in placement.get("participating_leafs") or []] + gateway_hosts
    )
    if l3 and not gateway_hosts:
        gateway_hosts = list(leaves)
    deploy = []
    for host in leaves:
        notes = []
        if l3 and host in gateway_hosts and gw.get("location") == "legacy":
            notes.append(
                "Vlan%s comes up with ip address virtual %s as soon as this configlet is applied, while the "
                "legacy gateway still answers. Apply it in the cutover window, or make sure the fabric and the "
                "legacy L2 are not bridged for this VLAN until the legacy SVI is shut." % (vlan_id, gw.get("virtual_ip") or model.get("gateway", {}).get("virtual_ip", ""))
            )
        verify = ["show vxlan vlan-to-vni | include %s  (expect VNI %s)" % (vlan_id, vni or "?")]
        if l3 and host in gateway_hosts:
            verify.append("show interfaces Vlan%s  (up, ip address virtual)" % vlan_id)
        deploy.append(
            _step(
                steps, vlan_id, phase="1-deploy", title="Deploy VLAN %s / VNI %s" % (vlan_id, vni or "?"), switch=host,
                status="blocked" if blockers else "ready", depends_on=list(pre),
                cli_source="configlet vxlan_vlan%s_%s.cfg written by playbooks/core/workflow_deploy.yml" % (vlan_id, host),
                rollback="Roll back the CVP change control (roles/cvp_rollback)",
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

    def prune_steps(phase, depends):
        ids = []
        for plan in by_phase.get(phase, []):
            execution = plan.get("execution") or {}
            ids.append(
                _step(
                    steps, vlan_id, phase=_PRUNE_PHASE_NAMES.get(phase, "5-teardown"),
                    title="%s: %s" % (plan.get("phase_label") or "prune", plan.get("hostname")),
                    switch=plan.get("hostname") or "",
                    status="ready" if plan.get("actions") else (plan.get("status") or "blocked"),
                    blocked_reason=plan.get("blocked_reason") or "",
                    cli=_plan_cli(plan), cli_source=files["prune_markdown"],
                    rollback=execution.get("rollback") or None,
                    save=execution.get("save") or "",
                    verify=["Re-run discovery for VLAN %s: %s shows the change" % (vlan_id, plan.get("hostname"))],
                    depends_on=list(depends),
                )
            )
        return ids

    phase1 = prune_steps(1, deploy or pre)
    phase2 = prune_steps(2, phase1 or deploy or pre)
    cutover = []
    if l3 and gw.get("location") == "legacy":
        for svi in gw.get("svis") or []:
            host = svi.get("hostname") or ""
            indent = "  " if os_by_host.get(host) == "nxos" else "   "
            verify = [
                "Ping every baseline endpoint from a fabric gateway leaf (playbooks/core/workflow_verify.yml)",
                "show ip arp vrf %s | include Vlan%s on a fabric gateway leaf: the endpoints are there"
                % (svi.get("vrf") or "default", vlan_id),
            ]
            notes = []
            if gmac.get("changes"):
                notes.append(
                    "Gateway MAC changes from %s to %s: hosts update their ARP entry for %s when the fabric "
                    "gateway ARPs for them (the ping above) or when it ages out."
                    % (", ".join(gmac.get("legacy_macs") or []), gmac.get("fabric_mac"), gw.get("virtual_ip") or "")
                )
            cutover.append(
                _step(
                    steps, vlan_id, phase="4-gateway-cutover", title="Shut the legacy gateway SVI", switch=host,
                    cli="interface Vlan%s\n%sshutdown" % (vlan_id, indent),
                    cli_source="mop_input (shut only; the SVI is removed in the teardown phase)",
                    rollback="interface Vlan%s\n%sno shutdown" % (vlan_id, indent),
                    verify=verify, notes=notes, depends_on=list(phase2 or phase1 or deploy or pre),
                )
            )
    teardown = prune_steps(3, cutover or phase2 or phase1 or deploy or pre)
    _step(
        steps, vlan_id, phase="6-verify", title="Post-change verification",
        cli_source="playbooks/core/workflow_verify.yml",
        verify=[
            "Every baseline host answers ping from the fabric (%s)" % files["endpoint_baseline"],
            "Re-run discovery: VLAN %s is VNI-mapped on %s and nothing is left to prune" % (vlan_id, ", ".join(leaves) or "the fabric leaves"),
        ],
        depends_on=list(teardown or cutover or phase2 or phase1 or deploy or pre),
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
    counts = {kind: len([f for f in findings if f["kind"] == kind]) for kind in _FINDING_PREFIX}
    status = "blocked" if counts["blocker"] else ("needs_decisions" if counts["decision"] else "ready")
    return {
        "schema": MOP_SCHEMA,
        "schema_version": MOP_SCHEMA_VERSION,
        "purpose": (
            "Input for writing the MOP for this VLAN. Facts come from read-only discovery. Use only the CLI in "
            "steps[].cli and the configlet files named in steps[].cli_source; cite IDs; do not invent commands."
        ),
        "run": run,
        "vlan": {
            "id": report.get("vlan_id"),
            "name": report.get("vlan_name") or "",
            "data_center": report.get("data_center") or "",
            "migration_type": report.get("migration_type") or "",
            "vrf": (report.get("gateway_assessment") or {}).get("vrf") or report.get("discovered_vrf") or "",
            "l2_vni": (evpn.get("l2") or {}).get("vni"),
            "l3_vni": (evpn.get("l3") or {}).get("vni"),
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
