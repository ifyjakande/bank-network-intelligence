from __future__ import annotations

import itertools

import pytest

from netgen.faults import FaultRegistry, FaultSpec, demo_scenarios, schedule_cycle
from netgen.topology import Estate

T0 = 1_000_000_000


@pytest.mark.parametrize(
    "spec",
    [
        FaultSpec("explode", "circuit:CKT-1001"),
        FaultSpec("degrade", "circuit:CKT-9999"),
        FaultSpec("degrade", "planet:mars"),
        FaultSpec("down", "pop:MER-NTH"),
        FaultSpec("slow", "circuit:CKT-1001"),
        FaultSpec("degrade", "server:cas-01"),
        FaultSpec("degrade", "circuit:CKT-1001", loss=0.9),
    ],
)
def test_invalid_faults_are_rejected(registry: FaultRegistry, spec: FaultSpec) -> None:
    with pytest.raises(ValueError):
        registry.add(spec, T0, T0 + 1, "test")


def test_transitions_emit_start_then_end_once(registry: FaultRegistry) -> None:
    f = registry.add(FaultSpec("degrade", "pop:MER-NTH", latency_ms=10), T0, T0 + 100, "test")
    assert registry.transitions(T0 - 1) == []
    assert [e["state"] for e in registry.transitions(T0)] == ["started"]
    assert registry.transitions(T0 + 50) == []
    ended = registry.transitions(T0 + 100)
    assert [(e["state"], e["fault_id"]) for e in ended] == [("ended", f.fault_id)]
    assert registry.snapshot() == []


def test_close_ends_running_and_drops_future_faults(registry: FaultRegistry) -> None:
    registry.add(FaultSpec("degrade", "pop:MER-NTH"), T0, T0 + 100, "test")
    registry.add(FaultSpec("degrade", "pop:SAV-EST"), T0 + 500, T0 + 600, "test")
    registry.transitions(T0)
    events = registry.close(T0 + 10)
    assert [(e["state"], e["target"]) for e in events] == [("ended", "pop:MER-NTH")]
    assert events[0]["end_us"] == T0 + 10


def test_cancel_ends_fault_early(registry: FaultRegistry) -> None:
    f = registry.add(FaultSpec("degrade", "pop:MER-NTH"), T0, T0 + 100, "test")
    registry.cancel(f.fault_id, T0 + 10)
    assert not f.active(T0 + 10)


def test_demo_scenarios_are_valid_and_do_not_overlap(estate: Estate) -> None:
    scenarios = demo_scenarios(estate)
    windows = sorted((s.at_min, s.at_min + s.for_min) for s in scenarios)
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(windows))
    reg = FaultRegistry(estate)
    schedule_cycle(reg, T0)
    assert len(reg.snapshot()) == len(scenarios)


def test_schedule_skips_incidents_already_over(estate: Estate) -> None:
    reg = FaultRegistry(estate)
    schedule_cycle(reg, T0, not_before_us=T0 + 20 * 60_000_000)
    assert all(f.end_us > T0 + 20 * 60_000_000 for f in reg.snapshot())
    assert len(reg.snapshot()) < len(demo_scenarios(estate))
