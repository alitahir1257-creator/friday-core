"""Deterministic task/privacy policies and the approved free model pool."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from friday.brain import MODEL_NAME
from friday.router import Route


class ModelPolicy(str, Enum):
    COMPUTER = "COMPUTER"
    FAST = "FAST"
    SMART = "SMART"
    CODING = "CODING"
    RESEARCH = "RESEARCH"
    PRIVATE = "PRIVATE"
    VISION = "VISION"
    VOICE = "VOICE"


class PrivacyLevel(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    LOCAL_ONLY = "local_only"


APPROVED_FREE_GEMINI_MODELS = frozenset(
    {
        "gemini/gemini-3.1-flash-lite",
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
    }
)
FAST_MODEL_DEFAULT = "gemini/gemini-3.1-flash-lite"
FAST_FALLBACK_MODEL_DEFAULT = "gemini/gemini-2.5-flash-lite"
SMART_MODEL_DEFAULT = "gemini/gemini-3-flash-preview"
SMART_FALLBACK_MODEL_DEFAULT = "gemini/gemini-2.5-flash"


@dataclass(frozen=True)
class ModelTarget:
    backend: str
    model: str | None = None


@dataclass(frozen=True)
class ModelSelection:
    policy: ModelPolicy
    privacy: PrivacyLevel
    reason: str
    preferred_backend: str | None
    preferred_model: str | None
    fallbacks: tuple[ModelTarget, ...] = ()
    selected_backend: str | None = None
    selected_model: str | None = None
    latency_ms: float | None = None
    fallback_count: int = 0

    @property
    def targets(self) -> tuple[ModelTarget, ...]:
        if self.preferred_backend is None:
            return ()
        return (ModelTarget(self.preferred_backend, self.preferred_model), *self.fallbacks)


@dataclass(frozen=True)
class ModelPolicyConfig:
    fast_model: str = FAST_MODEL_DEFAULT
    fast_fallback_model: str = FAST_FALLBACK_MODEL_DEFAULT
    smart_model: str = SMART_MODEL_DEFAULT
    smart_fallback_model: str = SMART_FALLBACK_MODEL_DEFAULT

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> ModelPolicyConfig:
        values = environ if environ is not None else os.environ

        def approved(name: str, default: str) -> str:
            candidate = values.get(name, "").strip()
            return candidate if candidate in APPROVED_FREE_GEMINI_MODELS else default

        return cls(
            fast_model=approved("FRIDAY_FAST_MODEL", FAST_MODEL_DEFAULT),
            fast_fallback_model=approved(
                "FRIDAY_FAST_FALLBACK_MODEL", FAST_FALLBACK_MODEL_DEFAULT
            ),
            smart_model=approved("FRIDAY_SMART_MODEL", SMART_MODEL_DEFAULT),
            smart_fallback_model=approved(
                "FRIDAY_SMART_FALLBACK_MODEL", SMART_FALLBACK_MODEL_DEFAULT
            ),
        )

    def __post_init__(self) -> None:
        if any(
            model not in APPROVED_FREE_GEMINI_MODELS
            for model in (
                self.fast_model,
                self.fast_fallback_model,
                self.smart_model,
                self.smart_fallback_model,
            )
        ):
            raise ValueError("Model policy models must be in the approved free Gemini pool.")


_PRIVATE_CUES = re.compile(
    r"\b(private|local[- ]only|confidential|sensitive|passwords?|passcodes?|"
    r"credentials?|api[ _-]*keys?|access tokens?|private keys?|secrets?|"
    r"financial documents?|bank statements?|tax returns?|personal data|"
    r"national id|ssn|private contracts?)\b",
    re.IGNORECASE,
)
_CODING_ACTION = re.compile(
    r"\b(?:fix|debug|implement|modify|edit|refactor|write|change|create|repair|patch)"
    r"\b.{0,100}\b(?:repository|repo|codebase|source files?|unit tests?|test suite|"
    r"code|function|script|project)\b|"
    r"\b(?:repository|repo|codebase|source files?|unit tests?|test suite)\b"
    r".{0,100}\b(?:fix|debug|implement|modify|edit|refactor|write|change|create|"
    r"repair|patch)\b",
    re.IGNORECASE,
)
_RESEARCH_CUES = re.compile(
    r"\b(research|sources?|citations?|latest|current|recent|today|news|fresh information|"
    r"external information|web search|look up)\b",
    re.IGNORECASE,
)
_SMART_CUES = re.compile(
    r"\b(compare|architecture|architectures|trade[- ]offs?|plan|planning|multi[- ]step|"
    r"complex|alternatives?|pros and cons|evaluate|reasoning|design options?)\b",
    re.IGNORECASE,
)
_FAST_CUES = re.compile(
    r"\b(rewrite|rephrase|summari[sz]e|translate|classify|define|what is|who is|"
    r"where is|when is|explain simply|explain briefly|simple explanation|"
    r"reply exactly with|answer exactly|say exactly)\b",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|salam|assalamu alaikum|thanks|thank you|good morning|"
    r"good afternoon|good evening)(?:\s+there)?(?:[,!.?]\s*(?:how are you|what's up))?"
    r"[!. ,?]*\s*$",
    re.IGNORECASE,
)


class ModelPolicyEngine:
    """Select an auditable capability policy without calling a model."""

    def __init__(self, config: ModelPolicyConfig | None = None) -> None:
        self.config = config or ModelPolicyConfig.from_environment()

    @staticmethod
    def classify_privacy(command: str, explicit: str | PrivacyLevel | None = None) -> PrivacyLevel:
        if isinstance(explicit, PrivacyLevel):
            explicit_level = explicit
        elif explicit is None:
            explicit_level = PrivacyLevel.PUBLIC
        else:
            try:
                explicit_level = PrivacyLevel(explicit)
            except ValueError as error:
                raise ValueError("privacy must be public, private, or local_only.") from error

        if explicit_level in {PrivacyLevel.PRIVATE, PrivacyLevel.LOCAL_ONLY}:
            return explicit_level
        if _PRIVATE_CUES.search(command):
            return PrivacyLevel.PRIVATE
        return PrivacyLevel.PUBLIC

    def select(
        self,
        command: str,
        *,
        route_hint: Route | ModelPolicy | str | None = None,
        privacy: str | PrivacyLevel | None = None,
        requested_policy: ModelPolicy | str | None = None,
        cloudflare_enabled: bool = False,
    ) -> ModelSelection:
        level = self.classify_privacy(command, privacy)
        route = self._route_value(route_hint)
        try:
            explicit_policy = (
                ModelPolicy(requested_policy)
                if requested_policy is not None
                else route if isinstance(route, ModelPolicy) else None
            )
        except ValueError as error:
            raise ValueError("requested_policy is not a supported model policy.") from error

        if (
            route == Route.COMPUTER
            or explicit_policy == ModelPolicy.COMPUTER
            or self._is_deterministic_computer(command)
        ):
            return ModelSelection(
                ModelPolicy.COMPUTER,
                level,
                "deterministic OS/browser action",
                "computer",
                None,
            )

        if explicit_policy == ModelPolicy.PRIVATE and level == PrivacyLevel.PUBLIC:
            level = PrivacyLevel.PRIVATE
        if level in {PrivacyLevel.PRIVATE, PrivacyLevel.LOCAL_ONLY}:
            return ModelSelection(
                ModelPolicy.PRIVATE,
                level,
                "private data requires local-only route",
                "local",
                MODEL_NAME,
            )

        if explicit_policy == ModelPolicy.CODING:
            return ModelSelection(
                ModelPolicy.CODING,
                level,
                "repository work is delegated to Codex",
                "codex",
                "codex",
            )
        if explicit_policy == ModelPolicy.RESEARCH:
            policy = ModelPolicy.RESEARCH
            reason = "research capability requested; using SMART reasoning route"
        elif explicit_policy in {ModelPolicy.VISION, ModelPolicy.VOICE}:
            return ModelSelection(
                explicit_policy,
                level,
                "capability routing is not implemented in this phase",
                None,
                None,
            )
        elif explicit_policy in {ModelPolicy.FAST, ModelPolicy.SMART}:
            policy = explicit_policy
            reason = "explicit capability policy requested"
        elif self._requires_repository_work(command):
            return ModelSelection(
                ModelPolicy.CODING,
                level,
                "repository work is delegated to Codex",
                "codex",
                "codex",
            )
        elif route == Route.CODING and not self._is_explanation_request(command):
            policy = ModelPolicy.SMART
            reason = "technical task without repository modification"
        elif route == Route.RESEARCH and _RESEARCH_CUES.search(command):
            policy = ModelPolicy.RESEARCH
            reason = "fresh external information or sources requested"
        elif _RESEARCH_CUES.search(command):
            policy = ModelPolicy.RESEARCH
            reason = "fresh external information or sources requested"
        elif _SMART_CUES.search(command):
            policy = ModelPolicy.SMART
            reason = "multi-step reasoning or tradeoffs requested"
        elif _GREETING.fullmatch(command) or _FAST_CUES.search(command):
            policy = ModelPolicy.FAST
            reason = "simple low-risk text task"
        else:
            policy = ModelPolicy.SMART
            reason = "uncertain task defaults to SMART"

        targets = self._targets(policy, cloudflare_enabled)
        return ModelSelection(
            policy,
            level,
            reason,
            targets[0].backend,
            targets[0].model,
            targets[1:],
        )

    @staticmethod
    def _route_value(route_hint: Route | ModelPolicy | str | None) -> Route | ModelPolicy | None:
        if isinstance(route_hint, (Route, ModelPolicy)) or route_hint is None:
            return route_hint
        try:
            return Route(route_hint)
        except ValueError:
            try:
                return ModelPolicy(route_hint)
            except ValueError:
                return None

    @staticmethod
    def _is_deterministic_computer(command: str) -> bool:
        from friday.capabilities.computer.parser import DeterministicComputerPlanner

        return DeterministicComputerPlanner().plan(command) is not None

    @staticmethod
    def _requires_repository_work(command: str) -> bool:
        if _CODING_ACTION.search(command):
            return True
        return bool(
            re.search(
                r"\b(?:implement|fix|debug|modify|edit|refactor|patch)\b.{0,80}"
                r"\b(?:code|python|script|tests?|repository|repo|codebase|project)\b",
                command,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _is_explanation_request(command: str) -> bool:
        return bool(
            re.search(
                r"^\s*(?:explain|describe|what(?:'s| is| does)|how does|walk me through)\b",
                command,
                re.IGNORECASE,
            )
        )

    def _targets(self, policy: ModelPolicy, cloudflare_enabled: bool) -> tuple[ModelTarget, ...]:
        if policy == ModelPolicy.FAST:
            models = (self.config.fast_model, self.config.fast_fallback_model)
        else:
            models = (self.config.smart_model, self.config.smart_fallback_model)
        targets = [ModelTarget("omniroute", models[0])]
        if models[1] != models[0]:
            targets.append(ModelTarget("omniroute", models[1]))
        targets.append(ModelTarget("freellmapi"))
        if policy != ModelPolicy.FAST and cloudflare_enabled:
            targets.append(ModelTarget("cloudflare"))
        targets.append(ModelTarget("local", MODEL_NAME))
        return tuple(targets)
