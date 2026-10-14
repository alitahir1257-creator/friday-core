"""Small process configuration helpers for secrets and child-process isolation."""

from __future__ import annotations

import os
from pathlib import Path


GEMINI_ENV_CONFIG_PATH = Path(r"D:\FRIDAY\config\.env.local")


def load_gemini_api_key_from_config(path: str | Path = GEMINI_ENV_CONFIG_PATH) -> bool:
    """Load only the approved D: key into this process, without logging or raising."""
    os.environ.pop("GEMINI_API_KEY", None)
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return False
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        name, value = candidate.split("=", 1)
        if name.strip() != "GEMINI_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            os.environ["GEMINI_API_KEY"] = value
            return True
    return False


def child_process_environment() -> dict[str, str]:
    """Keep FRIDAY's Gemini credential out of unrelated child processes."""
    environment = os.environ.copy()
    environment.pop("GEMINI_API_KEY", None)
    return environment


__all__ = ["GEMINI_ENV_CONFIG_PATH", "child_process_environment", "load_gemini_api_key_from_config"]
