"""Turns device counts, app demand and time of day into session arrivals."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from .apps import DEMAND, activity
from .topology import Device, Estate


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam < 30:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1
    return max(0, round(rng.gauss(lam, math.sqrt(lam))))


class Demand:
    def __init__(
        self, estate: Estate, rng: random.Random, load_factor: float, utc_offset_hours: float
    ) -> None:
        self.rng = rng
        self.load_factor = load_factor
        self.offset = timedelta(hours=utc_offset_hours)
        self.types: list[tuple[str, list[Device], list[str], list[float], float]] = []
        for dtype, apps in DEMAND.items():
            devices = estate.devices_by_type.get(dtype, [])
            if not devices:
                continue
            names = list(apps)
            cum, total = [], 0.0
            for n in names:
                total += apps[n]
                cum.append(total)
            # sessions per second for the whole device population at full activity
            self.types.append((dtype, devices, names, cum, total * len(devices) / 3600))

    def arrivals(self, t0_us: int, t1_us: int) -> Iterator[tuple[Device, str, int]]:
        dt = (t1_us - t0_us) / 1e6
        local = datetime.fromtimestamp(t0_us / 1e6, UTC) + self.offset
        hour = local.hour + local.minute / 60
        for dtype, devices, names, cum, rate in self.types:
            lam = rate * activity(dtype, hour, local.weekday()) * self.load_factor * dt
            for _ in range(poisson(self.rng, lam)):
                device = devices[self.rng.randrange(len(devices))]
                app = self.rng.choices(names, cum_weights=cum)[0]
                yield device, app, t0_us + self.rng.randrange(max(t1_us - t0_us, 1))

    def peak_sessions_per_s(self) -> float:
        return sum(t[4] for t in self.types) * self.load_factor
