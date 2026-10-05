"""Local Markdown knowledge vault with safe paths and deterministic retrieval."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any

from friday.knowledge.models import (
    ConcurrentNoteEditError,
    KnowledgeContext,
    KnowledgeNote,
    KnowledgeWriteRejected,
    NoteFrontmatter,
    NoteSearchResult,
)
from friday.memory.policy import contains_possible_secret
from friday.paths import VAULT_PATH


_LOG = logging.getLogger("friday.knowledge")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_TOP_LEVEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_PRIVACY_LEVELS = {"public": 0, "private": 1, "local_only": 2}
_STANDARD_FIELDS = ("title", "namespace", "type", "privacy", "created", "updated", "tags")


def default_vault_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("FRIDAY_VAULT_PATH", "").strip()
    return Path(override).expanduser() if override else VAULT_PATH


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN.findall(value)}


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError("Frontmatter values must be simple scalars or lists of scalars.")


def _frontmatter_block(key: str, value: Any) -> list[str]:
    if not _KEY.fullmatch(key):
        raise ValueError("Invalid frontmatter key.")
    if isinstance(value, (list, tuple)):
        lines = [f"{key}:\n"]
        lines.extend(f"  - {_scalar(item)}\n" for item in value)
        return lines
    return [f"{key}: {_scalar(value)}\n"]


def _split_document(text: str) -> tuple[tuple[str, ...], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return (), text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return tuple(lines[1:index]), "".join(lines[index + 1 :])
    return (), text


def _frontmatter_blocks(lines: Sequence[str]) -> list[tuple[str | None, list[str]]]:
    blocks: list[tuple[str | None, list[str]]] = []
    for line in lines:
        match = _TOP_LEVEL.match(line.rstrip("\r\n")) if line and not line[0].isspace() else None
        if match:
            blocks.append((match.group(1), [line]))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            blocks.append((None, [line]))
    return blocks


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value in {"true", "false", "null", "~"}:
        return {"true": True, "false": False, "null": None, "~": None}[value]
    if value.startswith('"'):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value.strip('"')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _block_value(lines: Sequence[str]) -> Any:
    first = lines[0].split(":", 1)[1].strip()
    if first:
        return _parse_scalar(first)
    items = []
    for line in lines[1:]:
        match = re.match(r"^\s+-\s+(.*?)(?:\r?\n)?$", line)
        if match:
            items.append(_parse_scalar(match.group(1)))
    return tuple(items)


def _decode_frontmatter(raw: Sequence[str], fallback_title: str) -> tuple[NoteFrontmatter, bool]:
    values: dict[str, Any] = {}
    for key, lines in _frontmatter_blocks(raw):
        if key is not None:
            values[key] = _block_value(lines)
    privacy = values.get("privacy", "private")
    if privacy not in _PRIVACY_LEVELS:
        privacy = "private"
    tags = values.get("tags", ())
    if isinstance(tags, str):
        tags = (tags,) if tags else ()
    if not isinstance(tags, (tuple, list)):
        tags = ()
    extra = {key: value for key, value in values.items() if key not in _STANDARD_FIELDS}
    frontmatter = NoteFrontmatter(
        title=str(values.get("title") or fallback_title),
        namespace=str(values.get("namespace") or "global"),
        note_type=str(values.get("type") or "note"),
        privacy=privacy,
        created=str(values["created"]) if values.get("created") is not None else None,
        updated=str(values["updated"]) if values.get("updated") is not None else None,
        tags=tuple(str(tag) for tag in tags),
        extra=extra,
    )
    return frontmatter, values.get("_friday_managed") is True


def _updated_frontmatter(raw: Sequence[str], updates: Mapping[str, Any]) -> tuple[str, ...]:
    blocks = _frontmatter_blocks(raw)
    pending = dict(updates)
    output: list[str] = []
    for key, lines in blocks:
        if key in pending:
            output.extend(_frontmatter_block(key, pending.pop(key)))
        else:
            output.extend(lines)
    for key, value in pending.items():
        output.extend(_frontmatter_block(key, value))
    return tuple(output)


def _render_document(raw: Sequence[str], body: str) -> str:
    return "---\n" + "".join(raw) + "---\n" + body


class KnowledgeVault:
    """A plain-file vault. Constructing it never creates or populates the vault."""

    def __init__(self, vault_path: str | os.PathLike[str] | None = None) -> None:
        configured = default_vault_path() if vault_path is None else Path(vault_path).expanduser()
        self.root = configured.resolve()

    def initialize_vault(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        readme = self._safe_path("FRIDAY/README.md")
        readme.parent.mkdir(parents=True, exist_ok=True)
        try:
            with readme.open("x", encoding="utf-8", newline="") as file:
                file.write("# FRIDAY\n\nThis folder contains FRIDAY project notes.\n")
        except FileExistsError:
            pass
        return self.root

    def _safe_path(self, note_path: str | os.PathLike[str]) -> Path:
        raw = os.fspath(note_path).strip()
        windows = PureWindowsPath(raw)
        if not raw or Path(raw).is_absolute() or windows.is_absolute() or windows.drive:
            raise ValueError("Vault note path must be relative.")
        parts = tuple(part for part in raw.replace("\\", "/").split("/") if part not in {"", "."})
        if not parts or any(part == ".." for part in parts):
            raise ValueError("Vault note path escapes the vault.")
        if any(part.casefold() == ".obsidian" for part in parts):
            raise ValueError("Obsidian configuration is not knowledge content.")
        relative = Path(*parts)
        if relative.suffix.casefold() != ".md":
            if relative.suffix:
                raise ValueError("Knowledge notes must be Markdown files.")
            relative = relative.with_suffix(".md")
        root = self.root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError("Vault note path escapes the vault.") from error
        return target

    @staticmethod
    def _read_text(path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as file:
            return file.read()

    def _note_from_text(self, relative_path: str, text: str) -> KnowledgeNote:
        raw, body = _split_document(text)
        title = Path(relative_path).stem
        frontmatter, managed = _decode_frontmatter(raw, title)
        return KnowledgeNote(relative_path, frontmatter, body, managed, raw)

    def _read_path(self, relative_path: str | os.PathLike[str]) -> tuple[Path, KnowledgeNote, str]:
        path = self._safe_path(relative_path)
        text = self._read_text(path)
        return path, self._note_from_text(path.relative_to(self.root).as_posix(), text), text

    def _validate_read(self, note: KnowledgeNote, privacy_level: str, route_backend: str | None) -> bool:
        if privacy_level not in _PRIVACY_LEVELS:
            raise ValueError("privacy_level must be public, private, or local_only.")
        if note.privacy == "public":
            return True
        if route_backend != "local":
            return False
        return privacy_level != "public"

    def read_note(
        self, note_path: str | os.PathLike[str], *, privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> KnowledgeNote:
        _, note, _ = self._read_path(note_path)
        if not self._validate_read(note, privacy_level, route_backend):
            raise PermissionError("Vault note is not available on this route.")
        return note

    @staticmethod
    def _reject_secret() -> None:
        _LOG.warning("knowledge_write_rejected reason=possible_secret")
        raise KnowledgeWriteRejected()

    def create_note(
        self,
        note_path: str | os.PathLike[str],
        body: str = "",
        *,
        title: str | None = None,
        namespace: str = "global",
        note_type: str = "note",
        privacy: str = "private",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> KnowledgeNote:
        if privacy not in _PRIVACY_LEVELS:
            raise ValueError("privacy must be public, private, or local_only.")
        if not isinstance(body, str):
            raise ValueError("Note body must be text.")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be non-empty text.")
        extra = dict(metadata or {})
        if any(key in _STANDARD_FIELDS or key == "_friday_managed" for key in extra):
            raise ValueError("metadata cannot replace standard vault fields.")
        note_title = (title or Path(os.fspath(note_path).replace("\\", "/")).stem).strip()
        if not note_title:
            raise ValueError("title must be non-empty text.")
        if contains_possible_secret((body, note_title, namespace, note_type, privacy, tags, extra)):
            self._reject_secret()
        path = self._safe_path(note_path)
        now = date.today().isoformat()
        values: dict[str, Any] = {
            "title": note_title,
            "namespace": namespace.strip(),
            "type": note_type,
            "privacy": privacy,
            "created": now,
            "updated": now,
            "tags": tuple(tags),
            "_friday_managed": True,
            **extra,
        }
        frontmatter: list[str] = []
        for key, value in values.items():
            frontmatter.extend(_frontmatter_block(key, value))
        document_body = body
        if document_body and not document_body.endswith(("\n", "\r")):
            document_body += "\n"
        document = _render_document(frontmatter, document_body)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="") as file:
            file.write(document)
        return self._note_from_text(path.relative_to(self.root).as_posix(), document)

    def _write_managed_update(
        self,
        note_path: str | os.PathLike[str],
        *,
        new_body: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> KnowledgeNote:
        if not isinstance(new_body, str):
            raise ValueError("Note body must be text.")
        path, note, original = self._read_path(note_path)
        if not note.managed:
            raise PermissionError("FRIDAY can update only FRIDAY-managed notes.")
        updates = dict(metadata or {})
        if "_friday_managed" in updates:
            raise ValueError("The FRIDAY ownership marker cannot be changed.")
        if "privacy" in updates and updates["privacy"] not in _PRIVACY_LEVELS:
            raise ValueError("privacy must be public, private, or local_only.")
        if any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in updates):
            raise ValueError("Invalid frontmatter key.")
        if contains_possible_secret((new_body, updates, note.raw_frontmatter)):
            self._reject_secret()
        old_values = {key: _block_value(lines) for key, lines in _frontmatter_blocks(note.raw_frontmatter) if key}
        changed = new_body != note.body or any(old_values.get(key) != value for key, value in updates.items())
        if not changed:
            return note
        updates["updated"] = date.today().isoformat()
        raw = _updated_frontmatter(note.raw_frontmatter, updates)
        replacement = _render_document(raw, new_body)
        current = self._read_text(path)
        if current != original:
            raise ConcurrentNoteEditError("Note changed while FRIDAY was preparing an update.")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(replacement)
            if self._read_text(path) != original:
                raise ConcurrentNoteEditError("Note changed while FRIDAY was preparing an update.")
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return self._note_from_text(path.relative_to(self.root).as_posix(), replacement)

    def update_note(
        self,
        note_path: str | os.PathLike[str],
        body: str | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        replace_body: bool = False,
    ) -> KnowledgeNote:
        _, note, _ = self._read_path(note_path)
        if body is not None and not replace_body:
            raise ValueError("Replacing note body requires replace_body=True.")
        return self._write_managed_update(
            note_path, new_body=note.body if body is None else body, metadata=metadata
        )

    def append_note(self, note_path: str | os.PathLike[str], text: str) -> KnowledgeNote:
        _, note, _ = self._read_path(note_path)
        if not isinstance(text, str):
            raise ValueError("Appended note text must be text.")
        separator = "" if not note.body or note.body.endswith(("\n", "\r")) else "\n"
        return self._write_managed_update(note_path, new_body=note.body + separator + text)

    def move_note(
        self, note_path: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> KnowledgeNote:
        source, note, _ = self._read_path(note_path)
        if not note.managed:
            raise PermissionError("FRIDAY can move only FRIDAY-managed notes.")
        target = self._safe_path(destination)
        if target == source:
            return note
        if target.exists():
            raise FileExistsError("Vault destination already exists.")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return self.read_note(target.relative_to(self.root).as_posix(), privacy_level="local_only", route_backend="local")

    def archive_note(self, note_path: str | os.PathLike[str]) -> KnowledgeNote:
        source = self._safe_path(note_path)
        return self.move_note(note_path, Path("Archive") / source.name)

    def _iter_notes(self) -> list[tuple[Path, KnowledgeNote]]:
        # ponytail: O(n) on-disk scan; add an index only if measured vault size makes search slow.
        if not self.root.exists():
            return []
        if not self.root.is_dir():
            raise NotADirectoryError("Vault root is not a directory.")
        result: list[tuple[Path, KnowledgeNote]] = []
        for candidate in self.root.rglob("*"):
            if candidate.suffix.casefold() != ".md" or any(part.casefold() == ".obsidian" for part in candidate.parts):
                continue
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            text = self._read_text(resolved)
            result.append((resolved, self._note_from_text(relative, text)))
        return result

    @staticmethod
    def _namespace_matches(note: KnowledgeNote, namespace: str | None) -> bool:
        if namespace is None:
            return True
        scope = namespace.casefold()
        parts = note.path.split("/")
        project = parts[1].casefold() if len(parts) > 2 and parts[0].casefold() == "projects" else None
        if project is not None:
            return project == scope or note.namespace.casefold() == scope
        return note.namespace.casefold() in {scope, "global"}

    def list_notes(
        self,
        *,
        namespace: str | None = None,
        privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> list[KnowledgeNote]:
        notes = [
            note for _, note in self._iter_notes()
            if self._namespace_matches(note, namespace)
            and self._validate_read(note, privacy_level, route_backend)
        ]
        return sorted(notes, key=lambda note: (note.path.casefold(), note.path))

    def search_notes(
        self,
        query: str,
        *,
        namespace: str | None = None,
        privacy_level: str = "public",
        route_backend: str | None = None,
        limit: int = 10,
    ) -> list[NoteSearchResult]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer.")
        if privacy_level not in _PRIVACY_LEVELS:
            raise ValueError("privacy_level must be public, private, or local_only.")
        terms = _tokens(query)
        if not terms or limit == 0:
            return []
        phrase = " ".join(_TOKEN.findall(query)).casefold()
        now = date.today()
        ranked: list[tuple[float, str, NoteSearchResult]] = []
        for _, note in self._iter_notes():
            if not self._namespace_matches(note, namespace) or not self._validate_read(note, privacy_level, route_backend):
                continue
            filename = Path(note.path).stem
            heading_text = "\n".join(
                line.lstrip("#").strip() for line in note.body.splitlines() if line.lstrip().startswith("#")
            )
            fields = (
                ("title", note.title, 100.0),
                ("filename", filename, 80.0),
                ("tags", " ".join(note.frontmatter.tags), 55.0),
                ("heading", heading_text, 40.0),
                ("body", note.body, 20.0),
                ("namespace", note.namespace, 8.0),
            )
            score = 0.0
            for field_name, value, weight in fields:
                field_tokens = _tokens(value)
                overlap = len(terms & field_tokens) / len(terms)
                if overlap:
                    score += weight * overlap
                    if phrase and phrase in " ".join(_TOKEN.findall(value)).casefold():
                        score += weight * 0.25
            if score == 0:
                continue
            scope = namespace.casefold() if namespace else None
            parts = note.path.split("/")
            project = parts[1].casefold() if len(parts) > 2 and parts[0].casefold() == "projects" else None
            if scope and project == scope:
                score += 12.0
            elif scope and note.namespace.casefold() == scope:
                score += 8.0
            if note.frontmatter.updated:
                try:
                    age_days = max(0, (now - date.fromisoformat(note.frontmatter.updated[:10])).days)
                    score += 0.01 / (1 + age_days / 30)
                except ValueError:
                    pass
            snippet = self._snippet(note.body, terms)
            ranked.append((score, note.path.casefold(), NoteSearchResult(note, score, snippet)))
        ranked.sort(key=lambda row: (-row[0], row[1], row[2].note.path))
        return [row[2] for row in ranked[:limit]]

    @staticmethod
    def _snippet(body: str, terms: set[str], max_chars: int = 320) -> str:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        for line in lines:
            if _tokens(line) & terms:
                return line[:max_chars]
        return " ".join(lines)[:max_chars]

    def build_context(
        self,
        query: str,
        *,
        namespace: str = "global",
        privacy_level: str = "public",
        route_backend: str | None = None,
        max_notes: int = 5,
        max_chars: int = 1800,
    ) -> KnowledgeContext:
        if max_notes < 0 or max_chars < 0:
            raise ValueError("context budgets must not be negative.")
        if max_notes == 0 or max_chars < len("Relevant knowledge:\n"):
            return KnowledgeContext("", 0, (), 0)
        results = self.search_notes(
            query, namespace=namespace, privacy_level=privacy_level,
            route_backend=route_backend, limit=max_notes,
        )
        header = "Relevant knowledge:\n"
        lines: list[str] = []
        paths: list[str] = []
        used = len(header)
        for result in results:
            prefix = f"- [{result.note.title}] "
            room = max_chars - used - len(prefix) - 1
            if room <= 0:
                break
            snippet = result.snippet[:room]
            line = prefix + snippet
            lines.append(line)
            paths.append(result.note.path)
            used += len(line) + 1
        text = header + "\n".join(lines) if lines else ""
        return KnowledgeContext(text, len(lines), tuple(paths), len(text))

    def get_outgoing_links(
        self,
        note: KnowledgeNote | str | os.PathLike[str],
        *,
        privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> tuple[str, ...]:
        if not isinstance(note, KnowledgeNote):
            note = self.read_note(note, privacy_level=privacy_level, route_backend=route_backend)
        links = []
        for match in _LINK.findall(note.body):
            target = re.split(r"[|#]", match, maxsplit=1)[0].strip()
            if target:
                links.append(target)
        return tuple(links)

    def outgoing_links(
        self, note_path: str | os.PathLike[str], *, privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> tuple[str, ...]:
        return self.get_outgoing_links(note_path, privacy_level=privacy_level, route_backend=route_backend)

    def get_backlinks(
        self,
        note_path_or_title: str | os.PathLike[str],
        *,
        namespace: str | None = None,
        privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> tuple[KnowledgeNote, ...]:
        raw = os.fspath(note_path_or_title)
        try:
            target_note = self.read_note(raw, privacy_level=privacy_level, route_backend=route_backend)
            targets = {target_note.title.casefold(), Path(target_note.path).stem.casefold(), target_note.path.casefold()}
        except (FileNotFoundError, ValueError):
            target = re.split(r"[|#]", raw.replace("\\", "/"), maxsplit=1)[0].strip().casefold()
            targets = {target, Path(target).stem, Path(target).name}
        backlinks: list[KnowledgeNote] = []
        for note in self.list_notes(namespace=namespace, privacy_level=privacy_level, route_backend=route_backend):
            if any(Path(link.replace("\\", "/")).stem.casefold() in targets for link in self.get_outgoing_links(note)):
                backlinks.append(note)
        return tuple(backlinks)

    def memory_to_note(
        self,
        memory_manager: Any,
        memory_id: str,
        note_path: str | os.PathLike[str] | None = None,
    ) -> KnowledgeNote:
        item = memory_manager.get(memory_id)
        if item is None:
            raise LookupError("Memory item was not found or is not active.")
        existing = self._promoted_note({item.id})
        if existing is not None:
            return existing
        folder = "Knowledge" if item.namespace == "global" else f"Projects/{item.namespace.upper()}"
        target = note_path or Path(folder) / f"Memory-{item.id[:8]}.md"
        return self.create_note(
            target, item.content, title=f"Memory {item.id[:8]}", namespace=item.namespace,
            note_type="memory", privacy=item.privacy.value, tags=("memory",),
            metadata={"source_memory_ids": [item.id]},
        )

    def create_note_from_memories(
        self,
        memory_manager: Any,
        memory_ids: Sequence[str],
        note_path: str | os.PathLike[str] | None = None,
    ) -> KnowledgeNote:
        ids = tuple(dict.fromkeys(memory_ids))
        if not ids or len(ids) != len(memory_ids):
            raise ValueError("memory_ids must be non-empty and unique.")
        items = [memory_manager.get(memory_id) for memory_id in ids]
        if any(item is None for item in items):
            raise LookupError("A memory item was not found or is not active.")
        existing = self._promoted_note(set(ids))
        if existing is not None:
            return existing
        scopes = {item.namespace for item in items}
        if len(scopes) != 1:
            raise ValueError("Promoted memories must share a namespace.")
        privacy = max((item.privacy.value for item in items), key=_PRIVACY_LEVELS.__getitem__)
        body = "\n\n".join(f"## {item.kind.value.title()}\n\n{item.content}" for item in items)
        short = "-".join(memory_id[:8] for memory_id in ids)
        folder = "Knowledge" if items[0].namespace == "global" else f"Projects/{items[0].namespace.upper()}"
        target = note_path or Path(folder) / f"Memories-{short}.md"
        return self.create_note(
            target, body, title=f"Promoted memories {short}", namespace=items[0].namespace,
            note_type="memory-summary", privacy=privacy, tags=("memory",),
            metadata={"source_memory_ids": list(ids)},
        )

    def _promoted_note(self, memory_ids: set[str]) -> KnowledgeNote | None:
        for note in self.list_notes(privacy_level="local_only", route_backend="local"):
            source_ids = note.frontmatter.extra.get("source_memory_ids", ())
            if isinstance(source_ids, str):
                source_ids = (source_ids,)
            if not isinstance(source_ids, (list, tuple, set)):
                continue
            existing_ids = set(str(value) for value in source_ids)
            if existing_ids == memory_ids:
                return note
            if existing_ids & memory_ids:
                raise ValueError("One or more memories already have a different vault promotion.")
        return None
