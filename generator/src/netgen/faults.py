"""Fault injection.

A fault targets one element (`circuit:CKT-1035`, `pop:SAV-EAST`, `server:cas-02`...) and
changes the physics of every session whose path crosses it. Each start and end is also
published as a ground-truth event, so localisation queries can be scored against it.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import asdict, dataclass, field

from .apps import APPS
from .topology import Estate

KINDS = ("degrade", "down", "slow")
NETWORK_TARGETS = ("switch", "router", "circuit", "pop", "gateway")
SERVICE_TARGETS = ("server", "app")
US_PER_MIN = 60_000_000


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """What to break and how badly; timing is applied when it is scheduled."""

    kind: str  # degrade: latency/loss on a network element; down: circuit outage; slow: server
    target: str  # "<type>:<id>"
    latency_ms: float = 0.0  # network latency (degrade) or extra server time (slow)
    loss: float = 0.0
    server_factor: float = 1.0
    label: str = ""


@dataclass(slots=True)
class Fault:
    fault_id: str
    spec: FaultSpec
    start_us: int
    end_us: int
    source: str

    @property
    def target(self) -> str:
        return self.spec.target

    def active(self, t_us: int) -> bool:
        return self.start_us <= t_us < self.end_us

    def as_dict(self) -> dict[str, object]:
        d = asdict(self.spec)
        d.update(
            fault_id=self.fault_id,
            start_us=self.start_us,
            end_us=self.end_us,
            source=self.source,
            target_type=self.target.split(":", 1)[0],
        )
        return d

    def event(self, state: str) -> dict[str, object]:
        return {**self.as_dict(), "state": state}


@dataclass(slots=True)
class PathEffect:
    latency_ms: float = 0.0
    loss: float = 0.0
    server_factor: float = 1.0
    server_extra_ms: float = 0.0
    down_circuits: set[str] = field(default_factory=set)


def known_ids(estate: Estate, ttype: str) -> set[str]:
    if ttype == "circuit":
        return set(estate.circuits)
    if ttype == "pop":
        return set(estate.pops)
    if ttype == "gateway":
        return set(estate.gateways)
    if ttype == "server":
        return set(estate.servers)
    if ttype == "router":
        return {b.router_id for b in estate.branches.values()}
    if ttype == "switch":
        return {s for b in estate.branches.values() for s in b.switch_ids}
    if ttype == "app":
        return set(APPS)
    raise ValueError(f"target type must be one of {NETWORK_TARGETS + SERVICE_TARGETS}")


def validate(estate: Estate, spec: FaultSpec) -> None:
    if spec.kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ttype, _, tid = spec.target.partition(":")
    if tid not in known_ids(estate, ttype):
        raise ValueError(f"unknown {ttype} '{tid}'")
    if spec.kind == "down" and ttype != "circuit":
        raise ValueError("'down' only applies to circuits")
    if spec.kind == "slow" and ttype not in SERVICE_TARGETS:
        raise ValueError("'slow' only applies to servers or apps")
    if spec.kind == "degrade" and ttype not in NETWORK_TARGETS:
        raise ValueError("'degrade' applies to network elements; use 'slow' for servers")
    if not 0 <= spec.loss < 0.5:
        raise ValueError("loss must be in [0, 0.5)")
    if spec.server_factor < 1:
        raise ValueError("server_factor must be >= 1")


class FaultRegistry:
    """Thread-safe: the control API adds and cancels faults while the engine reads."""

    def __init__(self, estate: Estate) -> None:
        self.estate = estate
        self._lock = threading.Lock()
        self._faults: dict[str, Fault] = {}
        self._started: set[str] = set()
        self._ids = itertools.count(1)

    def add(self, spec: FaultSpec, start_us: int, end_us: int, source: str) -> Fault:
        validate(self.estate, spec)
        if end_us <= start_us:
            raise ValueError("fault must have a positive duration")
        with self._lock:
            f = Fault(f"{source}-{next(self._ids):05d}", spec, start_us, end_us, source)
            self._faults[f.fault_id] = f
        return f

    def cancel(self, fault_id: str, now_us: int) -> Fault | None:
        with self._lock:
            f = self._faults.get(fault_id)
            if f is not None and f.end_us > now_us:
                f.end_us = max(f.start_us + 1, now_us)
            return f

    def snapshot(self) -> list[Fault]:
        with self._lock:
            return list(self._faults.values())

    def transitions(self, now_us: int) -> list[dict[str, object]]:
        """Start/end events that became due by now_us; finished faults are dropped."""
        events: list[dict[str, object]] = []
        with self._lock:
            for fid, f in list(self._faults.items()):
                if fid not in self._started and f.start_us <= now_us:
                    self._started.add(fid)
                    events.append(f.event("started"))
                if fid in self._started and f.end_us <= now_us:
                    events.append(f.event("ended"))
                    self._started.discard(fid)
                    del self._faults[fid]
        return events

    def close(self, now_us: int) -> list[dict[str, object]]:
        """End the run: running faults end now, ones that never started are dropped."""
        events: list[dict[str, object]] = []
        with self._lock:
            for fid, f in self._faults.items():
                if fid in self._started:
                    f.end_us = min(f.end_us, now_us)
                    events.append(f.event("ended"))
            self._faults.clear()
            self._started.clear()
        return events

    def effect(self, path: frozenset[str], t_us: int) -> PathEffect:
        eff = PathEffect()
        with self._lock:
            for f in self._faults.values():
                if not f.active(t_us) or f.target not in path:
                    continue
                s = f.spec
                if s.kind == "down":
                    eff.down_circuits.add(s.target.split(":", 1)[1])
                elif s.kind == "slow":
                    eff.server_factor *= s.server_factor
                    eff.server_extra_ms += s.latency_ms
                else:
                    eff.latency_ms += s.latency_ms
                    eff.loss = 1 - (1 - eff.loss) * (1 - s.loss)
        return eff


@dataclass(frozen=True, slots=True)
class Scenario:
    at_min: float  # offset inside the cycle
    for_min: float
    spec: FaultSpec


CYCLE_MINUTES = 45


def demo_scenarios(estate: Estate) -> list[Scenario]:
    """The story the dashboard tells, one incident at a time inside a repeating cycle."""
    branches = list(estate.branches.values())
    br17 = estate.branches.get("BR-0017", branches[0])
    pop = next(
        (p for p in estate.pops.values() if p.region == "East" and p.provider == "SAV"),
        next(iter(estate.pops.values())),
    )
    multi_sw = next((b for b in branches if b.size in ("large", "flagship")), branches[0])
    flagship = next((b for b in branches if b.size == "flagship"), branches[-1])
    return [
        Scenario(
            5,
            8,
            FaultSpec(
                "degrade",
                f"circuit:{br17.primary_circuit_id}",
                latency_ms=35,
                loss=0.025,
                label=f"{br17.branch_id} primary circuit lossy, branch still up",
            ),
        ),
        Scenario(
            15,
            8,
            FaultSpec(
                "degrade",
                f"pop:{pop.pop_id}",
                latency_ms=45,
                loss=0.008,
                label=f"congestion at provider PoP {pop.pop_id}",
            ),
        ),
        Scenario(
            25,
            6,
            FaultSpec(
                "degrade",
                f"switch:{multi_sw.switch_ids[0]}",
                latency_ms=2,
                loss=0.06,
                label=f"faulty access switch at {multi_sw.branch_id} (ATMs, card terminals)",
            ),
        ),
        Scenario(
            33,
            6,
            FaultSpec(
                "slow",
                "server:cas-02",
                server_factor=6.0,
                label="card authorisation server cas-02 slow",
            ),
        ),
        Scenario(
            41,
            4,
            FaultSpec(
                "down",
                f"circuit:{flagship.primary_circuit_id}",
                label=f"{flagship.branch_id} primary circuit down, running on backup",
            ),
        ),
    ]


def schedule_cycle(registry: FaultRegistry, cycle_start_us: int, not_before_us: int = 0) -> None:
    """Queue one cycle of demo incidents; ones already over by not_before_us are skipped."""
    for sc in demo_scenarios(registry.estate):
        start = cycle_start_us + int(sc.at_min * US_PER_MIN)
        end = start + int(sc.for_min * US_PER_MIN)
        if end > not_before_us:
            registry.add(sc.spec, max(start, not_before_us), end, source="sched")
