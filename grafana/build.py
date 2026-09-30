"""Dashboards as code: writes grafana/dashboards/*.json.

    python3 grafana/build.py          # regenerate
    python3 grafana/build.py --check  # CI: fail if the committed JSON is stale

Palette: the data-viz reference palette, dark steps (validated against Grafana's dark
panel surface #181b1f: categorical slots 1-3 pass all-pairs CVD and contrast).
Categorical colours carry identity only; status colours carry quality state only and
always come with a text label.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dashboards"
LOCALISE_SQL = ROOT.parent / "clickhouse" / "queries" / "localise.sql"

DS = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}

# categorical (identity), fixed order
SLOT = ["#3987e5", "#d95926", "#199e70"]
# status (state), reserved
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
MUTED = "#8e8e8a"
INCIDENT = "#9085e9"

CRITICALITY_COLOURS = dict(zip(["critical", "business", "bulk"], SLOT, strict=True))
PROVIDER_COLOURS = dict(zip(["MER", "SAV", "CRS"], SLOT, strict=True))
CRITICAL_APP_COLOURS = dict(zip(["card_authorisation", "core_banking", "swift"], SLOT,
                                strict=True))

Panel = dict[str, Any]


# --- query + field helpers -----------------------------------------------------------

def sql_target(sql: str, table: bool = True) -> dict[str, Any]:
    return {
        "refId": "A", "datasource": DS, "editorType": "sql", "rawSql": sql.strip(),
        "format": 1 if table else 0, "queryType": "table" if table else "timeseries",
    }


def steps(*pairs: tuple[float | None, str]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in pairs]}


def colour_overrides(mapping: dict[str, str]) -> list[dict[str, Any]]:
    # long-format series arrive named "<value column> <label>", so match on the label
    return [
        {"matcher": {"id": "byRegexp", "options": f"^(.* )?{name}$"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}
        for name, colour in mapping.items()
    ]


def quality_mappings() -> list[dict[str, Any]]:
    # colour never carries the state alone: every band has a label
    return [{"type": "range", "options": {"from": lo, "to": hi,
                                          "result": {"text": text, "color": colour}}}
            for lo, hi, text, colour in ((90, 100, "Good", GOOD), (70, 89.999, "Fair", WARNING),
                                         (50, 69.999, "Degraded", SERIOUS),
                                         (0, 49.999, "Poor", CRITICAL))]


# no custom all-value: "All" expands to the real list, so any mix of selections filters
# correctly (a sentinel like 'All' silently turned "All + North" into no filter at all)
# the current minute is still being stitched: every panel stops at the last complete one
COMPLETE = "minute < toStartOfMinute(now())"
LAST5 = f"minute >= toStartOfMinute(now()) - INTERVAL 5 MINUTE AND {COMPLETE}"
REGION_FILTER = "region IN (${region:singlequote})"
PROVIDER_FILTER = "provider IN (${provider:singlequote})"
# branch views filter on the branch's primary provider, not the circuit its traffic is on
# right now: a MER branch that failed over to its backup must stay in the MER view
BRANCH_PROVIDER_FILTER = ("dictGet('netflow.branch', 'primary_provider', branch_id) "
                          "IN (${provider:singlequote})")


# --- panel builders ----------------------------------------------------------------

def stat(title: str, sql: str, x: int, y: int, w: int = 4, h: int = 4, unit: str = "none",
         decimals: int | None = 1, thresholds: dict[str, Any] | None = None,
         description: str = "", colour_mode: str = "value", text: bool = False) -> Panel:
    defaults: dict[str, Any] = {
        "unit": unit, "thresholds": thresholds or steps((None, MUTED)),
        "color": {"mode": "thresholds"},
        "noValue": "–",  # an empty window is not a status: no green/red "No data"
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [sql_target(sql)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text else "",
                              "values": False},
            "colorMode": colour_mode, "graphMode": "none", "justifyMode": "center",
            "textMode": "value", "wideLayout": True, "showPercentChange": False,
        },
    }


def timeseries(title: str, sql: str, x: int, y: int, w: int, h: int, unit: str = "none",
               colours: dict[str, str] | None = None, description: str = "",
               series_label: str | None = None, stacked: bool = False) -> Panel:
    custom: dict[str, Any] = {
        "drawStyle": "line", "lineWidth": 2, "lineInterpolation": "smooth",
        "fillOpacity": 35 if stacked else 10, "gradientMode": "opacity",
        "showPoints": "never", "spanNulls": True, "axisBorderShow": False, "axisSoftMin": 0,
        "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
    }
    defaults: dict[str, Any] = {"unit": unit, "custom": custom,
                                "color": {"mode": "palette-classic"}}
    if series_label:
        defaults["displayName"] = "${__field.labels." + series_label + "}"
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [sql_target(sql, table=False)],
        "fieldConfig": {"defaults": defaults, "overrides": colour_overrides(colours or {})},
        "options": {
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def table(title: str, sql: str, x: int, y: int, w: int, h: int, description: str = "",
          overrides: list[dict[str, Any]] | None = None, sort: str | None = None) -> Panel:
    return {
        "type": "table", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [sql_target(sql)],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                    "inspect": False},
                         "color": {"mode": "thresholds"}, "thresholds": steps((None, MUTED))},
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "cellHeight": "sm",
                    "sortBy": [{"displayName": sort, "desc": True}] if sort else [],
                    "footer": {"show": False}},
    }


def col(name: str, **props: Any) -> dict[str, Any]:
    mapping = {
        "unit": "unit", "decimals": "decimals", "width": "custom.width",
        "thresholds": "thresholds", "mappings": "mappings",
        "cell": "custom.cellOptions", "min": "min", "max": "max",
    }
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": mapping[k], "value": v} for k, v in props.items()]}


def gauge_cell() -> dict[str, Any]:
    return {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}


def text_cell() -> dict[str, Any]:
    return {"type": "color-text"}


def row(title: str, y: int) -> Panel:
    return {"type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def note(content: str, x: int, y: int, w: int, h: int) -> Panel:
    return {"type": "text", "title": "", "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": content}, "transparent": True}


def variable(name: str, label: str, sql: str) -> dict[str, Any]:
    return {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": sql, "definition": sql, "refresh": 2, "includeAll": True, "multi": True,
        "current": {"text": "All", "value": "$__all"}, "sort": 1,
    }


INCIDENT_ANNOTATION = {
    "name": "Injected incidents (ground truth)", "datasource": DS, "enable": True,
    "iconColor": INCIDENT,
    "target": sql_target("""
        SELECT started_at AS time, if(ongoing, now64(6), ended_at) AS timeEnd,
               label AS text, kind AS tags
        FROM netflow.incidents
        WHERE started_at <= $__toTime AND (ongoing OR ended_at >= $__fromTime)
    """),
}


def dashboard(uid: str, title: str, description: str, panels: list[Panel],
              variables: list[dict[str, Any]] | None = None, time_from: str = "now-1h",
              refresh: str = "30s", annotations: bool = True) -> dict[str, Any]:
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    anns = [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
             "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
             "name": "Annotations & Alerts", "type": "dashboard"}]
    if annotations:
        anns.append(INCIDENT_ANNOTATION)
    return {
        "uid": uid, "title": title, "description": description, "tags": ["bank-network"],
        "editable": False, "graphTooltip": 1, "schemaVersion": 41, "version": 1,
        "time": {"from": time_from, "to": "now"}, "refresh": refresh,
        "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m"]},
        "templating": {"list": variables or []},
        "annotations": {"list": anns},
        "links": [{"type": "dashboards", "tags": ["bank-network"], "asDropdown": False,
                   "includeVars": True, "keepTime": True, "title": "Dashboards"}],
        "panels": panels,
    }


# --- dashboards ---------------------------------------------------------------------

def estate() -> dict[str, Any]:
    last5 = LAST5
    p: list[Panel] = [
        note("### Branch estate · service quality\n"
             "Measured per session from encrypted-traffic metadata (no payloads, no "
             "decryption). *Reachability* is what up/down monitoring sees; the other tiles "
             "are what customers actually get. Violet bands are injected incidents.",
             0, 0, 24, 2),
        stat("Reachability (what ping says)", f"""
            WITH expected AS (
                SELECT branch_id FROM netflow.circuit_by_tunnel
                WHERE role = 'primary' AND {PROVIDER_FILTER}
                  AND dictGet('netflow.branch', 'region', branch_id) IN (${{region:singlequote}}))
            SELECT 100 * (SELECT uniqExact(branch_id) FROM netflow.quality_1m
                          WHERE {last5} AND branch_id IN (SELECT branch_id FROM expected))
                       / (SELECT count() FROM expected)
        """, 0, 2, unit="percent", decimals=1,
             thresholds=steps((None, CRITICAL), (99, GOOD)),
             description="Branches in the selection with any traffic in the last 5 minutes."),
        stat("Estate quality score", f"""
            SELECT sum(quality_sum) / sum(sessions) FROM netflow.quality_1m
            WHERE {last5} AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
        """, 4, 2, thresholds=steps((None, CRITICAL), (70, WARNING), (90, GOOD)),
             description="Mean 0-100 session score against each app's SLA targets, "
                         "last 5 minutes."),
        stat("Degraded sessions", f"""
            SELECT 100 * sum(degraded) / sum(sessions) FROM netflow.quality_1m
            WHERE {last5} AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
        """, 8, 2, unit="percent", decimals=2,
             thresholds=steps((None, GOOD), (1, WARNING), (5, CRITICAL)),
             description="Share of sessions scoring under 70, last 5 minutes."),
        stat("Branches degraded now", f"""
            SELECT count() FROM (
                SELECT branch_id FROM netflow.quality_1m
                WHERE {last5} AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
                GROUP BY branch_id
                HAVING sum(sessions) >= 10 AND sum(degraded) / sum(sessions) >= 0.2)
        """, 12, 2, decimals=0, thresholds=steps((None, GOOD), (3, WARNING), (10, CRITICAL)),
             # measured live: 0-2 in quiet periods, 3-40 during incidents
             description="Branches where at least 20% of sessions are degraded (min 10)."),
        stat("Devices affected", f"""
            SELECT 100 * uniqMerge(degraded_devices) / uniqMerge(devices) FROM netflow.quality_1m
            WHERE {last5} AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
        """, 16, 2, unit="percent", decimals=1,
             # a share, not a count: the count tracks traffic. Measured live: 0.1-1.8%
             # in quiet periods, 2.7-19% during incidents
             thresholds=steps((None, GOOD), (2, WARNING), (5, CRITICAL)),
             description="Share of active ATMs, terminals and workstations with a "
                         "degraded session, last 5 minutes."),
        stat("Card authorisation p95", f"""
            SELECT quantilesTDigestMerge(0.5, 0.95)(response_ms)[2] FROM netflow.quality_1m
            WHERE {last5} AND app = 'card_authorisation'
              AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
        """, 20, 2, unit="ms", decimals=0,
             thresholds=steps((None, GOOD), (300, WARNING), (900, CRITICAL)),
             description="95th percentile time from request to first response byte."),

        row("Quality over time", 6),
        timeseries("Degraded sessions by app criticality", f"""
            SELECT minute AS time, criticality,
                   100 * sum(degraded) / sum(sessions) AS degraded_pct
            FROM netflow.quality_1m
            WHERE $__timeFilter(minute) AND {COMPLETE} AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
            GROUP BY time, criticality ORDER BY time
        """, 0, 7, 12, 8, unit="percent", colours=CRITICALITY_COLOURS, series_label="criticality",
                   description="Share of sessions scoring under 70, per minute."),
        timeseries("p95 response time · critical apps", f"""
            SELECT minute AS time, app,
                   quantilesTDigestMerge(0.5, 0.95)(response_ms)[2] AS p95_ms
            FROM netflow.quality_1m
            WHERE $__timeFilter(minute) AND {COMPLETE} AND criticality = 'critical'
              AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
            GROUP BY time, app ORDER BY time
        """, 12, 7, 12, 8, unit="ms", colours=CRITICAL_APP_COLOURS, series_label="app"),

        row("Where it hurts", 15),
        table("Branches ranked by degraded share", f"""
            SELECT q.branch_id AS Branch, any(q.region) AS Region,
                   anyIf(q.provider, q.circuit_role = 'primary') AS Provider,
                   -- on backup now (last 3 complete minutes), not at any point in the range
                   if(sumIf(q.sessions, q.circuit_role = 'backup'
                            AND q.minute >= toStartOfMinute(now()) - INTERVAL 3 MINUTE) > 0,
                      'backup', 'primary') AS Circuit,
                   sum(q.sessions) AS Sessions,
                   round(100 * sum(q.degraded) / sum(q.sessions), 1) AS `Degraded %`,
                   -- the mean hides the tail (19% degraded still averages ~90), so it is
                   -- shown as a plain number, not a Good/Fair label; Degraded % ranks
                   round(sum(q.quality_sum) / sum(q.sessions), 1) AS `Mean score`,
                   -- blank when nothing degraded: argMax would pick an arbitrary app
                   -- degraded sessions summed per app across all rows; blank when none
                   if(sum(q.degraded) = 0, '', topKWeighted(1)(q.app, q.degraded)[1])
                       AS `Most affected app`,
                   round(quantilesTDigestMergeIf(0.5, 0.95)(q.response_ms,
                         q.app = 'card_authorisation')[2]) AS `Card p95 ms`,
                   uniqMerge(q.degraded_devices) AS `Devices hit`,
                   -- first minute the branch crossed the 20% line; blank if it never did
                   nullIf(minIf(m.minute, m.bad), toDateTime(0, 'UTC')) AS `Degraded since`
            FROM netflow.quality_1m AS q
            GLOBAL LEFT JOIN (
                SELECT branch_id, minute,
                       sum(degraded) >= 0.2 * sum(sessions) AND sum(sessions) >= 3 AS bad
                FROM netflow.quality_1m
                WHERE $__timeFilter(minute) GROUP BY branch_id, minute
            ) AS m ON m.branch_id = q.branch_id AND m.minute = q.minute
            WHERE $__timeFilter(q.minute) AND q.minute < toStartOfMinute(now()) AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
            GROUP BY q.branch_id HAVING Sessions >= 10
            -- a branch running on its backup circuit always surfaces, whatever its score
            ORDER BY Circuit = 'backup' DESC, `Degraded %` DESC, Sessions DESC LIMIT 15
        """, 0, 16, 24, 12, overrides=[
            col("Degraded %", cell=gauge_cell(), min=0, max=100, unit="percent",
                thresholds=steps((None, GOOD), (5, WARNING), (20, CRITICAL))),
            col("Mean score", decimals=1),
            col("Card p95 ms", unit="ms", cell=text_cell(),
                thresholds=steps((None, GOOD), (300, WARNING), (900, CRITICAL))),
            col("Circuit", mappings=[{"type": "value", "options": {
                "backup": {"text": "on backup", "color": SERIOUS},
                "primary": {"text": "primary", "color": MUTED}}}], cell=text_cell()),
            col("Degraded since", unit="time:HH:mm"),
        ]),
        {
            # series are labelled "01 · BR-0017" so the worst branch is always the top row
            **timeseries("Worst branches · quality per minute", f"""
                SELECT q.minute AS time, r.label AS branch,
                       round(sum(q.quality_sum) / sum(q.sessions)) AS quality
                FROM netflow.quality_1m AS q
                GLOBAL INNER JOIN (
                    SELECT branch_id,
                           concat(leftPad(toString(row_number() OVER (
                                      ORDER BY sum(degraded) / sum(sessions) DESC)), 2, '0'),
                                  ' · ', branch_id) AS label
                    FROM netflow.quality_1m
                    WHERE $__timeFilter(minute) AND {COMPLETE}
                      AND {REGION_FILTER} AND {BRANCH_PROVIDER_FILTER}
                    GROUP BY branch_id HAVING sum(sessions) >= 10
                    ORDER BY sum(degraded) / sum(sessions) DESC LIMIT 10
                ) AS r ON r.branch_id = q.branch_id
                WHERE $__timeFilter(q.minute) AND q.minute < toStartOfMinute(now())
                GROUP BY time, branch ORDER BY time, branch
            """, 0, 28, 24, 10),
            "type": "state-timeline",
            "fieldConfig": {"defaults": {"mappings": quality_mappings(),
                                         "displayName": "${__field.labels.branch}",
                                         "color": {"mode": "thresholds"},
                                         "thresholds": steps((None, CRITICAL), (50, SERIOUS),
                                                             (70, WARNING), (90, GOOD)),
                                         "custom": {"lineWidth": 0, "fillOpacity": 85}},
                            "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                        "alignValue": "left", "legend": {"showLegend": True,
                                                         "displayMode": "list",
                                                         "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}},
        },
    ]
    variables = [
        variable("region", "Region",
                 "SELECT DISTINCT region FROM netflow.quality_1m "
                 "WHERE minute > now() - INTERVAL 1 DAY ORDER BY region"),
        variable("provider", "Provider",
                 "SELECT DISTINCT provider FROM netflow.quality_1m "
                 "WHERE minute > now() - INTERVAL 1 DAY ORDER BY provider"),
    ]
    return dashboard("bank-estate", "Branch estate · service quality",
                     "Live service quality across the branch estate", p, variables)


def localisation() -> dict[str, Any]:
    localise = (LOCALISE_SQL.read_text()
                .replace("{window_min:UInt16}", "${window}")
                .replace("{min_degraded:UInt32}", "5"))
    top = f"SELECT * FROM ({localise.split('LIMIT 10')[0]} LIMIT 1)"
    p: list[Panel] = [
        note("### Fault localisation\n"
             "Every element a degraded session crossed is scored by **precision** (share of "
             "its sessions that are degraded) and **coverage** (share of all degraded "
             "sessions it carries). The element that explains the most with the least "
             "collateral is the most likely cause. Elements that carry exactly the same "
             "sessions cannot be told apart from traffic alone: a branch router and its "
             "circuit always tie, and so does an access switch when only its devices are "
             "active (overnight, the ATMs). The ranked list shows every contender.",
             0, 0, 24, 3),
        stat("Most likely cause", f"SELECT concat(element_type, ' · ', element_id) FROM ({top})",
             0, 3, 8, 4, text=True, decimals=None, colour_mode="none"),
        stat("Explains (coverage)", f"SELECT 100 * coverage FROM ({top})", 8, 3, 4, 4,
             unit="percent", decimals=0, colour_mode="none"),
        stat("Precision", f"SELECT 100 * precision FROM ({top})", 12, 3, 4, 4,
             unit="percent", decimals=0, colour_mode="none"),
        stat("Devices hit", f"SELECT devices_hit FROM ({top})", 16, 3, 4, 4, decimals=0,
             colour_mode="none"),
        # a timestamp, not a server-formatted string: grafana renders it in the viewer's zone
        stat("Degraded since", f"SELECT toUnixTimestamp(since) * 1000 FROM ({top})",
             20, 3, 4, 4, unit="time:HH:mm", decimals=None, colour_mode="none"),
        table("Ranked candidates", localise, 0, 7, 24, 9, overrides=[
            col("score", cell=gauge_cell(), min=0, max=1, decimals=2,
                thresholds=steps((None, MUTED), (0.3, WARNING), (0.6, SERIOUS))),
            col("precision", unit="percentunit", decimals=0),
            col("coverage", unit="percentunit", decimals=0),
            col("since", unit="dateTimeAsIso"),
        ], sort="score"),
        timeseries("Degraded sessions per minute · estate", """
            SELECT minute AS time, sum(degraded) AS degraded
            FROM netflow.quality_1m WHERE $__timeFilter(minute)
            GROUP BY time ORDER BY time
        """, 0, 16, 14, 8, colours={"degraded": SLOT[0]}),
        table("Injected incidents (ground truth)", """
            SELECT started_at AS Started, if(ongoing, NULL, ended_at) AS Ended,
                   kind AS Kind, target AS Target, label AS Description, source AS Source
            FROM netflow.incidents
            WHERE started_at >= $__fromTime ORDER BY started_at DESC LIMIT 20
        """, 14, 16, 10, 8, overrides=[col("Started", unit="dateTimeAsIso", width=170),
                                         col("Ended", unit="dateTimeAsIso", width=170)]),
    ]
    window = {
        "name": "window", "label": "Window (minutes)", "type": "custom",
        "query": "5,10,15,30,60", "current": {"text": "5", "value": "5"},
        "options": [{"text": v, "value": v, "selected": v == "5"}
                    for v in ("5", "10", "15", "30", "60")],
    }
    return dashboard("bank-localise", "Fault localisation",
                     "Which single element explains the degraded sessions", p, [window])


# per branch-minute, charged to the branch's PRIMARY circuit and its provider:
#   network-degraded  sessions on the primary failing the network SLA (RTT, loss, no return)
#   on_backup         the branch ran on its backup link: an outage minute for the primary
#   bank_side         one of the branch's switches failing while another is healthy: the
#                     loss is inside the branch, not on the provider's circuit
SLA_BRANCH_MINUTES = """
    SELECT q.branch_id AS branch_id, q.minute AS minute,
           dictGet('netflow.branch', 'primary_circuit_id', q.branch_id) AS circuit_id,
           dictGet('netflow.branch', 'primary_provider', q.branch_id) AS provider,
           sumIf(q.sessions, q.circuit_role = 'primary') AS sessions,
           sumIf(q.net_degraded, q.circuit_role = 'primary') AS net_degraded,
           sumIf(q.retrans, q.circuit_role = 'primary') AS retrans,
           sumIf(q.packets, q.circuit_role = 'primary') AS packets,
           quantilesTDigestMergeStateIf(0.5, 0.95)(q.client_rtt_ms, q.circuit_role = 'primary')
               AS rtt,
           sumIf(q.sessions, q.circuit_role = 'backup') > 0 AS on_backup,
           any(sw.bad_switches) >= 1 AND any(sw.healthy_switches) >= 1 AS bank_side
    FROM netflow.quality_1m AS q
    GLOBAL LEFT JOIN (
        SELECT splitByString('-SW', element_id)[1] AS branch_id, minute,
               countIf(degraded >= greatest(2, 0.2 * sessions)) AS bad_switches,
               countIf(sessions >= 3 AND degraded <= 0.05 * sessions) AS healthy_switches
        FROM (SELECT e.element_id AS element_id, e.minute AS minute,
                     sum(e.sessions) AS sessions, sum(e.degraded) AS degraded
              FROM netflow.element_1m AS e
              WHERE e.element_type = 'switch' AND $__timeFilter(e.minute)
              GROUP BY element_id, minute)
        GROUP BY branch_id, minute
    ) AS sw ON sw.branch_id = q.branch_id AND sw.minute = q.minute
    WHERE $__timeFilter(q.minute) AND q.minute < toStartOfMinute(now())
    GROUP BY q.branch_id, q.minute
