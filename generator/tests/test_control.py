from __future__ import annotations

import json
import random
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from netgen import control
from netgen.demand import Demand
from netgen.faults import FaultRegistry
from netgen.topology import Estate


@pytest.fixture
def api(estate: Estate) -> Iterator[tuple[str, FaultRegistry, Demand]]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    reg = FaultRegistry(estate)
    demand = Demand(estate, random.Random(0), 1.0, 0)
    server = control.serve(port, estate, reg, demand)
    yield f"http://127.0.0.1:{port}", reg, demand
    server.shutdown()


def _call(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_fault_lifecycle(api: tuple[str, FaultRegistry, Demand]) -> None:
    url, reg, _ = api
    code, f = _call(
        "POST",
        f"{url}/faults",
        {
            "kind": "degrade",
            "target": "pop:MER-NTH",
            "latency_ms": 40,
            "loss": 0.01,
            "duration_s": 60,
        },
    )
    assert code == 201 and f["source"] == "api"
    code, listed = _call("GET", f"{url}/faults")
    assert [x["fault_id"] for x in listed] == [f["fault_id"]]
    code, _ = _call("DELETE", f"{url}/faults/{f['fault_id']}")
    assert code == 200
    assert not reg.snapshot()[0].active(f["start_us"] + 10**9)


def test_bad_requests_get_400(api: tuple[str, FaultRegistry, Demand]) -> None:
    url, _, _ = api
    assert _call("POST", f"{url}/faults", {"kind": "degrade", "target": "pop:NOPE"})[0] == 400
    assert _call("POST", f"{url}/faults", {"target": "pop:MER-NTH"})[0] == 400
    assert _call("POST", f"{url}/load", {"factor": 0})[0] == 400


def test_load_factor_can_be_changed(api: tuple[str, FaultRegistry, Demand]) -> None:
    url, _, demand = api
    assert _call("POST", f"{url}/load", {"factor": 25})[0] == 200
    assert demand.load_factor == 25
    assert _call("GET", f"{url}/targets?type=gateway")[1][0].startswith("gateway:")
