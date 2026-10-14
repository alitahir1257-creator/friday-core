"""Discover installed applications from Windows, without a fixed app catalog."""

from __future__ import annotations

import json
import os
import re
import subprocess
import winreg
from dataclasses import dataclass
from pathlib import Path

from friday.runtime_config import child_process_environment


def normalize(name: str) -> str:
    name = name.casefold().replace("&", " and ")
    name = re.sub(r"[^\w]+", " ", name, flags=re.UNICODE)
    return " ".join(name.split())


@dataclass(frozen=True)
class InstalledApp:
    name: str
    source: str
    launch_target: str


class AppResolutionError(ValueError):
    pass


class AppResolver:
    def __init__(self) -> None:
        self.apps: list[InstalledApp] = []

    def refresh(self) -> int:
        found: dict[str, InstalledApp] = {}

        def add(app: InstalledApp) -> None:
            key = normalize(app.name)
            if key and key not in found:
                found[key] = app

        roots = (
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
        )
        for root in roots:
            if root.is_dir():
                for shortcut in root.rglob("*.lnk"):
                    add(InstalledApp(shortcut.stem, "shortcut", str(shortcut)))

        # PowerShell emits a single JSON object for one result and an array for many.
        command = [
            str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile", "-Command",
            "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress -Depth 2",
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=20, check=True,
                env=child_process_environment(),
            )
            entries = json.loads(completed.stdout or "[]")
            for item in entries if isinstance(entries, list) else [entries]:
                if item.get("Name") and item.get("AppID"):
                    add(InstalledApp(str(item["Name"]), "start_app", str(item["AppID"])))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    registry = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths", 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with registry:
                    for index in range(winreg.QueryInfoKey(registry)[0]):
                        try:
                            name = winreg.EnumKey(registry, index)
                            with winreg.OpenKey(registry, name) as entry:
                                executable = os.path.expandvars(winreg.QueryValue(entry, None)).strip('"')
                            if Path(executable).is_file() and executable.lower().endswith(".exe"):
                                add(InstalledApp(Path(name).stem, "app_path", executable))
                        except OSError:
                            continue

        # PATH is lower priority and only executable files are indexed.
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            path = Path(directory)
            if path.is_dir():
                try:
                    for executable in path.glob("*.exe"):
                        add(InstalledApp(executable.stem, "path", str(executable)))
                except OSError:
                    continue
        self.apps = list(found.values())
        return len(self.apps)

    def resolve(self, request: str) -> InstalledApp:
        query = normalize(request)
        windir = os.environ.get("WINDIR")
        if query == "calculator" and windir:
            calculator = Path(windir) / "System32" / "calc.exe"
            if calculator.is_file():
                return InstalledApp("Calculator", "app_path", str(calculator))
        if not self.apps:
            self.refresh()
        # A spelling expansion, not an application allowlist.
        query = re.sub(r"\bvs\b", "visual studio", query)
        exact = [app for app in self.apps if normalize(app.name) == query]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AppResolutionError("I found multiple matching apps: " + ", ".join(a.name for a in exact[:5]))
        matches = [app for app in self.apps if normalize(app.name).endswith(" " + query)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AppResolutionError("I found multiple matching apps: " + ", ".join(a.name for a in matches[:5]))
        raise AppResolutionError(f"I couldn't confidently find an installed app named {request!r}.")

    def launch(self, request: str) -> InstalledApp:
        app = self.resolve(request)
        if app.source == "shortcut":
            os.startfile(app.launch_target)
        elif app.source == "start_app":
            if "\x00" in app.launch_target or "/" in app.launch_target:
                raise AppResolutionError("The installed app identifier is invalid.")
            explorer = Path(os.environ["WINDIR"]) / "explorer.exe"
            subprocess.Popen(
                [str(explorer), "shell:AppsFolder\\" + app.launch_target],
                close_fds=True, env=child_process_environment(),
            )
        else:
            subprocess.Popen([app.launch_target], close_fds=True, env=child_process_environment())
        return app
