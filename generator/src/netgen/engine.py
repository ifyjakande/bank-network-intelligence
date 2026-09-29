from __future__ import annotations

import heapq
import itertools
import random
import time
from typing import Any

from prometheus_client import Gauge

from .config import Settings
from .demand import Demand
from .faults import CYCLE_MINUTES, FaultRegistry, schedule_cycle
from .flows import Session, SessionFactory, export
from .sinks import Sink
from .topology import Estate

OPEN_SESSIONS = Gauge("netgen_open_sessions", "Sessions not yet fully exported")
ACTIVE_FAULTS = Gauge("netgen_active_faults", "Faults currently affecting traffic")
SIM_TIME = Gauge("netgen_sim_time_seconds", "Simulation clock (wall clock in live mode)")

CYCLE_US = CYCLE_MINUTES * 60 * 1_000_000


class Engine:
    def __init__(self, cfg: Settings, estate: Estate, sink: Sink) -> None:
        self.cfg = cfg
        self.sink = sink
        self.rng = random.Random(cfg.seed + 1)
        self.faults = FaultRegistry(estate)
        self.demand = Demand(estate, self.rng, cfg.load_factor, cfg.utc_offset_hours)
        self.factory = SessionFactory(estate, self.faults, self.rng, cfg.asym_rate)
        self.timeout_us = cfg.active_timeout_s * 1_000_000
        self._pending: list[tuple[int, int, Session]] = []
        self._late: list[tuple[int, int, dict[str, Any]]] = []
        self._seq = itertools.count()
        self._next_cycle_us: int | None = None
        self.stopping = False

    def _delay_us(self) -> int:
        return int(self.cfg.export_delay_s * self.rng.uniform(0.5, 1.5) * 1e6)

    def _schedule_faults(self, now_us: int) -> None:
        if self.cfg.fault_schedule != "demo":
            return
        if self._next_cycle_us is None:
            self._next_cycle_us = now_us - now_us % CYCLE_US
            schedule_cycle(self.faults, self._next_cycle_us, not_before_us=now_us)
            self._next_cycle_us += CYCLE_US
        while now_us >= self._next_cycle_us - 60_000_000:
            schedule_cycle(self.faults, self._next_cycle_us, not_before_us=now_us)
            self._next_cycle_us += CYCLE_US

    def _publish_fault_events(self, now_us: int, closing: bool = False) -> None:
        events = self.faults.close(now_us) if closing else self.faults.transitions(now_us)
        for ev in events:
            ev["event_at_us"] = now_us
            self.sink.send(self.cfg.faults_topic, str(ev["fault_id"]), ev)
        ACTIVE_FAULTS.set(sum(f.active(now_us) for f in self.faults.snapshot()))

    def _emit(self, rec: dict[str, Any], at_us: int) -> None:
        rec["exported_at_us"] = at_us
        key = str(rec["community_id"])
        self.sink.send(self.cfg.flows_topic, key, rec)
        # at-least-once delivery upstream means the store must tolerate replays
        if self.rng.random() < self.cfg.duplicate_rate:
            self.sink.send(self.cfg.flows_topic, key, rec)

    def step(self, t0_us: int, t1_us: int) -> None:
        """Advance the clock from t0 to t1: new sessions in, due records out."""
        self._schedule_faults(t1_us)
        self._publish_fault_events(t1_us)
        self._spawn(t0_us, t1_us)
        self._flush(t1_us)

    def _spawn(self, t0_us: int, t1_us: int) -> None:
        for device, app, start in self.demand.arrivals(t0_us, t1_us):
            s = self.factory.create(device, app, start)
            cut = min(s.end_us, start + self.timeout_us)
            heapq.heappush(self._pending, (cut + self._delay_us(), next(self._seq), s))

    def _flush(self, now_us: int) -> None:
        t1_us = now_us
        while self._pending and self._pending[0][0] <= t1_us:
            at, _, s = heapq.heappop(self._pending)
            cut = min(s.end_us, s.exported_until_us + self.timeout_us)
            for rec in export(s, cut, at):
                if self.rng.random() < self.cfg.late_rate:
                    late_at = at + int(self.rng.uniform(30, self.cfg.late_max_s) * 1e6)
                    heapq.heappush(self._late, (late_at, next(self._seq), rec))
                else:
                    self._emit(rec, at)
            if s.exported_until_us < s.end_us:
                nxt = min(s.end_us, s.exported_until_us + self.timeout_us)
                heapq.heappush(self._pending, (nxt + self._delay_us(), next(self._seq), s))

        while self._late and self._late[0][0] <= t1_us:
            at, _, rec = heapq.heappop(self._late)
            self._emit(rec, at)

        OPEN_SESSIONS.set(len(self._pending))
        SIM_TIME.set(t1_us / 1e6)
        self.sink.poll()

    def drain(self, now_us: int) -> None:
        """Export everything still open, advancing the clock as far as that takes."""
        horizon = now_us
        while self._pending or self._late:
            heads = [h[0][0] for h in (self._pending, self._late) if h]
            horizon = max(horizon, min(heads))
            self._flush(horizon)
        self._publish_fault_events(now_us, closing=True)

    def run_live(self) -> None:
        tick = 0.1
        last = int(time.time() * 1e6)
        while not self.stopping:
            time.sleep(max(0.0, tick - (time.time() - last / 1e6)))
            now = int(time.time() * 1e6)
            self.step(last, now)
            last = now
        self._publish_fault_events(int(time.time() * 1e6), closing=True)

    def run_backfill(self, start_us: int, end_us: int) -> None:
        t = start_us
        step = 1_000_000
        while t < end_us and not self.stopping:
            nxt = min(t + step, end_us)
            self.step(t, nxt)
            t = nxt
        self.drain(end_us)
