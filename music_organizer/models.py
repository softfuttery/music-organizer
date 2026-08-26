"""Small domain value objects shared by organizer components."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CueTrack:
    number: int
    file_name: str
    title: str = ""
    performer: str = ""
    composer: str = ""
    isrc: str = ""
    indexes: dict[int, float] | None = None

    def index(self, number: int) -> float | None:
        if not self.indexes:
            return None
        return self.indexes.get(number)


@dataclass
class CueSheet:
    title: str = ""
    performer: str = ""
    tracks: list[CueTrack] | None = None


@dataclass
class RunResult:
    scanned: int = 0
    organized: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
