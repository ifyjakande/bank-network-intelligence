"""Small HTTP API for driving the demo by hand.

    GET    /health
    GET    /faults                 active and scheduled faults
    POST   /faults                 {"kind", "target", "duration_s", "latency_ms", "loss",
                                    "server_factor", "label"}
    DELETE /faults/<id>            end a fault now
    GET    /targets?type=circuit   valid target ids (first 50)
    GET    /load                   current load factor
    POST   /load                   {"factor": 20}  e.g. for a burst test

Not meant to be public: bind it inside the compose network / reach it via SSM port forwarding.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .demand import Demand
from .faults import FaultRegistry, FaultSpec, known_ids
from .topology import Estate


def _targets(estate: Estate, ttype: str) -> list[str]:
    try:
        return [f"{ttype}:{i}" for i in sorted(known_ids(estate, ttype))[:50]]
    except ValueError:
        return []


def serve(
    port: int, estate: Estate, registry: FaultRegistry, demand: Demand
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: object) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/health":
                self._send(200, {"ok": True})
            elif url.path == "/faults":
                self._send(200, [f.as_dict() for f in registry.snapshot()])
            elif url.path == "/targets":
                ttype = parse_qs(url.query).get("type", ["circuit"])[0]
                self._send(200, _targets(estate, ttype))
            elif url.path == "/load":
                self._send(200, {"factor": demand.load_factor})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                body = self._body()
                if self.path == "/faults":
                    now = int(time.time() * 1e6)
                    spec = FaultSpec(
                        kind=str(body["kind"]),
                        target=str(body["target"]),
                        latency_ms=float(body.get("latency_ms", 0)),
                        loss=float(body.get("loss", 0)),
                        server_factor=float(body.get("server_factor", 1)),
                        label=str(body.get("label", "")),
                    )
                    duration_us = int(float(body.get("duration_s", 300)) * 1e6)
                    f = registry.add(spec, now, now + duration_us, source="api")
                    self._send(201, f.as_dict())
                elif self.path == "/load":
                    factor = float(body["factor"])
                    if not 0 < factor <= 500:
                        raise ValueError("factor must be in (0, 500]")
                    demand.load_factor = factor
                    self._send(200, {"factor": factor})
                else:
                    self._send(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

        def do_DELETE(self) -> None:
            if not self.path.startswith("/faults/"):
                self._send(404, {"error": "not found"})
                return
            f = registry.cancel(self.path.rsplit("/", 1)[1], int(time.time() * 1e6))
            self._send(200 if f else 404, f.as_dict() if f else {"error": "no such fault"})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="control-api").start()
    return server