"""


def sla() -> dict[str, Any]:
    p: list[Panel] = [
        note("### Provider SLA evidence\n"
             "Independent, timestamped evidence per rented circuit, charged to the branch's "
             "**primary** circuit and its provider. A *degraded minute*: at least 20% of the "
             "primary's sessions (and at least two) failed the network targets (WAN RTT, "
             "loss, no return path); a slow bank server is not counted. An *outage minute*: "
             "the branch ran on its backup link. Minutes where the loss sits on one of the "
             "branch's own switches while another is healthy are excluded as bank-side; with "
             "a single active switch (overnight) that cannot be told apart, so check **Fault "
             "localisation** before raising a dispute.", 0, 0, 24, 3),
        table("Provider scorecard", f"""
            SELECT provider AS Provider, uniqExact(circuit_id) AS Circuits,
                   sum(sessions) AS Sessions,
                   round(100 * sum(net_degraded) / greatest(1, sum(sessions)), 2)
                       AS `Network-degraded %`,
                   countIf(sessions >= 5 AND NOT bank_side
                           AND net_degraded >= greatest(2, 0.2 * sessions)) AS `Degraded minutes`,
                   countIf(on_backup) AS `Outage minutes`,
                   round(quantilesTDigestMerge(0.5, 0.95)(rtt)[2]) AS `WAN RTT p95 ms`,
                   round(100 * sum(retrans) / greatest(1, sum(packets)), 3) AS `Loss %`
            FROM ({SLA_BRANCH_MINUTES}) AS m
            WHERE provider IN (${{provider:singlequote}})
            GROUP BY provider ORDER BY provider
        """, 0, 3, 14, 6, overrides=[
            col("Network-degraded %", unit="percent", cell=text_cell(),
                thresholds=steps((None, GOOD), (1, WARNING), (5, CRITICAL))),
            col("Degraded minutes", cell=text_cell(),
                thresholds=steps((None, GOOD), (1, WARNING), (15, CRITICAL))),
            col("Outage minutes", cell=text_cell(),
                thresholds=steps((None, GOOD), (1, CRITICAL))),
            col("WAN RTT p95 ms", unit="ms", cell=text_cell(),
                thresholds=steps((None, GOOD), (60, WARNING), (70, CRITICAL))),
            col("Loss %", unit="percent", decimals=3),
        ]),
        timeseries("Network-degraded sessions by provider (primary circuits)", f"""
            SELECT minute AS time, provider,
                   100 * sumIf(net_degraded, NOT bank_side) / greatest(1, sum(sessions))
                       AS degraded_pct
            FROM ({SLA_BRANCH_MINUTES}) AS m
            WHERE provider IN (${{provider:singlequote}})
            GROUP BY time, provider ORDER BY time
        """, 14, 3, 10, 6, unit="percent", colours=PROVIDER_COLOURS, series_label="provider"),
        table("Circuit evidence", f"""
            SELECT circuit_id AS Circuit, any(branch_id) AS Branch, provider AS Provider,
                   dictGet('netflow.circuit_by_tunnel', 'pop_id',
                           toUInt64(extract(circuit_id, '[0-9]+'))) AS PoP,
                   sum(sessions) AS Sessions,
                   countIf(minute_degraded) AS `Degraded minutes`,
                   countIf(on_backup) AS `Outage minutes`,
                   round(100 * sum(net_degraded) / greatest(1, sum(sessions)), 2)
                       AS `Network-degraded %`,
                   round(quantilesTDigestMerge(0.5, 0.95)(rtt)[2]) AS `WAN RTT p95 ms`,
                   round(100 * sum(retrans) / greatest(1, sum(packets)), 2) AS `Loss %`,
                   minIf(minute, minute_degraded OR on_backup) AS `First affected`,
                   maxIf(minute, minute_degraded OR on_backup) AS `Last affected`
            FROM (
                SELECT *, sessions >= 5 AND NOT bank_side
                          AND net_degraded >= greatest(2, 0.2 * sessions) AS minute_degraded
                FROM ({SLA_BRANCH_MINUTES})
            ) AS m
            WHERE provider IN (${{provider:singlequote}})
            GROUP BY circuit_id, provider
            HAVING `Degraded minutes` + `Outage minutes` > 0
            ORDER BY `Outage minutes` + `Degraded minutes` DESC LIMIT 50
        """, 0, 9, 24, 12, overrides=[
            col("Degraded minutes", cell=gauge_cell(), min=0,
                thresholds=steps((None, WARNING), (5, SERIOUS), (15, CRITICAL))),
            col("Outage minutes", cell=text_cell(),
                thresholds=steps((None, GOOD), (1, CRITICAL))),
            col("Network-degraded %", unit="percent"), col("WAN RTT p95 ms", unit="ms"),
            col("Loss %", unit="percent"),
            col("First affected", unit="dateTimeAsIso"),
            col("Last affected", unit="dateTimeAsIso"),
        ]),
    ]
    variables = [variable("provider", "Provider",
                          "SELECT DISTINCT primary_provider FROM netflow.branch "
                          "ORDER BY primary_provider")]
    return dashboard("bank-sla", "Provider SLA evidence",
                     "Per-provider and per-circuit evidence for SLA conversations", p,
                     variables, time_from="now-6h", refresh="1m")


def pipeline() -> dict[str, Any]:
    # every query here hits a sort key or a skip index: the queue is ordered by arrival
    # time, sessions carry a minmax index on stitched_at, cluster state comes from the
    # admin-owned views in ops (the grafana user has no system.* or REMOTE rights)
    p: list[Panel] = [
        note("### Data pipeline health\n"
             "Kafka → ClickHouse (2 shards × 2 replicas) → stitcher → rollups: throughput, "
             "freshness, replication and storage.", 0, 0, 24, 2),
        stat("Ingest rate", """
            SELECT count() / 60 FROM netflow.stitch_queue
            WHERE ingested_at >= now() - INTERVAL 60 SECOND
        """, 0, 2, unit="rowsps", decimals=0, colour_mode="none",
             description="Half-flow records written per second, last minute."),
        stat("Stitch lag", """
            SELECT if(count() < 2, NULL, dateDiff('second', min(w), now())) FROM (
                SELECT argMax(watermark, updated_at) AS w FROM ops.etl_watermarks
                WHERE job = 'stitch' GROUP BY shard)
        """, 4, 2, unit="s", decimals=0,
             thresholds=steps((None, GOOD), (60, WARNING), (300, CRITICAL)),
             description="Oldest shard watermark behind now; empty until both shards have "
                         "stitched. 10-20 s is the safety margin."),
        stat("Max replica delay", "SELECT max(absolute_delay) FROM ops.replica_status",
             8, 2, unit="s", decimals=0,
             thresholds=steps((None, GOOD), (10, WARNING), (60, CRITICAL))),
        stat("Servers answering", "SELECT servers FROM ops.servers_up", 12, 2, decimals=0,
             thresholds=steps((None, CRITICAL), (4, GOOD)),
             description="ClickHouse servers answering, of 4."),
        stat("Ingest errors (24h)", """
            SELECT count() FROM netflow.ingest_errors
            WHERE received_at >= now() - INTERVAL 1 DAY
        """, 16, 2, decimals=0, thresholds=steps((None, GOOD), (1, CRITICAL))),
        stat("Sessions stitched (1h)", """
            SELECT count() FROM netflow.sessions
            WHERE stitched_at >= now() - INTERVAL 1 HOUR
              AND session_start >= now() - INTERVAL 25 HOUR
        """, 20, 2, unit="short", decimals=0, colour_mode="none"),
        timeseries("Records ingested per second", """
            SELECT toStartOfInterval(ingested_at, INTERVAL 10 SECOND) AS time,
                   concat('shard ', toString(_shard_num)) AS shard, count() / 10 AS rows_per_s
            FROM netflow.stitch_queue
            WHERE $__timeFilter(ingested_at) AND ingested_at < toStartOfInterval(now(), INTERVAL 10 SECOND)
            GROUP BY time, shard ORDER BY time
        """, 0, 6, 12, 8, unit="rowsps", series_label="shard", stacked=True,
                   colours={"shard 1": SLOT[0], "shard 2": SLOT[1]},
                   description="Stacked: the top edge is the total ingest rate."),
        timeseries("Session freshness · stitched minus ended (p95)", """
            SELECT toStartOfMinute(stitched_at) AS time,
                   quantile(0.95)(dateDiff('millisecond', session_end, stitched_at)) / 1000
                       AS seconds
            FROM netflow.sessions
            WHERE $__timeFilter(stitched_at) AND session_start >= $__fromTime - INTERVAL 1 DAY
            GROUP BY time ORDER BY time
        """, 12, 6, 12, 8, unit="s", colours={"seconds": SLOT[0]},
                   description="How long after a session ends it shows up in the store."),
        table("Storage by table", """
            SELECT table AS Table, rows AS Rows,
                   compressed_bytes AS Compressed, uncompressed_bytes AS Uncompressed,
                   round(uncompressed_bytes / compressed_bytes, 1) AS Ratio,
                   active_parts AS `Active parts`
            FROM ops.table_storage ORDER BY compressed_bytes DESC
        """, 0, 14, 14, 8, overrides=[
            col("Compressed", unit="bytes"), col("Uncompressed", unit="bytes"),
            col("Ratio", unit="none", decimals=1),
            col("Active parts", cell=text_cell(),
                thresholds=steps((None, GOOD), (300, WARNING), (1000, CRITICAL))),
        ]),
        table("Replicas", """
            SELECT host AS Host, table AS Table, is_leader AS Leader,
                   absolute_delay AS `Delay s`, queue_size AS Queue,
                   active_replicas AS `Active replicas`
            FROM ops.replica_status
            WHERE table IN ('halfflows_local', 'sessions_local')
            ORDER BY Table, Host
        """, 14, 14, 10, 8),
    ]
    return dashboard("bank-pipeline", "Data pipeline health",
                     "Freshness, replication and storage of the network quality store", p,
                     time_from="now-30m", refresh="10s", annotations=False)


DASHBOARDS = {"estate": estate, "localisation": localisation, "sla": sla,
              "pipeline": pipeline}

ALERTS_OUT = ROOT / "provisioning" / "alerting" / "rules.json"


# --- alerting ----------------------------------------------------------------------

def alert(uid: str, title: str, sql: str, op: str, threshold: float, for_: str,
          severity: str, summary: str, description: str, dashboard: str,
          no_data: str = "OK") -> dict[str, Any]:
    """One rule: a ClickHouse query (string columns become alert labels, the numeric
    column the value) and a threshold on it. One alert instance per result row."""
    return {
        "uid": uid, "title": title, "condition": "C", "for": for_,
        "noDataState": no_data, "execErrState": "Error",
        "labels": {"severity": severity, "team": "network-ops"},
        # grafana only accepts __dashboardUid__ together with a panel id; a link is enough
        "annotations": {"summary": summary, "description": description,
                        "runbook_url": f"/d/{dashboard}"},
        "data": [
            {"refId": "A", "datasourceUid": DS["uid"],
             "relativeTimeRange": {"from": 300, "to": 0},
             "model": {**sql_target(sql), "intervalMs": 60000, "maxDataPoints": 43200}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": op, "params": [threshold]}}]}},
        ],
    }


# the dashboard's localisation query, fixed to a 5 minute window with a minimum support
LOCALISE_ALERT_SQL = (LOCALISE_SQL.read_text()
                      .replace("{window_min:UInt16}", "5")
                      .replace("{min_degraded:UInt32}", "20")
                      .rsplit("LIMIT 10", 1)[0] + "LIMIT 3")


def alert_rules() -> dict[str, Any]:
    last3 = ("minute >= toStartOfMinute(now()) - INTERVAL 3 MINUTE "
             "AND minute < toStartOfMinute(now())")
    service = [
        # labels come from the inventory, not the traffic: a failover must not change an
        # alert's identity (that would resolve and re-fire it)
        # the branch's own network, not a shared cause: sessions failing the network SLA
        # (RTT, loss, no return path) at 20%+ and 3x the estate's network rate. A slow
        # central server degrades every branch but fails nobody's network, so it raises
        # "Card authorisation slow" / "Fault localised" once instead of 400 branch alerts,
        # and it cannot mask a genuinely broken branch.
        # alerting wants exactly one numeric column per row, hence the subquery
        alert("branch-degraded", "Branch network degraded", f"""
            SELECT branch_id, region, provider,
                   if(pct >= greatest(20, 3 * estate_pct), pct, 0) AS net_degraded_pct
            FROM (
                SELECT branch_id,
                       dictGet('netflow.branch', 'region', branch_id) AS region,
                       dictGet('netflow.branch', 'primary_provider', branch_id) AS provider,
                       round(100 * sum(net_degraded) / sum(sessions), 1) AS pct,
                       (SELECT 100 * sum(net_degraded) / sum(sessions)
                        FROM netflow.quality_1m WHERE {last3}) AS estate_pct
                FROM netflow.quality_1m
                WHERE {last3}
                GROUP BY branch_id HAVING sum(sessions) >= 10)
        """, "gt", 0, "2m", "critical",
              "{{ $labels.branch_id }} network degraded: {{ $values.A }}% of sessions",
              "{{ $labels.branch_id }} ({{ $labels.region }}, primary provider "
              "{{ $labels.provider }}) is reachable but {{ $values.A }}% of its sessions fail "
              "the network SLA (WAN RTT, loss or no return path). Open Fault localisation "
              "for the likely cause.",
              "bank-localise"),
        # one alert per culprit: the localisation ranking, run on the last 5 minutes
        alert("fault-localised", "Fault localised", f"""
            SELECT element_type, element_id, score FROM ({LOCALISE_ALERT_SQL})
            WHERE score >= 0.5
        """, "gt", 0, "2m", "critical",
              "Likely cause: {{ $labels.element_type }} {{ $labels.element_id }}",
              "Degraded sessions converge on {{ $labels.element_type }} "
              "{{ $labels.element_id }} (score {{ $values.A }}). See Fault localisation for "
              "coverage, precision and the devices and branches affected.",
              "bank-localise"),
        alert("card-auth-slow", "Card authorisation slow", f"""
            SELECT quantilesTDigestMerge(0.5, 0.95)(response_ms)[2] AS p95_ms
            FROM netflow.quality_1m
            WHERE {last3} AND app = 'card_authorisation'
        """, "gt", 900, "3m", "critical",
              "Card authorisation p95 at {{ $values.A }} ms",
              "Estate-wide p95 time to first response for card authorisation is above the "
              "900 ms SLA ceiling.", "bank-estate"),
        alert("branch-on-backup", "Branch running on backup circuit", """
            SELECT branch_id,
                   dictGet('netflow.branch', 'region', branch_id) AS region,
                   sum(sessions) AS sessions
            FROM netflow.quality_1m
            WHERE minute >= toStartOfMinute(now()) - INTERVAL 2 MINUTE
              AND minute < toStartOfMinute(now()) AND circuit_role = 'backup'
            GROUP BY branch_id
        """, "gt", 0, "1m", "warning",
              "{{ $labels.branch_id }} failed over to its backup circuit",
              "Traffic from {{ $labels.branch_id }} ({{ $labels.region }}) is on its backup "
              "link: the primary is down. Backup links are smaller, expect congestion.",
              "bank-estate"),
    ]
    platform = [
        # a shard that has never reported counts as infinitely behind
        alert("stitch-lag", "Stitcher falling behind", """
            SELECT if(count() < 2, 86400, dateDiff('second', min(w), now())) AS lag_s FROM (
                SELECT argMax(watermark, updated_at) AS w FROM ops.etl_watermarks
                WHERE job = 'stitch' GROUP BY shard)
        """, "gt", 120, "3m", "warning", "Stitch lag {{ $values.A }} s",
              "Sessions are reaching the store late, or a shard has not been stitched at "
              "all; check the stitcher logs.", "bank-pipeline"),
        alert("replica-down", "ClickHouse replica down",
              "SELECT servers FROM ops.servers_up", "lt", 4, "1m", "critical",
              "Only {{ $values.A }} of 4 ClickHouse servers answer",
              "A shard is running on one replica: no redundancy until it is back.",
              "bank-pipeline", no_data="Alerting"),
        alert("replica-delay", "ClickHouse replication delay",
              "SELECT max(absolute_delay) AS delay_s FROM ops.replica_status",
              "gt", 60, "2m", "warning", "Replica {{ $values.A }} s behind",
              "A replica is lagging; reads from it are stale.", "bank-pipeline"),
        alert("ingest-errors", "Records quarantined at ingest", """
            SELECT count() AS errors FROM netflow.ingest_errors
            WHERE received_at >= now() - INTERVAL 10 MINUTE
        """, "gt", 0, "0s", "warning", "{{ $values.A }} records quarantined in 10 min",
              "Messages on the topic failed to parse or carried invalid values; see "
              "netflow.ingest_errors for the payload and reason.", "bank-pipeline"),
        alert("ingest-stopped", "Ingestion stopped", """
            SELECT count() AS records FROM netflow.stitch_queue
            WHERE ingested_at >= now() - INTERVAL 2 MINUTE
        """, "lt", 1, "2m", "critical", "No records ingested for 2 minutes",
              "Kafka consumers or the producer are down.", "bank-pipeline",
              no_data="Alerting"),
    ]
    group = {"orgId": 1, "folder": "Bank network", "interval": "1m"}
    return {"apiVersion": 1, "groups": [
        {**group, "name": "service-quality", "rules": service},
        {**group, "name": "data-platform", "rules": platform},
    ]}


def main() -> int:
    check = "--check" in sys.argv
    OUT.mkdir(exist_ok=True)
    stale = []
    for name, build in DASHBOARDS.items():
        rendered = json.dumps(build(), indent=2) + "\n"
        path = OUT / f"{name}.json"
        if check:
            if not path.exists() or path.read_text() != rendered:
                stale.append(path.name)
        else:
            path.write_text(rendered)
            print(f"wrote {path.relative_to(ROOT.parent)}")
    rendered = json.dumps(alert_rules(), indent=2) + "\n"
    if check:
        if not ALERTS_OUT.exists() or ALERTS_OUT.read_text() != rendered:
            stale.append(ALERTS_OUT.name)
    else:
        ALERTS_OUT.write_text(rendered)
        print(f"wrote {ALERTS_OUT.relative_to(ROOT.parent)}")
    if stale:
        print(f"stale dashboards, run python3 grafana/build.py: {stale}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
