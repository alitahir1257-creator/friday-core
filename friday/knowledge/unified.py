"""Merge independent knowledge sources without changing their storage logic."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timezone

from friday.connectors.registry import ConnectorRegistry
from friday.knowledge.models import UnifiedContext, UnifiedResult
from friday.knowledge.retrieval import PRIVACY, fingerprint, read_allowed, relevance, snippet
from friday.memory.policy import contains_possible_secret, normalize_namespace

_LOG = logging.getLogger("friday.knowledge")
_HEADERS = {"memory": "Relevant memory:", "vault": "Relevant knowledge:", "local_file": "Relevant files:"}


class UnifiedKnowledgeManager:
    def __init__(self, memory=None, vault=None, connectors: ConnectorRegistry | None = None) -> None:
        self.memory = memory
        self.vault = vault
        self.connectors = connectors if connectors is not None else ConnectorRegistry()

    @staticmethod
    def _limits(max_results: int, per_source_limits: dict[str, int] | None) -> dict[str, int]:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 0 <= max_results <= 1000:
            raise ValueError("max_results must be an integer from 0 to 1000.")
        limits = {"memory": 5, "vault": 5, "local_file": 5, "future_connector": 5}
        limits.update(per_source_limits or {})
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000 for v in limits.values()):
            raise ValueError("Source limits must be integers from 0 to 1000.")
        return limits

    def _gather(self, query: str, namespace: str, privacy_level: str, route_backend: str | None,
                limit: int, limits: dict[str, int]) -> list[UnifiedResult]:
        output = []
        if self.memory is not None and limits["memory"]:
            try:
                batch = []
                for item in self.memory.search(query, namespace=namespace, privacy_level=privacy_level,
                                               route_backend=route_backend, limit=limit):
                    if contains_possible_secret(item.content):
                        continue
                    batch.append(UnifiedResult(
                        "memory", "Memory", item.id, item.namespace, item.kind.value,
                        snippet(item.content, query), item.privacy.value,
                        relevance(query, item.content), item.confidence, item.updated_at,
                        {"content_fingerprint": fingerprint(item.content)},
                    ))
                output.extend(batch)
            except Exception as error:
                _LOG.warning("unified_memory_failed error=%s", type(error).__name__)
        if self.vault is not None and limits["vault"]:
            try:
                batch = []
                for hit in self.vault.search_notes(query, namespace=namespace, privacy_level=privacy_level,
                                                  route_backend=route_backend, limit=limit):
                    note = hit.note
                    if contains_possible_secret((note.body, note.title)):
                        continue
                    parts = note.path.split("/")
                    scope = parts[1] if len(parts) > 2 and parts[0].casefold() == "projects" else note.namespace
                    batch.append(UnifiedResult(
                        "vault", "Obsidian Vault", note.path, scope, note.title,
                        snippet(note.body, query), note.privacy,
                        relevance(query, note.body, note.title), 0.8, note.frontmatter.updated,
                        {"content_fingerprint": fingerprint(note.body)},
                    ))
                output.extend(batch)
            except Exception as error:
                _LOG.warning("unified_vault_failed error=%s", type(error).__name__)
        try:
            if limits["local_file"] or limits["future_connector"]:
                output.extend(self.connectors.search(query, namespace=namespace, privacy_level=privacy_level,
                                                      route_backend=route_backend, limit=limit))
        except Exception as error:
            _LOG.warning("unified_connectors_failed error=%s", type(error).__name__)
        return output

    @staticmethod
    def _score(item: UnifiedResult, query: str, namespace: str, now: datetime) -> float:
        project = namespace != "global" and item.namespace == namespace
        tier = (5 if project else 3) if item.source_type == "memory" else (
            (4 if project else 2) if item.source_type == "vault" else 1
        )
        recency = 0.0
        if item.updated_at:
            try:
                updated = datetime.fromisoformat(item.updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = max(0.0, (now - updated).total_seconds() / 86400)
                recency = 1 / (1 + age / 30)
            except (ValueError, TypeError, OverflowError):
                pass
        # Priority only breaks comparable relevance; lexical coverage dominates.
        return item.relevance + tier * 0.01 + item.confidence * 0.001 + recency * 0.001 + (
            relevance(query, item.title) * 0.002
        ) + (
            0.002 if project and item.source_type not in {"memory", "vault"} else 0.0
        )

    def search(self, query: str, *, namespace: str = "global", privacy_level: str = "public",
               route_backend: str | None = None, max_results: int = 10,
               per_source_limits: dict[str, int] | None = None) -> list[UnifiedResult]:
        scope = normalize_namespace(namespace)
        read_allowed("public", privacy_level, route_backend)
        limits = self._limits(max_results, per_source_limits)
        if not isinstance(query, str):
            raise ValueError("query must be text.")
        if not query.strip() or not max_results:
            return []
        candidates = self._gather(query, scope, privacy_level, route_backend,
                                  min(1000, max(100, max_results * 10)), limits)
        accepted = []
        for item in candidates:
            try:
                if not isinstance(item, UnifiedResult) or not read_allowed(item.privacy, privacy_level, route_backend):
                    continue
                if not all(isinstance(v, str) for v in (item.source_type, item.source_name, item.source_id,
                                                        item.namespace, item.title, item.snippet)):
                    continue
                if not isinstance(item.metadata, dict) or (item.updated_at is not None and not isinstance(item.updated_at, str)):
                    continue
                ns = normalize_namespace(item.namespace)
                if ns not in {scope, "global"} or not item.snippet.strip() or not limits.get(item.source_type, 0):
                    continue
                if contains_possible_secret((item.snippet, item.title, item.metadata)):
                    continue
                if not all(not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1 for v in (item.relevance, item.confidence)):
                    continue
                if not item.relevance:
                    continue
                accepted.append(replace(item, namespace=ns))
            except (ValueError, TypeError, AttributeError):
                continue
        now = datetime.now(timezone.utc)
        accepted.sort(key=lambda item: (-round(self._score(item, query, scope, now), 6), item.source_type, item.source_name, item.source_id))
        deduplicated: dict[str, UnifiedResult] = {}
        for item in accepted:
            key = item.metadata.get("content_fingerprint") or fingerprint(item.snippet)
            if not isinstance(key, str):
                key = fingerprint(item.snippet)
            if key in deduplicated:
                old = deduplicated[key]
                metadata = dict(old.metadata)
                previous = metadata.get("alternate_sources", ())
                alternatives = list(previous) if isinstance(previous, (list, tuple)) else []
                alternatives.append({"source_type": item.source_type, "source_name": item.source_name,
                                     "source_id": item.source_id, "privacy": item.privacy})
                metadata["alternate_sources"] = alternatives
                privacy = max((old.privacy, item.privacy), key=PRIVACY.__getitem__)
                deduplicated[key] = replace(old, metadata=metadata, privacy=privacy)
            else:
                deduplicated[key] = item
        selected = []
        counts: dict[str, int] = {}
        for item in deduplicated.values():
            count = counts.get(item.source_type, 0)
            if count < limits[item.source_type]:
                selected.append(item)
                counts[item.source_type] = count + 1
                if len(selected) == max_results:
                    break
        return selected

    def build_context(self, query: str, *, namespace: str = "global", privacy_level: str = "public",
                      route_backend: str | None = None, max_results: int = 10,
                      per_source_limits: dict[str, int] | None = None, max_chars: int = 3600) -> UnifiedContext:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
            raise ValueError("max_chars must be a non-negative integer.")
        results = self.search(query, namespace=namespace, privacy_level=privacy_level, route_backend=route_backend,
                              max_results=max_results, per_source_limits=per_source_limits)
        groups: dict[str, list[str]] = {}
        included = []
        used = 0
        for item in results:
            header = _HEADERS.get(item.source_type, "Relevant sources:")
            title = " ".join(item.title.split()).replace("[", "").replace("]", "")[:80]
            prefix = f"- [{title}] "
            overhead = 1 if header in groups else len(header) + 1 + (2 if groups else 0)
            room = min(320, max_chars - used - overhead - len(prefix))
            if room <= 0:
                continue
            excerpt = item.snippet[:room]
            line = prefix + excerpt
            groups.setdefault(header, []).append(line)
            included.append(replace(item, snippet=excerpt))
            used += overhead + len(line)
        text = "\n\n".join(header + "\n" + "\n".join(lines) for header, lines in groups.items())
        privacy = max((item.privacy for item in included), key=PRIVACY.__getitem__, default="public")
        return UnifiedContext(text, tuple(included), privacy)
