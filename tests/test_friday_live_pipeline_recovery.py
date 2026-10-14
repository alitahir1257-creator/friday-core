import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from friday.capabilities.computer.apps import AppResolver, InstalledApp
from friday.capabilities.computer.controller import ComputerController
from friday.capabilities.computer.parser import (
    CanonicalComputerIntentParser,
    DeterministicComputerPlanner,
)
from friday.brain_manager import BrainReply
from friday.core import FridayCore
from friday.profile import AssistantProfile
from friday.router import BuiltinRouter, Route, RoutingDecision
from friday.tools.adapters import register_computer_tools
from friday.tools.registry import ToolRegistry


def _computer_registry():
    registry = ToolRegistry()
    controller = ComputerController()
    register_computer_tools(registry, lambda: controller)
    return registry


def _disable_automation_store(monkeypatch):
    monkeypatch.setattr(
        "friday.core.AutomationManager.from_core",
        lambda _core: SimpleNamespace(close=lambda: None),
    )


def test_computer_input_normalization_keeps_voice_correction_bounded():
    assert FridayCore._normalize_computer_input(
        "C drive mai kitni space bachi hai? guess mat krna"
    ) == "C drive mai kitni space bachi hai?"
    assert FridayCore._normalize_computer_input("calculator kholo") == "calculator kholo"


def test_computer_normalizer_handles_the_observed_roman_urdu_whisper_variant():
    transcript = "Friday, Calculator. Jholo."

    query = FridayCore._normalize_computer_input(transcript)
    assert query == "Calculator kholo."
    plan = DeterministicComputerPlanner().plan(query)
    assert plan is not None
    assert [(action.operation, action.target) for action in plan.actions] == [("OPEN_APP", "Calculator")]


@pytest.mark.parametrize(("command", "installed_name"), [
    ("calculator kholo", "Calculator"),
    ("open calculator", "Calculator"),
    ("VS Code kholo", "Visual Studio Code"),
    ("open VS Code", "Visual Studio Code"),
    ("Chrome kholo", "Google Chrome"),
    ("open Chrome", "Google Chrome"),
    ("Notepad kholo", "Notepad"),
])
def test_canonical_app_open_reaches_tool_registry_and_app_resolver(command, installed_name, tmp_path, monkeypatch):
    _disable_automation_store(monkeypatch)
    class GeneralRouter:
        @staticmethod
        def route(_command):
            return RoutingDecision(Route.GENERAL, 0.9, "test general route", "test_router")

    brain = SimpleNamespace(
        learning=None, memory=None, unified=None, cloud_enabled=False, close=lambda: None,
    )
    planner = MagicMock()
    planner.has_public_api_intent.return_value = False
    system_one = MagicMock()
    core = FridayCore(
        AssistantProfile(), router=GeneralRouter(), brain_manager=brain,
        laya_client=MagicMock(), systemone_manager=system_one, planner=planner,
        worker_executor=MagicMock(), research_engine=MagicMock(),
    )
    resolver = core._computer_controller().apps
    resolver.apps = [InstalledApp(installed_name, "start_app", "test-app-id")]
    launched = []

    def safe_launch(request):
        app = resolver.resolve(request)
        launched.append(app.name)
        return app

    resolver.launch = safe_launch
    try:
        with patch("friday.core.LOG_PATH", tmp_path / "friday.log"):
            _answer, _tokens, decision = core.respond(command, voice_response=True)
    finally:
        core.close()

    assert decision.route is Route.COMPUTER
    assert decision.source == "canonical_app_open"
    assert launched == [installed_name]
    assert core.last_tool_result.success
    assert core.last_tool_result.tool_name == "computer.open_app"
    assert core.tool_registry.receipts[-1].tool_name == "computer.open_app"
    system_one.decide_choice.assert_not_called()
    assert "source=canonical_app_open" in (tmp_path / "friday.log").read_text(encoding="utf-8")


def test_unrelated_vscode_phrase_is_not_a_canonical_app_open():
    assert CanonicalComputerIntentParser().parse("What is VS Code?") is None


def test_short_answer_preference_stays_on_conversation_route_without_computer_tools(tmp_path, monkeypatch):
    _disable_automation_store(monkeypatch)
    class GeneralRouter:
        @staticmethod
        def route(_command):
            return RoutingDecision(Route.GENERAL, 0.9, "test general route", "test_router")

    brain = SimpleNamespace(
        learning=None, memory=None, unified=None, cloud_enabled=False, close=lambda: None,
        respond=lambda *_args, **_kwargs: BrainReply("I will keep it short.", provider="test_brain"),
    )
    planner = MagicMock()
    planner.has_public_api_intent.return_value = False
    core = FridayCore(
        AssistantProfile(), router=GeneralRouter(), brain_manager=brain,
        laya_client=MagicMock(), systemone_manager=MagicMock(), planner=planner,
        worker_executor=MagicMock(), research_engine=MagicMock(),
    )
    try:
        with patch("friday.core.LOG_PATH", tmp_path / "friday.log"):
            answer, _tokens, decision = core.respond("mujhy short jawab dena", voice_response=True)
    finally:
        core.close()

    assert answer == "I will keep it short."
    assert decision.route is Route.GENERAL
    assert core.last_tool_result is None
    assert core.tool_registry.receipts == ()


