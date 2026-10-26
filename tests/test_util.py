"""Helpers independent of the rest of the integration."""

from __future__ import annotations

import pytest

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.util import (
    parse_kwh,
    portal_history_url,
)


@pytest.mark.parametrize(
    ("language", "portal"),
    (("de", "de"), ("de-CH", "de"), ("en-GB", "en"), ("fr", "en"), ("", "en")),
)
def test_the_portal_opens_in_the_users_language_where_it_has_it(
    language: str, portal: str
) -> None:
    assert portal_history_url(language) == const.ECOFLOW_PORTAL_HISTORY_URL.format(
        language=portal
    )


@pytest.mark.parametrize(
    ("text", "value"),
    (("3139", 3139.0), ("3139,5", 3139.5), ("3139.5", 3139.5), (" 18 271,2 ", 18271.2)),
)
def test_a_total_can_be_typed_with_a_decimal_comma_or_point(
    text: str, value: float
) -> None:
    assert parse_kwh(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ("", "abc", "-1", "nan", "inf", "18,271.2", "1.234,5"))
def test_what_is_not_a_total_is_refused(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message is the caller's
        parse_kwh(text)
