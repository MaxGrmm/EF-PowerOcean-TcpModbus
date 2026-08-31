"""Utility functions that are independent of coordination, energy processing etc."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_datetime(raw: Any) -> datetime | None:
    """Parse an ISO format datetime string into a datetime object."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
