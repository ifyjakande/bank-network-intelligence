"""Stitcher: turns half-flows into sessions, one shard at a time.

Each shard has its own watermark on the work queue (`ops.etl_watermarks`). A run:

1. syncs the replica it reads from (bounded wait), so rows already inserted on the
   partner replica are visible before the watermark moves past them;
2. stitches the window (watermark, now - safety], capped at max_window so a backlog is
   worked off in chunks, plus re-checks of the same window at fixed offsets later
   (see stitch.sql) and the idle-timeout sweep;
3. advances the watermark.

Correctness never depends on a run being unique: the query skips sessions already in
sessions_local, so retries, overlapping re-checks and `restitch` only fill gaps.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from clickhouse_connect.driver.client import Client
from prometheus_client import Counter, Gauge, Histogram

from .ch import ShardClient
from .config import Settings

log = logging.getLogger(__name__)

JOB = "stitch"
FAR_FUTURE = datetime(2200, 1, 1)  # live runs exclude nothing
HEARTBEAT = Path("/tmp/flowetl-stitch-heartbeat")
# the tables a run reads; sessions_local too, for the emit-once check
SYNC_TABLES = ("netflow.halfflows_local", "netflow.stitch_queue_local", "netflow.sessions_local")

SESSIONS = Counter("flowetl_stitched_sessions_total", "Sessions written", ["shard"])
RUNS = Counter("flowetl_stitch_runs_total", "Stitch windows processed", ["shard", "result"])
LAG = Gauge("flowetl_stitch_lag_seconds", "Server time minus watermark", ["shard"])
SYNC_TIMEOUTS = Counter(
    "flowetl_replica_sync_timeouts_total", "Replica syncs that hit the bounded wait", ["shard"]
)
DURATION = Histogram(
    "flowetl_stitch_seconds",
    "Time per stitch window",
    ["shard"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)


def render_sql(offsets_s: list[int]) -> str:
    """The stitch query with one work-queue slice per look-back offset.

    Written out as OR-ed ranges (not arrayExists) so the queue's sort key on ingested_at
    prunes every slice.
    """
    ranges = "\n           OR ".join(
        f"(ingested_at > {{lo:DateTime64(3)}} - toIntervalSecond({o})"
        f" AND ingested_at <= {{hi:DateTime64(3)}} - toIntervalSecond({o}))"
        for o in sorted({0, *offsets_s})
    )
    template = resources.files("flowetl.sql").joinpath("stitch.sql").read_text()
    return template.replace("/* QUEUE_SLICES */", ranges)


@dataclass(frozen=True, slots=True)
class Window:
    lo: datetime
    hi: datetime


def next_window(
    watermark: datetime, server_now: datetime, safety_s: int, max_window_s: int
) -> Window | None:
    hi = min(server_now - timedelta(seconds=safety_s), watermark + timedelta(seconds=max_window_s))
    return Window(watermark, hi) if hi > watermark else None


def windows_between(since: datetime, until: datetime, max_window_s: int) -> list[Window]:
    step = timedelta(seconds=max_window_s)
    out, lo = [], since
    while lo < until:
        hi = min(lo + step, until)
        out.append(Window(lo, hi))
        lo = hi
    return out


class Stitcher:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.sql = render_sql([*cfg.stitch_recheck_offsets_s, cfg.stitch_timeout_s])
        self.shards = [
            ShardClient(
                i + 1,
                replicas,
                cfg.ch_user,
                cfg.ch_password.get_secret_value(),
                sync_timeout_s=cfg.stitch_sync_timeout_s,
            )
            for i, replicas in enumerate(cfg.shards())
        ]
        self.stopping = False

    def _sync_replica(self, shard: ShardClient, client: Client) -> None:
        if not shard.sync(client, SYNC_TABLES):
            SYNC_TIMEOUTS.labels(shard.shard_no).inc()

    def _watermark(self, client: Client, shard_no: int) -> datetime:
        rows = client.query(
            "SELECT watermark FROM ops.etl_watermarks FINAL WHERE job = {job:String} "
            "AND shard = {shard:UInt8}",
            parameters={"job": JOB, "shard": shard_no},
        ).result_rows
        if rows:
            return rows[0][0]  # type: ignore[no-any-return]
        # first run on this shard: start just before the oldest queued record
        start = client.query(
            "SELECT min(ingested_at) - INTERVAL 1 MILLISECOND FROM netflow.stitch_queue_local"
        ).result_rows[0][0]
        if start.year < 2000:  # empty queue
            start = client.query("SELECT now64(3, 'UTC')").result_rows[0][0]
        return start  # type: ignore[no-any-return]

    def _stitch(
        self, shard: ShardClient, client: Client, win: Window, live_from: datetime = FAR_FUTURE
    ) -> int:
        started = time.monotonic()
        summary = client.command(
            self.sql,
            parameters={
                "lo": win.lo,
                "hi": win.hi,
                "live_from": live_from,
                "timeout_s": self.cfg.stitch_timeout_s,
                "lookback_s": self.cfg.stitch_lookback_s,
                "pair_tolerance_ms": self.cfg.stitch_pair_tolerance_ms,
                "probe_skew_ms": self.cfg.stitch_probe_skew_ms,
                "degraded_below": self.cfg.degraded_below,
            },
            settings={
                "join_algorithm": "full_sorting_merge,hash",
                "max_execution_time": self.cfg.stitch_query_timeout_s,
            },
        )
        # result_rows is the insert itself; written_rows also counts the rollup MVs' rows
        written = int(getattr(summary, "result_rows", 0) or 0)
        DURATION.labels(shard.shard_no).observe(time.monotonic() - started)
        SESSIONS.labels(shard.shard_no).inc(written)
        log.info(
            "shard %s via %s: %s -> %s, %d sessions",
            shard.shard_no,
            shard.replica,
            win.lo.isoformat(),
            win.hi.isoformat(),
            written,
        )
        return written

    def run_once(self, shard: ShardClient) -> bool:
        """Stitch the next window on one shard; True if more backlog is waiting."""
        client = shard.get()
        self._sync_replica(shard, client)
        server_now = client.query("SELECT now64(3, 'UTC')").result_rows[0][0]
        wm = self._watermark(client, shard.shard_no)
        LAG.labels(shard.shard_no).set((server_now - wm).total_seconds())
        win = next_window(wm, server_now, self.cfg.stitch_safety_s, self.cfg.stitch_max_window_s)
        if win is None:
            return False
        self._stitch(shard, client, win)
        client.insert(
            "ops.etl_watermarks",
            [[JOB, shard.shard_no, win.hi]],
            column_names=["job", "shard", "watermark"],
        )
        RUNS.labels(shard.shard_no, "ok").inc()
        LAG.labels(shard.shard_no).set((server_now - win.hi).total_seconds())
        return win.hi - win.lo >= timedelta(seconds=self.cfg.stitch_max_window_s)

    def run_forever(self) -> None:
        while not self.stopping:
            # one window per shard per interval; loop straight on only while catching up,
            # otherwise every freshly written session would trigger a sliver-sized rerun
            backlog, all_ok = False, True
            for shard in self.shards:
                try:
                    backlog |= self.run_once(shard)
                except Exception:
                    all_ok = False
                    RUNS.labels(shard.shard_no, "error").inc()
                    log.exception("shard %s: stitch failed, will retry", shard.shard_no)
                    shard.reset()
            if all_ok:
                # the container healthcheck reads this: healthy means stitching, not just up
                HEARTBEAT.touch()
            if not backlog:
                time.sleep(self.cfg.stitch_interval_s)

    def restitch(self, since: datetime, until: datetime) -> int:
        """Re-run a past range on every shard without touching watermarks. Safe to repeat:
        only sessions missing from the store are written. The range ends before anything
        the live stitcher can still touch (its watermark minus the widest re-check slice),
        so the two never race to write the same session."""
        # the live stitcher's widest slice is the idle sweep (timeout), plus one window
        live_reach = timedelta(
            seconds=max([*self.cfg.stitch_recheck_offsets_s, self.cfg.stitch_timeout_s])
            + self.cfg.stitch_max_window_s
        )
        total = 0
        for shard in self.shards:
            client = shard.get()
            self._sync_replica(shard, client)
            end = min(until, self._watermark(client, shard.shard_no) - live_reach)
            for win in windows_between(since, end, self.cfg.stitch_max_window_s):
                # sessions with records arriving after `end` are still the live stitcher's
                total += self._stitch(shard, client, win, live_from=end)
        return total
