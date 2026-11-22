"""Keeps the integration talking to the inverter its entry was set up for."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import issues
from .const import DOMAIN
from .modbus import ModbusClient

_LOGGER = logging.getLogger(__name__)


class IdentityGuard:
    """Blocks writes until the device has shown the serial the entry expects.

    Otherwise a changed DHCP lease could hand us Modbus control of an inverter
    nobody configured. Entries made before the serial was read are keyed by host
    and port; for those, the first serial read is the one expected from then on.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: ModbusClient, host: str
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._host = host
        self._verified = False
        self._mismatch = False
        # For entries still keyed by host:port.
        self._pinned_serial: str | None = None

    @property
    def verified(self) -> bool:
        """Whether the device has shown the expected serial since it connected."""
        return self._verified

    @property
    def mismatch(self) -> bool:
        """Whether the device reports another serial than the expected one."""
        return self._mismatch

    def expected_serial(self) -> str | None:
        unique_id = self._entry.unique_id
        if unique_id and ":" not in unique_id:
            return unique_id
        return self._pinned_serial

    def mark_unverified(self) -> None:
        """Block writes until the serial is checked again, as after a reconnect."""
        self._verified = False
        self._client.block_writes("identity_unverified")

    def accept_unreadable(self) -> None:
        """Go on without a serial, where there is nothing to compare it against.

        This keeps the behaviour from before the check.
        """
        if self.expected_serial() is None:
            self._set_verified()

    def check(self, serial: str | None) -> bool:
        """Return whether the device may be read, blocking it if not."""
        if not serial:
            self.accept_unreadable()
            return self._verified

        expected = self.expected_serial()
        if expected is not None and serial != expected:
            self._block(serial, expected)
            return False

        self._pinned_serial = self._pinned_serial or serial
        self._set_verified()
        return True

    def _block(self, serial: str, expected: str) -> None:
        reason = f"the device at {self._host} reports serial {serial}, not {expected}"
        self._client.block_writes(
            "identity_mismatch",
            {"host": self._host, "found": serial, "expected": expected},
        )
        if not self._mismatch:
            _LOGGER.warning(
                "%s. Nothing is read from or written to it. Check the address, "
                "or remove the entry and add the new inverter",
                reason[0].upper() + reason[1:],
            )
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issues.issue_id(issues.IDENTITY_MISMATCH, self._entry),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="identity_mismatch",
                translation_placeholders={
                    "host": str(self._host),
                    "expected": expected,
                    "found": serial,
                },
            )
        self._mismatch = True

    def _set_verified(self) -> None:
        self._verified = True
        self._client.allow_writes()
        if self._mismatch:
            _LOGGER.info("The device at %s shows the expected serial again", self._host)
            ir.async_delete_issue(
                self._hass,
                DOMAIN,
                issues.issue_id(issues.IDENTITY_MISMATCH, self._entry),
            )
        self._mismatch = False
