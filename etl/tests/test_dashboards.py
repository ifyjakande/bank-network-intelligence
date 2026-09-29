"""Every dashboard component, exercised through Grafana itself (`pytest -m integration`).

Runs each panel, variable and annotation query through Grafana's query API, as the
provisioned datasource (the locked-down grafana ClickHouse user), under every variable
mode, and checks the provisioned dashboards and alert rules are the committed ones.
Needs GRAFANA_ADMIN_PASSWORD and a stack with a few minutes of traffic.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
DASHBOARDS = sorted((REPO / "grafana" / "dashboards").glob("*.json"))
RULES = json.loads((REPO / "grafana" / "provisioning" / "alerting" / "rules.json").read_text())
GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000")

# panels that are legitimately empty on a quiet estate (no degradation in the window)
MAY_BE_EMPTY = {
    "Circuit evidence",
    "Injected incidents (ground truth)",
    "Ranked candidates",
    "Most likely cause",
    "Explains (coverage)",
    "Precision",
    "Devices hit",
    "Degraded since",
}


def _auth() -> str:
    token = base64.b64encode(f"admin:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode())
    return "Basic " + token.decode()


def _api(path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        f"{GRAFANA}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": _auth()},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def query(target: dict[str, Any], sql: str) -> tuple[str | None, list[dict[str, Any]]]:
    body = {"from": "now-30m", "to": "now", "queries": [{**target, "refId": "A", "rawSql": sql}]}
    res = _api("/api/ds/query", body).get("results", {}).get("A", {})
    return res.get("error"), res.get("frames", [])


def rows(frames: list[dict[str, Any]]) -> int:
    return sum(len((f.get("data", {}).get("values") or [[]])[0]) for f in frames)


def variable_values(dash: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for v in dash["templating"]["list"]:
        if v["type"] == "query":
            err, frames = query(
                {
                    "datasource": v["datasource"],
                    "format": 1,
                    "queryType": "table",
                    "editorType": "sql",
                },
                v["query"],
            )
            assert err is None, f"variable {v['name']}: {err}"
            out[v["name"]] = [str(x) for x in frames[0]["data"]["values"][0]]
            assert out[v["name"]], f"variable {v['name']} has no options"
        elif v["type"] == "custom":
            out[v["name"]] = [o["value"] for o in v["options"]]
    return out


def render(sql: str, values: dict[str, list[str]]) -> str:
    for name, chosen in values.items():
        sql = sql.replace("${" + name + ":singlequote}", ",".join(f"'{x}'" for x in chosen))
        sql = sql.replace("${" + name + "}", chosen[0])
    return sql


def modes(options: dict[str, list[str]]) -> list[tuple[str, dict[str, list[str]]]]:
    """All selected (Grafana expands it), one value, and several values, per variable."""
    out = [("all", {k: v for k, v in options.items()})]
    for name, vals in options.items():
        out.append((f"{name}=one", {**options, name: vals[:1]}))
        if len(vals) > 1:
            out.append((f"{name}=two", {**options, name: vals[:2]}))
    return out


def panels_with_targets(dash: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items = [(p["title"], t) for p in dash["panels"] for t in p.get("targets", [])]
    items += [("annotation", a["target"]) for a in dash["annotations"]["list"] if "target" in a]
    return items


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_provisioned_dashboard_is_the_committed_one(path: Path) -> None:
    committed = json.loads(path.read_text())
    served = _api(f"/api/dashboards/uid/{committed['uid']}")["dashboard"]
    assert served["title"] == committed["title"]
    assert [p["title"] for p in served["panels"]] == [p["title"] for p in committed["panels"]]
    assert served["links"] and all(link["includeVars"] for link in served["links"])


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_panel_answers_under_every_variable_mode(path: Path) -> None:
    dash = json.loads(path.read_text())
    options = variable_values(dash)
    failures = []
    for (mode, values), (title, target) in itertools.product(
        modes(options), panels_with_targets(dash)
    ):
        err, frames = query(target, render(target["rawSql"], values))
        if err:
            failures.append(f"[{mode}] {title}: {err[:160]}")
            continue
        expects_data = mode == "all" and title not in MAY_BE_EMPTY | {"annotation"}
        if expects_data and rows(frames) == 0:
            failures.append(f"[{mode}] {title}: no data")
        if target.get("format") == 0 and frames and rows(frames):
            kinds = {f.get("type") for f in frames[0]["schema"]["fields"]}
            if "time" not in kinds:
                failures.append(f"[{mode}] {title}: time series without a time field")
    assert not failures, "\n".join(failures)


def test_localisation_window_variants_all_answer() -> None:
    dash = json.loads((REPO / "grafana" / "dashboards" / "localisation.json").read_text())
    windows = next(v for v in dash["templating"]["list"] if v["name"] == "window")
    assert [o["value"] for o in windows["options"]] == ["5", "10", "15", "30", "60"]
    for window in ("5", "10", "15", "30", "60"):
        for title, target in panels_with_targets(dash):
            err, _ = query(target, render(target["rawSql"], {"window": [window]}))
            assert err is None, f"window={window} {title}: {err}"


def test_alert_rules_are_provisioned_and_healthy() -> None:
    expected = {r["uid"] for g in RULES["groups"] for r in g["rules"]}
    served = _api("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]
    rules = [r for g in served for r in g["rules"]]
    assert {r["name"] for r in rules} == {r["title"] for g in RULES["groups"] for r in g["rules"]}
    assert len(rules) == len(expected)
    unhealthy = [f"{r['name']}: {r.get('lastError')}" for r in rules if r["health"] != "ok"]
    assert not unhealthy, unhealthy


def test_every_alert_query_runs_and_has_one_value_column() -> None:
    # Grafana alerting turns string columns into labels and needs exactly one numeric
    # column per row; a second number fails evaluation even though the panel query works
    numeric = {"number", "int", "float"}
    for group in RULES["groups"]:
        for rule in group["rules"]:
            model = rule["data"][0]["model"]
            err, frames = query(model, model["rawSql"])
            assert err is None, f"{rule['title']}: {err}"
            fields = frames[0]["schema"]["fields"]
            values = [f["name"] for f in fields if f.get("type") in numeric]
            assert len(values) == 1, f"{rule['title']}: numeric columns {values}"
