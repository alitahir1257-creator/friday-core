"""SQLite persistence for FRIDAY's local memory."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from friday.memory.models import MemoryItem, MemoryKind, MemoryLink, MemoryPrivacy, MemoryStatus
from friday.memory.policy import normalize_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_memory_db_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("FRIDAY_MEMORY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "FRIDAY" / "memory" / "friday-memory.sqlite3"
    return Path.home() / ".local" / "share" / "FRIDAY" / "memory" / "friday-memory.sqlite3"


class SQLiteMemoryStore:
    """Small connection-per-operation store; SQLite remains the source of truth."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(db_path).expanduser() if db_path is not None else default_memory_db_path()
        raw = str(self.path)
        if raw.startswith(("\\\\", "//")) or raw.casefold().startswith(("file:", "http:", "https:")):
            raise ValueError("Memory database path must be local.")
        self.path = self.path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts_available = False
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('FACT','PREFERENCE','DECISION','PROJECT','TASK','RELATIONSHIP','EPISODE')),
                    namespace TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    privacy TEXT NOT NULL CHECK (privacy IN ('public','private','local_only')),
                    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','FORGOTTEN')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    expires_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS memory_items_active_dedup
                    ON memory_items(namespace, kind, privacy, normalized_content)
                    WHERE status = 'ACTIVE';
                CREATE INDEX IF NOT EXISTS memory_items_scope_status
                    ON memory_items(namespace, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS memory_links (
                    id TEXT PRIMARY KEY,
                    from_memory_id TEXT NOT NULL REFERENCES memory_items(id),
                    relation TEXT NOT NULL,
                    to_memory_id TEXT NOT NULL REFERENCES memory_items(id),
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(from_memory_id, relation, to_memory_id)
                );
                """
            )
            try:
                existed = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
                ).fetchone()
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content, namespace UNINDEXED, kind UNINDEXED)"
                )
                if existed is None:
                    db.execute(
                        "INSERT INTO memory_fts(memory_id, content, namespace, kind) "
                        "SELECT id, content, namespace, kind FROM memory_items"
                    )
                db.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_items BEGIN
                        INSERT INTO memory_fts(memory_id, content, namespace, kind)
                        VALUES (new.id, new.content, new.namespace, new.kind);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE OF content, namespace, kind ON memory_items BEGIN
                        DELETE FROM memory_fts WHERE memory_id = old.id;
                        INSERT INTO memory_fts(memory_id, content, namespace, kind)
                        VALUES (new.id, new.content, new.namespace, new.kind);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_items BEGIN
                        DELETE FROM memory_fts WHERE memory_id = old.id;
                    END;
                    """
                )
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False

    @staticmethod
    def _item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"], kind=MemoryKind(row["kind"]), namespace=row["namespace"],
            content=row["content"], source=row["source"], privacy=MemoryPrivacy(row["privacy"]),
            importance=row["importance"], confidence=row["confidence"], status=MemoryStatus(row["status"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"], expires_at=row["expires_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    def get(self, memory_id: str) -> MemoryItem | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        return self._item(row) if row else None

    def find_active_duplicate(
        self, namespace: str, kind: MemoryKind, privacy: MemoryPrivacy, normalized: str
    ) -> MemoryItem | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM memory_items WHERE namespace=? AND kind=? AND privacy=? "
                "AND normalized_content=? AND status='ACTIVE'",
                (namespace, kind.value, privacy.value, normalized),
            ).fetchone()
        return self._item(row) if row else None

    def save_item(self, *, memory_id: str, kind: MemoryKind, namespace: str, content: str,
                  source: str, privacy: MemoryPrivacy, importance: float, confidence: float,
                  expires_at: str | None, metadata: dict[str, Any], existing_id: str | None = None) -> MemoryItem:
        now = utc_now()
        normalized = normalize_text(content)
        with self._connection() as db:
            if existing_id:
                db.execute(
                    "UPDATE memory_items SET kind=?, namespace=?, content=?, normalized_content=?, source=?, privacy=?, "
                    "importance=?, confidence=?, status='ACTIVE', updated_at=?, expires_at=?, metadata_json=? WHERE id=?",
                    (kind.value, namespace, content, normalized, source, privacy.value, importance, confidence,
                     now, expires_at, json.dumps(metadata, ensure_ascii=False, sort_keys=True), existing_id),
                )
                row = db.execute("SELECT * FROM memory_items WHERE id=?", (existing_id,)).fetchone()
            else:
                db.execute(
                    "INSERT INTO memory_items(id,kind,namespace,content,normalized_content,source,privacy,importance,confidence,status,created_at,updated_at,expires_at,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?)",
                    (memory_id, kind.value, namespace, content, normalized, source, privacy.value, importance,
                     confidence, now, now, expires_at, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
                )
                row = db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,)).fetchone()
        return self._item(row)

    def merge_duplicate(self, memory_id: str, *, content: str, source: str, importance: float,
                        confidence: float, expires_at: str | None, metadata: dict[str, Any]) -> MemoryItem:
        now = utc_now()
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_items WHERE id=? AND status='ACTIVE'", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            combined_metadata = json.loads(row["metadata_json"])
            combined_metadata.update(metadata)
            db.execute(
                "UPDATE memory_items SET source=?, importance=?, confidence=?, updated_at=?, "
                "expires_at=COALESCE(?,expires_at), metadata_json=? WHERE id=?",
                (source, max(row["importance"], importance), max(row["confidence"], confidence), now,
                 expires_at, json.dumps(combined_metadata, ensure_ascii=False, sort_keys=True), memory_id),
            )
            updated = db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,)).fetchone()
        return self._item(updated)

    def update_item(self, old_id: str, *, new_id: str, kind: MemoryKind, namespace: str,
                    content: str, source: str, privacy: MemoryPrivacy, importance: float,
                    confidence: float, expires_at: str | None, metadata: dict[str, Any]) -> MemoryItem:
        now = utc_now()
        with self._connection() as db:
            old = db.execute("SELECT * FROM memory_items WHERE id=? AND status='ACTIVE'", (old_id,)).fetchone()
            if old is None:
                raise KeyError(old_id)
            if old["normalized_content"] == normalize_text(content):
                db.execute(
                    "UPDATE memory_items SET kind=?, namespace=?, content=?, normalized_content=?, source=?, privacy=?, "
                    "importance=?, confidence=?, updated_at=?, expires_at=?, metadata_json=? WHERE id=?",
                    (kind.value, namespace, content, normalize_text(content), source, privacy.value,
                     importance, confidence, now, expires_at,
                     json.dumps(metadata, ensure_ascii=False, sort_keys=True), old_id),
                )
                row = db.execute("SELECT * FROM memory_items WHERE id=?", (old_id,)).fetchone()
            else:
                db.execute("UPDATE memory_items SET status='SUPERSEDED', updated_at=? WHERE id=?", (now, old_id))
                db.execute(
                    "INSERT INTO memory_items(id,kind,namespace,content,normalized_content,source,privacy,importance,confidence,status,created_at,updated_at,expires_at,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?)",
                    (new_id, kind.value, namespace, content, normalize_text(content), source, privacy.value,
                     importance, confidence, now, now, expires_at,
                     json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
                )
                row = db.execute("SELECT * FROM memory_items WHERE id=?", (new_id,)).fetchone()
        return self._item(row)

    def set_status(self, memory_id: str, status: MemoryStatus) -> MemoryItem | None:
        with self._connection() as db:
            db.execute("UPDATE memory_items SET status=?, updated_at=? WHERE id=?", (status.value, utc_now(), memory_id))
            row = db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,)).fetchone()
        return self._item(row) if row else None

    def search_rows(self, *, query: str, namespaces: tuple[str, ...], privacy: tuple[MemoryPrivacy, ...],
                    limit: int, include_inactive: bool, include_expired: bool, fts_query: str) -> list[MemoryItem]:
        if not namespaces or not privacy:
            return []
        now = utc_now()
        namespace_marks = ",".join("?" for _ in namespaces)
        privacy_marks = ",".join("?" for _ in privacy)
        filters = [f"m.namespace IN ({namespace_marks})", f"m.privacy IN ({privacy_marks})"]
        params: list[Any] = [*namespaces, *(tier.value for tier in privacy)]
        if not include_inactive:
            filters.append("m.status='ACTIVE'")
        if not include_expired:
            filters.append("(m.expires_at IS NULL OR m.expires_at > ?)")
            params.append(now)
        join = ""
        terms = tuple(dict.fromkeys(re.findall(r"[\w-]+", normalize_text(query))))
        if self.fts_available and fts_query:
            join = "JOIN memory_fts f ON f.memory_id=m.id"
            filters.append("memory_fts MATCH ?")
            params.append(fts_query)
        elif terms:
            filters.append("(" + " OR ".join("instr(m.normalized_content, ?) > 0" for _ in terms) + ")")
            params.extend(terms)
        sql = "SELECT m.* FROM memory_items m " + join + " WHERE " + " AND ".join(filters) + " ORDER BY m.updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        try:
            with self._connection() as db:
                rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if not self.fts_available:
                raise
            # Unsupported FTS syntax/module errors degrade to deterministic text matching.
            fallback_filters = [item for item in filters if item != "memory_fts MATCH ?"]
            fallback_params = list(params[:-2])
            if terms:
                fallback_filters.append("(" + " OR ".join("instr(m.normalized_content, ?) > 0" for _ in terms) + ")")
                fallback_params.extend(terms)
            fallback_params.append(params[-1])
            sql = "SELECT m.* FROM memory_items m WHERE " + " AND ".join(fallback_filters) + " ORDER BY m.updated_at DESC LIMIT ?"
            with self._connection() as db:
                rows = db.execute(sql, fallback_params).fetchall()
        return [self._item(row) for row in rows]

    def touch(self, memory_ids: tuple[str, ...]) -> None:
        if not memory_ids:
            return
        marks = ",".join("?" for _ in memory_ids)
        with self._connection() as db:
            db.execute(f"UPDATE memory_items SET last_accessed_at=? WHERE id IN ({marks})", (utc_now(), *memory_ids))

    def create_link(self, from_id: str, relation: str, to_id: str, metadata: dict[str, Any]) -> MemoryLink:
        link_id = str(uuid.uuid4())
        now = utc_now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO memory_links(id,from_memory_id,relation,to_memory_id,created_at,metadata_json) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(from_memory_id,relation,to_memory_id) DO NOTHING",
                (link_id, from_id, relation, to_id, now, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
            )
            row = db.execute(
                "SELECT * FROM memory_links WHERE from_memory_id=? AND relation=? AND to_memory_id=?",
                (from_id, relation, to_id),
            ).fetchone()
        return self._link(row)

    @staticmethod
    def _link(row: sqlite3.Row) -> MemoryLink:
        return MemoryLink(row["id"], row["from_memory_id"], row["relation"], row["to_memory_id"],
                          row["created_at"], json.loads(row["metadata_json"]))

    def get_links(self, memory_id: str, *, include_inactive: bool = False) -> list[MemoryLink]:
        status_clause = "" if include_inactive else "AND a.status='ACTIVE' AND b.status='ACTIVE' AND (a.expires_at IS NULL OR a.expires_at>?) AND (b.expires_at IS NULL OR b.expires_at>?)"
        params: tuple[Any, ...] = (memory_id, memory_id) if include_inactive else (memory_id, memory_id, utc_now(), utc_now())
        with self._connection() as db:
            rows = db.execute(
                "SELECT l.* FROM memory_links l JOIN memory_items a ON a.id=l.from_memory_id "
                "JOIN memory_items b ON b.id=l.to_memory_id WHERE (l.from_memory_id=? OR l.to_memory_id=?) "
                + status_clause + " ORDER BY l.created_at, l.id",
                params,
            ).fetchall()
        return [self._link(row) for row in rows]

    def count_items(self) -> int:
        with self._connection() as db:
            return int(db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0])
