"""Local memory API, deterministic retrieval, and prompt-context boundary."""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime
from os import PathLike
from typing import Any

from friday.memory.models import (
    MemoryContext,
    MemoryItem,
    MemoryKind,
    MemoryLink,
    MemoryNotFoundError,
    MemoryPrivacy,
    MemorySearchResult,
    MemoryStatus,
    MemoryWriteRejected,
)
from friday.memory.policy import (
    MemoryWritePolicy,
    bounded_score,
    contains_possible_secret,
    normalize_namespace,
    normalize_text,
    validate_metadata,
)
from friday.memory.semantic import SemanticMemoryIndex
from friday.memory.store import SQLiteMemoryStore, normalize_timestamp, utc_now


_LOG = logging.getLogger("friday.memory")
_QUERY_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_RELATION = re.compile(r"^[A-Za-z][A-Za-z0-9_ -]{0,63}$")
_PRIVACY_ORDER = {MemoryPrivacy.PUBLIC: 0, MemoryPrivacy.PRIVATE: 1, MemoryPrivacy.LOCAL_ONLY: 2}


class MemoryManager:
    """Authoritative local SQLite memory with content-free operational logging."""

    def __init__(
        self,
        db_path: str | PathLike[str] | None = None,
        *,
        write_policy: MemoryWritePolicy | None = None,
        semantic_model_path: str | PathLike[str] | None = None,
    ) -> None:
        self.store = SQLiteMemoryStore(db_path)
        self.write_policy = write_policy or MemoryWritePolicy()
        self.semantic_index = SemanticMemoryIndex(self.store, semantic_model_path)

    def _index_semantic(self, item: MemoryItem) -> None:
        if not self.semantic_index.semantic_enabled:
            return
        try:
            self.semantic_index.index(item)
        except Exception as error:
            self.semantic_index.mark_failure(error)
            _LOG.warning("memory_semantic_index_failed error=%s", type(error).__name__)

    def _remove_semantic(self, memory_id: str) -> None:
        try:
            self.semantic_index.remove(memory_id)
        except Exception as error:
            self.semantic_index.mark_failure(error)
            _LOG.warning("memory_semantic_remove_failed error=%s", type(error).__name__)

    @staticmethod
    def _enum(value: Any, enum_type: type, name: str):
        if isinstance(value, enum_type):
            return value
        try:
            return enum_type(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Unsupported {name}.") from error

    @staticmethod
    def _secret_reject() -> None:
        _LOG.warning("memory_write_rejected reason=possible_secret")
        raise MemoryWriteRejected("possible_secret")

    def _validate_write(self, content: str, source: str, namespace: str, metadata: dict[str, Any]) -> None:
        if not isinstance(content, str) or not content.strip() or not isinstance(source, str) or not source.strip():
            raise ValueError("content and source must be non-empty text.")
        if contains_possible_secret((content, source, namespace, metadata)):
            self._secret_reject()

    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind | str = MemoryKind.FACT,
        namespace: str = "global",
        source: str = "user",
        privacy: MemoryPrivacy | str = MemoryPrivacy.PUBLIC,
        importance: float = 0.5,
        confidence: float = 0.7,
        expires_at: str | datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem:
        """Persist a caller-approved fact; this API does not ingest chat automatically."""
        started = time.perf_counter()
        memory_kind = self._enum(kind, MemoryKind, "memory kind")
        memory_privacy = self._enum(privacy, MemoryPrivacy, "privacy")
        scope = normalize_namespace(namespace)
        importance = bounded_score(importance, "importance")
        confidence = bounded_score(confidence, "confidence")
        attributes = validate_metadata(metadata)
        self._validate_write(content, source, scope, attributes)
        expiry = normalize_timestamp(expires_at)
        normalized = normalize_text(content)
        duplicate = self.store.find_active_duplicate(scope, memory_kind, memory_privacy, normalized)
        if duplicate is not None and duplicate.expires_at is not None and duplicate.expires_at <= utc_now():
            self.store.set_status(duplicate.id, MemoryStatus.SUPERSEDED)
            self._remove_semantic(duplicate.id)
            duplicate = None
        if duplicate is not None:
            item = self.store.merge_duplicate(
                duplicate.id, content=content.strip(), source=source.strip(), importance=importance,
                confidence=confidence, expires_at=expiry, metadata=attributes,
            )
            result = "deduplicated"
        else:
            item = self.store.save_item(
                memory_id=str(uuid.uuid4()), kind=memory_kind, namespace=scope, content=content.strip(),
                source=source.strip(), privacy=memory_privacy, importance=importance, confidence=confidence,
                expires_at=expiry, metadata=attributes,
            )
            result = "inserted"
        self._index_semantic(item)
        _LOG.info(
            "memory_write id=%s kind=%s namespace=%s privacy=%s result=%s duration_ms=%.1f",
            item.id, item.kind.value, item.namespace, item.privacy.value, result,
            (time.perf_counter() - started) * 1000,
        )
        return item

    def consider(
        self, content: str, *, kind: MemoryKind | str = MemoryKind.FACT, namespace: str = "global",
        source: str = "conversation", privacy: MemoryPrivacy | str = MemoryPrivacy.PUBLIC,
        importance: float = 0.5, confidence: float = 0.7, explicit: bool = False,
        expires_at: str | datetime | None = None, metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, MemoryItem | None, str]:
        memory_kind = self._enum(kind, MemoryKind, "memory kind")
        memory_privacy = self._enum(privacy, MemoryPrivacy, "privacy")
        scope = normalize_namespace(namespace)
        level = bounded_score(confidence, "confidence")
        attributes = validate_metadata(metadata)
        self._validate_write(content, source, scope, attributes)
        decision = self.write_policy.evaluate(
            content, kind=memory_kind, source=source, namespace=scope,
            confidence=level, metadata=attributes, explicit=explicit,
        )
        if not decision.accepted:
            if decision.reason == "possible_secret":
                self._secret_reject()
            return False, None, decision.reason
        item = self.remember(
            content, kind=memory_kind, namespace=scope, source=source, privacy=memory_privacy,
            importance=importance, confidence=level, expires_at=expires_at, metadata=attributes,
        )
        return True, item, decision.reason

    def update(
        self, memory_id: str, content: str, *, kind: MemoryKind | str | None = None,
        namespace: str | None = None, source: str | None = None,
        privacy: MemoryPrivacy | str | None = None, importance: float | None = None,
        confidence: float | None = None, expires_at: str | datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem:
        started = time.perf_counter()
        old = self.store.get(memory_id)
        if old is None or old.status != MemoryStatus.ACTIVE or (
            old.expires_at is not None and old.expires_at <= utc_now()
        ):
            raise MemoryNotFoundError()
        new_kind = self._enum(kind, MemoryKind, "memory kind") if kind is not None else old.kind
        new_namespace = normalize_namespace(namespace) if namespace is not None else old.namespace
        new_source = source if source is not None else old.source
        new_privacy = self._enum(privacy, MemoryPrivacy, "privacy") if privacy is not None else old.privacy
        if _PRIVACY_ORDER[new_privacy] < _PRIVACY_ORDER[old.privacy]:
            new_privacy = old.privacy
        new_importance = bounded_score(importance, "importance") if importance is not None else old.importance
        new_confidence = bounded_score(confidence, "confidence") if confidence is not None else old.confidence
        attributes = dict(old.metadata)
        attributes.update(validate_metadata(metadata))
        self._validate_write(content, new_source, new_namespace, attributes)
        expiry = normalize_timestamp(expires_at) if expires_at is not None else old.expires_at
        duplicate = self.store.find_active_duplicate(new_namespace, new_kind, new_privacy, normalize_text(content))
        if duplicate is not None and duplicate.id != old.id:
            self.store.set_status(old.id, MemoryStatus.SUPERSEDED)
            self._remove_semantic(old.id)
            item = self.store.merge_duplicate(
                duplicate.id, content=content.strip(), source=new_source.strip(), importance=new_importance,
                confidence=new_confidence, expires_at=expiry, metadata=attributes,
            )
        else:
            try:
                item = self.store.update_item(
                    old.id, new_id=str(uuid.uuid4()), kind=new_kind, namespace=new_namespace,
                    content=content.strip(), source=new_source.strip(), privacy=new_privacy,
                    importance=new_importance, confidence=new_confidence, expires_at=expiry,
                    metadata=attributes,
                )
            except KeyError as error:
                raise MemoryNotFoundError() from error
            if item.id != old.id:
                self._remove_semantic(old.id)
        self._index_semantic(item)
        _LOG.info(
            "memory_update id=%s old_id=%s kind=%s namespace=%s privacy=%s duration_ms=%.1f",
            item.id, old.id, item.kind.value, item.namespace, item.privacy.value,
            (time.perf_counter() - started) * 1000,
        )
        return item

    def forget(self, memory_id: str) -> MemoryItem:
        item = self.store.set_status(memory_id, MemoryStatus.FORGOTTEN)
        if item is None:
            raise MemoryNotFoundError()
        self._remove_semantic(memory_id)
        _LOG.info("memory_forget id=%s kind=%s namespace=%s privacy=%s", item.id, item.kind.value, item.namespace, item.privacy.value)
        return item

    def get(self, memory_id: str, *, include_inactive: bool = False) -> MemoryItem | None:
        item = self.store.get(memory_id)
        if item is None or (not include_inactive and (
            item.status != MemoryStatus.ACTIVE
            or (item.expires_at is not None and item.expires_at <= utc_now())
        )):
            return None
        return item

    def search(
        self, query: str, *, namespace: str = "global", limit: int = 10,
        privacy_level: str = "public", route_backend: str | None = None,
        include_global: bool = True, include_inactive: bool = False,
        include_expired: bool = False,
    ) -> list[MemorySearchResult]:
        return self.retrieve_relevant(
            query, namespace=namespace, limit=limit, privacy_level=privacy_level,
            route_backend=route_backend, include_global=include_global,
            include_inactive=include_inactive, include_expired=include_expired,
        )

    def retrieve_relevant(
        self, query: str, *, namespace: str = "global", limit: int = 10,
        privacy_level: str = "public", route_backend: str | None = None,
        include_global: bool = True, include_inactive: bool = False,
        include_expired: bool = False,
    ) -> list[MemorySearchResult]:
        started = time.perf_counter()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
            raise ValueError("limit must be an integer from 0 to 1000.")
        scope = normalize_namespace(namespace)
        normalized_query = normalize_text(query if isinstance(query, str) else "")
        if not normalized_query or limit == 0:
            return []
        terms = list(dict.fromkeys(_QUERY_TOKEN.findall(normalized_query)))[:24]
        fts_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in terms)
        namespaces = (scope, "global") if include_global and scope != "global" else (scope,)
        allowed = (MemoryPrivacy.PUBLIC,)
        try:
            requested_privacy = MemoryPrivacy(privacy_level)
        except (TypeError, ValueError) as error:
            raise ValueError("privacy_level must be public, private, or local_only.") from error
        if route_backend == "local" and requested_privacy != MemoryPrivacy.PUBLIC:
            allowed = tuple(MemoryPrivacy)
        candidates = self.store.search_rows(
            query=normalized_query, namespaces=namespaces, privacy=allowed,
            limit=max(100, limit * 20), include_inactive=include_inactive,
            include_expired=include_expired, fts_query=fts_query,
        ) if terms else []
        candidates_by_id = {item.id: item for item in candidates}
        semantic_scores: dict[str, float] = {}
        term_set = set(terms)
        lexical_match_is_strong = any(
            normalized_query in normalize_text(item.content)
            or len(term_set & set(_QUERY_TOKEN.findall(normalize_text(item.content)))) / max(1, len(term_set)) >= 0.8
            for item in candidates
        )
        # Exact/near-exact FTS hits already answer the query; reserve vector work for paraphrases and weak matches.
        if not lexical_match_is_strong:
            try:
                semantic_hits = self.semantic_index.search(
                    query, namespaces=namespaces, privacy=tuple(tier.value for tier in allowed),
                    limit=limit, include_inactive=include_inactive, include_expired=include_expired,
                )
                for hit in semantic_hits:
                    candidates_by_id.setdefault(hit.item.id, hit.item)
                    semantic_scores[hit.item.id] = hit.similarity
            except Exception as error:
                self.semantic_index.mark_failure(error)
                _LOG.warning("memory_semantic_search_failed error=%s", type(error).__name__)
        now = datetime.now().astimezone()
        namespace_rank = {value: index for index, value in enumerate(namespaces)}
        ranked: list[tuple[int, float, MemorySearchResult]] = []
        for item in candidates_by_id.values():
            if contains_possible_secret((item.content, item.source, item.namespace, item.metadata)):
                continue
            words = set(_QUERY_TOKEN.findall(normalize_text(item.content)))
            overlap = len(term_set & words) / max(1, len(term_set))
            phrase = 1.0 if normalized_query and normalized_query in normalize_text(item.content) else 0.0
            lexical_relevance = min(1.0, overlap * 0.8 + phrase * 0.2)
            age_days = max(0.0, (now - datetime.fromisoformat(item.updated_at.replace("Z", "+00:00")).astimezone()).total_seconds() / 86400)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = (
                lexical_relevance * 0.70 + item.importance * 0.12
                + item.confidence * 0.10 + recency * 0.05
            )
            semantic_similarity = semantic_scores.get(item.id)
            display_relevance = lexical_relevance
            if semantic_similarity is not None:
                semantic_score = (
                    semantic_similarity * 0.75 + lexical_relevance * 0.20
                    + item.importance * 0.10 + item.confidence * 0.10 + recency * 0.05
                )
                score = max(score, min(1.0, semantic_score))
                display_relevance = max(lexical_relevance, semantic_similarity)
            result = MemorySearchResult(
                id=item.id, kind=item.kind, content=item.content, privacy=item.privacy,
                relevance=round(display_relevance, 4), score=round(score, 4), source=item.source,
                created_at=item.created_at, updated_at=item.updated_at,
                last_accessed_at=item.last_accessed_at, expires_at=item.expires_at,
                namespace=item.namespace, importance=item.importance, confidence=item.confidence,
            )
            ranked.append((namespace_rank.get(item.namespace, len(namespaces)), -score, result))
        ranked.sort(key=lambda row: (-row[1], row[2].updated_at, row[2].id), reverse=True)
        ranked.sort(key=lambda row: row[0])
        results = [row[2] for row in ranked[:limit]]
        self.store.touch(tuple(result.id for result in results))
        _LOG.info(
            "memory_retrieval namespace=%s privacy=%s result_count=%d duration_ms=%.1f",
            scope, ",".join(tier.value for tier in allowed), len(results),
            (time.perf_counter() - started) * 1000,
        )
        return results

    def link(self, from_id: str, relation: str, to_id: str, *, metadata: dict[str, Any] | None = None) -> MemoryLink:
        relation = " ".join(relation.split()).upper() if isinstance(relation, str) else ""
        attributes = validate_metadata(metadata)
        if not _RELATION.fullmatch(relation) or contains_possible_secret((relation, attributes)):
            if contains_possible_secret((relation, attributes)):
                self._secret_reject()
            raise ValueError("relation must be 1 to 64 letters, numbers, spaces, underscores, or hyphens.")
        if self.get(from_id) is None or self.get(to_id) is None:
            raise MemoryNotFoundError()
        link = self.store.create_link(from_id, relation, to_id, attributes)
        _LOG.info("memory_link id=%s relation=%s", link.id, link.relation)
        return link

    def get_links(self, memory_id: str, *, include_inactive: bool = False) -> list[MemoryLink]:
        return self.store.get_links(memory_id, include_inactive=include_inactive)

    def build_context(
        self, query: str, *, namespace: str = "global", privacy_level: str = "public",
        route_backend: str | None = None, max_memories: int = 5, max_chars: int = 1800,
    ) -> MemoryContext:
        if max_memories < 0 or max_chars < 0:
            raise ValueError("context budgets must not be negative.")
        tier = MemoryPrivacy(privacy_level)
        results = self.retrieve_relevant(
            query, namespace=namespace, limit=max_memories, privacy_level=tier.value,
            route_backend=route_backend,
        )
        lines: list[str] = []
        ids: list[str] = []
        used = 0
        for result in results:
            line = f"- [{result.kind.value}] {result.content}"
            added = len(line) + (1 if lines else len("Relevant memory:\n"))
            if used + added > max_chars:
                continue
            lines.append(line)
            ids.append(result.id)
            used += added
        text = "Relevant memory:\n" + "\n".join(lines) if lines else ""
        context_privacy = tier if route_backend == "local" else MemoryPrivacy.PUBLIC
        return MemoryContext(text, len(lines), tuple(ids), normalize_namespace(namespace), context_privacy, len(text))

    def semantic_diagnostics(self) -> dict[str, Any]:
        return self.semantic_index.diagnostics()

    def backfill_semantic_index(self, *, batch_size: int = 32, progress=None) -> dict[str, Any]:
        return self.semantic_index.backfill(batch_size=batch_size, progress=progress)

    def unload_semantic_model(self) -> None:
        self.semantic_index.unload_model()

    def close(self) -> None:
        """Release lazy semantic-model memory; SQLite handles are operation-scoped."""
        self.semantic_index.unload_model()


__all__ = ["MemoryManager"]
