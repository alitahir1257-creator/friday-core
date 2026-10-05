"""Phase 5B acceptance checks; all files and SQLite data stay temporary."""

from __future__ import annotations

import io
import logging
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
from friday.knowledge import KnowledgeVault, KnowledgeWriteRejected, default_vault_path  # noqa: E402
from friday.lifecycle import LocalBrainState  # noqa: E402
from friday.memory import MemoryManager  # noqa: E402
from friday.memory.store import default_memory_db_path  # noqa: E402
from friday.profile import AssistantProfile  # noqa: E402


class FakeLocalProvider:
    name = "local"
    is_local = True

    def __init__(self) -> None:
        self.messages = None
        self.calls = 0
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

    def chat(self, messages):
        self.calls += 1
        self.messages = messages
        return BrainReply("mock private answer", provider="local", model="Qwen3.5-4B")

    def close(self) -> None:
        return


class FakeOmniRoute:
    name = "omniroute"
    is_local = False

    def __init__(self) -> None:
        self.messages = None
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "MOCK"

    def chat_with_model(self, messages, model):
        self.messages = messages
        self.calls += 1
        return BrainReply("mock public answer", provider="omniroute", model=model)

    def chat(self, messages):
        raise AssertionError("BrainManager must use explicit OmniRoute model routing.")


def message_text(messages) -> str:
    assert messages is not None
    return "\n".join(message["content"] for message in messages)


