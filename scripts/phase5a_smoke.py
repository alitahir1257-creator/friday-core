"""Focused Phase 5A acceptance checks using temporary SQLite and mocked providers."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import friday.brain_manager as brain_manager_module  # noqa: E402
from friday.brain_manager import BrainManager, BrainReply, BrainRequestMetadata  # noqa: E402
from friday.lifecycle import LocalBrainState  # noqa: E402
from friday.knowledge import KnowledgeVault  # noqa: E402
from friday.memory import (  # noqa: E402
    MemoryKind,
    MemoryManager,
    MemoryPrivacy,
    MemoryStatus,
    MemoryWriteRejected,
)
from friday.memory.store import default_memory_db_path  # noqa: E402
from friday.profile import AssistantProfile  # noqa: E402


class FakeLocalProvider:
    name = "local"
    is_local = True

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None
        self.started = False
        self.brain = SimpleNamespace(
            model="Qwen3.5-4B",
            messages=lambda command, history: [
                {"role": "system", "content": "FRIDAY test system"},
                *history,
                {"role": "user", "content": command},
            ],
        )
        self.lifecycle = SimpleNamespace(health=lambda: False, state=LocalBrainState.OFF)

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "OFF"

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        self.messages = messages
        return BrainReply("mock private answer", provider="local", model="Qwen3.5-4B")

    def close(self) -> None:
        return


class FakeOmniRoute:
    name = "omniroute"
    is_local = False

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "MOCK"

    def chat_with_model(self, messages: list[dict[str, str]], model: str) -> BrainReply:
        self.messages = messages
        self.calls += 1
        return BrainReply("mock public answer", provider="omniroute", model=model)

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        raise AssertionError("BrainManager must use explicit OmniRoute model routing.")


def message_text(messages: list[dict[str, str]] | None) -> str:
    assert messages is not None
    return "\n".join(message["content"] for message in messages)


def run() -> dict[str, object]:
    tracemalloc.start()
    retrieval_ms = 0.0
    test_log = io.StringIO()
    logger = logging.getLogger("friday.memory")
    handler = logging.StreamHandler(test_log)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    with tempfile.TemporaryDirectory(prefix="friday-phase5a-") as temporary:
        base = Path(temporary)
        db_path = base / "smoke.sqlite3"
        manager = MemoryManager(db_path)

        # A / J: the first manager writes, closes, and a fresh manager reads it back.
        persistent = manager.remember(
            "FRIDAY uses OmniRoute.", kind=MemoryKind.FACT, namespace="friday", source="user"
        )
        manager.close()
        manager = MemoryManager(db_path)
        assert manager.get(persistent.id).content == "FRIDAY uses OmniRoute."
        print("A persistence: PASS")
        print("J restart durability: PASS")

        # B: local namespace ranks first and global is included; atsum never leaks.
        manager.remember("Global memory marker is available.", namespace="global")
        friday_specific = manager.remember("Friday memory marker is scoped.", namespace="friday")
        manager.remember("Atsum memory marker belongs elsewhere.", namespace="atsum")
        results = manager.search("memory marker", namespace="friday")
        assert {row.namespace for row in results} == {"friday", "global"}
        assert results[0].id == friday_specific.id
        print("B namespace isolation: PASS")

        # C: whitespace/case variants share one active normalized item.
        before = manager.store.count_items()
        first = manager.remember("The app uses SQLite.", kind="FACT", namespace="friday")
        second = manager.remember("  THE   APP uses sqlite.  ", kind="FACT", namespace="friday")
        assert first.id == second.id
        assert manager.store.count_items() == before + 1
        same_text_update = manager.update(first.id, first.content, importance=0.9, metadata={"reviewed": True})
        assert same_text_update.id == first.id and same_text_update.importance == 0.9
        assert same_text_update.metadata["reviewed"] is True
        print("C duplicate protection: PASS")

        accepted, _, reason = manager.consider("Hello!", namespace="friday")
        assert not accepted and reason == "transient_smalltalk"
        accepted, _, reason = manager.consider(
            "FRIDAY project branch is friday-dev.", kind="PROJECT", namespace="friday"
        )
        assert accepted and reason == "accepted"
        print("Write policy: PASS")

        # D: updating creates the replacement and retains the prior record as superseded.
        old = manager.remember("Preferred model is model-A.", kind="PREFERENCE", namespace="friday")
        new = manager.update(old.id, "Preferred model is model-B.")
        assert manager.get(old.id, include_inactive=True).status == MemoryStatus.SUPERSEDED
        assert manager.get(new.id).status == MemoryStatus.ACTIVE
        print("D update / supersede: PASS")

        # E: forget is a lifecycle transition and default retrieval omits it.
        forgotten = manager.remember("Forgettable xylophone marker.", namespace="friday")
        manager.forget(forgotten.id)
        assert all(row.id != forgotten.id for row in manager.search("xylophone marker", namespace="friday"))
        print("E forget: PASS")

        # F: explicit links are durable, queryable edges.
        node_a = manager.remember("Component Alpha exists.", namespace="friday")
        node_b = manager.remember("Component Beta exists.", namespace="friday")
        edge = manager.link(node_a.id, "USES", node_b.id)
        assert edge in manager.get_links(node_a.id)
        assert edge.relation == "USES"
        print("F relationship links: PASS")

        # G: synthetic credentials are refused and absent from both DB and logs.
        synthetic_secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"
        try:
            manager.remember(f"Test credential {synthetic_secret}", namespace="friday")
            raise AssertionError("secret memory was accepted")
        except MemoryWriteRejected as error:
            assert error.reason == "possible_secret"
        db = sqlite3.connect(db_path)
        try:
            raw_contents = [row[0] for row in db.execute("SELECT content FROM memory_items")]
        finally:
            db.close()
        assert all(synthetic_secret not in content for content in raw_contents)
        assert synthetic_secret.encode("utf-8") not in db_path.read_bytes()
        assert synthetic_secret not in test_log.getvalue()
        assert "memory_write_rejected reason=possible_secret" in test_log.getvalue()
        try:
            manager.remember("Harmless text", namespace="friday", metadata={"api_key": "synthetic-value"})
            raise AssertionError("secret metadata was accepted")
        except MemoryWriteRejected:
            pass
        try:
            manager.remember("token=synthetic-test-token-12345", namespace="friday")
            raise AssertionError("token field was accepted")
        except MemoryWriteRejected:
            pass
        assert "synthetic-value" not in test_log.getvalue()
        assert "synthetic-test-token-12345" not in test_log.getvalue()
        print("G secret rejection: PASS")

        # H: cloud/public retrieval is public-only; private tiers require a local private route.
        manager.remember("Public privacy marker is okay.", namespace="friday", privacy="public")
        private = manager.remember("Private privacy marker stays on device.", namespace="friday", privacy="private")
        local_only = manager.remember("Local privacy marker stays on device.", namespace="friday", privacy="local_only")
        public_results = manager.search("privacy marker", namespace="friday", privacy_level="public", route_backend="omniroute")
        private_results = manager.search("privacy marker", namespace="friday", privacy_level="private", route_backend="omniroute")
        local_results = manager.search("privacy marker", namespace="friday", privacy_level="private", route_backend="local")
        public_ids = {row.id for row in public_results}
        assert private.id not in public_ids and local_only.id not in public_ids
        assert private.id not in {row.id for row in private_results}
        assert {private.id, local_only.id} <= {row.id for row in local_results}
        print("H privacy retrieval boundary: PASS")

        # I: expiry is excluded even when matching terms remain in FTS.
        expired = manager.remember("Expired meteor marker.", namespace="friday", expires_at="2000-01-01T00:00:00Z")
        assert all(row.id != expired.id for row in manager.search("meteor marker", namespace="friday"))
        assert manager.get(expired.id) is None
        print("I expiration: PASS")

        # K: FTS ranks a direct term match above unrelated candidate memories.
        best = manager.remember("Architecture retrieval needle belongs to Friday.", namespace="friday", importance=0.3)
        manager.remember("The cooking recipe contains basil and tomatoes.", namespace="friday", importance=1.0)
        t0 = time.perf_counter()
        ranked = manager.search("architecture retrieval needle", namespace="friday")
        retrieval_ms = round((time.perf_counter() - t0) * 1000, 2)
        assert ranked and ranked[0].id == best.id
        assert manager.store.fts_available
        fts_state = manager.store.fts_available
        manager.store.fts_available = False
        fallback_ranked = manager.search("architecture retrieval needle", namespace="friday")
        manager.store.fts_available = fts_state
        assert fallback_ranked and fallback_ranked[0].id == best.id
        print("K FTS/search retrieval and text fallback: PASS")

        # L: the context builder applies both count and approximate character budgets.
        for index in range(12):
            manager.remember(f"Budget marker memory {index} carries a short durable fact.", namespace="friday")
        context = manager.build_context("budget marker", namespace="friday", max_memories=3, max_chars=150)
        assert context.result_count <= 3 and context.character_count <= 150
        assert context.character_count == len(context.text)
        assert len(context.item_ids) == context.result_count
        print("L context budget: PASS")

        # M: real BrainManager and policy, with in-process provider doubles only.
        public_memory = manager.remember("Friday architecture marker is public context.", namespace="friday", privacy="public")
        private_memory = manager.remember("Friday architecture marker is private context.", namespace="friday", privacy="private")
        local_memory = manager.remember("Friday architecture marker is local-only context.", namespace="friday", privacy="local_only")
        omn = FakeOmniRoute()
        local = FakeLocalProvider()
        old_log_path = brain_manager_module.LOG_PATH
        brain_manager_module.LOG_PATH = base / "brain.log"
        brain = BrainManager(
            AssistantProfile(), local_provider=local, omniroute_provider=omn,
            memory_manager=manager, knowledge_vault=KnowledgeVault(base / "vault"),
        )
        brain.respond(
            "Rewrite clearly: the app is very fast; architecture marker.", [],
            request_metadata=BrainRequestMetadata(task_class="FAST", memory_namespace="friday"),
        )
        public_prompt = message_text(omn.messages)
        assert public_memory.content in public_prompt
        assert private_memory.content not in public_prompt and local_memory.content not in public_prompt
        print("T BrainManager public memory integration: PASS")
        remote_calls_before_private = omn.calls
        brain.respond(
            "Summarize my private architecture marker.", [],
            request_metadata=BrainRequestMetadata(privacy="private", task_class="PRIVATE", memory_namespace="friday"),
        )
        local_prompt = message_text(local.messages)
        assert private_memory.content in local_prompt and local_memory.content in local_prompt
        assert omn.calls == remote_calls_before_private
        assert not local.started
        print("U BrainManager PRIVATE memory integration: PASS")
        brain.close()
        class BrokenMemory:
            def build_context(self, *_args: object, **_kwargs: object):
                raise OSError("synthetic memory unavailable")

            def close(self) -> None:
                return

        fallback_omn = FakeOmniRoute()
        fallback_brain = BrainManager(
            AssistantProfile(), local_provider=FakeLocalProvider(),
            omniroute_provider=fallback_omn, memory_manager=BrokenMemory(),
            knowledge_vault=KnowledgeVault(base / "vault"),
        )
        fallback_reply = fallback_brain.respond(
            "Rewrite clearly: memory retrieval should fail safely.", [],
            request_metadata=BrainRequestMetadata(task_class="FAST"),
        )
        assert fallback_reply.text == "mock public answer" and fallback_omn.calls == 1
        fallback_brain.close()
        print("Memory retrieval failure handling: PASS")
        brain_manager_module.LOG_PATH = old_log_path
        default_path = default_memory_db_path({"LOCALAPPDATA": r"Z:\FridayTest"})
        override_path = default_memory_db_path({"FRIDAY_MEMORY_DB_PATH": r"Z:\custom\memory.sqlite3"})
        assert default_path == Path(r"D:\FRIDAY\data\memory\friday-memory.sqlite3")
        assert str(override_path).endswith(r"Z:\custom\memory.sqlite3")
        print("Default DB path: %s" % default_path.as_posix())

        db = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        assert {"memory_items", "memory_links", "memory_fts"} <= tables
        db_size = db_path.stat().st_size
        _, peak_bytes = tracemalloc.get_traced_memory()
        print("Resource check: DB=%d bytes, retrieval=%s ms, traced_peak=%d bytes, FTS5=%s" % (
            db_size, retrieval_ms, peak_bytes, manager.store.fts_available
        ))
        manager.close()
    logger.removeHandler(handler)
    tracemalloc.stop()
    return {
        "db_size_bytes": db_size,
        "retrieval_ms": retrieval_ms,
        "traced_peak_bytes": peak_bytes,
    }


if __name__ == "__main__":
    run()
