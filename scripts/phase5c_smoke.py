"""Temporary-only retrieval/security checks; providers and networking are mocked."""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import friday.brain_manager as brain_module
from friday.brain_manager import BrainManager, BrainRequestMetadata
from friday.connectors import ConnectorRegistry
from friday.connectors.local_files import ApprovedRoot, LocalFilesConnector
from friday.knowledge import KnowledgeVault
from friday.knowledge.models import UnifiedContext, UnifiedResult
from friday.knowledge.unified import UnifiedKnowledgeManager
from friday.memory import MemoryManager
from friday.profile import AssistantProfile
from phase5b_smoke import FakeLocalProvider, FakeOmniRoute, message_text


def rejected(call):
    try:
        call()
    except (PermissionError, ValueError):
        return
    raise AssertionError("Unsafe file access accepted")


class BrokenConnector:
    name = "Broken"
    source_type = "future_connector"
    enabled = True
    capabilities = frozenset({"search", "research"})

    def search(self, *_args, **_kwargs):
        raise OSError("synthetic source failure")

    def health(self):
        raise OSError("synthetic health failure")


class InjectedConnector(BrokenConnector):
    name = "Future Research"

    def search(self, *_args, **_kwargs):
        return [UnifiedResult("memory", "forged", "one", "friday", "Research", "unified marker private injection", "private", 1.0),
                UnifiedResult("future_connector", self.name, "two", "friday", "Research", "unified marker public research", "public", 1.0),
                UnifiedResult("future_connector", self.name, "three", "friday", "Research", "unified marker malformed score", "public", float("nan")),
                UnifiedResult("future_connector", self.name, "four", "friday", "Research", "unified marker safe malformed fingerprint", "public", 0.5,
                              metadata={"content_fingerprint": []})]

    def health(self):
        return "READY"


