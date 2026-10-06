"""Common, provenance-carrying records for local documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DocumentSourceRef:
    file_path: str
    filename: str
    file_type: str
    location: str
    privacy: str = "private"


@dataclass(frozen=True)
class DocumentSection:
    title: str
    content: str
    source_ref: DocumentSourceRef
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentTable:
    title: str
    rows: tuple[tuple[Any, ...], ...]
    source_ref: DocumentSourceRef


@dataclass(frozen=True)
class DocumentCell:
    address: str
    formula: str | None
    value: Any


@dataclass(frozen=True)
class DocumentSheet:
    name: str
    used_range: str
    cells: tuple[DocumentCell, ...]
    tables: tuple[DocumentTable, ...] = ()


@dataclass(frozen=True)
class NormalizedDocument:
    source_ref: DocumentSourceRef
    title: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sections: tuple[DocumentSection, ...] = ()
    tables: tuple[DocumentTable, ...] = ()
    sheets: tuple[DocumentSheet, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentSearchResult:
    title: str
    snippet: str
    source_ref: DocumentSourceRef
