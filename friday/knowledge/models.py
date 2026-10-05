"""Plain data records for FRIDAY's Markdown knowledge vault."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NoteFrontmatter:
    title: str
    namespace: str = "global"
    note_type: str = "note"
    privacy: str = "private"
    created: str | None = None
    updated: str | None = None
    tags: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeNote:
    path: str
    frontmatter: NoteFrontmatter
    body: str
    managed: bool = False
    raw_frontmatter: tuple[str, ...] = field(default=(), repr=False)

    @property
    def title(self) -> str:
        return self.frontmatter.title

    @property
    def privacy(self) -> str:
        return self.frontmatter.privacy

    @property
    def namespace(self) -> str:
        return self.frontmatter.namespace


@dataclass(frozen=True)
class NoteSearchResult:
    note: KnowledgeNote
    score: float
    snippet: str


@dataclass(frozen=True)
class KnowledgeContext:
    text: str
    result_count: int
    note_paths: tuple[str, ...]
    character_count: int


class KnowledgeWriteRejected(ValueError):
    """Content-free rejection used when a managed write may contain a secret."""

    def __init__(self) -> None:
        super().__init__("Knowledge write rejected: possible_secret")


class ConcurrentNoteEditError(RuntimeError):
    """A note changed while FRIDAY was preparing an update."""
