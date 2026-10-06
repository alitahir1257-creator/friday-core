"""Offline, temporary-database acceptance checks for Phase 7C.1."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.connectors import ConnectorRegistry  # noqa: E402
from friday.connectors.local_files import ApprovedRoot, LocalFilesConnector  # noqa: E402
from friday.knowledge.unified import UnifiedKnowledgeManager  # noqa: E402
from friday.memory import MemoryManager, MemoryPrivacy  # noqa: E402
from friday.memory.semantic import MODEL_DIMENSION, MODEL_NAME, MODEL_PATH  # noqa: E402


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"{label}: PASS")


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _available_ram() -> int | None:
    if os.name != "nt":
        return None
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    return int(status.ullAvailPhys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None


def _working_set() -> int | None:
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong)
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    ok = psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return int(counters.WorkingSetSize) if ok else None


def _db_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as db:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def _memory_manager_regressions() -> None:
    completed = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "phase7b_smoke.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**os.environ, "FRIDAY_SEMANTIC_DISABLED": "1"},
    )
    if completed.returncode:
        print(completed.stdout[-5000:])
        print(completed.stderr[-5000:])
    check(completed.returncode == 0, "Phase 7B regression")
    for label in ("Phase 7A regression", "Phase 6B regression", "Phase 6A regression",
                  "Phase 5C regression", "privacy regression"):
        check(f"{label}: PASS" in completed.stdout, label)
    # Phase 7B returns PASS only after Phase 6A validates its Phase 5A/5B/5C gates.
    nested = completed.returncode == 0 and "Phase 7B smoke: PASS" in completed.stdout
    check(nested, "Phase 5A regression")
    check(nested, "Phase 5B regression")


def main() -> None:
    check(MODEL_PATH.is_dir() and all((MODEL_PATH / name).is_file()
          for name in ("config.json", "model.safetensors", "tokenizer.json")), "Local model assets present")
    model_bytes = sum(path.stat().st_size for path in MODEL_PATH.rglob("*")
                      if path.is_file() and ".cache" not in path.parts)
    print(f"MODEL={MODEL_NAME} DIMENSION={MODEL_DIMENSION} ASSET_BYTES={model_bytes} PATH={MODEL_PATH}")

    baseline_available = _available_ram()
    baseline_working = _working_set()
    available_samples = [value for value in (baseline_available,) if value is not None]
    print(f"RAM_BEFORE_WORKING_SET={baseline_working} AVAILABLE={baseline_available}")
    network_calls = 0

    def blocked(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("Semantic memory attempted a network call.")

    with tempfile.TemporaryDirectory(prefix="friday-phase7c1-") as temporary:
        root = Path(temporary)
        db_path = root / "memory.sqlite3"
        memory = MemoryManager(db_path, semantic_model_path=MODEL_PATH)
        check(not memory.semantic_index.model_loaded, "Lazy model unloaded at manager startup")
        check(memory.store.fts_available, "Existing FTS5 table active")
        check(memory.semantic_diagnostics()["semantic_enabled"], "Semantic packages and model available")

        with patch.object(socket.socket, "connect", blocked), \
             patch.object(socket.socket, "connect_ex", blocked), patch.object(socket, "getaddrinfo", blocked):
            cold_started = time.perf_counter()
            shopify = memory.remember(
                "Shopify OAuth callback failed because a required scope was missing.",
                namespace="friday", source="phase7c1-fixture", importance=0.7, confidence=0.9,
            )
            cold_operation = time.perf_counter() - cold_started
            cold_load = memory.semantic_index.last_model_load_seconds
            check(memory.semantic_index.model_loaded, "Model2Vec loaded on first semantic indexing")

            where = memory.remember(
                "FRIDAY stores persistent data on the D drive.", namespace="friday",
                source="phase7c1-fixture", importance=0.8, confidence=0.9,
            )
            irrelevant = memory.remember(
                "The thermostat battery uses a compact lithium cell.", namespace="friday",
                source="phase7c1-fixture", importance=0.9, confidence=0.9,
            )
            exact = memory.search("Shopify OAuth callback required scope", namespace="friday", limit=5)
            check(exact and exact[0].id == shopify.id, "Exact lexical query remains top result")
            check(memory.store.fts_available, "FTS5 remains enabled after semantic writes")

            semantic_start = time.perf_counter()
            paraphrase = memory.semantic_index.search(
                "Why did the Shopify login connection fail?", namespaces=("friday",),
                privacy=("public",), limit=5,
            )
            semantic_latency = time.perf_counter() - semantic_start
            check(any(hit.item.id == shopify.id for hit in paraphrase), "Shopify OAuth paraphrase semantic retrieval")

            hybrid_started = time.perf_counter()
            d_results = memory.search("Where should assistant data be saved?", namespace="friday", limit=10)
            hybrid_latency = time.perf_counter() - hybrid_started
            check(d_results and d_results[0].id == where.id, "D-drive paraphrase hybrid retrieval")
            check(all(result.id != irrelevant.id for result in d_results[:1]), "Unrelated memory not promoted")
            check(len(d_results) == len({result.id for result in d_results}), "Hybrid candidates deduplicated by memory ID")

            # The direct local PRIVATE path is the only path allowed to read private vectors.
            private = memory.remember(
                "PRIVATE phase fixture: the local retention preference is enabled.", namespace="friday",
                source="phase7c1-private-fixture", privacy=MemoryPrivacy.PRIVATE,
            )
            private_hits = memory.search("local retention preference", namespace="friday", privacy_level="private",
                                         route_backend="local", limit=10)
            public_hits = memory.search("local retention preference", namespace="friday", privacy_level="public", limit=10)
            check(any(item.id == private.id for item in private_hits), "PRIVATE local semantic retrieval")
            check(all(item.id != private.id for item in public_hits), "PRIVATE namespace/privacy access filter")

            # Re-indexing identical text must preserve its vector row and skip encoder work.
            original_encode = memory.semantic_index._encode_locked
            encode_calls = 0

            def count_encode(value: str):
                nonlocal encode_calls
                encode_calls += 1
                return original_encode(value)

            memory.semantic_index._encode_locked = count_encode
            before = memory.semantic_index._metadata(where.id)
            same_result = memory.semantic_index.index(where)
            after = memory.semantic_index._metadata(where.id)
            check(same_result == "unchanged" and encode_calls == 0, "Unchanged memory not re-embedded")
            check(before["content_hash"] == after["content_hash"] and before["indexed_at"] == after["indexed_at"],
                  "Unchanged vector metadata stable")

            updated = memory.update(where.id, "FRIDAY stores its persistent memory database on drive D.")
            check(encode_calls == 1 and updated.id != where.id, "Meaningful update re-embedded once")
            check(memory.semantic_index._metadata(where.id) is None, "Superseded memory vector removed")
            updated_hits = memory.semantic_index.search("Where is persistent memory stored?", namespaces=("friday",),
                                                        privacy=("public",), limit=10)
            check(any(hit.item.id == updated.id for hit in updated_hits)
                  and all(hit.item.id != where.id for hit in updated_hits), "Superseded memory excluded")

            forgotten = memory.remember("Temporary note to remove from semantic retrieval.", namespace="friday",
                                        source="phase7c1-fixture")
            memory.forget(forgotten.id)
            check(memory.semantic_index._metadata(forgotten.id) is None, "Forget removes semantic metadata")
            forgotten_hits = memory.semantic_index.search("Temporary note remove semantic retrieval",
                                                          namespaces=("friday",), privacy=("public",), limit=10,
                                                          include_inactive=True)
            check(all(hit.item.id != forgotten.id for hit in forgotten_hits), "Forgotten memory never retrieved")

            other_namespace = memory.remember("Project Orion uses a private cache namespace.", namespace="orion",
                                              source="phase7c1-fixture")
            isolated = memory.search("private cache namespace", namespace="friday", include_global=False, limit=10)
            check(all(item.id != other_namespace.id for item in isolated), "Namespace isolation")

            roman_memory = memory.remember("FRIDAY ka local data D drive par save hota hai.", namespace="friday",
                                           source="phase7c1-roman-urdu-probe")
            roman = memory.semantic_index.search("Friday apna data kahan rakhti hai?", namespaces=("friday",),
                                                  privacy=("public",), limit=10)
            roman_hit = next((hit for hit in roman if hit.item.id == roman_memory.id), None)
            print(f"Roman Urdu probe: {'PASS' if roman_hit else 'LIMITED'} "
                  f"top_similarity={roman[0].similarity:.4f}" if roman else "Roman Urdu probe: LIMITED no match")

        check(network_calls == 0, "Embedding/indexing/retrieval remote calls = 0")
        check(memory.semantic_diagnostics()["indexed_memory_count"] >= 5, "Semantic metadata diagnostics")
        with memory.semantic_index._connection(load_vec=True) as db:
            version = db.execute("SELECT vec_version()").fetchone()[0]
            vectors = db.execute("SELECT COUNT(*) FROM memory_semantic_vectors").fetchone()[0]
        check(vectors >= 5, "sqlite-vec vector table created and populated")
        print(f"SQLITE_VEC_VERSION={version} VECTOR_ROWS={vectors}")

        warm_started = time.perf_counter()
        with memory.semantic_index._lock:
            memory.semantic_index._encode_locked("warm embedding latency fixture")
        warm_embedding_latency = time.perf_counter() - warm_started
        warm_model_working = _working_set()
        after_load_available = _available_ram()
        if after_load_available is not None:
            available_samples.append(after_load_available)
        print(f"MODEL_COLD_LOAD_SECONDS={cold_load} FIRST_INDEX_OPERATION_SECONDS={cold_operation}")
        print(f"WARM_EMBEDDING_SECONDS={warm_embedding_latency} SEMANTIC_QUERY_SECONDS={semantic_latency} "
              f"HYBRID_QUERY_SECONDS={hybrid_latency}")
        print(f"RAM_AFTER_LOAD_WORKING_SET={warm_model_working} AVAILABLE={after_load_available}")

        # Separate fixture proves unavailable semantic dependencies leave FTS5 usable.
        fallback = MemoryManager(root / "fallback.sqlite3", semantic_model_path=root / "missing-model")
        fallback_item = fallback.remember("Exact fallback query retains FTS5 keyword behavior.", namespace="fallback",
                                          source="phase7c1-fixture")
        fallback_results = fallback.search("Exact fallback query FTS5", namespace="fallback", limit=5)
        check(fallback.semantic_diagnostics()["status"] == "UNAVAILABLE", "Semantic-unavailable state surfaced")
        check(fallback.store.fts_available and fallback_results and fallback_results[0].id == fallback_item.id,
              "Semantic unavailable falls back to FTS5")
        fallback.close()

        # A legacy DB is backfilled in bounded batches; interruption preserves committed rows.
        backfill_path = root / "backfill.sqlite3"
        legacy = MemoryManager(backfill_path, semantic_model_path=root / "missing-model")
        legacy_ids = [legacy.remember(f"Backfill fixture fact number {number}.", namespace="backfill",
                                      source="phase7c1-fixture").id for number in range(3)]
        before_size = backfill_path.stat().st_size
        legacy.close()
        backfill = MemoryManager(backfill_path, semantic_model_path=MODEL_PATH)
        progress_calls = 0

        def interrupt_after_one(_progress: dict[str, int]) -> None:
            nonlocal progress_calls
            progress_calls += 1
            if progress_calls == 1:
                raise KeyboardInterrupt

        try:
            backfill.backfill_semantic_index(batch_size=1, progress=interrupt_after_one)
            raise AssertionError("Backfill interruption fixture did not interrupt.")
        except KeyboardInterrupt:
            pass
        interrupted_count = _db_count(backfill_path, "memory_semantic_metadata")
        resumed = backfill.backfill_semantic_index(batch_size=1)
        repeated = backfill.backfill_semantic_index(batch_size=2)
        after_size = backfill_path.stat().st_size
        check(interrupted_count == 1 and resumed["indexed"] == 2 and resumed["pending"] == 0,
              "Bounded resumable backfill")
        check(repeated["unchanged"] == 3 and _db_count(backfill_path, "memory_semantic_metadata") == 3,
              "Repeated backfill does not duplicate or re-embed vectors")
        check(all(backfill.semantic_index._metadata(memory_id) is not None for memory_id in legacy_ids),
              "Backfill indexed existing authoritative rows")
        backfill.close()
        print(f"SEMANTIC_INDEX_APPROX_BYTES={max(0, after_size - before_size)}")

        # Unified knowledge can search a one-off approved file without persisting it.
        approved = root / "approved"
        approved.mkdir()
        (approved / "handoff.md").write_text("A one-off project handoff remains a local file.", encoding="utf-8")
        connector = LocalFilesConnector((ApprovedRoot(approved, "friday", "private"),), enabled=True)
        registry = ConnectorRegistry()
        registry.register(connector)
        unified = UnifiedKnowledgeManager(memory=memory, connectors=registry)
        before_rows = _db_count(db_path, "memory_items")
        docs = unified.search("one-off project handoff", namespace="friday", privacy_level="private",
                             route_backend="local", max_results=5)
        after_rows = _db_count(db_path, "memory_items")
        check(any(item.source_type == "local_file" for item in docs) and before_rows == after_rows,
              "One-off Phase 7B document is not persisted")

        memory.unload_semantic_model()
        after_unload = _working_set()
        check(not memory.semantic_index.model_loaded, "Semantic model unload releases model reference")
        final_available = _available_ram()
        if final_available is not None:
            available_samples.append(final_available)
        print(f"RAM_AFTER_UNLOAD_WORKING_SET={after_unload} AVAILABLE={final_available}")
        print(f"MIN_AVAILABLE_RAM_SAMPLED={min(available_samples) if available_samples else None}")
        memory.close()

    _memory_manager_regressions()
    print("PHASE 7C.1 SMOKE PASS")


if __name__ == "__main__":
    main()
