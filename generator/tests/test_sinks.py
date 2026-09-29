from __future__ import annotations

from typing import Any

import pytest

from netgen.sinks import KafkaSink, SinkStalled


class FakeError:
    def __init__(self, text: str, fatal: bool = False) -> None:
        self.text, self._fatal = text, fatal

    def fatal(self) -> bool:
        return self._fatal

    def __str__(self) -> str:
        return self.text


class FakeMsg:
    def topic(self) -> str:
        return "t"


class FakeProducer:
    """Delivers or fails every record on the next poll, like librdkafka's callbacks."""

    def __init__(self) -> None:
        self.fail: FakeError | None = None
        self.queued: list[Any] = []
        self.flushed_with: float | None = None

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any) -> None:
        self.queued.append(on_delivery)

    def poll(self, timeout: float) -> int:
        for cb in self.queued:
            cb(self.fail, FakeMsg())
        n, self.queued = len(self.queued), []
        return n

    def flush(self, timeout: float) -> int:
        self.flushed_with = timeout
        return 0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr("netgen.sinks.time.monotonic", lambda: now[0])
    return now


def test_healthy_producer_never_stalls(clock: list[float]) -> None:
    p = FakeProducer()
    sink = KafkaSink("x", stall_after_s=120, producer=p)
    for _ in range(10):
        sink.send("t", "k", {"a": 1})
        clock[0] += 60
        sink.poll()


def test_short_failure_burst_is_tolerated(clock: list[float]) -> None:
    p = FakeProducer()
    sink = KafkaSink("x", stall_after_s=120, producer=p)
    p.fail = FakeError("Local: Message timed out")
    sink.send("t", "k", {})
    clock[0] += 60
    sink.poll()  # failing, but only for 60 s
    p.fail = None
    sink.send("t", "k", {})
    clock[0] += 100
    sink.poll()  # delivered again: the stall clock resets


def test_sustained_failure_raises(clock: list[float]) -> None:
    p = FakeProducer()
    sink = KafkaSink("x", stall_after_s=120, producer=p)
    p.fail = FakeError("Local: Fatal error")
    sink.send("t", "k", {})
    sink.poll()
    clock[0] += 121
    sink.send("t", "k", {})
    with pytest.raises(SinkStalled, match="nothing delivered for 121s"):
        sink.poll()


def test_fatal_error_raises_at_once_and_close_does_not_wait(clock: list[float]) -> None:
    p = FakeProducer()
    sink = KafkaSink("x", producer=p)
    sink._on_error(FakeError("idempotent producer fenced", fatal=True))
    with pytest.raises(SinkStalled, match="fatal producer error"):
        sink.poll()
    sink.close()
    assert p.flushed_with == 5


def test_non_fatal_client_error_is_only_logged(clock: list[float]) -> None:
    sink = KafkaSink("x", producer=FakeProducer())
    sink._on_error(FakeError("broker transport failure"))
    sink.poll()