@pytest.mark.parametrize(("command", "source", "tool"), [
    ("C drive mein kitni space bachi hai", "canonical_disk_space", "computer.disk_free_space"),
    ("project folder mai README dhondo", "canonical_file_search", "computer.find_project_readme"),
    ("latest downloaded file konsi hai", "canonical_latest_download", "computer.latest_download"),
    ("2 plus 2 calculate kro", "canonical_arithmetic", "computer.calculate"),
])
@pytest.mark.parametrize("provider_state", ["low_memory", "unavailable"])
def test_optional_openjev_state_does_not_block_canonical_tool_routes(command, source, tool, provider_state, tmp_path, monkeypatch):
    _disable_automation_store(monkeypatch)
    class GeneralRouter:
        @staticmethod
        def route(_command):
            return RoutingDecision(Route.GENERAL, 0.9, "test general route", "test_router")

    class OptionalOpenJEV:
        status = provider_state

        @staticmethod
        def decide_choice(*_args, **_kwargs):
            raise AssertionError("canonical commands must not call OpenJEV")

    brain = SimpleNamespace(
        learning=None, memory=None, unified=None, cloud_enabled=False, close=lambda: None,
    )
    planner = MagicMock()
    planner.has_public_api_intent.return_value = False
    core = FridayCore(
        AssistantProfile(), router=GeneralRouter(), brain_manager=brain,
        laya_client=MagicMock(), systemone_manager=OptionalOpenJEV(),
        planner=planner, worker_executor=MagicMock(), research_engine=MagicMock(),
    )
    try:
        with patch("friday.core.LOG_PATH", tmp_path / "friday.log"):
            _answer, _tokens, decision = core.respond(command, voice_response=True)
    finally:
        core.close()

    assert decision.route is Route.COMPUTER
    assert decision.source == source
    assert core.last_tool_result.success
    assert core.last_tool_result.tool_name == tool
    assert core.tool_registry.receipts[-1].tool_name == tool
    assert core.last_route_telemetry["openjev_ms"] == 0
    assert core.tool_registry.receipts[-1].risk_level.value in {"READ_ONLY", "REVERSIBLE"}
    assert f"source={source}" in (tmp_path / "friday.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(("command", "source", "tool"), [
    ("calculator kholo", "canonical_app_open", "computer.open_app"),
    ("C drive check kro", "canonical_disk_space", "computer.disk_free_space"),
    ("disk space check kro", "canonical_disk_space", "computer.disk_free_space"),
    ("C drive mein kitni space bachi hai", "canonical_disk_space", "computer.disk_free_space"),
    ("project folder mai README dhondo", "canonical_file_search", "computer.find_project_readme"),
    ("find README", "canonical_file_search", "computer.find_project_readme"),
    ("README file kaha hai", "canonical_file_search", "computer.find_project_readme"),
    ("latest downloaded file konsi hai", "canonical_latest_download", "computer.latest_download"),
    ("newest download dikhao", "canonical_latest_download", "computer.latest_download"),
    ("last downloaded file kya hai", "canonical_latest_download", "computer.latest_download"),
    ("2 plus 2 calculate kro", "canonical_arithmetic", "computer.calculate"),
    ("10 multiply by 5", "canonical_arithmetic", "computer.calculate"),
    ("25 divided by 5", "canonical_arithmetic", "computer.calculate"),
])
def test_canonical_intent_parser_extracts_tool_and_entity(command, source, tool):
    intent = CanonicalComputerIntentParser().parse(command)
    assert intent is not None
    assert (intent.source, intent.tool_name) == (source, tool)


@pytest.mark.parametrize("command", [
    "VS Code acha editor hai",
    "calculator useful app hai",
    "C drive kya hoti hai",
    "README kya hota hai",
    "Ali ka naam kya hai",
    "mujhy joke suna do",
    "Chrome fast browser hai",
])
def test_canonical_intent_parser_abstains_on_descriptions_and_questions(command):
    assert CanonicalComputerIntentParser().parse(command) is None


@pytest.mark.parametrize("command", [
    "VS Code acha editor hai",
    "calculator useful app hai",
    "C drive kya hoti hai",
    "README kya hota hai",
    "Ali ka naam kya hai",
    "mujhy joke suna do",
    "Chrome fast browser hai",
])
def test_builtin_router_does_not_send_negative_phrases_to_computer(command):
    assert BuiltinRouter().route(command).route is not Route.COMPUTER
    if "vs code" in command.casefold():
        assert BuiltinRouter().ambiguous_routes(command) is None


