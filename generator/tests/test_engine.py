from __future__ import annotations

from collections import Counter

from netgen.config import Settings
from netgen.engine import Engine
from netgen.topology import build_estate

from .conftest import CaptureSink

START = 1_790_676_000_000_000  # 2026-09-29 10:00 UTC, a Tuesday mid-morning
MIN = 60_000_000


def _run(minutes: int, **overrides: object) -> CaptureSink:
    params: dict[str, object] = {
        "branches": 40,
        "load_factor": 5,
        "late_rate": 0.01,
        "duplicate_rate": 0.01,
        **overrides,
    }
    cfg = Settings(**params)  # type: ignore[arg-type]
    sink = CaptureSink()
    Engine(cfg, build_estate(cfg.seed, cfg.branches), sink).run_backfill(
        START, START + minutes * MIN
    )
    return sink


def test_backfill_is_deterministic() -> None:
    a = _run(2).topic("netflow.halfflows.v1")
    b = _run(2).topic("netflow.halfflows.v1")
    assert [r["record_id"] for r in a] == [r["record_id"] for r in b]


def test_every_half_flow_is_exported_exactly_once_to_completion() -> None:
    flows = _run(3, fault_schedule="none").topic("netflow.halfflows.v1")
    unique = {r["record_id"]: r for r in flows}.values()
    halves = Counter((r["community_id"], r["first_seen_us"], r["direction"]) for r in unique)
    finals = Counter(
        (r["community_id"], r["first_seen_us"], r["direction"]) for r in unique if r["is_final"]
    )
    assert set(finals) == set(halves)
    assert all(n == 1 for n in finals.values())
    per_dir = Counter(d for _, _, d in finals)
    assert per_dir["c2s"] == per_dir["s2c"]


def test_reused_five_tuples_do_not_collide() -> None:
    flows = _run(3, fault_schedule="none", late_rate=0, duplicate_rate=0).topic(
        "netflow.halfflows.v1"
    )
    assert len({r["record_id"] for r in flows}) == len(flows)


def test_duplicates_and_late_records_are_injected() -> None:
    flows = _run(3, fault_schedule="none").topic("netflow.halfflows.v1")
    dupes = sum(c - 1 for c in Counter(r["record_id"] for r in flows).values())
    late = sum(1 for r in flows if r["exported_at_us"] - r["flow_end_us"] > 20_000_000)
    assert 0.005 < dupes / len(flows) < 0.02
    assert 0.005 < late / len(flows) < 0.02


def test_fault_ground_truth_pairs_up() -> None:
    events = _run(20).topic("netflow.faults.v1")
    by_id: dict[str, list[str]] = {}
    for e in events:
        by_id.setdefault(str(e["fault_id"]), []).append(str(e["state"]))
    assert by_id, "a 20 minute window must contain at least one demo incident"
    assert all(states == ["started", "ended"] for states in by_id.values())
    assert all(int(e["event_at_us"]) <= START + 20 * MIN for e in events)
