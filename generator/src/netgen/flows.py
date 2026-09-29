"""Session physics and half-flow export.

One session crosses the WAN twice: client->server through one gateway probe and
server->client through another (asymmetric routing is the normal case here). Each probe
exports only what it saw, so no single record can produce an RTT or a response time.
Those only exist once the two halves are stitched back together downstream:

    server-side RTT  = synack_us (s2c probe) - syn_us   (c2s probe)
    client-side RTT  = ack_us    (c2s probe) - synack_us (s2c probe)
    response time    = first_resp_us (s2c)   - first_req_us (c2s)
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import math
import random
import struct
from dataclasses import dataclass

from .apps import APPS, App
from .faults import FaultRegistry
from .topology import Device, Estate, Server

LAN_MS = 0.3  # one-way device -> branch router, including the access switch
ROUTER_MS = 0.4
DC_MS = 0.25  # one-way gateway -> server inside the data centre
BASE_LOSS = {"primary": 0.0005, "backup": 0.002}
FAILOVER_PENALTY = {"latency_ms": 30.0, "loss": 0.01}  # backup circuits are undersized
RTO_US = 200_000  # minimum TCP retransmission timeout
MSS = 1460
INTERNET_NET = ipaddress.ip_network("198.18.0.0/15")  # benchmark range, never routed


def community_id(
    src_ip: str, dst_ip: str, sport: int, dport: int, proto: int, seed: int = 0
) -> str:
    """Community ID v1 (https://github.com/corelight/community-id-spec)."""
    s, d = ipaddress.ip_address(src_ip).packed, ipaddress.ip_address(dst_ip).packed
    if (s, sport) > (d, dport):
        s, d, sport, dport = d, s, dport, sport
    data = struct.pack("!H", seed) + s + d + struct.pack("!BBHH", proto, 0, sport, dport)
    return "1:" + base64.b64encode(hashlib.sha1(data).digest()).decode()


@dataclass(slots=True)
class Session:
    community_id: str
    app: App
    client_ip: str
    server_ip: str
    client_port: int
    tunnel_id: int
    probe_c2s: str
    probe_s2c: str
    start_us: int
    end_us: int
    bytes_c2s: int
    bytes_s2c: int
    pkts_c2s: int
    pkts_s2c: int
    retrans_c2s: int
    retrans_s2c: int
    syn_us: int | None
    synack_us: int | None
    ack_us: int | None
    first_req_us: int | None
    first_resp_us: int | None
    next_export_us: int = 0
    exported_until_us: int = 0
    seq: int = 0


class SessionFactory:
    def __init__(
        self, estate: Estate, faults: FaultRegistry, rng: random.Random, asym_rate: float = 0.7
    ) -> None:
        self.e = estate
        self.faults = faults
        self.rng = rng
        self.asym_rate = asym_rate
        self._internet_hosts = int(INTERNET_NET.num_addresses) - 2

    def _pick_server(self, app: App, device: Device) -> tuple[str, Server | None, str]:
        if app.hosted == "internet":
            ip = str(INTERNET_NET.network_address + 1 + self.rng.randrange(self._internet_hosts))
            return ip, None, "DC1"  # all internet traffic backhauls to the DC1 edge
        srv = self.rng.choice(self.e.servers_by_app[app.name])
        return srv.ip, srv, srv.dc

    def create(self, device: Device, app_name: str, start_us: int) -> Session:
        rng, app = self.rng, APPS[app_name]
        branch = self.e.branches[device.branch_id]
        client_ip = device.ip
        if device.device_type == "guest":
            client_ip = client_ip.rsplit(".", 1)[0] + f".{rng.randint(100, 249)}"

        server_ip, server, dc = self._pick_server(app, device)
        gws = self.e.gateways_in(dc)
        probe_c2s = gws[rng.randrange(len(gws))].gateway_id
        probe_s2c = probe_c2s
        if rng.random() < self.asym_rate:
            probe_s2c = next(g.gateway_id for g in gws if g.gateway_id != probe_c2s)

        primary = self.e.circuits[branch.primary_circuit_id]
        path = {
            f"switch:{device.switch_id}",
            f"router:{branch.router_id}",
            f"circuit:{primary.circuit_id}",
            f"pop:{primary.pop_id}",
            f"gateway:{probe_c2s}",
            f"gateway:{probe_s2c}",
            f"app:{app.name}",
        }
        if server is not None:
            path.add(f"server:{server.server_id}")
        eff = self.faults.effect(frozenset(path), start_us)

        circuit, extra_ms, extra_loss = primary, 0.0, 0.0
        if primary.circuit_id in eff.down_circuits:
            circuit = self.e.circuits[branch.backup_circuit_id]
            extra_ms, extra_loss = FAILOVER_PENALTY["latency_ms"], FAILOVER_PENALTY["loss"]
            # the backup path goes through its own PoP, so re-evaluate faults on that leg
            leg = self.faults.effect(
                frozenset({f"circuit:{circuit.circuit_id}", f"pop:{circuit.pop_id}"}), start_us
            )
            extra_ms += leg.latency_ms
            extra_loss = 1 - (1 - extra_loss) * (1 - leg.loss)
        pop = self.e.pops[circuit.pop_id]

        jitter = rng.lognormvariate(0, 0.15)
        client_one_way = (LAN_MS + ROUTER_MS + circuit.access_ms + pop.backbone_ms) * jitter
        client_rtt_ms = 2 * client_one_way + eff.latency_ms + extra_ms
        server_rtt_ms = 2 * DC_MS * rng.lognormvariate(0, 0.2)
        if server is None:
            server_rtt_ms += rng.uniform(15, 60)  # internet destinations sit past the edge
        loss = 1 - (1 - BASE_LOSS[circuit.role]) * (1 - eff.loss) * (1 - extra_loss)

        bytes_c2s = max(64, int(rng.lognormvariate(math.log(app.bytes_c2s), 0.8)))
        bytes_s2c = max(64, int(rng.lognormvariate(math.log(app.bytes_s2c), 0.8)))
        pkts_c2s = max(1, math.ceil(bytes_c2s / (MSS if bytes_c2s > 5e4 else 400)))
        pkts_s2c = max(1, math.ceil(bytes_s2c / (MSS if bytes_s2c > 5e4 else 400)))

        # a lossy or slow path stretches bulk transfers (Mathis et al. TCP throughput bound)
        rtt_s = max(client_rtt_ms + server_rtt_ms, 1.0) / 1000
        mathis_bps = (MSS * 8 * 1.22) / (rtt_s * math.sqrt(max(loss, 1e-6)))
        link_bps = circuit.bandwidth_mbps * 1e6 * rng.uniform(0.15, 0.5)
        transfer_s = max(bytes_c2s, bytes_s2c) * 8 / min(mathis_bps, link_bps)
        duration_s = max(rng.lognormvariate(math.log(app.duration_s), 0.5), transfer_s)
        end_us = start_us + int(duration_s * 1e6)

        retrans_c2s = rng.binomialvariate(pkts_c2s, loss)
        retrans_s2c = rng.binomialvariate(pkts_s2c, loss)

        syn = synack = ack = req = resp = None
        if app.ip_proto == 6:
            syn = start_us
            synack = syn + int(server_rtt_ms * 1000)
            ack = synack + int(client_rtt_ms * 1000)
            tls_rtts = 2 if app.l7 == "tls1.2" else 1
            req = ack + int(tls_rtts * (client_rtt_ms + server_rtt_ms) * 1000)
            think_ms = rng.lognormvariate(math.log(max(app.server_ms, 1.0)), 0.35)
            think_ms *= eff.server_factor
            resp = req + int((server_rtt_ms + think_ms + eff.server_extra_ms) * 1000)
            # loss in the first exchange costs at least one retransmission timeout
            if rng.random() < 1 - (1 - loss) ** 4:
                resp += RTO_US + int(rng.uniform(0, 1) * client_rtt_ms * 1000)
            end_us = max(end_us, resp + 1000)
        else:
            req = start_us
            resp = start_us + int((client_rtt_ms + server_rtt_ms) * 1000)

        sport = rng.randint(32768, 60999)
        cid = community_id(client_ip, server_ip, sport, app.dst_port, app.ip_proto)
        return Session(
            community_id=cid,
            app=app,
            client_ip=client_ip,
            server_ip=server_ip,
            client_port=sport,
            tunnel_id=circuit.tunnel_id,
            probe_c2s=probe_c2s,
            probe_s2c=probe_s2c,
            start_us=start_us,
            end_us=end_us,
            bytes_c2s=bytes_c2s,
            bytes_s2c=bytes_s2c,
            pkts_c2s=pkts_c2s,
            pkts_s2c=pkts_s2c,
            retrans_c2s=retrans_c2s,
            retrans_s2c=retrans_s2c,
            syn_us=syn,
            synack_us=synack,
            ack_us=ack,
            first_req_us=req,
            first_resp_us=resp,
            next_export_us=start_us,
            exported_until_us=start_us,
        )


def _share(total: int, lo: int, hi: int, start: int, end: int) -> int:
    """Portion of `total` that falls in [lo, hi) of a session spanning [start, end)."""
    span = max(end - start, 1)
    return round(total * (hi - start) / span) - round(total * (lo - start) / span)


def record_id(cid: str, first_seen_us: int, probe: str, direction: str, seq: int) -> str:
    # 5-tuples get reused (same device, same ephemeral port, a minute later), so the
    # community id alone is not a session key; the probe's first-seen time makes it one
    key = f"{cid}|{first_seen_us}|{probe}|{direction}|{seq}"
    return hashlib.blake2b(key.encode(), digest_size=8).hexdigest()


def first_seen(s: Session) -> tuple[int, int]:
    """When each probe saw the session's first packet in its own direction."""
    s2c = s.synack_us if s.synack_us is not None else s.first_resp_us
    return s.start_us, s2c if s2c is not None else s.start_us


def export(s: Session, until_us: int, exported_at_us: int) -> list[dict[str, object]]:
    """Both half-flow records covering [exported_until_us, until_us)."""
    lo, hi = s.exported_until_us, min(until_us, s.end_us)
    seen_c2s, seen_s2c = first_seen(s)
    final = hi >= s.end_us
    first = s.seq == 0
    tcp_first = first and s.app.ip_proto == 6  # request/response timing is a TCP measurement
    common: dict[str, object] = {
        "exported_at_us": exported_at_us,
        "community_id": s.community_id,
        "ip_proto": s.app.ip_proto,
        "tunnel_id": s.tunnel_id,
        "app": s.app.name,
        "app_category": s.app.category,
        "l7_proto": s.app.l7,
        "flow_start_us": lo,
        "flow_end_us": hi,
        "record_seq": s.seq,
        "is_final": int(final),
    }
    c2s = {
        **common,
        "record_id": record_id(s.community_id, seen_c2s, s.probe_c2s, "c2s", s.seq),
        "probe_id": s.probe_c2s,
        "direction": "c2s",
        "first_seen_us": seen_c2s,
        "src_ip": s.client_ip,
        "dst_ip": s.server_ip,
        "src_port": s.client_port,
        "dst_port": s.app.dst_port,
        "bytes": _share(s.bytes_c2s, lo, hi, s.start_us, s.end_us),
        "packets": _share(s.pkts_c2s, lo, hi, s.start_us, s.end_us),
        "retrans_packets": _share(s.retrans_c2s, lo, hi, s.start_us, s.end_us),
        "tcp_syn_us": s.syn_us if first else None,
        "tcp_synack_us": None,
        "tcp_ack_us": s.ack_us if first else None,
        "first_req_us": s.first_req_us if tcp_first else None,
        "first_resp_us": None,
    }
    s2c = {
        **common,
        "record_id": record_id(s.community_id, seen_s2c, s.probe_s2c, "s2c", s.seq),
        "probe_id": s.probe_s2c,
        "direction": "s2c",
        "first_seen_us": seen_s2c,
        "src_ip": s.server_ip,
        "dst_ip": s.client_ip,
        "src_port": s.app.dst_port,
        "dst_port": s.client_port,
        "bytes": _share(s.bytes_s2c, lo, hi, s.start_us, s.end_us),
        "packets": _share(s.pkts_s2c, lo, hi, s.start_us, s.end_us),
        "retrans_packets": _share(s.retrans_s2c, lo, hi, s.start_us, s.end_us),
        "tcp_syn_us": None,
        "tcp_synack_us": s.synack_us if first else None,
        "tcp_ack_us": None,
        "first_req_us": None,
        "first_resp_us": s.first_resp_us if tcp_first else None,
    }
    s.exported_until_us = hi
    s.seq += 1
    return [c2s, s2c]
