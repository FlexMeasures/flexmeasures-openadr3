"""Unit tests for turning VTN events into beliefs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from flexmeasures_openadr3.utils.vtn_events import _as_utc


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 15, 8, 0, tzinfo=timezone(timedelta(hours=2))), datetime(2026, 10, 15, 6, 0, tzinfo=UTC)),
        (datetime(2026, 10, 15, 6, 0, tzinfo=UTC), datetime(2026, 10, 15, 6, 0, tzinfo=UTC)),
        (datetime(2026, 10, 15, 6, 0), datetime(2026, 10, 15, 6, 0, tzinfo=UTC)),  # noqa: DTZ001
    ],
)
def test_vtn_moments_are_expressed_in_utc(moment: datetime, expected: datetime) -> None:
    """Moments from the VTN are recorded in UTC, whatever offset the VTN expressed them in."""
    as_utc = _as_utc(moment)

    assert as_utc == expected
    assert as_utc.tzinfo is UTC
