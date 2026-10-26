"""Utility functions that are independent of coordination, energy processing etc."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import ECOFLOW_PORTAL_HISTORY_URL, ECOFLOW_PORTAL_LANGUAGES


def parse_datetime(raw: Any) -> datetime | None:
    """Parse an ISO format datetime string into a datetime object."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def portal_history_url(language: str) -> str:
    """Return EcoFlow's portal page with the energy totals in this language.

    Home Assistant's language may carry a region, as in de-CH; the portal is
    matched on the language alone, falling back to its first language.
    """
    base = language.split("-")[0].lower()
    if base not in ECOFLOW_PORTAL_LANGUAGES:
        base = ECOFLOW_PORTAL_LANGUAGES[0]
    return ECOFLOW_PORTAL_HISTORY_URL.format(language=base)
