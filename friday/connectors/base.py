"""Read-only source contract; future research sources use the same records."""

from typing import Protocol

from friday.knowledge.models import UnifiedResult


class Connector(Protocol):
    name: str
    source_type: str
    enabled: bool
    capabilities: frozenset[str]

    def search(self, query: str, *, namespace: str = "global", privacy_level: str = "public",
               route_backend: str | None = None, limit: int = 10) -> list[UnifiedResult]: ...

    def get(self, source_id: str, *, privacy_level: str = "public",
            route_backend: str | None = None) -> UnifiedResult | None: ...

    def health(self) -> str: ...
