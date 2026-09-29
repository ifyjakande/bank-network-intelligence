"""Stitcher: turns half-flows into sessions, one shard at a time.

Each shard has its own watermark on the work queue (`ops.etl_watermarks`). A run reads
the window (watermark, now - safety], bounded to max_window so a backlog is worked off
in chunks, stitches it with one INSERT ... SELECT on the shard's local tables, then
advances the watermark. The insert carries a dedup token derived from the window, so a
crash between insert and watermark update cannot double-insert on retry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources

from prometheus_client import Counter, Gauge, Histogram

from .ch import ShardClient
from .config import Settings

log = logging.getLogger(__name__)

JOB = "stitch"
STITCH_SQL = resources.files("flowetl.sql").joinpath("stitch.sql").read_text()

SESSIONS = Counter("flowetl_stitched_sessions_total", "Sessions written", ["shard"])
RUNS = Counter("flowetl_stitch_runs_total", "Stitch windows processed", ["shard", "result"])
LAG = Gauge("flowetl_stitch_lag_seconds", "Server time minus watermark", ["shard"])
REPLICA_DELAY = Gauge(
    "flowetl_replica_delay_seconds", "Replication delay of the replica read", ["shard"]
)
DURATION = Histogram(
    "flowetl_stitch_seconds",
    "Time per stitch window",
    ["shard"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)


@dataclass(frozen=True, slots=True)
class Window:
    lo: datetime
    hi: datetime

    @property
    def token(self) -> str:
        return f"{JOB}:{self.lo.isoformat()}:{self.hi.isoformat()}"


def next_window(
    watermark: datetime, server_now: datetime, safety_s: int, max_window_s: int
) -> Window | None:
    hi = min(server_now - timedelta(seconds=safety_s), watermark + timedelta(seconds=max_window_s))
    return Window(watermark, hi) if hi > watermark else None


class Stitcher:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.shards = [
            ShardClient(
                i + 1,
                replicas,
                cfg.ch_user,
                cfg.ch_password.get_secret_value(),
                sync_tables=("netflow.sessions_local",),
            )
            for i, replicas in enumerate(cfg.shards())
        ]
        self.stopping = False

    def _watermark(self, shard: ShardClient) -> datetime:
        client = shard.get()
        rows = client.query(
            "SELECT watermark FROM ops.etl_watermarks FINAL WHERE job = {job:String} "
            "AND shard = {shard:UInt8}",
            parameters={"job": JOB, "shard": shard.shard_no},
        ).result_rows
        if rows:
            return rows[0][0]  # type: ignore[no-any-return]
        # first run on this shard: start just before the oldest queued record
        start = client.query(
            "SELECT min(ingested_at) - INTERVAL 1 MILLISECOND FROM netflow.stitch_queue_local"
        ).result_rows[0][0]
        if start.year < 2000:  # empty queue
            start = client.query("SELECT now64(3)").result_rows[0][0]
        return start  # type: ignore[no-any-return]

    def run_once(self, shard: ShardClient) -> int:
        """Stitch one window on one shard; returns sessions written (0 if nothing to do)."""
        client = shard.get()
        server_now, delay = client.query(
            "SELECT now64(3), (SELECT max(absolute_delay) FROM system.replicas "
            "WHERE database = 'netflow' AND table IN ('halfflows_local', 'stitch_queue_local'))"
        ).result_rows[0]
        # a replica that is behind has not seen every row stamped before now - safety yet;
        # hold the window back by its delay rather than skip rows the watermark passes
        REPLICA_DELAY.labels(shard.shard_no).set(delay)
        wm = self._watermark(shard)
        LAG.labels(shard.shard_no).set((server_now - wm).total_seconds())
        win = next_window(
            wm, server_now, self.cfg.stitch_safety_s + int(delay), self.cfg.stitch_max_window_s
        )
        if win is None:
            return 0

        started = time.monotonic()
        summary = client.command(
            STITCH_SQL,
            parameters={
                "lo": win.lo,
                "hi": win.hi,
                "timeout_s": self.cfg.stitch_timeout_s,
                "lookback_s": self.cfg.stitch_lookback_s,
                "pair_tolerance_ms": self.cfg.stitch_pair_tolerance_ms,
                "degraded_below": self.cfg.degraded_below,
            },
            settings={
                "insert_deduplication_token": f"{win.token}:{shard.shard_no}",
                "join_algorithm": "full_sorting_merge,hash",
            },
        )
        written = int(getattr(summary, "written_rows", 0) or 0)
        client.insert(
            "ops.etl_watermarks",
            [[JOB, shard.shard_no, win.hi]],
            column_names=["job", "shard", "watermark"],
        )
        DURATION.labels(shard.shard_no).observe(time.monotonic() - started)
        SESSIONS.labels(shard.shard_no).inc(written)
        RUNS.labels(shard.shard_no, "ok").inc()
        LAG.labels(shard.shard_no).set((server_now - win.hi).total_seconds())
        log.info(
            "shard %s via %s: %s -> %s, %d sessions",
            shard.shard_no,
            shard.replica,
            win.lo.isoformat(),
            win.hi.isoformat(),
            written,
        )
        return written

    def run_forever(self) -> None:
        while not self.stopping:
            busy = False
            for shard in self.shards:
                try:
                    busy |= self.run_once(shard) > 0
                except Exception:
                    RUNS.labels(shard.shard_no, "error").inc()
                    log.exception("shard %s: stitch failed, will retry", shard.shard_no)
                    shard.reset()
            if not busy:
                time.sleep(self.cfg.stitch_interval_s)
