"""Deterministic model of the bank's branch estate and WAN.

Everything is derived from the seed, so the generator, the inventory export and
the tests always agree on the same estate.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field

# region -> (short code, one-way access latency from a branch to its provider's PoP in ms)
REGION_TABLE: dict[str, tuple[str, float]] = {
    "North": ("NTH", 12.0), "North-East": ("NEA", 15.0), "East": ("EST", 9.0),
    "Central": ("CEN", 4.0), "West": ("WST", 10.0), "South-West": ("SWE", 14.0),
    "South": ("STH", 11.0), "Coast": ("CST", 7.0),
}  # fmt: skip
REGIONS: tuple[str, ...] = tuple(REGION_TABLE)
PROVIDERS: tuple[tuple[str, str, float], ...] = (
    ("MER", "Meridian Telecom", 1.00),
    ("SAV", "Savanna Fibre", 1.15),
    ("CRS", "Crescent Networks", 0.90),
)
DATA_CENTRES: tuple[str, ...] = ("DC1", "DC2")

SIZES: tuple[tuple[str, float], ...] = (
    ("small", 0.50),
    ("medium", 0.30),
    ("large", 0.15),
    ("flagship", 0.05),
)
# (min, max) devices per branch size
DEVICE_COUNTS: dict[str, dict[str, tuple[int, int]]] = {
    "teller": {"small": (2, 4), "medium": (4, 8), "large": (8, 14), "flagship": (14, 20)},
    "backoffice": {"small": (1, 3), "medium": (3, 6), "large": (6, 12), "flagship": (12, 20)},
    "atm": {"small": (3, 6), "medium": (4, 7), "large": (5, 7), "flagship": (6, 8)},
    "card_terminal": {"small": (1, 2), "medium": (2, 4), "large": (3, 6), "flagship": (4, 8)},
    "cctv": {"small": (1, 1), "medium": (1, 1), "large": (2, 2), "flagship": (2, 2)},
}
SWITCHES_BY_SIZE = {"small": 1, "medium": 2, "large": 3, "flagship": 4}
PRIMARY_MBPS = {"small": 20, "medium": 50, "large": 100, "flagship": 200}
BACKUP_MBPS = {"small": 10, "medium": 20, "large": 20, "flagship": 50}
# first host octet per device type inside the branch /24
HOST_BASE = {"teller": 20, "backoffice": 60, "atm": 120, "card_terminal": 150, "cctv": 200}
# payment and physical-security kit sits on the first switch, staff on the rest
SW1_TYPES = frozenset({"atm", "card_terminal", "cctv"})


@dataclass(frozen=True, slots=True)
class Provider:
    code: str
    name: str
    latency_factor: float


@dataclass(frozen=True, slots=True)
class Pop:
    pop_id: str
    provider: str
    region: str
    backbone_ms: float  # one-way PoP -> data centre


@dataclass(frozen=True, slots=True)
class Gateway:
    gateway_id: str
    dc: str


@dataclass(frozen=True, slots=True)
class Circuit:
    circuit_id: str
    tunnel_id: int
    branch_id: str
    provider: str
    pop_id: str
    role: str  # primary | backup
    bandwidth_mbps: int
    access_ms: float  # one-way branch -> PoP


@dataclass(frozen=True, slots=True)
class Branch:
    branch_id: str
    index: int
    region: str
    size: str
    lan_prefix: str
    guest_prefix: str
    router_id: str
    switch_ids: tuple[str, ...]
    primary_circuit_id: str
    backup_circuit_id: str


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    device_type: str
    branch_id: str
    switch_id: str
    ip: str


@dataclass(frozen=True, slots=True)
class Server:
    server_id: str
    app: str
    dc: str
    ip: str


@dataclass
class Estate:
    seed: int
    providers: dict[str, Provider] = field(default_factory=dict)
    pops: dict[str, Pop] = field(default_factory=dict)
    gateways: dict[str, Gateway] = field(default_factory=dict)
    circuits: dict[str, Circuit] = field(default_factory=dict)
    branches: dict[str, Branch] = field(default_factory=dict)
    devices: dict[str, Device] = field(default_factory=dict)
    servers: dict[str, Server] = field(default_factory=dict)
    devices_by_type: dict[str, list[Device]] = field(default_factory=lambda: defaultdict(list))
    servers_by_app: dict[str, list[Server]] = field(default_factory=lambda: defaultdict(list))

    def gateways_in(self, dc: str) -> list[Gateway]:
        return [g for g in self.gateways.values() if g.dc == dc]


def _lan_octets(index: int) -> tuple[int, int]:
    return 16 + index // 256, index % 256


# (app, server count per DC) - internet-hosted apps have no servers here
SERVER_LAYOUT: dict[str, tuple[tuple[str, int], ...]] = {
    "core_banking": (("DC1", 2), ("DC2", 1)),
    "card_authorisation": (("DC1", 1), ("DC2", 1)),
    "atm_management": (("DC2", 1),),
    "swift": (("DC1", 1),),
    "file_share": (("DC2", 1),),
    "cctv_upload": (("DC2", 1),),
}
SERVER_PREFIX = {
    "core_banking": "cbs",
    "card_authorisation": "cas",
    "atm_management": "atmm",
    "swift": "swift",
    "file_share": "fs",
    "cctv_upload": "vms",
}


def build_estate(seed: int = 42, n_branches: int = 400) -> Estate:
    if not 1 <= n_branches <= 4096:
        raise ValueError("n_branches must be between 1 and 4096")
    rng = random.Random(seed)
    est = Estate(seed=seed)

    for code, name, factor in PROVIDERS:
        est.providers[code] = Provider(code, name, factor)
        for region in REGIONS:
            pop_id = f"{code}-{REGION_TABLE[region][0]}"
            est.pops[pop_id] = Pop(pop_id, code, region, round(rng.uniform(2.0, 8.0), 2))

    for dc in DATA_CENTRES:
        for side in ("A", "B"):
            gid = f"{dc}-GW-{side}"
            est.gateways[gid] = Gateway(gid, dc)

    ip_seq = 10
    for app, layout in SERVER_LAYOUT.items():
        n = 1
        for dc, count in layout:
            for _ in range(count):
                sid = f"{SERVER_PREFIX[app]}-{n:02d}"
                octet = 1 if dc == "DC1" else 2
                srv = Server(sid, app, dc, f"10.200.{octet}.{ip_seq}")
                est.servers[sid] = srv
                est.servers_by_app[app].append(srv)
                n += 1
                ip_seq += 1

    size_names = [s for s, _ in SIZES]
    size_weights = [w for _, w in SIZES]
    provider_codes = [p[0] for p in PROVIDERS]
    tunnel = 1000

    for idx in range(n_branches):
        bid = f"BR-{idx:04d}"
        region = REGIONS[rng.randrange(len(REGIONS))]
        size = rng.choices(size_names, size_weights)[0]
        o2, o3 = _lan_octets(idx)
        n_sw = SWITCHES_BY_SIZE[size]
        switch_ids = tuple(f"{bid}-SW{i + 1}" for i in range(n_sw))

        primary_p, backup_p = rng.sample(provider_codes, 2)
        circuits = []
        for role, prov in (("primary", primary_p), ("backup", backup_p)):
            tunnel += 1
            pop = next(p for p in est.pops.values() if p.provider == prov and p.region == region)
            base = REGION_TABLE[region][1] * est.providers[prov].latency_factor
            if role == "backup":
                base *= 1.6  # backup links are usually a cheaper, longer path
            mbps = PRIMARY_MBPS[size] if role == "primary" else BACKUP_MBPS[size]
            c = Circuit(
                circuit_id=f"CKT-{tunnel}",
                tunnel_id=tunnel,
                branch_id=bid,
                provider=prov,
                pop_id=pop.pop_id,
                role=role,
                bandwidth_mbps=mbps,
                access_ms=round(base * rng.uniform(0.8, 1.2), 2),
            )
            est.circuits[c.circuit_id] = c
            circuits.append(c)

        est.branches[bid] = Branch(
            branch_id=bid,
            index=idx,
            region=region,
            size=size,
            lan_prefix=f"10.{o2}.{o3}.0/24",
            guest_prefix=f"172.{o2}.{o3}.0/24",
            router_id=f"{bid}-RTR",
            switch_ids=switch_ids,
            primary_circuit_id=circuits[0].circuit_id,
            backup_circuit_id=circuits[1].circuit_id,
        )

        staff_switches = switch_ids[1:] or switch_ids
        for dtype, by_size in DEVICE_COUNTS.items():
            lo, hi = by_size[size]
            for n in range(rng.randint(lo, hi)):
                sw = (
                    switch_ids[0] if dtype in SW1_TYPES else staff_switches[n % len(staff_switches)]
                )
                dev = Device(
                    device_id=f"{bid}-{dtype.upper().replace('_', '')[:4]}{n + 1:02d}",
                    device_type=dtype,
                    branch_id=bid,
                    switch_id=sw,
                    ip=f"10.{o2}.{o3}.{HOST_BASE[dtype] + n}",
                )
                est.devices[dev.device_id] = dev
                est.devices_by_type[dtype].append(dev)

        # guest wi-fi is one pseudo-device per branch; sessions pick a random DHCP host
        guest = Device(
            device_id=f"{bid}-GUEST",
            device_type="guest",
            branch_id=bid,
            switch_id=staff_switches[-1],
            ip=f"172.{o2}.{o3}.0",
        )
        est.devices[guest.device_id] = guest
        est.devices_by_type["guest"].append(guest)

    return est
