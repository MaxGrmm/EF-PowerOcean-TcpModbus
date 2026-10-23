"""Fault codes are named through the translations, in Home Assistant's language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.fault_names import (
    FAULT_CODE_SELECTOR,
    async_get_fault_names,
)

COMPONENT = Path(const.__file__).parent


def _fault_names(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    names: dict[str, str] = data["selector"][FAULT_CODE_SELECTOR]["options"]
    return names


def test_every_language_names_only_codes_english_knows() -> None:
    """English is the list of codes; another language may name fewer of them."""
    known = _fault_names(COMPONENT / "translations" / "en.json")
    assert known
    assert all(code.isdigit() for code in known)
    for path in sorted(COMPONENT.glob("translations/*.json")):
        assert _fault_names(path).keys() <= known.keys(), path.name


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_names_follow_the_language(hass: HomeAssistant) -> None:
    hass.config.language = "de"

    names = await async_get_fault_names(hass)

    assert names[521] == "Kommunikationsfehler des Energiespeichers"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_language_without_names_falls_back_to_english(
    hass: HomeAssistant,
) -> None:
    hass.config.language = "fr"

    names = await async_get_fault_names(hass)

    assert names[521] == "Energy storage communication error"
