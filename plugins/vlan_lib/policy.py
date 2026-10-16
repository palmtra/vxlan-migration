# -*- coding: utf-8 -*-
"""Routing policy parsers and evaluation for VLAN prefix advertisement.

Discovery asks one question of each policy: does it let this VLAN prefix
through? Prefix-lists and route-maps are evaluated in sequence order. A
route-map clause that matches on anything discovery cannot evaluate (ACL,
community, tag on a connected route) gives ``unknown`` instead of a guess.
"""

import re

from vlan_lib.parsers import _dotted_mask_to_prefix_len, network_prefix

_PREFIX_ENTRY_RE = re.compile(
    r"^(?:seq\s+(?P<seq>\d+)\s+)?(?P<action>permit|deny)\s+(?P<prefix>\d+\.\d+\.\d+\.\d+/\d+)"
    r"(?:\s+ge\s+(?P<ge>\d+))?(?:\s+le\s+(?P<le>\d+))?(?:\s+ge\s+(?P<ge2>\d+))?",
    re.I,
)


def _ipv4_int(text):
    parts = str(text or "").split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return None
    if any(part < 0 or part > 255 for part in octets):
        return None
    return (octets[0] << 24) + (octets[1] << 16) + (octets[2] << 8) + octets[3]


def _split_prefix(prefix):
    text = network_prefix(str(prefix or "").strip())
    if "/" not in text:
        return None
    address, plen = text.split("/", 1)
    value = _ipv4_int(address)
    try:
        length = int(plen)
    except ValueError:
        return None
    if value is None or not 0 <= length <= 32:
        return None
    return value, length


def prefix_within(inner, outer):
    """True when *inner* is equal to or more specific than *outer*."""
    a = _split_prefix(inner)
    b = _split_prefix(outer)
    if not a or not b or a[1] < b[1]:
        return False
    mask = (0xFFFFFFFF << (32 - b[1])) & 0xFFFFFFFF if b[1] else 0
    return (a[0] & mask) == (b[0] & mask)


def parse_prefix_lists(text):
    """Parse ``ip prefix-list`` in one-line (NXOS/IOS) and block (EOS) form.

    Returns ``{name: [{seq, action, prefix, ge, le}, ...]}`` in sequence order.
    """
    lists = {}
    current = None
    auto_seq = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("!"):
            continue
        head = re.match(r"^ip\s+prefix-list\s+(\S+)(?:\s+(.*))?$", line, re.I)
        if head and not raw_line[:1].isspace():
            name, rest = head.group(1), (head.group(2) or "").strip()
            lists.setdefault(name, [])
            current = name
            if not rest or rest.lower().startswith("description"):
                continue
            entry_text = rest
        elif current and raw_line[:1].isspace():
            entry_text = line
        else:
            current = None
            continue
        match = _PREFIX_ENTRY_RE.match(entry_text)
        if not match:
            continue
        seq = match.group("seq")
        if seq is None:
            auto_seq[current] = auto_seq.get(current, 0) + 5
            seq = auto_seq[current]
        lists[current].append(
            {
                "seq": int(seq),
                "action": match.group("action").lower(),
                "prefix": network_prefix(match.group("prefix")),
                "ge": int(match.group("ge") or match.group("ge2") or 0) or None,
                "le": int(match.group("le")) if match.group("le") else None,
            }
        )
    for name in lists:
        lists[name].sort(key=lambda item: item["seq"])
    return lists


def _prefix_entry_matches(entry, prefix):
    parsed = _split_prefix(prefix)
    base = _split_prefix(entry.get("prefix"))
    if not parsed or not base or not prefix_within(prefix, entry.get("prefix")):
        return False
    length = parsed[1]
    ge = entry.get("ge")
    le = entry.get("le")
    if ge is None and le is None:
        return length == base[1]
    low = ge if ge is not None else base[1]
    high = le if le is not None else 32
    return low <= length <= high


def evaluate_prefix_list(entries, prefix):
    """Return ``(action, entry)`` for the first matching entry, or ``('deny', None)``."""
    for entry in entries or []:
        if _prefix_entry_matches(entry, prefix):
            return entry["action"], entry
    return "deny", None


