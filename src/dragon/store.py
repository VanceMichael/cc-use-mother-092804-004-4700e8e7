"""内存存储，便于测试；结构稳定后可替换为持久化实现。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Appeal,
    Boat,
    CheckIn,
    Entry,
    LaneAssignment,
    LedgerEntry,
    Membership,
    Person,
    Race,
    ResultSubmission,
    Stage,
    Substitution,
    Team,
    User,
)


@dataclass
class Store:
    users: dict[str, User] = field(default_factory=dict)
    teams: dict[str, Team] = field(default_factory=dict)
    persons: dict[str, Person] = field(default_factory=dict)
    memberships: dict[str, Membership] = field(default_factory=dict)
    stages: dict[str, Stage] = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)
    checkins: dict[str, CheckIn] = field(default_factory=dict)
    substitutions: dict[str, Substitution] = field(default_factory=dict)
    boats: dict[str, Boat] = field(default_factory=dict)
    lanes: dict[str, list[LaneAssignment]] = field(default_factory=dict)
    races: dict[str, Race] = field(default_factory=dict)
    results: dict[str, list[ResultSubmission]] = field(default_factory=dict)
    ledger: dict[str, list[LedgerEntry]] = field(default_factory=dict)
    appeals: dict[str, Appeal] = field(default_factory=dict)
    audit: list = field(default_factory=list)
    seq: int = 0

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq:04d}"
