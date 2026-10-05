"""Typed records used by FRIDAY's local persistent memory core."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryKind(str, Enum):
    FACT = "FACT"
    PREFERENCE = "PREFERENCE"
    DECISION = "DECISION"
    PROJECT = "PROJECT"
    TASK = "TASK"
    RELATIONSHIP = "RELATIONSHIP"
    EPISODE = "EPISODE"


class MemoryPrivacy(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    LOCAL_ONLY = "local_only"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FORGOTTEN = "FORGOTTEN"


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: MemoryKind
    namespace: str
    content: str
    source: str
    privacy: MemoryPrivacy
    importance: float
    confidence: float
    status: MemoryStatus
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    expires_at: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySearchResult:
    id: str
    kind: MemoryKind
    content: str
    privacy: MemoryPrivacy
    relevance: float
    score: float
    source: str
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    expires_at: str | None
    namespace: str
    importance: float
    confidence: float


@dataclass(frozen=True)
class MemoryLink:
    id: str
    from_memory_id: str
    relation: str
    to_memory_id: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryContext:
    text: str
    result_count: int
    item_ids: tuple[str, ...]
    namespace: str
    privacy: MemoryPrivacy
    character_count: int


@dataclass(frozen=True)
class MemoryWriteDecision:
    accepted: bool
    reason: str


class MemoryWriteRejected(ValueError):
    """A safe, content-free rejection from the deterministic write policy."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Memory write rejected: {reason}")


class MemoryNotFoundError(LookupError):
    """Raised when an active memory item cannot be found for update."""

    def __init__(self) -> None:
        super().__init__("Memory item was not found or is not active.")
