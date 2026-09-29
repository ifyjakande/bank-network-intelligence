from __future__ import annotations

from netgen.topology import Estate, build_estate


def test_same_seed_same_estate() -> None:
    a, b = build_estate(1, 50), build_estate(1, 50)
    assert a.branches == b.branches
    assert a.devices == b.devices
    assert build_estate(2, 50).branches != a.branches


def test_estate_shape(estate: Estate) -> None:
    assert len(estate.branches) == 400
    assert len(estate.pops) == 24
    assert len(estate.circuits) == 800
    assert 1800 <= len(estate.devices_by_type["atm"]) <= 2300


def test_ids_and_addresses_are_unique(estate: Estate) -> None:
    ips = [d.ip for d in estate.devices.values()]
    assert len(ips) == len(set(ips))
    tunnels = [c.tunnel_id for c in estate.circuits.values()]
    assert len(tunnels) == len(set(tunnels))


def test_primary_and_backup_use_different_providers_in_the_same_region(estate: Estate) -> None:
    for b in estate.branches.values():
        primary = estate.circuits[b.primary_circuit_id]
        backup = estate.circuits[b.backup_circuit_id]
        assert primary.provider != backup.provider
        assert estate.pops[primary.pop_id].region == b.region
        assert estate.pops[backup.pop_id].region == b.region
        assert backup.bandwidth_mbps <= primary.bandwidth_mbps


def test_payment_kit_sits_on_the_first_switch(estate: Estate) -> None:
    for d in estate.devices_by_type["atm"] + estate.devices_by_type["card_terminal"]:
        assert d.switch_id == estate.branches[d.branch_id].switch_ids[0]
