"""Assistant identity and response style configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = PROJECT_ROOT / "profile.json"


@dataclass(frozen=True)
class AssistantProfile:
    assistant_name: str = "Friday"
    user_name: str = "Ali"
    language: str = "auto"
    default_response_style: str = "concise"
    cloud_brain_enabled: bool = False

    def system_prompt(self) -> str:
        language_rule = (
            "Match the user's language and use natural Roman Urdu when they do."
            if self.language.casefold() == "auto"
            else f"Respond in {self.language}."
        )
        return (
            f"You are {self.assistant_name}, an assistant for "
            f"{self.user_name}. {language_rule} "
            f"Keep replies {self.default_response_style}. "
            "When asked for exact text, output only that text."
        )


def load_profile(path: Path = PROFILE_PATH) -> AssistantProfile:
    defaults = AssistantProfile()
    try:
        supplied = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        supplied = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read profile.json: {error}") from error
    if not isinstance(supplied, dict):
        raise RuntimeError("profile.json must contain a JSON object.")

    values = {
        field: str(supplied.get(field, getattr(defaults, field))).strip()
        or getattr(defaults, field)
        for field in (
            "assistant_name",
            "user_name",
            "language",
            "default_response_style",
        )
    }
    cloud_enabled = supplied.get("cloud_brain_enabled", defaults.cloud_brain_enabled)
    if not isinstance(cloud_enabled, bool):
        raise RuntimeError("cloud_brain_enabled must be true or false.")
    values["cloud_brain_enabled"] = cloud_enabled
    return AssistantProfile(**values)
