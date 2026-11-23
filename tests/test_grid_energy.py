"""Grid energy integrated from Grid Power, for inverters whose counters read 0."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ef_powerocean_tcpmodbus.powerocean import const as device_const
from custom_components.ef_powerocean_tcpmodbus.powerocean.grid_energy import (
    GridEnergyIntegrator,
    split_energy,
)

START = datetime(2026, 11, 19, 10, tzinfo=UTC)
ZERO = dict.fromkeys(device_const.GRID_ENERGY_REGISTER_KEYS, 0.0)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    (
        (2000.0, 4000.0, (3.0, 0.0)),
        (-2000.0, -4000.0, (0.0, 3.0)),
        # Crossing 0 halfway: a quarter of the hour at an average of 1 kW each way.
        (1000.0, -1000.0, (0.25, 0.25)),
        (-1000.0, 3000.0, (1.125, 0.125)),
    ),
)
def test_import_and_export_are_split_where_grid_power_crosses_zero(
    before: float, after: float, expected: tuple[float, float]
) -> None:
    assert split_energy(before, after, 1.0) == pytest.approx(expected)


def test_counts_on_from_the_totals_published_last() -> None:
    integrator = GridEnergyIntegrator()
    integrator.start({"grid_import_total": 120.5, "grid_export_total": 3.25})

    integrator.add(3600.0, START)
    integrator.add(3600.0, START + timedelta(seconds=10))

    assert integrator.totals() == {
        "grid_import_total": 120.51,
        "grid_export_total": 3.25,
    }


@pytest.mark.parametrize(
    "readings",
    (
        # An outage between two readings.
        ((3600.0, 0), (3600.0, device_const.GRID_ENERGY_MAX_GAP_S + 1)),
        # A failed read of Grid Power in between.
        ((3600.0, 0), (None, 5), (3600.0, 10)),
    ),
)
def test_a_gap_in_the_readings_adds_no_energy(
    readings: tuple[tuple[float | None, int], ...],
) -> None:
    integrator = GridEnergyIntegrator()
    integrator.start({})

    for power, seconds in readings:
        integrator.add(power, START + timedelta(seconds=seconds))

    assert integrator.totals()["grid_import_total"] == 0


def test_integrates_without_counting_while_not_active() -> None:
    integrator = GridEnergyIntegrator()

    integrator.add(3600.0, START)
    added = integrator.add(3600.0, START + timedelta(seconds=10))

    assert added == pytest.approx((0.01, 0.0))
    assert integrator.import_kwh == 0


def _watch(integrator: GridEnergyIntegrator, kwh: float, at: datetime, **raw) -> None:
    integrator.observe_registers({**ZERO, **raw}, kwh, at)


def test_registers_at_zero_for_a_day_while_the_grid_is_used_do_not_count() -> None:
    integrator = GridEnergyIntegrator()
    _watch(integrator, 0.0, START)
    _watch(integrator, 0.6, START + timedelta(hours=12))
    _watch(integrator, 0.6, START + timedelta(hours=23))

    assert not integrator.registers_not_counting(START + timedelta(hours=23))
    assert integrator.registers_not_counting(START + timedelta(hours=24))


def test_registers_at_zero_while_the_grid_is_not_used_are_no_finding() -> None:
    integrator = GridEnergyIntegrator()
    _watch(integrator, 0.0, START)
    _watch(integrator, 0.5, START + timedelta(days=2))

    assert not integrator.registers_not_counting(START + timedelta(days=2))


def test_a_register_that_counts_starts_the_watch_over() -> None:
    integrator = GridEnergyIntegrator()
    _watch(integrator, 0.0, START)
    _watch(integrator, 5.0, START + timedelta(hours=12))
    _watch(integrator, 0.0, START + timedelta(hours=13), grid_import_today=0.1)
    _watch(integrator, 0.0, START + timedelta(hours=14))
    _watch(integrator, 5.0, START + timedelta(hours=30))

    assert not integrator.registers_not_counting(START + timedelta(hours=30))
    assert integrator.registers_not_counting(START + timedelta(hours=38))


def test_a_poll_without_the_registers_tells_nothing() -> None:
    integrator = GridEnergyIntegrator()
    _watch(integrator, 0.0, START)
    _watch(integrator, 2.0, START + timedelta(days=1), grid_export_total=None)

    assert integrator.zero_since == START
    assert integrator.kwh_while_zero == 0


def test_the_state_survives_a_restart() -> None:
    integrator = GridEnergyIntegrator()
    integrator.start({"grid_import_total": 7.5})
    _watch(integrator, 0.0, START)
    _watch(integrator, 1.5, START + timedelta(hours=1))

    restored = GridEnergyIntegrator()
    restored.load_state(integrator.dump_state())

    assert restored.dump_state() == integrator.dump_state()