@pytest.mark.parametrize("command", [
    "VS Code acha editor hai",
    "calculator useful app hai",
    "C drive kya hoti hai",
    "README kya hota hai",
    "Ali ka naam kya hai",
    "mujhy joke suna do",
    "Chrome fast browser hai",
])
def test_negative_phrases_never_execute_computer_tools(command, tmp_path, monkeypatch):
    _disable_automation_store(monkeypatch)
    brain = SimpleNamespace(
        learning=None, memory=None, unified=None, cloud_enabled=False, close=lambda: None,
        respond=lambda *_args, **_kwargs: BrainReply("No computer action was taken.", provider="test"),
    )
    core = FridayCore(
        AssistantProfile(), brain_manager=brain, laya_client=MagicMock(),
        systemone_manager=MagicMock(), planner=MagicMock(), worker_executor=MagicMock(),
        research_engine=MagicMock(),
    )
    try:
        with patch("friday.core.LOG_PATH", tmp_path / "friday.log"):
            core.respond(command)
    finally:
        core.close()

    assert not any(receipt.tool_name.startswith("computer.") for receipt in core.tool_registry.receipts)


def test_calculator_open_is_distinct_from_arithmetic_and_aliases_normalize():
    parser = CanonicalComputerIntentParser()
    app = parser.parse("open VS Code")
    calculation = parser.parse("2 plus 2 calculate kro")
    assert app.tool_name == "computer.open_app" and app.arguments == {"app": "VS Code"}
    assert calculation.tool_name == "computer.calculate"
    assert calculation.arguments == {"left": "2", "operator": "plus", "right": "2"}


def test_latest_download_tool_returns_the_actual_newest_download(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    older = downloads / "older.txt"
    newer = downloads / "newer.txt"
    older.write_text("older", encoding="utf-8")
    newer.write_text("newer", encoding="utf-8")
    import os
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    result = _computer_registry().execute(
        "computer.latest_download", {}, caller="friday_core", privacy="local_only",
    )
    assert result.success
    assert str(newer) in result.summary


@pytest.mark.parametrize(("left", "operator", "right", "expected"), [
    ("2", "plus", "2", "4"),
    ("10", "multiply", "5", "50"),
    ("25", "divide", "5", "5"),
])
def test_safe_arithmetic_tool_computes_without_expression_evaluation(left, operator, right, expected):
    result = _computer_registry().execute(
        "computer.calculate", {"left": left, "operator": operator, "right": right},
        caller="friday_core", privacy="local_only",
    )
    assert result.success
    assert result.summary == f"{left} {operator} {right} = {expected}"


def test_calculator_resolves_to_the_installed_windows_launcher_without_catalog_probe(monkeypatch):
    expected = Path(r"C:\Windows\System32\calc.exe")
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    monkeypatch.setattr(Path, "is_file", lambda path: str(path).casefold() == str(expected).casefold())
    resolver = AppResolver()
    monkeypatch.setattr(resolver, "refresh", lambda: (_ for _ in ()).throw(AssertionError("catalog probe")))

    app = resolver.resolve("calculator")

    assert (app.name, app.source, app.launch_target) == ("Calculator", "app_path", str(expected))


def test_read_only_computer_tools_return_current_values_and_existing_paths():
    registry = _computer_registry()
    disk = registry.execute("computer.disk_free_space", {}, caller="friday_core", privacy="local_only")
    assert disk.success
    reported = int(re.search(r"C: (\d+) bytes free", disk.summary).group(1))
    current = shutil.disk_usage("C:\\").free
    assert abs(reported - current) < 8 * 1024 * 1024

    files = registry.execute("computer.find_project_readme", {}, caller="friday_core", privacy="local_only")
    expected_root = Path(__file__).resolve().parents[1]
    found = [Path(line) for line in files.summary.splitlines() if Path(line).is_file()]
    assert files.success and found
    assert all(path.parent == expected_root for path in found)


def test_notepad_typing_is_held_for_explicit_tool_registry_approval():
    plan = DeterministicComputerPlanner().plan("notepad mai BAKEOFF likh do")
    assert plan is not None
    assert [(action.operation, action.target) for action in plan.actions] == [
        ("OPEN_APP", "notepad"),
        ("TYPE_TEXT", "BAKEOFF"),
    ]

    result = _computer_registry().execute(
        "computer.dispatch_request",
        {"request": "notepad mai BAKEOFF likh do"},
        caller="friday_core",
        privacy="public",
    )
    assert not result.success
    assert result.status == "approval_required"
    assert result.requires_followup
    assert result.metadata["receipt"]["approval_result"] == "REQUIRE_CONFIRMATION"
