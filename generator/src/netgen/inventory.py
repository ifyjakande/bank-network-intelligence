"""Export the estate as CSV: the CMDB the probes never see, loaded into Postgres later."""

from __future__ import annotations

import csv
from dataclasses import astuple, fields
from pathlib import Path
from typing import Any

from .apps import APPS
from .topology import Estate

# the traffic-shape fields are generator internals, not inventory
APP_COLUMNS = ("name", "category", "criticality", "ip_proto", "l7", "dst_port", "hosted")


def write_inventory(estate: Estate, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[Any]] = {
        "providers": list(estate.providers.values()),
        "pops": list(estate.pops.values()),
        "gateways": list(estate.gateways.values()),
        "branches": list(estate.branches.values()),
        "circuits": list(estate.circuits.values()),
        "devices": list(estate.devices.values()),
        "servers": list(estate.servers.values()),
    }
    for name, rows in tables.items():
        with (out / f"{name}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([f.name for f in fields(rows[0])])
            for r in rows:
                w.writerow(["|".join(v) if isinstance(v, tuple) else v for v in astuple(r)])
        print(f"{name}: {len(rows)} rows -> {out / (name + '.csv')}")

    with (out / "apps.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(APP_COLUMNS)
        for app in APPS.values():
            w.writerow([getattr(app, c) for c in APP_COLUMNS])
    print(f"apps: {len(APPS)} rows -> {out / 'apps.csv'}")
