"""Optional local semantic index for authoritative SQLite memory rows."""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from friday.memory.models import MemoryItem, MemoryStatus
from friday.memory.policy import contains_possible_secret, normalize_text
from friday.memory.store import SQLiteMemoryStore, utc_now
from friday.paths import MODELS_PATH


MODEL_NAME = "minishlab/potion-base-4M"
MODEL_PATH = MODELS_PATH / "embeddings" / "potion-base-4M"
MODEL_DIMENSION = 128
MIN_SIMILARITY = 0.25
MAX_CANDIDATES = 200
MAX_QUERY_EMBEDDINGS = 64
_META_TABLE = "memory_semantic_metadata"
_VECTOR_TABLE = "memory_semantic_vectors"
_MODEL_FILES = ("config.json", "model.safetensors", "tokenizer.json")


class SemanticUnavailableError(RuntimeError):
    """Raised when local semantic dependencies or model assets are unavailable."""


@dataclass(frozen=True)
class SemanticHit:
    item: MemoryItem
    similarity: float


class SemanticMemoryIndex:
    """Small, lazy Model2Vec encoder backed by sqlite-vec; it stores no memory text."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        model_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.model_path = Path(model_path) if model_path is not None else MODEL_PATH
        self._disabled = os.environ.get("FRIDAY_SEMANTIC_DISABLED", "").strip().casefold() in {
            "1", "true", "yes", "on",
        }
        self._model: Any | None = None
        self._dimension: int | None = None
        self._query_embeddings: OrderedDict[str, bytes] = OrderedDict()
        # ponytail: one lock serializes this low-volume CPU encoder; split if measured contention appears.
        self._lock = threading.RLock()
        self._status = "AVAILABLE" if self.semantic_enabled else "UNAVAILABLE"
        self._last_error_type: str | None = None
        self.last_model_load_seconds: float | None = None
        self.last_embedding_seconds: float | None = None

    @property
    def semantic_enabled(self) -> bool:
        if self._disabled:
            return False
        try:
            return (
                importlib.util.find_spec("model2vec") is not None
                and importlib.util.find_spec("sqlite_vec") is not None
                and all((self.model_path / name).is_file() for name in _MODEL_FILES)
            )
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    @property
    def status(self) -> str:
        if not self.semantic_enabled:
            return "UNAVAILABLE"
        return self._status

    def mark_failure(self, error: Exception) -> None:
        self._last_error_type = type(error).__name__
        self._status = "UNAVAILABLE" if isinstance(error, SemanticUnavailableError) else "DEGRADED"

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.semantic_enabled:
            self._status = "UNAVAILABLE"
            raise SemanticUnavailableError("Local semantic dependencies or model assets are missing.")
        started = time.perf_counter()
        try:
            from model2vec import StaticModel

            # Passing an existing Path with force_download=False keeps runtime inference local-only.
            model = StaticModel.from_pretrained(self.model_path, force_download=False)
        except Exception:
            self._status = "DEGRADED"
            raise
        self._model = model
        self.last_model_load_seconds = time.perf_counter() - started
        self._status = "AVAILABLE"
        self._last_error_type = None
        return model

    def _encode_locked(self, text: str) -> tuple[bytes, int]:
        model = self._load_model()
        started = time.perf_counter()
        vectors = model.encode([text])
        elapsed = time.perf_counter() - started
        if len(vectors) != 1:
            raise RuntimeError("Model2Vec returned an invalid embedding batch.")
        vector = [float(value) for value in vectors[0]]
        dimension = len(vector)
        if dimension != MODEL_DIMENSION or not all(math.isfinite(value) for value in vector):
            raise RuntimeError("Model2Vec returned an invalid embedding vector.")
        self.last_embedding_seconds = elapsed
        self._dimension = dimension
        return struct.pack(f"{dimension}f", *vector), dimension

    def _encode_query_locked(self, query: str) -> tuple[bytes, int]:
        key = hashlib.sha256(query.encode("utf-8")).hexdigest()
        embedding = self._query_embeddings.get(key)
        if embedding is not None:
            self._query_embeddings.move_to_end(key)
            return embedding, MODEL_DIMENSION
        embedding, dimension = self._encode_locked(query)
        self._query_embeddings[key] = embedding
        if len(self._query_embeddings) > MAX_QUERY_EMBEDDINGS:
            self._query_embeddings.popitem(last=False)
        return embedding, dimension

    @contextmanager
    def _connection(self, *, load_vec: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.store.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        try:
            if load_vec:
                import sqlite_vec

                # Load only the extension path resolved by the trusted installed package.
                db.enable_load_extension(True)
                try:
                    sqlite_vec.load(db)
                finally:
                    db.enable_load_extension(False)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection, dimension: int) -> None:
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL UNIQUE REFERENCES memory_items(id) ON DELETE CASCADE,
                embedding_model TEXT NOT NULL,
                embedding_dimension INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                namespace TEXT NOT NULL,
                privacy TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {_META_TABLE}_scope ON {_META_TABLE}(namespace, privacy)"
        )
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (_VECTOR_TABLE,)
        ).fetchone()
        if row is None:
            db.execute(
                f"CREATE VIRTUAL TABLE {_VECTOR_TABLE} USING vec0("
                f"embedding float[{dimension}] distance_metric=cosine, "
                "namespace TEXT PARTITION KEY, privacy TEXT PARTITION KEY)"
            )
            return
        match = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", row["sql"] or "", re.IGNORECASE)
        if match is None or int(match.group(1)) != dimension:
            raise RuntimeError("Semantic vector table dimension does not match the selected model.")

    def _metadata(self, memory_id: str) -> sqlite3.Row | None:
        try:
            with self._connection() as db:
                return db.execute(
                    f"SELECT * FROM {_META_TABLE} WHERE memory_id=?", (memory_id,)
                ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).casefold():
                return None
            raise

    def index(self, item: MemoryItem) -> str:
        if item.status != MemoryStatus.ACTIVE:
            self.remove(item.id)
            return "skipped"
        if contains_possible_secret((item.content, item.source, item.namespace, item.metadata)):
            self.remove(item.id)
            return "skipped"
        content_hash = self._hash(item.content)
        with self._lock:
            existing = self._metadata(item.id)
            same_embedding = (
                existing is not None
                and existing["content_hash"] == content_hash
                and existing["embedding_model"] == MODEL_NAME
                and existing["embedding_dimension"] == MODEL_DIMENSION
            )
            if same_embedding and (
                existing["namespace"] != item.namespace or existing["privacy"] != item.privacy.value
            ):
                with self._connection(load_vec=True) as db:
                    self._ensure_schema(db, MODEL_DIMENSION)
                    db.execute(
                        f"UPDATE {_VECTOR_TABLE} SET namespace=?, privacy=? WHERE rowid=?",
                        (item.namespace, item.privacy.value, existing["vector_id"]),
                    )
                    db.execute(
                        f"UPDATE {_META_TABLE} SET namespace=?, privacy=?, indexed_at=? WHERE memory_id=?",
                        (item.namespace, item.privacy.value, utc_now(), item.id),
                    )
                self._status = "AVAILABLE"
                self._last_error_type = None
                return "metadata_updated"
            if same_embedding:
                return "unchanged"

            embedding, dimension = self._encode_locked(item.content)
            with self._connection(load_vec=True) as db:
                self._ensure_schema(db, dimension)
                existing = db.execute(
                    f"SELECT vector_id FROM {_META_TABLE} WHERE memory_id=?", (item.id,)
                ).fetchone()
                if existing is None:
                    cursor = db.execute(
                        f"INSERT INTO {_META_TABLE}(memory_id, embedding_model, embedding_dimension, "
                        "content_hash, namespace, privacy, indexed_at) VALUES(?,?,?,?,?,?,?)",
                        (item.id, MODEL_NAME, dimension, content_hash, item.namespace, item.privacy.value, utc_now()),
                    )
                    vector_id = int(cursor.lastrowid)
                else:
                    vector_id = int(existing["vector_id"])
                    db.execute(f"DELETE FROM {_VECTOR_TABLE} WHERE rowid=?", (vector_id,))
                    db.execute(
                        f"UPDATE {_META_TABLE} SET embedding_model=?, embedding_dimension=?, content_hash=?, "
                        "namespace=?, privacy=?, indexed_at=? WHERE memory_id=?",
                        (MODEL_NAME, dimension, content_hash, item.namespace, item.privacy.value, utc_now(), item.id),
                    )
                db.execute(
                    f"INSERT INTO {_VECTOR_TABLE}(rowid, embedding, namespace, privacy) VALUES(?,?,?,?)",
                    (vector_id, embedding, item.namespace, item.privacy.value),
                )
            self._status = "AVAILABLE"
            self._last_error_type = None
            return "indexed"

    def remove(self, memory_id: str) -> None:
        with self._lock:
            row = self._metadata(memory_id)
            if row is None:
                return
            with self._connection(load_vec=True) as db:
                db.execute(f"DELETE FROM {_VECTOR_TABLE} WHERE rowid=?", (row["vector_id"],))
                db.execute(f"DELETE FROM {_META_TABLE} WHERE memory_id=?", (memory_id,))
        self._status = "AVAILABLE"
        self._last_error_type = None

    def search(
        self,
        query: str,
        *,
        namespaces: tuple[str, ...],
        privacy: tuple[str, ...],
        limit: int,
        include_inactive: bool = False,
        include_expired: bool = False,
    ) -> list[SemanticHit]:
        if not query.strip() or not namespaces or not privacy or limit <= 0:
            return []
        if not self.semantic_enabled:
            self._status = "UNAVAILABLE"
            return []
        candidate_limit = max(20, min(MAX_CANDIDATES, limit * 10))
        with self._lock:
            embedding, dimension = self._encode_query_locked(query)
            scores: dict[int, float] = {}
            with self._connection(load_vec=True) as db:
                self._ensure_schema(db, dimension)
                for namespace in namespaces:
                    for privacy_tier in privacy:
                        rows = db.execute(
                            f"SELECT rowid, distance FROM {_VECTOR_TABLE} "
                            "WHERE embedding MATCH ? AND k=? AND namespace=? AND privacy=? ORDER BY distance",
                            (embedding, candidate_limit, namespace, privacy_tier),
                        ).fetchall()
                        for row in rows:
                            distance = float(row["distance"])
                            similarity = min(1.0, max(0.0, 1.0 - distance))
                            if math.isfinite(similarity) and similarity >= MIN_SIMILARITY:
                                vector_id = int(row["rowid"])
                                scores[vector_id] = max(similarity, scores.get(vector_id, 0.0))
                if not scores:
                    self._status = "AVAILABLE"
                    self._last_error_type = None
                    return []
                vector_ids = tuple(scores)
                marks = ",".join("?" for _ in vector_ids)
                namespace_marks = ",".join("?" for _ in namespaces)
                privacy_marks = ",".join("?" for _ in privacy)
                filters = [
                    f"s.vector_id IN ({marks})",
                    f"s.namespace IN ({namespace_marks})",
                    f"s.privacy IN ({privacy_marks})",
                    f"m.namespace IN ({namespace_marks})",
                    f"m.privacy IN ({privacy_marks})",
                    "s.embedding_model=?",
                    "s.embedding_dimension=?",
                ]
                params: list[Any] = [
                    *vector_ids, *namespaces, *privacy, *namespaces, *privacy, MODEL_NAME, dimension,
                ]
                if include_inactive:
                    filters.append("m.status!='FORGOTTEN'")
                else:
                    filters.append("m.status='ACTIVE'")
                if not include_expired:
                    filters.append("(m.expires_at IS NULL OR m.expires_at>?)")
                    params.append(utc_now())
                rows = db.execute(
                    f"SELECT m.*, s.vector_id, s.content_hash FROM {_META_TABLE} s "
                    "JOIN memory_items m ON m.id=s.memory_id WHERE " + " AND ".join(filters),
                    params,
                ).fetchall()

            hits = []
            for row in rows:
                item = SQLiteMemoryStore._item(row)
                if (
                    item.namespace not in namespaces
                    or item.privacy.value not in privacy
                    or item.status == MemoryStatus.FORGOTTEN
                    or (not include_inactive and item.status != MemoryStatus.ACTIVE)
                    or contains_possible_secret((item.content, item.source, item.namespace, item.metadata))
                    or self._hash(item.content) != row["content_hash"]
                ):
                    continue
                similarity = scores.get(int(row["vector_id"]))
                if similarity is not None:
                    hits.append(SemanticHit(item, similarity))
            hits.sort(key=lambda hit: (-hit.similarity, hit.item.id))
            self._status = "AVAILABLE"
            self._last_error_type = None
            return hits[:candidate_limit]

    def _pending_counts(self) -> tuple[int, int]:
        now = utc_now()
        with self._connection() as db:
            metadata_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_META_TABLE,)
            ).fetchone() is not None
            vector_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_VECTOR_TABLE,)
            ).fetchone() is not None
            if metadata_exists and vector_exists:
                cursor = db.execute(
                    f"SELECT m.id, m.content, m.source, m.namespace, m.metadata_json, "
                    f"s.content_hash, s.embedding_model, s.embedding_dimension "
                    f"FROM memory_items m LEFT JOIN {_META_TABLE} s ON s.memory_id=m.id "
                    "WHERE m.status='ACTIVE' AND (m.expires_at IS NULL OR m.expires_at>?) ORDER BY m.id",
                    (now,),
                )
            else:
                cursor = db.execute(
                    "SELECT id, content, source, namespace, metadata_json, NULL AS content_hash, "
                    "NULL AS embedding_model, NULL AS embedding_dimension FROM memory_items "
                    "WHERE status='ACTIVE' AND (expires_at IS NULL OR expires_at>?) ORDER BY id",
                    (now,),
                )
            indexed = 0
            pending = 0
            while rows := cursor.fetchmany(64):
                for row in rows:
                    try:
                        metadata = json.loads(row["metadata_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    if contains_possible_secret((row["content"], row["source"], row["namespace"], metadata)):
                        continue
                    if (
                        vector_exists
                        and row["content_hash"] == self._hash(row["content"])
                        and row["embedding_model"] == MODEL_NAME
                        and row["embedding_dimension"] == MODEL_DIMENSION
                    ):
                        indexed += 1
                    else:
                        pending += 1
            return indexed, pending

    def diagnostics(self) -> dict[str, Any]:
        try:
            indexed, pending = self._pending_counts()
        except (sqlite3.Error, OSError):
            indexed, pending = 0, 0
        return {
            "semantic_enabled": self.semantic_enabled,
            "status": self.status,
            "model_loaded": self.model_loaded,
            "model_name": MODEL_NAME,
            "model_path": str(self.model_path),
            "indexed_memory_count": indexed,
            "pending_backfill_count": pending,
            "vector_dimension": self._dimension or (MODEL_DIMENSION if self.semantic_enabled else None),
            "last_error_type": self._last_error_type,
        }

    def backfill(
        self,
        *,
        batch_size: int = 32,
        progress: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int | str | None]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 256:
            raise ValueError("batch_size must be an integer from 1 to 256.")
        report: dict[str, int | str | None] = {
            "processed": 0, "indexed": 0, "unchanged": 0, "skipped": 0,
            "failed": 0, "pending": 0, "error_type": None,
        }
        last_id = ""
        while True:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT * FROM memory_items WHERE status='ACTIVE' "
                    "AND (expires_at IS NULL OR expires_at>?) AND id>? ORDER BY id LIMIT ?",
                    (utc_now(), last_id, batch_size),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                report["processed"] = int(report["processed"]) + 1
                item = SQLiteMemoryStore._item(row)
                try:
                    result = self.index(item)
                except Exception as error:
                    self.mark_failure(error)
                    report["failed"] = int(report["failed"]) + 1
                    report["error_type"] = type(error).__name__
                    break
                key = "indexed" if result == "indexed" else "unchanged" if result == "unchanged" else "skipped"
                report[key] = int(report[key]) + 1
            if progress is not None:
                progress({key: int(value) for key, value in report.items() if isinstance(value, int)})
            if report["failed"]:
                break
        try:
            _indexed, report["pending"] = self._pending_counts()
        except (sqlite3.Error, OSError):
            report["pending"] = -1
        return report

    def unload_model(self) -> None:
        with self._lock:
            self._model = None
            self._query_embeddings.clear()
            gc.collect()


__all__ = [
    "MODEL_DIMENSION",
    "MODEL_NAME",
    "MODEL_PATH",
    "SemanticHit",
    "SemanticMemoryIndex",
    "SemanticUnavailableError",
]