def parse_route_maps(text):
    """Parse top-level ``route-map NAME permit|deny SEQ`` clauses.

    Returns ``{name: [{seq, action, matches: [{type, values}], sets: [...]}]}``.
    Lines under ``router bgp`` that merely reference a route-map are ignored.
    """
    maps = {}
    current = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("!"):
            continue
        head = re.match(r"^route-map\s+(\S+)(?:\s+(permit|deny))?(?:\s+(\d+))?\s*$", raw_line, re.I)
        if head:
            current = {
                "seq": int(head.group(3) or 10),
                "action": (head.group(2) or "permit").lower(),
                "matches": [],
                "sets": [],
            }
            maps.setdefault(head.group(1), []).append(current)
            continue
        if not raw_line[:1].isspace():
            current = None
            continue
        if current is None:
            continue
        match = re.match(r"^match\s+(.+)$", line, re.I)
        if match:
            body = match.group(1).strip()
            pl = re.match(r"^ip\s+address\s+prefix-list\s+(.+)$", body, re.I)
            if pl:
                current["matches"].append(
                    {"type": "prefix-list", "values": pl.group(1).split()}
                )
                continue
            words = body.split()
            if words[0].lower() == "interface":
                current["matches"].append({"type": "interface", "values": words[1:]})
            elif words[0].lower() == "tag":
                current["matches"].append({"type": "tag", "values": words[1:]})
            else:
                current["matches"].append({"type": "other", "values": [body]})
            continue
        if line.lower().startswith("set "):
            current["sets"].append(line[4:].strip())
    for name in maps:
        maps[name].sort(key=lambda item: item["seq"])
    return maps


def _iface_key(name):
    text = re.sub(r"\s+", "", str(name or "").lower())
    return re.sub(r"^vlan", "vl", text)


def evaluate_route_map(name, prefix, route_maps, prefix_lists, context=None):
    """Does route-map *name* permit *prefix*?

    ``context`` may carry ``interface`` (the SVI for a connected route) and
    ``tag`` (a static route tag). Returns ``{verdict, reason}`` where verdict
    is ``permit``, ``deny``, ``unknown``, ``missing``, or ``none`` (no policy).
    """
    context = context or {}
    if not name:
        return {"verdict": "none", "reason": "no route-map"}
    clauses = (route_maps or {}).get(name)
    if clauses is None:
        return {"verdict": "missing", "reason": "route-map %s is not defined" % name}
    for clause in clauses:
        result = True
        why = []
        for match in clause.get("matches") or []:
            kind = match.get("type")
            values = match.get("values") or []
            if kind == "prefix-list":
                hit = None
                for pl_name in values:
                    entries = (prefix_lists or {}).get(pl_name)
                    if entries is None:
                        why.append("prefix-list %s is not defined" % pl_name)
                        continue
                    action, entry = evaluate_prefix_list(entries, prefix)
                    if action == "permit":
                        hit = True
                        why.append(
                            "%s seq %s permit %s" % (pl_name, entry["seq"], entry["prefix"])
                        )
                        break
                    hit = False
                if hit is None:
                    # Platforms differ on an undefined prefix-list; do not guess.
                    result = None
                elif not hit:
                    result = False
                    break
            elif kind == "interface" and context.get("interface"):
                if _iface_key(context["interface"]) not in [_iface_key(v) for v in values]:
                    result = False
                    break
                why.append("match interface %s" % context["interface"])
            elif kind == "tag" and context.get("tag"):
                if str(context["tag"]) not in [str(v) for v in values]:
                    result = False
                    break
                why.append("match tag %s" % context["tag"])
            else:
                result = None
                why.append("match %s %s" % (kind, " ".join(values)))
        if result is False:
            continue
        if result is None:
            return {
                "verdict": "unknown",
                "reason": "%s seq %s matches on %s; check by hand"
                % (name, clause["seq"], "; ".join(why)),
            }
        detail = "; ".join(why) if why else "no match clause"
        return {
            "verdict": clause["action"],
            "reason": "%s seq %s %s (%s)" % (name, clause["seq"], clause["action"], detail),
        }
    return {"verdict": "deny", "reason": "%s has no clause for %s (implicit deny)" % (name, prefix)}


