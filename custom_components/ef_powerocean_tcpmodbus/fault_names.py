"""Names for the fault codes the inverter reports, in the user's language."""

from __future__ import annotations

from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

# The translations keep the names under selector.fault_code.options, one entry per
# code. A language without a code falls back to the English name.
FAULT_CODE_SELECTOR: Final = "fault_code"


async def async_get_fault_names(hass: HomeAssistant) -> dict[int, str]:
    """Return the known fault names by code, in Home Assistant's language."""
    translations = await async_get_translations(
        hass, hass.config.language, "selector", {DOMAIN}
    )
    prefix = f"component.{DOMAIN}.selector.{FAULT_CODE_SELECTOR}.options."
    return {
        int(key.removeprefix(prefix)): name
        for key, name in translations.items()
        if key.startswith(prefix)
    }
