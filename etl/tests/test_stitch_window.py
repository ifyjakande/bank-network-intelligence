from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flowetl.config import Settings
from flowetl.stitch import Window, next_window

T = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def test_window_stops_short_of_in_flight_inserts() -> None:
    w = next_window(T, T + timedelta(seconds=30), safety_s=10, max_window_s=120)
    assert w == Window(T, T + timedelta(seconds=20))


def test_backlog_is_worked_off_in_bounded_chunks() -> None:
    w = next_window(T, T + timedelta(hours=1), safety_s=10, max_window_s=120)
    assert w is not None and w.hi - w.lo == timedelta(seconds=120)


def test_nothing_to_do_when_caught_up() -> None:
    assert next_window(T, T + timedelta(seconds=5), safety_s=10, max_window_s=120) is None


def test_token_is_stable_per_window() -> None:
    a, b = Window(T, T + timedelta(seconds=1)), Window(T, T + timedelta(seconds=1))
    assert a.token == b.token != Window(T, T + timedelta(seconds=2)).token


def test_shard_topology_parsing() -> None:
    cfg = Settings(ch_shards="a:1,b:2;c:3,d")
    assert cfg.shards() == [[("a", 1), ("b", 2)], [("c", 3), ("d", 8123)]]