def parse_ospf_context(text):
    """Parse ``router ospf`` blocks: networks, redistribution, passive interfaces.

    NXOS ``redistribute direct`` is reported as source ``connected``.
    """
    processes = []
    current = None
    vrf_indent = None
    base = None
    for raw_line in str(text or "").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("!"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        head = re.match(r"^router\s+ospf\s+(\S+)(?:\s+vrf\s+(\S+))?", line, re.I)
        if head and indent == 0:
            base = {"process": head.group(1), "vrf": head.group(2) or "default"}
            current = dict(base, networks=[], redistribute=[], passive=[])
            processes.append(current)
            vrf_indent = None
            continue
        if indent == 0:
            current = None
            base = None
            continue
        if current is None:
            continue
        vrf = re.match(r"^vrf\s+(\S+)$", line, re.I)
        if vrf:
            current = dict(base, vrf=vrf.group(1), networks=[], redistribute=[], passive=[])
            processes.append(current)
            vrf_indent = indent
            continue
        if vrf_indent is not None and indent <= vrf_indent:
            vrf_indent = None
        net = re.match(
            r"^network\s+(\d+\.\d+\.\d+\.\d+)(?:/(\d+)|\s+(\d+\.\d+\.\d+\.\d+))\s+area\s+(\S+)",
            line,
            re.I,
        )
        if net:
            if net.group(2):
                prefix = network_prefix("%s/%s" % (net.group(1), net.group(2)))
            else:
                wildcard = _ipv4_int(net.group(3))
                mask = "%d.%d.%d.%d" % tuple(
                    255 - int(part) for part in net.group(3).split(".")
                ) if wildcard is not None else ""
                plen = _dotted_mask_to_prefix_len(mask) if mask else None
                prefix = network_prefix("%s/%s" % (net.group(1), plen)) if plen is not None else net.group(1)
            current["networks"].append({"prefix": prefix, "area": net.group(4)})
            continue
        redist = re.match(r"^redistribute\s+(\S+)(?:.*?\broute-map\s+(\S+))?", line, re.I)
        if redist:
            source = redist.group(1).lower()
            current["redistribute"].append(
                {
                    "source": "connected" if source == "direct" else source,
                    "route_map": redist.group(2) or "",
                }
            )
            continue
        passive = re.match(r"^passive-interface\s+(\S+)", line, re.I)
        if passive:
            current["passive"].append(passive.group(1))
    return processes


def parse_bgp_prefix_detail(text):
    """Parse ``show ip bgp <prefix> vrf <vrf>`` (EOS / NXOS).

    Returns ``{present, advertised_to, local}``. ``local`` means this switch
    originates the prefix (network, redistribute, or aggregate).
    """
    body = str(text or "")
    result = {"present": False, "advertised_to": [], "local": False}
    if not re.search(r"(?i)BGP routing table entry for", body):
        return result
    result["present"] = True
    result["local"] = bool(
        re.search(r"(?i)locally originated|\bsourced\b|path type:\s*(?:redist|local|aggr)|^\s+Local\s*$", body, re.M)
    )
    lines = body.splitlines()
    collecting = False
    for line in lines:
        if re.search(r"(?i)advertised to (?:update-groups|peers)\s*:?\s*$", line):
            collecting = True
            continue
        if not collecting:
            continue
        addresses = re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", line)
        if not addresses or not line[:1].isspace():
            collecting = False
            continue
        for address in addresses:
            if address not in result["advertised_to"]:
                result["advertised_to"].append(address)
    return result


def collect_command_errors(results):
    """Return ``[{command, error}]`` for failed ``cli_command`` results.

    *results* is a list of registered results, or a ``{label: result}`` dict
    so each error names its command (registered results do not always keep
    the invocation). Loop results are walked in order. Discovery tasks use
    ``failed_when: false``, which resets ``failed`` to false, so a result with
    a ``msg`` and no ``stdout`` is also an error. Skipped tasks are not errors.
    """
    if isinstance(results, dict):
        pairs = list(results.items())
    else:
        pairs = [("", item) for item in (results or [])]
    errors = []

    def visit(label, item):
        if not isinstance(item, dict) or item.get("skipped"):
            return
        if isinstance(item.get("results"), list):
            for child in item["results"]:
                visit(label, child)
            return
        if not item.get("failed") and ("stdout" in item or not item.get("msg")):
            return
        args = (item.get("invocation") or {}).get("module_args") or {}
        command = args.get("command") or label
        if item.get("item") not in (None, ""):
            command = "%s %s" % (label, item["item"]) if label else item["item"]
        errors.append(
            {
                "command": str(command or "command"),
                "error": str(item.get("msg") or "failed").strip()[:300],
            }
        )

    for label, item in pairs:
        visit(label, item)
    return errors
