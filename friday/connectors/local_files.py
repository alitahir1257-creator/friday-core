"""Bounded, read-only text search in explicitly approved local directories."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PureWindowsPath

from friday.knowledge.models import UnifiedResult
from friday.knowledge.retrieval import PRIVACY, fingerprint, read_allowed, relevance, snippet
from friday.knowledge.vault import default_vault_path
from friday.memory.policy import contains_possible_secret, normalize_namespace
from friday.paths import MEMORY_DB_PATH, VAULT_PATH

_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm"}
_IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", ".obsidian"}
_SECRET_PREFIXES = ("credentials", "secrets", "token", "cookies", "id_rsa", "id_ed25519")


def _excluded(name: str) -> bool:
    value = name.casefold()
    return (value.startswith(".") or value in _IGNORED or value.startswith(_SECRET_PREFIXES)
            or value.endswith((".pem", ".key")))


def _linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True)
class ApprovedRoot:
    path: str | Path
    namespace: str = "global"
    privacy: str = "private"


class LocalFilesConnector:
    name = "Local Files"
    source_type = "local_file"
    capabilities = frozenset({"search", "get"})

    def __init__(self, roots: tuple[ApprovedRoot | str | Path, ...] = (), *, enabled: bool = False,
                 max_file_bytes: int = 262144, max_entries: int = 5000) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (max_file_bytes, max_entries)):
            raise ValueError("File and scan budgets must be positive integers.")
        self.enabled = enabled
        self.max_file_bytes = max_file_bytes
        self.max_entries = max_entries
        self.roots: tuple[ApprovedRoot, ...] = tuple(self._approve(r) for r in roots)
        for index, root in enumerate(self.roots):
            if any(Path(root.path).is_relative_to(other.path) or Path(other.path).is_relative_to(root.path)
                   for other in self.roots[:index]):
                raise ValueError("Approved roots must not overlap; privacy must have one authority.")

    def _approve(self, root: ApprovedRoot | str | Path) -> ApprovedRoot:
        config = root if isinstance(root, ApprovedRoot) else ApprovedRoot(root)
        path = Path(config.path).expanduser()
        if not path.is_absolute() or path == Path(path.anchor) or not path.is_dir():
            raise ValueError("Approve an existing absolute directory below a drive root.")
        if config.privacy not in PRIVACY:
            raise ValueError("Unsupported root privacy.")
        resolved = path.resolve(strict=True)
        if self._forbidden(resolved) or any(_excluded(p.name) or _linked(p) for p in (path, *path.parents) if p.name):
            raise ValueError("Excluded or linked directories cannot be approved.")
        return ApprovedRoot(resolved, normalize_namespace(config.namespace), config.privacy)

    @staticmethod
    def _forbidden(path: Path) -> bool:
        return any(path.is_relative_to(base.resolve()) for base in (MEMORY_DB_PATH.parent, VAULT_PATH, default_vault_path()))

    def health(self) -> str:
        if not self.enabled:
            return "DISABLED"
        if not self.roots:
            return "NO_ROOTS"
        return "READY" if all(Path(r.path).is_dir() for r in self.roots) else "UNAVAILABLE"

    def _safe_path(self, root: ApprovedRoot, relative: str, extensions: frozenset[str] | set[str] = _EXTENSIONS) -> Path:
        win = PureWindowsPath(relative)
        parts = relative.replace("\\", "/").split("/")
        if not relative or win.drive or win.is_absolute() or Path(relative).is_absolute():
            raise PermissionError("File must be relative to an approved root.")
        if any(p in {"", ".", ".."} or _excluded(p) or ":" in p for p in parts):
            raise PermissionError("Excluded file path.")
        base = Path(root.path)
        candidate = base.joinpath(*parts)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(base) or self._forbidden(resolved):
            raise PermissionError("File is outside its approved root.")
        if any(_linked(p) for p in (candidate, *candidate.parents) if p == base or p.is_relative_to(base)):
            raise PermissionError("Linked files and directories are excluded.")
        if resolved.suffix.casefold() not in extensions or not resolved.is_file():
            raise PermissionError("Unsupported file type.")
        return resolved

    def read_approved_file(
        self,
        source_id: str,
        *,
        extensions: frozenset[str],
        max_bytes: int,
        privacy_level: str = "public",
        route_backend: str | None = None,
    ) -> tuple[Path, bytes, ApprovedRoot]:
        """Read one bounded file under an approved root, rechecking identity after open."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= self.max_file_bytes:
            raise ValueError("Read limit must be positive and no larger than the connector limit.")
        if not extensions or any(not isinstance(ext, str) or not ext.startswith(".") for ext in extensions):
            raise ValueError("File extensions must be explicit suffixes.")
        try:
            prefix, relative = source_id.split(":", 1)
            index = int(prefix)
            if not 0 <= index < len(self.roots):
                raise ValueError
            root = self.roots[index]
        except (ValueError, AttributeError, IndexError, TypeError):
            raise PermissionError("Unknown approved-root file ID.") from None
        if not self.enabled or not read_allowed(root.privacy, privacy_level, route_backend):
            raise PermissionError("File is not available on this privacy route.")
        path = self._safe_path(root, relative, frozenset(ext.casefold() for ext in extensions))
        before = path.stat()
        if before.st_size > max_bytes:
            raise ValueError("FILE_TOO_LARGE")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as file:
            opened = os.fstat(file.fileno())
            checked = self._safe_path(root, relative, frozenset(ext.casefold() for ext in extensions)).stat()
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
                opened.st_dev, opened.st_ino
            ) != (checked.st_dev, checked.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise PermissionError("File changed while it was being opened.")
            data = file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("FILE_TOO_LARGE")
        return path, data, root

    def _read(self, index: int, relative: str, query: str) -> UnifiedResult | None:
        root = self.roots[index]
        path = self._safe_path(root, relative)
        before = path.stat()
        if before.st_size > self.max_file_bytes:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as file:
            opened = os.fstat(file.fileno())
            # Recheck after opening so a replaced path cannot become an approved file.
            checked = self._safe_path(root, relative).stat()
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
                opened.st_dev, opened.st_ino
            ) != (checked.st_dev, checked.st_ino):
                return None
            data = file.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes or b"\x00" in data:
            return None
        text = data.decode("utf-8-sig")
        if any(ord(c) < 32 and c not in "\t\r\n" for c in text) or contains_possible_secret(text):
            return None
        score = relevance(query, text, path.stem) if query else 1.0
        if not score:
            return None
        return UnifiedResult(
            self.source_type, self.name, f"{index}:{relative}", root.namespace,
            path.stem, snippet(text, query), root.privacy, score, 0.8,
            datetime.fromtimestamp(opened.st_mtime, timezone.utc).isoformat(),
            {"content_fingerprint": fingerprint(text), "relative_path": relative, "capabilities": ("search", "get")},
        )

    def get(self, source_id: str, *, privacy_level: str = "public",
            route_backend: str | None = None) -> UnifiedResult | None:
        read_allowed("public", privacy_level, route_backend)
        if not self.enabled or not self.roots:
            return None
        try:
            prefix, relative = source_id.split(":", 1)
            index = int(prefix)
            if not 0 <= index < len(self.roots):
                raise ValueError()
        except (ValueError, AttributeError):
            raise PermissionError("Unknown approved-root file ID.") from None
        if not read_allowed(self.roots[index].privacy, privacy_level, route_backend):
            return None
        return self._read(index, relative, "")

    def search(self, query: str, *, namespace: str = "global", privacy_level: str = "public",
               route_backend: str | None = None, limit: int = 10) -> list[UnifiedResult]:
        scope = normalize_namespace(namespace)
        read_allowed("public", privacy_level, route_backend)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
            raise ValueError("limit must be an integer from 0 to 1000.")
        if not self.enabled or not self.roots or not query.strip() or not limit:
            return []
        results = []
        visited = 0
        # ponytail: bounded on-demand scan; add an index only after measured approved-root search latency warrants it.
        for index, root in enumerate(self.roots):
            if root.namespace not in {scope, "global"} or not read_allowed(root.privacy, privacy_level, route_backend):
                continue
            stack = [Path(root.path)]
            while stack and visited < self.max_entries:
                current = stack.pop()
                try:
                    if _linked(current) or self._forbidden(current):
                        continue
                    with os.scandir(current) as iterator:
                        entries = sorted(islice(iterator, self.max_entries - visited), key=lambda e: e.name.casefold())
                    visited += len(entries)
                    directories = []
                    for entry in entries:
                        if _excluded(entry.name):
                            continue
                        path = Path(entry.path)
                        if _linked(path) or self._forbidden(path.resolve()):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(path)
                        elif path.suffix.casefold() in _EXTENSIONS:
                            try:
                                result = self._read(index, path.relative_to(root.path).as_posix(), query)
                                if result is not None:
                                    results.append(result)
                            except (OSError, ValueError):
                                continue
                    stack.extend(reversed(directories))
                except OSError:
                    continue
        results.sort(key=lambda r: (-r.relevance, r.source_id))
        return results[:limit]
