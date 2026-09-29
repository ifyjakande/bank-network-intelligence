from __future__ import annotations

import sys
import time
from typing import Any, Protocol

import orjson
from prometheus_client import Counter

RECORDS = Counter("netgen_records_total", "Records handed to the sink", ["topic"])
DELIVERED = Counter("netgen_delivered_total", "Records acknowledged by the broker", ["topic"])
FAILED = Counter("netgen_delivery_failed_total", "Records the broker rejected", ["topic"])


class Sink(Protocol):
    def send(self, topic: str, key: str, value: dict[str, Any]) -> None: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class NullSink:
    def send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        RECORDS.labels(topic).inc()

    def poll(self) -> None:
        pass

    def close(self) -> None:
        pass


class StdoutSink:
    def send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        RECORDS.labels(topic).inc()
        sys.stdout.buffer.write(orjson.dumps({"topic": topic, **value}) + b"\n")

    def poll(self) -> None:
        pass

    def close(self) -> None:
        sys.stdout.flush()


class SinkStalled(RuntimeError):
    """The producer can no longer deliver; the process exits so Docker restarts it."""


class KafkaSink:
    """Kafka producer that fails loudly instead of running on with nothing delivered.

    An idempotent producer that hits a fatal error (possible after a broker stall) rejects
    every record from then on. Rather than generate forever into the void while looking
    healthy, `poll()` raises SinkStalled on a fatal error, or when records keep failing
    and none has been delivered for `stall_after_s`.
    """

    def __init__(self, bootstrap: str, stall_after_s: float = 120.0, producer: Any = None) -> None:
        if producer is None:
            from confluent_kafka import Producer

            # idempotent + acks=all: no loss and no broker-side duplicates on retry
            producer = Producer(
                {
                    "bootstrap.servers": bootstrap,
                    "enable.idempotence": True,
                    "acks": "all",
                    "compression.type": "zstd",
                    "linger.ms": 50,
                    "batch.size": 1_048_576,
                    "queue.buffering.max.messages": 2_000_000,
                    "client.id": "netgen",
                    "error_cb": self._on_error,
                }
            )
        self._p = producer
        self._stall_after_s = stall_after_s
        self._fatal: str | None = None
        self._last_delivered = time.monotonic()
        self._failing_since: float | None = None
        self._last_error: str | None = None

    def _on_error(self, err: Any) -> None:
        print(f"netgen: kafka error: {err}", file=sys.stderr)
        if err.fatal():
            self._fatal = str(err)

    def _on_delivery(self, err: Any, msg: Any) -> None:
        if err:
            FAILED.labels(msg.topic()).inc()
            if self._failing_since is None:
                self._failing_since = time.monotonic()
                print(f"netgen: delivery failing: {err}", file=sys.stderr)
            self._last_error = str(err)
        else:
            DELIVERED.labels(msg.topic()).inc()
            self._last_delivered = time.monotonic()
            self._failing_since = None

    def _check(self) -> None:
        if self._fatal:
            raise SinkStalled(f"fatal producer error: {self._fatal}")
        if self._failing_since is not None:
            quiet = time.monotonic() - self._last_delivered
            if quiet > self._stall_after_s:
                raise SinkStalled(
                    f"nothing delivered for {quiet:.0f}s, last error: {self._last_error}"
                )

    def send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        payload = orjson.dumps(value)
        while True:
            try:
                self._p.produce(topic, payload, key.encode(), on_delivery=self._on_delivery)
                break
            except BufferError:
                self._p.poll(0.1)  # local queue full: back-pressure instead of dropping
                self._check()
        RECORDS.labels(topic).inc()

    def poll(self) -> None:
        self._p.poll(0)
        self._check()

    def close(self) -> None:
        # a stalled producer would only time out here; don't hold up the restart
        remaining = self._p.flush(5 if self._fatal or self._failing_since else 30)
        if remaining:
            print(f"netgen: {remaining} records not delivered at shutdown", file=sys.stderr)


def make_sink(kind: str, bootstrap: str) -> Sink:
    if kind == "kafka":
        return KafkaSink(bootstrap)
    if kind == "stdout":
        return StdoutSink()
    return NullSink()
