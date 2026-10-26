"""Helpers independent of the rest of the integration."""

from __future__ import annotations

import pytest

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.util import portal_history_url


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
