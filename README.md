# FRIDAY Core MVP

A small Windows desktop command window backed by the local llama.cpp OpenAI-compatible endpoint. It uses only Python standard-library modules and Tkinter.

## Run

From this directory, run: `.venv\Scripts\python.exe friday_core.py` after installing `requirements-computer.txt` into a Python 3.11 venv.

The first GENERAL command checks http://127.0.0.1:8080/v1. If it is unavailable, FRIDAY starts the configured local launcher at D:\FRIDAY\scripts\friday-local-brain.ps1 with a 4096-token context. No model download or paid API is used.

Edit profile.json to change the assistant name, user name, language, or concise response style.

The `friday` package separates the Tkinter app, local Qwen brain, profile, bounded conversation history, and route labels. GENERAL, CODING, RESEARCH, and MEMORY use Qwen. COMPUTER uses a deterministic action planner and the computer capability dispatcher. Unrecognized computer actions stop with a clear error.

For computer control, use Python 3.11 and install the two packages in `requirements-computer.txt`. Windows UI Automation uses pywinauto. Website automation uses Playwright with an already installed Chrome or Edge executable; no Playwright browser install command is needed. Its dedicated local browser profile is ignored by Git. The UI asks for approval before consequential actions.
