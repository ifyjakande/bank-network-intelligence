from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOWETL_")

    # "host:port,host:port;host:port,host:port" - shards separated by ';', replicas by ','
    ch_shards: str = "ch-s1r1:8123,ch-s1r2:8123;ch-s2r1:8123,ch-s2r2:8123"
    ch_user: str = "etl"
    ch_password: SecretStr = SecretStr("")
    ch_admin_user: str = "admin"
    ch_admin_password: SecretStr = SecretStr("")

    pg_dsn: SecretStr = SecretStr("postgresql://inventory_loader@postgres:5432/bank")

    migrations_dir: Path = Path("/app/migrations")
    inventory_dir: Path = Path("/inventory")
    seeds_dir: Path = Path("/app/seeds")

    stitch_interval_s: float = Field(10.0, gt=0)
    stitch_safety_s: int = Field(10, ge=0)  # never read rows newer than this: inserts in flight
    stitch_max_window_s: int = Field(120, gt=0)  # catch up in bounded chunks
    stitch_timeout_s: int = Field(900, gt=0)  # emit a session incomplete after this long idle
    # re-examine each window this many seconds later, before the idle-timeout sweep
    stitch_recheck_offsets_s: list[int] = Field(default_factory=lambda: [60, 300])
    stitch_sync_timeout_s: int = Field(10, gt=0)  # bounded wait for replica catch-up
    # longest session we stitch: the furthest back a run's first_seen bound may reach
    stitch_lookback_s: int = Field(86400, gt=0)
    stitch_pair_tolerance_ms: int = Field(5000, gt=0)  # max c2s -> s2c first-seen gap
    stitch_probe_skew_ms: int = Field(50, ge=0)  # clock skew allowed between two probes
    stitch_query_timeout_s: int = Field(240, gt=0)  # server cancels before the client gives up
    degraded_below: int = Field(70, ge=1, le=100)  # quality score under this = degraded

    metrics_port: int = 9103

    def shards(self) -> list[list[tuple[str, int]]]:
        out = []
        for shard in self.ch_shards.split(";"):
            replicas = []
            for hp in shard.split(","):
                host, _, port = hp.strip().partition(":")
                replicas.append((host, int(port or 8123)))
            out.append(replicas)
        return out
