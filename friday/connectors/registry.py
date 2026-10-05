"""In-process opt-in connectors with independent failure boundaries."""

import logging
from dataclasses import replace

from friday.connectors.base import Connector
from friday.knowledge.models import UnifiedResult
from friday.knowledge.retrieval import read_allowed
from friday.memory.policy import normalize_namespace

_LOG = logging.getLogger("friday.connectors")


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if not connector.name or connector.source_type not in {"local_file", "future_connector"}:
            raise ValueError("Connector must have a name and a connector source type.")
        if not isinstance(connector.name, str) or not isinstance(connector.enabled, bool):
            raise ValueError("Connector name and enabled state are invalid.")
        if not isinstance(connector.capabilities, (set, frozenset)) or not all(isinstance(v, str) for v in connector.capabilities):
            raise ValueError("Connector capabilities must be a set of names.")
        if any(not callable(getattr(connector, capability, None))
               for capability in ("search", "get", "health") if capability in connector.capabilities or capability == "health"):
            raise ValueError("Connector capabilities require callable methods.")
        if connector.name in self._connectors:
            raise ValueError("Connector name already registered.")
        self._connectors[connector.name] = connector

    def unregister(self, name: str) -> None:
        self._connectors.pop(name, None)

    def disable(self, name: str) -> None:
        self._connectors[name].enabled = False

    def list_connectors(self, capability: str | None = None) -> tuple[Connector, ...]:
        return tuple(c for _, c in sorted(self._connectors.items())
                     if capability is None or capability in c.capabilities)

    def status(self) -> dict[str, str]:
        states = {}
        for c in self.list_connectors():
            try:
                states[c.name] = c.health() if c.enabled else "DISABLED"
            except Exception:
                states[c.name] = "UNAVAILABLE"
        return states

    def search(self, query: str, *, namespace: str = "global", privacy_level: str = "public",
               route_backend: str | None = None, limit: int = 10) -> list[UnifiedResult]:
        scope = normalize_namespace(namespace)
        read_allowed("public", privacy_level, route_backend)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000:
            raise ValueError("limit must be an integer from 0 to 1000.")
        results = []
        if not limit:
            return results
        for c in self.list_connectors("search"):
            if not c.enabled:
                continue
            try:
                # Materialize before accepting anything: a failed generator cannot leak a partial batch.
                batch = list(c.search(query, namespace=scope, privacy_level=privacy_level,
                                      route_backend=route_backend, limit=limit))[:limit]
                for item in batch:
                    if isinstance(item, UnifiedResult) and read_allowed(item.privacy, privacy_level, route_backend):
                        if normalize_namespace(item.namespace) in {scope, "global"}:
                            results.append(replace(item, source_type=c.source_type, source_name=c.name))
            except Exception as error:
                _LOG.warning("connector_search_failed error=%s", type(error).__name__)
        return results
