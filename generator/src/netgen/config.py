from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETGEN_")

    seed: int = 42
    branches: int = Field(400, ge=1, le=4096)
    mode: Literal["live", "backfill"] = "live"
    backfill_start: datetime | None = None
    backfill_end: datetime | None = None
    # multiple of a realistic 400-branch bank's demand; 1.0 is roughly 110 sessions/s at peak
    load_factor: float = Field(5.0, gt=0)
    utc_offset_hours: float = 1.0  # branch local time, drives opening hours
    asym_rate: float = Field(0.7, ge=0, le=1)
    active_timeout_s: int = Field(60, ge=5)
    export_delay_s: float = 1.5  # probes export in batches, not per packet
    late_rate: float = Field(0.002, ge=0, le=1)
    late_max_s: int = 300
    duplicate_rate: float = Field(0.001, ge=0, le=1)
    fault_schedule: Literal["demo", "none"] = "demo"

    sink: Literal["kafka", "stdout", "null"] = "kafka"
    kafka_bootstrap: str = "localhost:9092"
    flows_topic: str = "netflow.halfflows.v1"
    faults_topic: str = "netflow.faults.v1"

    control_port: int = 8088  # 0 disables the control API
    metrics_port: int = 9102  # 0 disables /metrics
