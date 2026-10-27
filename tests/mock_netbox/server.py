#!/usr/bin/env python3
"""A tiny stand-in for the NetBox REST API, for tests/run_netbox_rt_mock.sh.

    server.py <port> <seed.json> <writes.jsonl>

Serves ``/api/<app>/<model>/`` from the seed: GET filters by exact match on
every query parameter, POST adds an object, PATCH ``.../<id>/`` merges into
one. Every POST and PATCH is appended to the writes file as one JSON line
(method, path, body). Requests without ``Authorization: Token test`` get 403.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "Token test"


def main():
    port, seed_file, writes_file = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    with open(seed_file, encoding="utf-8") as handle:
        store = json.load(handle)
    next_id = [1000]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            """``(collection key, object id or None)`` for /api/<app>/<model>/[<id>/]."""
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            if len(parts) < 3 or parts[0] != "api":
                return None, None
            key = "%s/%s" % (parts[1], parts[2])
            return key, (int(parts[3]) if len(parts) > 3 else None)

        def _authorized(self):
            if self.headers.get("Authorization") != TOKEN:
                self._reply(403, {"detail": "Invalid token"})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _record_write(self, body):
            with open(writes_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"method": self.command, "path": urlsplit(self.path).path, "body": body}) + "\n")

        def do_GET(self):
            if not self._authorized():
                return
            key, _ = self._route()
            query = parse_qs(urlsplit(self.path).query)
            results = [
                obj for obj in store.get(key, [])
                if all(str(obj.get(field)) == values[0] for field, values in query.items())
            ]
            self._reply(200, {"count": len(results), "results": results})

        def do_POST(self):
            if not self._authorized():
                return
            key, _ = self._route()
            body = self._body()
            self._record_write(body)
            next_id[0] += 1
            obj = dict(body, id=next_id[0])
            store.setdefault(key, []).append(obj)
            self._reply(201, obj)

        def do_PATCH(self):
            if not self._authorized():
                return
            key, obj_id = self._route()
            body = self._body()
            self._record_write(body)
            for obj in store.get(key, []):
                if obj.get("id") == obj_id:
                    obj.update(body)
                    self._reply(200, obj)
                    return
            self._reply(404, {"detail": "Not found."})

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
