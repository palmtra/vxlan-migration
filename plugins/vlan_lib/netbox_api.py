# -*- coding: utf-8 -*-
"""A small NetBox REST client for the action plugins (control node only).

Not imported by vlan_lib/__init__ (it needs ansible.module_utils). Errors
carry the HTTP status and NetBox's reason, never the token.
"""

import json
from urllib.parse import parse_qsl, urlencode, urlsplit

from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.urls import open_url

_PAGE = 1000


class NetboxError(Exception):
    """``str()`` names the request; ``reason`` is the status and NetBox's answer alone."""

    def __init__(self, message, reason=""):
        super(NetboxError, self).__init__(message)
        self.reason = reason or message


class Netbox(object):
    def __init__(self, url, token, validate_certs, timeout):
        self.base = str(url or "").rstrip("/")
        self.headers = {"Authorization": "Token %s" % token, "Accept": "application/json"}
        self.validate_certs = validate_certs
        self.timeout = timeout

    def request(self, path, params=None, method="GET", body=None, missing_ok=False):
        url = "%s/api/%s" % (self.base, path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        headers = dict(self.headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        try:
            response = open_url(
                url, data=data, headers=headers, method=method,
                validate_certs=self.validate_certs, timeout=self.timeout,
            )
            return json.loads(response.read() or b"{}")
        except HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            detail = ""
            try:
                payload = json.loads(exc.read() or b"{}")
                detail = payload.get("detail") or json.dumps(payload, sort_keys=True)[:300]
            except (ValueError, AttributeError):
                pass
            hint = ""
            if exc.code in (401, 403):
                hint = ". Check netbox_token and its permissions"
            elif exc.code == 404:
                hint = ". Check netbox_url: it should be the NetBox address without /api"
            reason = "HTTP %s%s%s" % (exc.code, (": %s" % detail) if detail else "", hint)
            raise NetboxError("%s /api/%s: %s" % (method, path, reason), reason)
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                raise NetboxError(
                    "the NetBox TLS certificate is not trusted (%s). Install the CA that signed it on this host, "
                    "or set netbox_validate_certs: false" % reason
                )
            raise NetboxError("no HTTP answer from NetBox for /api/%s: %s" % (path, reason),
                              "no HTTP answer from NetBox: %s" % reason)

    def all(self, path, params, missing_ok=False):
        """Every result of a list query, following ``next``."""
        params = dict(params, limit=_PAGE)
        page = self.request(path, params, missing_ok=missing_ok)
        if page is None:
            return None
        results = list(page.get("results") or [])
        while page.get("next"):
            nxt = urlsplit(page["next"])
            # NetBox may answer with its own host name; keep ours.
            page = self.request(nxt.path.split("/api/", 1)[-1], dict(parse_qsl(nxt.query, keep_blank_values=True)))
            results.extend(page.get("results") or [])
        return results


def _query(text):
    from urllib.parse import parse_qsl

    return parse_qsl(text, keep_blank_values=True)
