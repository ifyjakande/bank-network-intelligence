from __future__ import annotations

from netgen.faults import FaultRegistry, FaultSpec
from netgen.flows import SessionFactory, community_id, export
from netgen.topology import Estate

T0 = 1_790_676_000_000_000
HOUR = 3_600_000_000


def test_community_id_matches_spec_vector() -> None:
    # from the community-id-spec README
    cid = community_id("128.232.110.120", "66.35.250.204", 34855, 80, 6)
    assert cid == "1:LQU9qZlK+B5F3KDmev6m5PMibrg="


def test_community_id_is_direction_independent() -> None:
    assert community_id("10.0.0.1", "10.0.0.2", 1234, 443, 6) == community_id(
        "10.0.0.2", "10.0.0.1", 443, 1234, 6
    )


def _atm(estate: Estate, branch: str = "BR-0017"):  # type: ignore[no-untyped-def]
    return next(d for d in estate.devices_by_type["atm"] if d.branch_id == branch)


def test_each_half_carries_only_what_its_probe_saw(estate: Estate, factory: SessionFactory) -> None:
    s = factory.create(_atm(estate), "card_authorisation", T0)
    c2s, s2c = export(s, s.end_us, s.end_us)
    assert c2s["community_id"] == s2c["community_id"]
    assert c2s["probe_id"] == s.probe_c2s and s2c["probe_id"] == s.probe_s2c
    assert c2s["tcp_syn_us"] and c2s["tcp_ack_us"] and c2s["first_req_us"]
    assert c2s["tcp_synack_us"] is None and c2s["first_resp_us"] is None
    assert s2c["tcp_synack_us"] and s2c["first_resp_us"]
    assert s2c["tcp_syn_us"] is None and s2c["first_req_us"] is None
    # stitched timings are only meaningful once both halves are joined
    assert s2c["tcp_synack_us"] > c2s["tcp_syn_us"]
    assert c2s["tcp_ack_us"] > s2c["tcp_synack_us"]
    assert s2c["first_resp_us"] > c2s["first_req_us"]
    assert (c2s["src_ip"], c2s["dst_ip"]) == (s2c["dst_ip"], s2c["src_ip"])


def test_routing_is_mostly_asymmetric(estate: Estate, factory: SessionFactory) -> None:
    sessions = [factory.create(_atm(estate), "card_authorisation", T0) for _ in range(2000)]
    share = sum(s.probe_c2s != s.probe_s2c for s in sessions) / len(sessions)
    assert 0.65 < share < 0.75


def test_interim_exports_conserve_volume(estate: Estate, factory: SessionFactory) -> None:
    cctv = estate.devices_by_type["cctv"][0]
    s = factory.create(cctv, "cctv_upload", T0)
    assert s.end_us - s.start_us > 60_000_000, "needs a long session"
    total_c2s, seqs = 0, []
    cut = s.start_us
    while s.exported_until_us < s.end_us:
        cut = min(s.end_us, cut + 60_000_000)
        c2s, _ = export(s, cut, cut)
        total_c2s += int(c2s["bytes"])  # type: ignore[call-overload]
        seqs.append(c2s["record_seq"])
        assert (c2s["tcp_syn_us"] is None) == (c2s["record_seq"] != 0)
    assert total_c2s == s.bytes_c2s
    assert seqs == list(range(len(seqs)))
    assert c2s["is_final"] == 1


def _median_client_rtt(factory: SessionFactory, estate: Estate, n: int = 300) -> float:
    rtts = []
    for _ in range(n):
        s = factory.create(_atm(estate), "card_authorisation", T0)
        assert s.ack_us and s.synack_us
        rtts.append(s.ack_us - s.synack_us)
    return sorted(rtts)[n // 2] / 1000


def test_degraded_circuit_hurts_only_its_branch(
    estate: Estate, registry: FaultRegistry, factory: SessionFactory
) -> None:
    br = estate.branches["BR-0017"]
    before = _median_client_rtt(factory, estate)
    registry.add(
        FaultSpec("degrade", f"circuit:{br.primary_circuit_id}", latency_ms=80, loss=0.05),
        T0 - 1,
        T0 + HOUR,
        "test",
    )
    after = _median_client_rtt(factory, estate)
    assert after - before > 60
    other = next(d for d in estate.devices_by_type["atm"] if d.branch_id != "BR-0017")
    unaffected = factory.create(other, "card_authorisation", T0)
    assert unaffected.ack_us and unaffected.synack_us
    assert (unaffected.ack_us - unaffected.synack_us) / 1000 < before + 40


def test_circuit_down_fails_over_to_backup(
    estate: Estate, registry: FaultRegistry, factory: SessionFactory
) -> None:
    br = estate.branches["BR-0017"]
    primary, backup = estate.circuits[br.primary_circuit_id], estate.circuits[br.backup_circuit_id]
    assert factory.create(_atm(estate), "card_authorisation", T0).tunnel_id == primary.tunnel_id
    registry.add(FaultSpec("down", f"circuit:{primary.circuit_id}"), T0 - 1, T0 + HOUR, "test")
    assert factory.create(_atm(estate), "card_authorisation", T0).tunnel_id == backup.tunnel_id


def test_slow_server_raises_response_time_not_rtt(
    estate: Estate, registry: FaultRegistry, factory: SessionFactory
) -> None:
    def sample() -> tuple[float, float]:
        resp, rtt = [], []
        for _ in range(300):
            s = factory.create(_atm(estate), "card_authorisation", T0)
            if s.server_ip != estate.servers["cas-02"].ip:
                continue
            assert s.first_resp_us and s.first_req_us and s.ack_us and s.synack_us
            resp.append(s.first_resp_us - s.first_req_us)
            rtt.append(s.ack_us - s.synack_us)
        return sorted(resp)[len(resp) // 2] / 1000, sorted(rtt)[len(rtt) // 2] / 1000

    resp0, rtt0 = sample()
    registry.add(FaultSpec("slow", "server:cas-02", server_factor=6), T0 - 1, T0 + HOUR, "test")
    resp1, rtt1 = sample()
    assert resp1 > 3 * resp0
    assert abs(rtt1 - rtt0) < 10
