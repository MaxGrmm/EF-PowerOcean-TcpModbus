"""Utility functions that are independent of coordination, energy processing etc."""

from __future__ import annotations

import math
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


def parse_kwh(text: Any) -> float:
    """Parse an energy total as typed, with a decimal comma or a decimal point.

    Spaces are ignored. A value with both a comma and a point is refused rather
    than guessed at, since either may be a thousands separator. Raises ValueError
    for anything that is not a number of 0 or more.
    """
    cleaned = str(text).replace(" ", "").replace("\u00a0", "")
    if "," in cleaned and "." in cleaned:
        raise ValueError(f"Ambiguous decimal separator in {text!r}")
    value = float(cleaned.replace(",", "."))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Not an energy total: {text!r}")
    return value
