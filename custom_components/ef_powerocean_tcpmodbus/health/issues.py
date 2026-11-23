"""The repairs issues an entry raises while it runs, in one place.

Each is named by its kind and the entry it belongs to, so unloading the entry can
take them all down. The outage issue (outage.py) is kept apart, as it outlives a
reload, and so are the watchdog's alerts (watchdog.py), which it clears itself.
"""

from __future__ import annotations

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from ..const import DOMAIN
from ..powerocean.reading_checks import CHECKS

IDENTITY_MISMATCH: Final = "identity_mismatch"
HELD_TOTALS: Final = "energy_totals_held"
GRID_ENERGY: Final = "grid_energy_not_counted"
MODBUS_DISABLED: Final = "modbus_disabled"
# The remedies of the reading checks, each raised by the announcer.
REMEDIES: Final = frozenset(check.remedy for check in CHECKS if check.remedy)

KINDS: Final = frozenset(
    {IDENTITY_MISMATCH, HELD_TOTALS, GRID_ENERGY, MODBUS_DISABLED} | REMEDIES
)


def issue_id(kind: str, entry: ConfigEntry) -> str:
    """Return the id of the issue of this kind for *entry*."""
    return f"{kind}_{entry.entry_id}"


def is_kind(kind: str, issue: str) -> bool:
    """Return whether *issue* is an id of the issue of this kind."""
    return issue.startswith(f"{kind}_")


def issue_ids(entry: ConfigEntry) -> list[str]:
    """Return the id of every issue *entry* can raise while it runs."""
    return sorted(issue_id(kind, entry) for kind in KINDS)


@callback
def async_delete_all(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take down every issue *entry* raised while it ran."""
    for issue in issue_ids(entry):
        ir.async_delete_issue(hass, DOMAIN, issue)
