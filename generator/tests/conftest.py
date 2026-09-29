from __future__ import annotations

import random
from typing import Any

import pytest

from netgen.faults import FaultRegistry
from netgen.flows import SessionFactory
from netgen.topology import Estate, build_estate


class CaptureSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        self.sent.append((topic, key, dict(value)))

    def poll(self) -> None:
        pass

    def close(self) -> None:
        pass

    def topic(self, name: str) -> list[dict[str, Any]]:
        return [v for t, _, v in self.sent if t == name]


@pytest.fixture(scope="session")
def estate() -> Estate:
    return build_estate(seed=42, n_branches=400)


@pytest.fixture
def registry(estate: Estate) -> FaultRegistry:
    return FaultRegistry(estate)


@pytest.fixture
def factory(estate: Estate, registry: FaultRegistry) -> SessionFactory:
    return SessionFactory(estate, registry, random.Random(7))
