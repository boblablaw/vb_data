"""Season (fall-year) helpers.

NCAA WVB seasons are named by their fall year. The daily/weekly scripts derive the "current"
season the same way (Aug–Dec -> this year; Jan–Jul -> last year); this mirrors that so the API
can default `season` when a caller omits it.
"""
from __future__ import annotations

from datetime import date


def current_season(today: date | None = None) -> int:
    """Fall-year season for a date. Aug–Dec -> that year; Jan–Jul -> previous year."""
    d = today or date.today()  # noqa: DTZ011 — season is a coarse local-calendar boundary
    return d.year if d.month >= 8 else d.year - 1