def run() -> dict[str, float | int]:
    tracemalloc.start()
    logger = logging.getLogger("friday.knowledge")
    log_text = io.StringIO()
    handler = logging.StreamHandler(log_text)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    old_log_path = brain_manager_module.LOG_PATH
    search_ms = 0.0
    db_size = vault_size = peak_bytes = 0
    try:
        with tempfile.TemporaryDirectory(prefix="friday-phase5b-") as temporary:
            base = Path(temporary)
            default_db = default_memory_db_path({"LOCALAPPDATA": r"Z:\must-not-be-used"})
            override_db = default_memory_db_path({"FRIDAY_MEMORY_DB_PATH": str(base / "override.sqlite3")})
            assert default_db == Path(r"D:\FRIDAY\data\memory\friday-memory.sqlite3")
            assert override_db == base / "override.sqlite3"
            print("A/B memory default and override: PASS")

            default_vault = default_vault_path({})
            override_vault = default_vault_path({"FRIDAY_VAULT_PATH": str(base / "override-vault")})
            assert default_vault == Path(r"D:\FRIDAY\vault")
            assert override_vault == base / "override-vault"
            vault_root = base / "vault"
            vault = KnowledgeVault(vault_root)
            assert not vault_root.exists()
            vault.initialize_vault()
            assert (vault_root / "FRIDAY" / "README.md").is_file()
            print("D/J vault default, override, explicit initialization: PASS")

            # Create/read/frontmatter/restart/update and wiki links.
            architecture = vault.create_note(
                "Projects/FRIDAY/Architecture.md",
                "FRIDAY architecture overview. See [[Persistent Memory]] for SQLite details.\n",
                title="FRIDAY Architecture", namespace="friday", note_type="project",
                privacy="public", tags=("friday", "architecture"),
                metadata={"owner": "human-edited", "topic": "architecture"},
            )
            vault.create_note(
                "Knowledge/Persistent Memory.md", "SQLite remains the operational source of truth.\n",
                title="Persistent Memory", namespace="friday", privacy="public", tags=("memory",),
            )
            read = vault.read_note("Projects/FRIDAY/Architecture.md", privacy_level="public", route_backend="omniroute")
            assert read.title == "FRIDAY Architecture" and read.frontmatter.note_type == "project"
            assert read.namespace == "friday" and read.privacy == "public"
            assert read.frontmatter.tags == ("friday", "architecture")
            assert read.frontmatter.extra["owner"] == "human-edited"
            print("E/I create/read and frontmatter: PASS")

            assert vault.get_outgoing_links("Projects/FRIDAY/Architecture.md") == ("Persistent Memory",)
            backlinks = vault.get_backlinks("Knowledge/Persistent Memory.md", namespace="friday")
            assert [note.title for note in backlinks] == ["FRIDAY Architecture"]
            print("J wikilinks/backlinks: PASS")

            del vault
            vault = KnowledgeVault(vault_root)
            assert "Persistent Memory" in vault.read_note("Projects/FRIDAY/Architecture.md").body
            old_owner = vault.read_note(
                "Projects/FRIDAY/Architecture.md", privacy_level="public", route_backend="local"
            ).frontmatter.extra["owner"]
            updated = vault.update_note(
                "Projects/FRIDAY/Architecture.md",
                "# FRIDAY Architecture\n\nUpdated FRIDAY architecture content.\n",
                replace_body=True,
            )
            assert "Updated FRIDAY architecture content." in updated.body
            assert updated.frontmatter.extra["owner"] == old_owner
            assert vault.update_note(
                "Projects/FRIDAY/Architecture.md", metadata={"topic": "architecture v2"}
            ).frontmatter.extra["topic"] == "architecture v2"
            print("F/G/H restart persistence and safe update: PASS")

            manual = vault_root / "Knowledge" / "Manual.md"
            manual.write_text("# Human note\n\nKeep this formatting.\n", encoding="utf-8")
            try:
                vault.update_note("Knowledge/Manual.md", "changed", replace_body=True)
                raise AssertionError("FRIDAY overwrote an unmanaged note")
            except PermissionError:
                assert manual.read_text(encoding="utf-8") == "# Human note\n\nKeep this formatting.\n"
            print("Q manual-note update protection: PASS")

            vault.create_note(
                "Projects/ATSUM/Rates.md", "ATSUM rates and sourcing notes.\n",
                title="ATSUM Rates", namespace="atsum", privacy="public", tags=("rates",),
            )
            vault.create_note(
                "Knowledge/Random Note.md", "A random garden note.\n",
                title="Random Note", namespace="global", privacy="public",
            )
            ranked = vault.search_notes("FRIDAY architecture", namespace="friday", privacy_level="public")
            assert ranked and ranked[0].note.title == "FRIDAY Architecture"
            assert all(note.note.title != "ATSUM Rates" for note in ranked)
            print("H/O search ranking and namespace isolation: PASS")

            for name, privacy in (("Public", "public"), ("Private", "private"), ("Local", "local_only")):
                vault.create_note(
                    f"Projects/FRIDAY/Privacy {name}.md", f"Privacy context marker {name}.\n",
                    title=f"Privacy Context {name}", namespace="friday", privacy=privacy,
                )
            cloud_privacy = vault.search_notes(
                "privacy context marker", namespace="friday", privacy_level="public", route_backend="omniroute"
            )
            local_privacy = vault.search_notes(
                "privacy context marker", namespace="friday", privacy_level="private", route_backend="local"
            )
            assert {item.note.privacy for item in cloud_privacy} == {"public"}
            assert {item.note.privacy for item in local_privacy} == {"public", "private", "local_only"}
            print("M privacy filtering: PASS")

            synthetic_secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"
            try:
                vault.create_note("Knowledge/Rejected.md", f"Synthetic key {synthetic_secret}")
                raise AssertionError("vault accepted a likely secret")
            except KnowledgeWriteRejected as error:
                assert "possible_secret" in str(error)
            assert synthetic_secret not in log_text.getvalue()
            assert "knowledge_write_rejected reason=possible_secret" in log_text.getvalue()
            assert all(synthetic_secret.encode() not in path.read_bytes() for path in vault_root.rglob("*.md"))
            print("N/AG secret write rejection and content-free logging: PASS")

            for unsafe in ("../../secret", base / "outside.md", r"C:\Users\someone\secret.md"):
                try:
                    vault.create_note(unsafe, "must not escape")
                    raise AssertionError(f"accepted unsafe path: {unsafe}")
                except ValueError:
                    pass
            assert not (base / "outside.md").exists()
            print("K/L path traversal and absolute path protection: PASS")

            for index in range(12):
                vault.create_note(
                    f"Projects/FRIDAY/Budget {index}.md", f"Budget marker record {index} is short.\n",
                    title=f"Budget Marker {index}", namespace="friday", privacy="public",
                )
            context = vault.build_context(
                "budget marker", namespace="friday", privacy_level="public",
                route_backend="omniroute", max_notes=2, max_chars=180,
            )
            assert context.result_count <= 2 and context.character_count <= 180
            assert context.character_count == len(context.text)
            print("P context count/character budget: PASS")

            temp_sqlite = base / "memory.sqlite3"
            memory = MemoryManager(temp_sqlite)
            promoted_source = memory.remember(
                "Explicit promotion keeps the SQLite memory intact.", namespace="friday", privacy="private"
            )
            before_promotion = memory.get(promoted_source.id)
            promoted = vault.memory_to_note(memory, promoted_source.id)
            after_promotion = memory.get(promoted_source.id)
            duplicate_promotion = vault.memory_to_note(memory, promoted_source.id)
            assert promoted.path == duplicate_promotion.path
            assert before_promotion == after_promotion
            first_summary = memory.remember("First summary source memory.", namespace="friday", privacy="public")
            second_summary = memory.remember("Second summary source memory.", namespace="friday", privacy="private")
            summary = vault.create_note_from_memories(memory, [first_summary.id, second_summary.id])
            summary_again = vault.create_note_from_memories(memory, [first_summary.id, second_summary.id])
            assert summary.path == summary_again.path and summary.privacy == "private"
            assert memory.get(first_summary.id) == first_summary and memory.get(second_summary.id) == second_summary
            print("R explicit memory-to-note promotion and deduplication: PASS")

            for index in range(8):
                vault.create_note(
                    f"Knowledge/Search {index}.md", f"Search latency marker {index}.\n",
                    title=f"Search Marker {index}", namespace="global", privacy="public",
                )
            started = time.perf_counter()
            vault.search_notes("search latency marker", namespace="friday", privacy_level="public", limit=5)
            search_ms = round((time.perf_counter() - started) * 1000, 2)

            # BrainManager receives only mocked providers; no HTTP call is made.
            brain_manager_module.LOG_PATH = base / "brain.log"
            public_memory = memory.remember("Brain context marker from public memory.", namespace="friday", privacy="public")
            private_memory = memory.remember("Brain context marker from private memory.", namespace="friday", privacy="private")
            local_memory = memory.remember("Brain context marker from local memory.", namespace="friday", privacy="local_only")
            for name, privacy in (("Public", "public"), ("Private", "private"), ("Local", "local_only")):
                vault.create_note(
                    f"Projects/FRIDAY/Brain {name}.md", f"Brain context marker from {name.lower()} knowledge.\n",
                    title=f"Brain Context {name}", namespace="friday", privacy=privacy,
                )
            remote = FakeOmniRoute()
            local = FakeLocalProvider()
            brain = BrainManager(
                AssistantProfile(), local_provider=local, omniroute_provider=remote,
                memory_manager=memory, knowledge_vault=vault,
            )
            brain.respond(
                "Rewrite clearly: brain context marker.", [],
                request_metadata=BrainRequestMetadata(task_class="FAST", memory_namespace="friday"),
            )
            remote_prompt = message_text(remote.messages)
            assert public_memory.content in remote_prompt
            assert "Brain context marker from public knowledge." in remote_prompt
            assert private_memory.content not in remote_prompt and local_memory.content not in remote_prompt
            assert "Brain context marker from private knowledge." not in remote_prompt
            assert "Brain context marker from local knowledge." not in remote_prompt
            print("S BrainManager public memory/knowledge integration: PASS")

            remote_calls = remote.calls
            brain.respond(
                "Summarize my private brain context marker.", [],
                request_metadata=BrainRequestMetadata(privacy="private", task_class="PRIVATE", memory_namespace="friday"),
            )
            local_prompt = message_text(local.messages)
            assert private_memory.content in local_prompt and local_memory.content in local_prompt
            assert "Brain context marker from private knowledge." in local_prompt
            assert "Brain context marker from local knowledge." in local_prompt
            assert remote.calls == remote_calls
            print("T BrainManager PRIVATE local-only integration: PASS")
            brain.close()

            invalid_root = base / "not-a-directory"
            invalid_root.write_text("file occupies vault root", encoding="utf-8")

            class BrokenVault:
                def build_context(self, *_args, **_kwargs):
                    raise OSError("temporary unavailable vault")

            fallback_remote = FakeOmniRoute()
            fallback = BrainManager(
                AssistantProfile(), local_provider=FakeLocalProvider(),
                omniroute_provider=fallback_remote, memory_manager=memory,
                knowledge_vault=KnowledgeVault(invalid_root),
            )
            fallback.respond(
                "Rewrite clearly: brain context marker.", [],
                request_metadata=BrainRequestMetadata(task_class="FAST", memory_namespace="friday"),
            )
            assert fallback_remote.calls == 1
            assert public_memory.content in message_text(fallback_remote.messages)
            fallback.close()
            print("Q vault unavailable graceful memory-only fallback: PASS")

            obsidian = vault_root / ".obsidian"
            obsidian.mkdir(exist_ok=True)
            (obsidian / "app.json").write_text('{"app":"not knowledge"}', encoding="utf-8")
            (obsidian / "Injected.md").write_text(
                "---\ntitle: Injected\nprivacy: public\n---\nobsidian injected sentinel\n", encoding="utf-8"
            )
            listed = vault.list_notes(privacy_level="local_only", route_backend="local")
            assert all(".obsidian" not in note.path.casefold() for note in listed)
            assert not vault.search_notes("injected sentinel", namespace=None, privacy_level="public")
            try:
                vault.read_note(".obsidian/Injected.md", privacy_level="public")
                raise AssertionError(".obsidian content was exposed")
            except ValueError:
                pass
            print("U .obsidian exclusion: PASS")

            archive_source = vault.create_note(
                "Inbox/Archive me.md", "Temporary inbox item.\n", namespace="friday", privacy="private"
            )
            archived = vault.archive_note(archive_source.path)
            assert archived.path == "Archive/Archive me.md"
            print("Vault archive operation: PASS")

            vault_size = sum(path.stat().st_size for path in vault_root.rglob("*") if path.is_file())
            _, peak_bytes = tracemalloc.get_traced_memory()
            db_size = temp_sqlite.stat().st_size
            print(f"Resource check: search_ms={search_ms} vault_bytes={vault_size} sqlite_bytes={db_size} traced_peak_bytes={peak_bytes}")
            memory.close()
    finally:
        brain_manager_module.LOG_PATH = old_log_path
        logger.removeHandler(handler)
        tracemalloc.stop()
    print("External/network calls: 0 (all BrainManager providers mocked)")
    return {"search_ms": search_ms, "vault_bytes": vault_size, "sqlite_bytes": db_size, "traced_peak_bytes": peak_bytes}


if __name__ == "__main__":
    run()
