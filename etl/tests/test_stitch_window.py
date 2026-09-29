from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from flowetl.config import Settings
from flowetl.stitch import Window, next_window, render_sql, windows_between

T = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def test_window_stops_short_of_in_flight_inserts() -> None:
    w = next_window(T, T + timedelta(seconds=30), safety_s=10, max_window_s=120)
    assert w == Window(T, T + timedelta(seconds=20))


def test_backlog_is_worked_off_in_bounded_chunks() -> None:
    w = next_window(T, T + timedelta(hours=1), safety_s=10, max_window_s=120)
    assert w is not None and w.hi - w.lo == timedelta(seconds=120)


def test_nothing_to_do_when_caught_up() -> None:
    assert next_window(T, T + timedelta(seconds=5), safety_s=10, max_window_s=120) is None


def test_restitch_ranges_are_tiled_without_gaps() -> None:
    wins = windows_between(T, T + timedelta(seconds=250), max_window_s=120)
    assert [w.hi - w.lo for w in wins] == [timedelta(seconds=s) for s in (120, 120, 10)]
    assert all(a.hi == b.lo for a, b in itertools.pairwise(wins))


def test_sql_reads_one_queue_slice_per_offset() -> None:
    sql = render_sql([60, 300, 900])
    assert "/* QUEUE_SLICES */" not in sql
    for offset in (0, 60, 300, 900):
        assert f"toIntervalSecond({offset})" in sql


def test_shard_topology_parsing() -> None:
    cfg = Settings(ch_shards="a:1,b:2;c:3,d")
    assert cfg.shards() == [[("a", 1), ("b", 2)], [("c", 3), ("d", 8123)]]