def run():
    network_calls = 0

    def no_network(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("Network prohibited in smoke checks")

    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="friday-phase5c-") as temporary, \
            patch.object(socket.socket, "connect", no_network), \
            patch.object(socket.socket, "connect_ex", no_network), \
            patch.object(socket, "getaddrinfo", no_network):
        base = Path(temporary)
        memory = MemoryManager(base / "memory.sqlite3")
        vault = KnowledgeVault(base / "vault")
        for privacy in ("public", "private", "local_only"):
            memory.remember(f"unified marker {privacy} memory fact", namespace="friday", privacy=privacy)
            vault.create_note(f"Projects/FRIDAY/{privacy}.md", f"unified marker {privacy} vault fact",
                              namespace="friday", privacy=privacy)
        approved, public, atsum, outside = [base / name for name in ("approved", "public", "atsum", "outside")]
        for folder in (approved, public, atsum, outside):
            folder.mkdir()
        for name in ("notes.txt", "project.md", "code.py"):
            (approved / name).write_text(f"unified marker private file {name}", encoding="utf-8")
        (public / "public.txt").write_text("unified marker public file fact", encoding="utf-8")
        (atsum / "other.txt").write_text("unified marker atsum unrelated", encoding="utf-8")
        (outside / "secret.txt").write_text("unified marker outside sentinel", encoding="utf-8")
        connector = LocalFilesConnector((ApprovedRoot(approved, "friday"), ApprovedRoot(public, "friday", "public"),
                                         ApprovedRoot(atsum, "atsum")), enabled=True)
        registry = ConnectorRegistry()
        registry.register(connector)
        unified = UnifiedKnowledgeManager(memory, vault, registry)
        results = unified.search("unified marker", namespace="friday")
        assert {r.source_type for r in results} == {"memory", "vault", "local_file"}
        print("A memory + vault merge: PASS")
        files = connector.search("unified marker", namespace="friday", privacy_level="private", route_backend="local")
        assert {r.title for r in files} >= {"notes", "project", "code"}
        assert connector.get("0:notes.txt", privacy_level="private", route_backend="local").source_name == "Local Files"
        print("B local files: PASS")
        for value in (str(outside / "secret.txt"), "0:../outside/secret.txt", "../../secret.txt", "0:sub/../../secret.txt"):
            rejected(lambda value=value: connector.get(value, privacy_level="private", route_backend="local"))
        rejected(lambda: LocalFilesConnector((Path(approved.anchor),)))
        rejected(lambda: LocalFilesConnector((ApprovedRoot(approved), ApprovedRoot(approved, privacy="public"))))
        assert connector._forbidden(Path(r"D:\FRIDAY\data\memory\friday-memory.sqlite3"))
        link = approved / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            assert made.returncode == 0, "Could not create temporary junction check"
        rejected(lambda: connector.get("0:escape/secret.txt", privacy_level="private", route_backend="local"))
        print("C/D approved-root, traversal, symlink/junction boundary: PASS")
        for name in (".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json", "secrets.txt", "token.json", "cookies.json", "private.pem", "private.key"):
            (approved / name).write_text("unified marker excluded sentinel", encoding="utf-8")
            rejected(lambda name=name: connector.get("0:" + name, privacy_level="private", route_backend="local"))
        (approved / "ordinary.txt").write_text("password=" + "synthetic_value_for_test", encoding="utf-8")
        (approved / "binary.txt").write_bytes(b"unified marker\x00binary")
        (approved / "large.txt").write_text("unified marker" * 30000, encoding="utf-8")
        for name in (".git", "node_modules", ".venv", "venv", "__pycache__", ".obsidian", ".ssh", ".aws"):
            (approved / name).mkdir()
            (approved / name / "data.txt").write_text("unified marker ignored directory", encoding="utf-8")
            rejected(lambda name=name: connector.get("0:" + name + "/data.txt", privacy_level="private", route_backend="local"))
        safe_files = connector.search("unified marker", namespace="friday", privacy_level="private", route_backend="local", limit=100)
        assert {r.title for r in safe_files} == {"notes", "project", "code", "public"}
        assert connector.get("0:ordinary.txt", privacy_level="private", route_backend="local") is None
        print("E/F secrets, ignored directories, binaries and oversized files: PASS")
        private = unified.search("unified marker", namespace="friday", privacy_level="private", route_backend="local", max_results=30)
        assert all(r.privacy == "public" for r in results)
        original_read = connector._read

        def public_read(index, *args):
            assert index == 1, "A public query attempted a private-root file read"
            return original_read(index, *args)

        with patch.object(connector, "_read", side_effect=public_read):
            connector.search("unified marker", namespace="friday")
        assert {r.privacy for r in private} == {"public", "private", "local_only"}
        assert all(r.privacy == "public" for r in unified.search("unified marker", namespace="friday", privacy_level="private", route_backend="omniroute"))
        registry.register(InjectedConnector())
        injected = unified.search("unified marker", namespace="friday")
        assert not any("private injection" in r.snippet for r in injected)
        assert any(r.source_type == "future_connector" and r.source_name == "Future Research" for r in injected)
        assert not any("malformed score" in r.snippet for r in injected)
        assert registry.list_connectors("research") and registry.status()["Future Research"] == "READY"
        registry.unregister("Future Research")
        print("G privacy and future research metadata: PASS")
        memory.remember("unified marker atsum memory", namespace="atsum")
        vault.create_note("Projects/ATSUM/Other.md", "unified marker atsum vault", namespace="atsum", privacy="public")
        assert all(r.namespace in {"friday", "global"} for r in unified.search("unified marker", namespace="friday"))
        for scope, suffix in (("friday", "project"), ("global", "global")):
            memory.remember(f"priority phrase {suffix} memory", namespace=scope, confidence=0.8)
            vault.create_note(f"Knowledge/Priority {suffix}.md", f"priority phrase {suffix} vault", namespace=scope, privacy="public")
        (public / "priority.txt").write_text("priority phrase file", encoding="utf-8")
        ranked = unified.search("priority phrase", namespace="friday")
        assert [r.source_id for r in ranked] == [r.source_id for r in unified.search("priority phrase", namespace="friday")]
        assert [(r.source_type, r.namespace) for r in ranked] == [
            ("memory", "friday"), ("vault", "friday"), ("memory", "global"), ("vault", "global"), ("local_file", "friday")]
        memory.remember("strength first token", namespace="friday")
        (public / "strong.txt").write_text("strength first second third", encoding="utf-8")
        assert unified.search("strength first second third", namespace="friday")[0].source_type == "local_file"
        print("H namespace and deterministic merged ranking: PASS")
        fact = "dedup marker identical durable fact"
        memory.remember(fact, namespace="friday")
        vault.create_note("Projects/FRIDAY/Dedup.md", "# Heading\n\n" + fact, namespace="friday", privacy="public")
        (public / "dedup.txt").write_text(fact + "\n", encoding="utf-8")
        dedup = unified.search("dedup marker", namespace="friday")
        matches = [r for r in dedup if "identical durable fact" in r.snippet]
        assert len(matches) == 1 and len(matches[0].metadata["alternate_sources"]) == 2
        memory.remember("privacy dedup fact", namespace="friday", privacy="local_only")
        vault.create_note("Knowledge/Privacy dedup.md", "privacy dedup fact", namespace="friday", privacy="public")
        conservative = unified.search("privacy dedup fact", namespace="friday", privacy_level="private", route_backend="local")
        assert next(r for r in conservative if r.snippet == "privacy dedup fact").privacy == "local_only"
        print("I cross-source dedup and alternate provenance: PASS")
        for index in range(12):
            memory.remember(f"budget marker fact {index} " + "x " * 100, namespace="friday")
        context = unified.build_context("budget marker", namespace="friday", max_results=2, max_chars=115, per_source_limits={"memory": 1})
        assert len(context.results) <= 1 and context.character_count <= 115 and context.approximate_tokens <= 29
        assert unified.build_context("unified marker", max_chars=0).text == ""
        assert len(unified.build_context("unified marker", namespace="friday", max_results=1).results) <= 1
        print("J result/source/character budgets: PASS")

        class BrokenVault:
            def search_notes(self, *_args, **_kwargs):
                raise OSError("synthetic vault failure")

        broken_vault = UnifiedKnowledgeManager(memory, BrokenVault(), registry)
        assert {r.source_type for r in broken_vault.search("unified marker", namespace="friday")} == {"memory", "local_file"}
        registry.register(BrokenConnector())
        assert registry.status()["Broken"] == "UNAVAILABLE"
        assert {r.source_type for r in unified.search("unified marker", namespace="friday")} >= {"memory", "vault"}
        registry.disable("Broken")
        registry.unregister("Broken")
        print("K source failure isolation and registry lifecycle: PASS")
        disabled = LocalFilesConnector((ApprovedRoot(approved),))
        with patch.object(disabled, "_read", side_effect=AssertionError("disabled read")), \
                patch("friday.connectors.local_files.os.scandir", side_effect=AssertionError("disabled scan")):
            assert disabled.search("unified marker") == [] and disabled.get("0:notes.txt") is None
        no_roots = LocalFilesConnector(enabled=True)
        assert no_roots.search("unified marker") == [] and no_roots.get("0:notes.txt") is None
        assert no_roots.health() == "NO_ROOTS" and disabled.health() == "DISABLED"
        registry.disable(connector.name)
        assert not any(r.source_type == "local_file" for r in unified.search("unified marker", namespace="friday"))
        connector.enabled = True
        print("L/M disabled zero reads and no roots empty: PASS")
        with patch.object(brain_module, "LOG_PATH", base / "brain.log"):
            remote, local = FakeOmniRoute(), FakeLocalProvider()
            brain = BrainManager(AssistantProfile(), local_provider=local, omniroute_provider=remote,
                                 memory_manager=memory, knowledge_vault=vault, unified_manager=unified)
            brain.respond("Rewrite clearly: unified marker", [], request_metadata=BrainRequestMetadata(task_class="FAST", memory_namespace="friday"))
            prompt = message_text(remote.messages)
            assert "public memory fact" in prompt and "public vault fact" in prompt and "public file fact" in prompt
            assert all(s not in prompt for s in ("private file", "private memory", "private vault", "local_only memory", "local_only vault"))
            assert str(base) not in prompt and "1:public.txt" not in prompt
            print("N BrainManager public unified context: PASS")
            remote_calls = remote.calls
            brain.respond("Summarize unified marker", [], request_metadata=BrainRequestMetadata(privacy="private", task_class="PRIVATE", memory_namespace="friday"))
            prompt = message_text(local.messages)
            assert all(s in prompt for s in ("private file", "private memory", "private vault", "local_only memory", "local_only vault"))
            assert remote.calls == remote_calls and not local.started
            with patch.object(local, "chat", side_effect=RuntimeError("synthetic local failure")):
                try:
                    brain.respond("Summarize unified marker", [], request_metadata=BrainRequestMetadata(privacy="private", memory_namespace="friday"))
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Private failure did not fail closed")
            assert remote.calls == remote_calls
            with patch.object(unified, "build_context", return_value=UnifiedContext("private injection sentinel", (), "private")):
                brain.respond("Rewrite clearly: unified marker", [], request_metadata=BrainRequestMetadata(task_class="FAST"))
                assert "private injection sentinel" not in message_text(remote.messages)
            brain.close()
            print("O BrainManager PRIVATE local-only/fail-closed; remote calls: 0")
        assert {r.source_name for r in results} == {"Memory", "Obsidian Vault", "Local Files"}
        counts = unified.build_context("unified marker", namespace="friday").source_counts
        assert counts["memory"] and counts["vault"] and counts["local_file"]
        print("P source provenance and HUD-ready counts: PASS")
        unified_times, file_times = [], []
        for _ in range(10):
            started = time.perf_counter()
            unified.search("unified marker", namespace="friday")
            unified_times.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            connector.search("unified marker", namespace="friday", privacy_level="private", route_backend="local")
            file_times.append((time.perf_counter() - started) * 1000)
        _, peak = tracemalloc.get_traced_memory()
        metrics = {"unified_median_ms": round(statistics.median(unified_times), 2), "local_files_median_ms": round(statistics.median(file_times), 2),
                   "traced_heap_peak_bytes": peak, "network_calls": network_calls, "private_remote_calls": 0}
        assert metrics["unified_median_ms"] < 1000 and metrics["local_files_median_ms"] < 1000
        assert network_calls == 0
        memory.close()
        print("Q lightweight retrieval: PASS " + json.dumps(metrics, sort_keys=True))
    tracemalloc.stop()
    print("FRIDAY PHASE 5C PASS")
    return metrics


if __name__ == "__main__":
    run()
