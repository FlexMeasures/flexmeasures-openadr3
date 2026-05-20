from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EventActivePeriod:
    start: datetime
    end: datetime
    event_id: str
