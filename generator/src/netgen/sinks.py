from __future__ import annotations

import sys
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


class KafkaSink:
    def __init__(self, bootstrap: str) -> None:
        from confluent_kafka import Producer

        # idempotent + acks=all: no loss and no broker-side duplicates on retry
        self._p = Producer(
            {
                "bootstrap.servers": bootstrap,
                "enable.idempotence": True,
                "acks": "all",
                "compression.type": "zstd",
                "linger.ms": 50,
                "batch.size": 1_048_576,
                "queue.buffering.max.messages": 2_000_000,
                "client.id": "netgen",
            }
        )

    def _on_delivery(self, err: Any, msg: Any) -> None:
        (FAILED if err else DELIVERED).labels(msg.topic()).inc()

    def send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        payload = orjson.dumps(value)
        while True:
            try:
                self._p.produce(topic, payload, key.encode(), on_delivery=self._on_delivery)
                break
            except BufferError:
                self._p.poll(0.1)  # local queue full: back-pressure instead of dropping
        RECORDS.labels(topic).inc()

    def poll(self) -> None:
        self._p.poll(0)

    def close(self) -> None:
        remaining = self._p.flush(30)
        if remaining:
            print(f"netgen: {remaining} records not delivered at shutdown", file=sys.stderr)


def make_sink(kind: str, bootstrap: str) -> Sink:
    if kind == "kafka":
        return KafkaSink(bootstrap)
    if kind == "stdout":
        return StdoutSink()
    return NullSink()
