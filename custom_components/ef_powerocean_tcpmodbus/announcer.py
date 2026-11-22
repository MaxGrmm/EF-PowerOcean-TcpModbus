"""Tells the user about the reading checks' findings.

Each finding is announced when it starts and when it ends: an event the logbook
shows, and one line in the log each way. A finding whose check names a remedy
also raises that repairs issue once it has lasted the check's issue_after, and
the issue goes as soon as the finding does. Nothing here knows one check from
another; the findings say what to announce.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt

from . import issues
from .const import (
    ATTR_CHECK,
    ATTR_STATUS,
    DOMAIN,
    EVENT_READING_CHECK_CLEARED,
    EVENT_READING_CHECK_FAILED,
)
from .powerocean.reading_checks import Finding

_LOGGER = logging.getLogger(__name__)


class Announcer:
    """Announces findings as they start and end, and raises their issues."""

    def __init__(self) -> None:
        # The findings announced as started, by check.
        self._announced: dict[str, Finding] = {}
        # The remedies whose issue is up.
        self._raised: set[str] = set()

    @callback
    def update(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        findings: Iterable[Finding],
        now: datetime,
    ) -> None:
        """Announce what changed since the last poll's findings."""
        current = {finding.check: finding for finding in findings}
        for check in [check for check in self._announced if check not in current]:
            ended = self._announced.pop(check)
            _LOGGER.info("The %s reading check passes again", ended.check)
            self._fire(hass, entry, EVENT_READING_CHECK_CLEARED, ended)
        for check, finding in current.items():
            if check in self._announced:
                continue
            self._announced[check] = finding
            _LOGGER.info(
                "The %s reading check failed (%s since %s)",
                finding.check,
                finding.status,
                finding.since,
            )
            self._fire(hass, entry, EVENT_READING_CHECK_FAILED, finding)
        self._update_issues(hass, entry, host, current.values(), now)

    def _update_issues(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        findings: Iterable[Finding],
        now: datetime,
    ) -> None:
        due: dict[str, Finding] = {}
        pending: set[str] = set()
        for finding in findings:
            if finding.remedy is None:
                continue
            pending.add(finding.remedy)
            if (
                finding.issue_after is None
                or now - finding.since >= finding.issue_after
            ):
                due.setdefault(finding.remedy, finding)
        # An issue stays up while its finding holds, even if a poll found it
        # newer than issue_after, and goes once the finding does.
        for remedy in self._raised - pending:
            self._raised.discard(remedy)
            ir.async_delete_issue(hass, DOMAIN, issues.issue_id(remedy, entry))
        for remedy, finding in due.items():
            if remedy in self._raised:
                continue
            self._raised.add(remedy)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issues.issue_id(remedy, entry),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=remedy,
                translation_placeholders={
                    "host": host,
                    "since": dt.as_local(finding.since).strftime("%Y-%m-%d %H:%M"),
                },
            )

    @staticmethod
    def _fire(
        hass: HomeAssistant, entry: ConfigEntry, event_type: str, finding: Finding
    ) -> None:
        devices = device_registry.async_entries_for_config_entry(
            device_registry.async_get(hass), entry.entry_id
        )
        hass.bus.async_fire(
            event_type,
            {
                ATTR_DEVICE_ID: devices[0].id if devices else None,
                ATTR_STATUS: str(finding.status),
                ATTR_CHECK: finding.check,
            },
        )
