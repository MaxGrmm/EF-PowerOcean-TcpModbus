"""How the control test plans a run, judges it, and compares two reports."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ef_powerocean_tcpmodbus.control_test_core import (
    CORE_TESTS,
    ControlTestReport,
    Difference,
    FeatureResult,
    Plan,
    Verdict,
    compare_reports,
    compose_control_word,
    judge,
    judge_modes,
    plan_test,
)
from custom_components.ef_powerocean_tcpmodbus.models import (
    ControlStatus,
    GridFeedMode,
)

TESTS = {test.name: test for test in CORE_TESTS}


def frame(**values: Any) -> dict[str, Any]:
    return {
        "battery_soc": 50.0,
        "min_soc_limit": 10,
        "battery_power": 0.0,
        "grid_power": 300.0,
        "solar_power": 0.0,
        "house_power": 300.0,
        "inverter_output_power": 0.0,
        "grid_feed_mode": GridFeedMode.UNLIMITED,
        **values,
    }


def test_a_full_battery_skips_a_charge() -> None:
    plan, reason = plan_test(TESTS["battery charge"], frame(battery_soc=97), 1500)

    assert plan is None
    assert "too full" in reason


def test_a_battery_near_its_reserve_skips_a_discharge() -> None:
    plan, reason = plan_test(
        TESTS["battery discharge"], frame(battery_soc=14, min_soc_limit=10), 1500
    )

    assert plan is None
    assert "reserve" in reason


def test_an_export_cap_lowers_the_grid_feed_power() -> None:
    plan, _ = plan_test(
        TESTS["grid feed"],
        frame(grid_feed_mode=GridFeedMode.LIMITED, feed_in_power_max=800),
        1500,
    )

    assert plan is not None
    assert plan.power == 800
    assert "export cap" in plan.notes[0]


def test_a_power_already_flowing_is_moved_to_one_that_tells() -> None:
    """Charging at 1500 W already would make a 1500 W charge prove nothing."""
    plan, _ = plan_test(TESTS["battery charge"], frame(battery_power=1500.0), 1500)

    assert plan is not None
    assert plan.decisive
    assert plan.power != 1500


def test_a_settled_command_is_followed() -> None:
    test = TESTS["battery charge"]
    plan, _ = plan_test(test, frame(), 1500)
    assert plan is not None

    verdict, _detail, achieved = judge(
        test, plan, 12.0, ControlStatus.ACTIVE, [1400.0, 1500.0, 1500.0]
    )

    assert verdict is Verdict.FOLLOWED
    assert achieved == pytest.approx(1466.7, abs=0.1)


def test_a_command_never_reached_is_not_followed() -> None:
    test = TESTS["battery charge"]
    plan, _ = plan_test(test, frame(), 1500)
    assert plan is not None

    verdict, detail, _ = judge(test, plan, None, ControlStatus.RAMPING, [0.0, 0.0])

    assert verdict is Verdict.NOT_FOLLOWED
    assert "+0 W against +1500 W" in detail


def test_a_full_battery_during_the_command_is_unreachable() -> None:
    test = TESTS["battery charge"]
    plan, _ = plan_test(test, frame(), 1500)
    assert plan is not None

    verdict, _, _ = judge(
        test, plan, None, ControlStatus.UNREACHABLE_BATTERY_FULL, [0.0]
    )

    assert verdict is Verdict.UNREACHABLE


def test_the_reported_method_has_to_hold_for_the_last_polls() -> None:
    assert (
        judge_modes("battery_limits", [(1, "default"), (2, "battery_limits")] * 2)[0]
        is False
    )
    assert judge_modes(
        "battery_limits", [(1, "default"), *((t, "battery_limits") for t in (2, 3, 4))]
    ) == (True, "reports battery_limits within 2s")


def report(**verdicts: Verdict | tuple[Verdict, float]) -> dict[str, Any]:
    run = ControlTestReport(source="test", created_at="now", heartbeat="accepted")
    for name, value in verdicts.items():
        verdict, settle = value if isinstance(value, tuple) else (value, None)
        result = FeatureResult.for_test(TESTS[name.replace("_", " ")], verdict)
        result.settle_s = settle
        result.method_reported = verdict is Verdict.FOLLOWED
        run.features.append(result)
    return run.to_dict()


def test_identical_reports_have_no_differences() -> None:
    old = report(battery_charge=(Verdict.FOLLOWED, 15.0))

    assert compare_reports(old, old) == []


def test_a_method_the_new_firmware_ignores_is_significant() -> None:
    differences = compare_reports(
        report(inverter_feed=Verdict.FOLLOWED),
        report(inverter_feed=Verdict.NOT_FOLLOWED),
    )

    assert [(d.feature, d.what, d.significant) for d in differences] == [
        ("inverter feed", "verdict", True)
    ]


def test_a_test_the_weather_skipped_is_noted_but_not_significant() -> None:
    differences = compare_reports(
        report(battery_charge=Verdict.FOLLOWED),
        report(battery_charge=Verdict.SKIPPED),
    )

    assert [d.significant for d in differences] == [False]


def test_a_much_slower_ramp_is_significant_and_jitter_is_not() -> None:
    old = report(battery_charge=(Verdict.FOLLOWED, 15.0))

    assert compare_reports(old, report(battery_charge=(Verdict.FOLLOWED, 20.0))) == []
    slower = compare_reports(old, report(battery_charge=(Verdict.FOLLOWED, 45.0)))
    assert [(d.what, d.significant) for d in slower] == [("settle time", True)]


def test_a_hand_back_no_longer_seen_is_significant() -> None:
    old, new = report(), report()
    old["handback_s"] = 62.0

    differences = compare_reports(old, new)

    assert [(d.feature, d.significant) for d in differences] == [("hand back", True)]


def test_decisive_verdicts_are_shouted_in_the_script() -> None:
    assert Verdict.FOLLOWED.label == "FOLLOWED"
    assert Verdict.NOT_FOLLOWED.label == "NOT FOLLOWED"
    assert Verdict.NOT_TESTED.label == "not tested"


def test_the_control_word_carries_the_battery_saver_bit() -> None:
    method = TESTS["battery charge"].method
    plain = compose_control_word(method, battery_saver=False)
    saving = compose_control_word(method, battery_saver=True)

    assert saving & ~plain
    assert saving & plain == plain


def test_an_unreadable_inverter_output_is_battery_minus_solar() -> None:
    test = TESTS["inverter feed"]
    readable = frame(battery_power=-900.0, solar_power=100.0)
    readable["inverter_output_power"] = None

    plan, _ = plan_test(test, readable, 1500)

    assert plan is not None
    assert plan.baseline == -1000.0
    assert any("battery minus solar" in note for note in plan.notes)


def test_an_inverter_output_with_nothing_to_derive_it_from_is_unknown() -> None:
    unknown = frame(battery_power=None)
    unknown["inverter_output_power"] = None

    plan, _ = plan_test(TESTS["inverter feed"], unknown, 1500)

    assert plan is not None
    assert plan.baseline is None


def test_an_inverter_feed_may_use_the_house_load_on_top_of_the_cap() -> None:
    capped = frame(
        grid_feed_mode=GridFeedMode.LIMITED,
        feed_in_power_max=0,
        feed_in_power_max_setting=800,
        house_power=400.0,
        solar_power=200.0,
    )

    plan, _ = plan_test(TESTS["inverter feed"], capped, 1500)

    assert plan is not None
    assert plan.power == 1000  # 400 W of house plus the 800 W cap, less 200 W solar


def test_a_cap_too_small_to_tell_skips_the_feed() -> None:
    capped = frame(grid_feed_mode=GridFeedMode.LIMITED, feed_in_power_max=100)

    plan, reason = plan_test(TESTS["grid feed"], capped, 1500)

    assert plan is None
    assert reason == "the export cap leaves only 100 W to feed"


def test_an_unknown_battery_level_skips_nothing() -> None:
    plan, reason = plan_test(TESTS["battery charge"], frame(battery_soc=None), 1500)

    assert plan is not None
    assert reason == ""


def test_a_target_already_met_is_inconclusive_and_keeps_the_notes() -> None:
    test = TESTS["battery charge"]
    plan = Plan(power=1500, baseline=1500.0, decisive=False, notes=["a note"])

    verdict, detail, _ = judge(test, plan, 5.0, ControlStatus.ACTIVE, [1500.0])

    assert verdict is Verdict.INCONCLUSIVE
    assert (
        detail == "battery_power was already near the target before the command; a note"
    )


def test_a_difference_reads_as_a_line_of_the_comparison() -> None:
    significant = Difference("grid feed", "verdict", "followed", "not_followed", True)
    noted = Difference("grid feed", "verdict", "followed", "skipped", False)

    assert str(significant) == "! grid feed: verdict followed -> not_followed"
    assert str(noted) == "~ grid feed: verdict followed -> skipped"


def test_a_feature_only_one_report_has_is_noted() -> None:
    differences = compare_reports(report(grid_feed=Verdict.FOLLOWED), report())

    assert [(d.feature, d.what, d.old, d.new, d.significant) for d in differences] == [
        ("grid feed", "presence", "tested", "absent", False)
    ]


def test_a_method_no_longer_reported_is_significant() -> None:
    old = report(grid_feed=Verdict.FOLLOWED)
    new = report(grid_feed=Verdict.FOLLOWED)
    new["features"][0]["method_reported"] = False

    differences = compare_reports(old, new)

    assert [(d.what, d.significant) for d in differences] == [("method reported", True)]


def test_a_heartbeat_the_inverter_now_refuses_is_significant() -> None:
    old, new = report(), report()
    new["heartbeat"] = "refused"

    differences = compare_reports(old, new)

    assert [(d.feature, d.new, d.significant) for d in differences] == [
        ("heartbeat", "refused", True)
    ]
