from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class EventActivePeriod:
    """Time window during which an OpenADR event interval is active."""

    start: datetime
    end: datetime
    event_id: str
