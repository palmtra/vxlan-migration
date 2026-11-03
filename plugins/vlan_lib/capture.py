# -*- coding: utf-8 -*-
"""Validation data capture: file naming and secret removal.

A capture holds what a run read from the switches, NetBox and CloudVision,
so parsers and API field names can be checked against real data. Switch
outputs use the mock fixture layout (``devices/<host>/<slug>.txt``), so a
capture replays through the mock CLI as is.

Secrets are removed before anything is written: on a config line, every
word after a secret keyword (password, secret, key, SNMP community, ...); in
JSON, the value of any key that names a secret. Hostnames, addresses and
MACs stay: the checks need them.
"""

import re

_SECRET_WORD = re.compile(
    r"(?i)(?<![\w-])(password|secret|key|key-string|passphrase|md5|auth-key|authentication-key|"
    r"pre-shared-key|psk|token|bearer)(?![\w-])"
)
# BGP communities are routing data; only SNMP communities are secrets.
_SNMP_COMMUNITY = re.compile(r"(?i)\bsnmp.*?(?<![\w-])(community)(?![\w-])")
_SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|community|private[_-]?key)")
REMOVED = "<removed>"


def command_slug(command):
    """File name for a command, the same as the mock CLI uses."""
    return re.sub(r"[^a-z0-9]+", "_", str(command).lower()).strip("_")


def redact_text(text):
    """*text* with everything after a secret keyword on a line removed.

    ``neighbor 10.0.0.1 password 7 0822455D0A16`` becomes
    ``neighbor 10.0.0.1 password <removed>``; JSON-looking lines keep their
    quotes balanced (``"password": "<removed>"``).
    """
    if not isinstance(text, str) or not text:
        return text
    out = []
    for line in text.split("\n"):
        match = _SECRET_WORD.search(line) or _SNMP_COMMUNITY.search(line)
        # Port descriptions are free text the role detection reads.
        if match and not re.search(r"(?i)description", line[: match.start(1)]):
            end = match.end(1)
            rest = line[end:]
            if rest.lstrip().startswith(('":', "':", '"', ":")):
                # "password": "abc", -> "password": "<removed>",
                line = line[:end] + re.sub(r'(:\s*)("[^"]*"|[^,}\s]+)', r'\1"' + REMOVED + '"', rest, count=1)
            elif rest.strip():
                line = line[:end] + " " + REMOVED
        out.append(line)
    return "\n".join(out)


def redact_data(data):
    """A copy of *data* with secret values removed (keys by name, strings by line)."""
    if isinstance(data, dict):
        return {
            key: (REMOVED if _SECRET_KEY.search(str(key)) and value not in (None, "", [], {}) else redact_data(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, str) and "\n" in data:
        return redact_text(data)
    return data
