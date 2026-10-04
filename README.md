# FRIDAY Core MVP

A small Windows desktop command window backed by the local llama.cpp OpenAI-compatible endpoint. It uses only Python standard-library modules and Tkinter.

## Run

From this directory, run: `.venv\Scripts\python.exe friday_core.py` after installing `requirements-computer.txt` into a Python 3.11 venv.

The first GENERAL command checks http://127.0.0.1:8080/v1. If it is unavailable, FRIDAY starts the configured local launcher at D:\FRIDAY\scripts\friday-local-brain.ps1 with a 4096-token context. No model download or paid API is used.

Edit profile.json to change the assistant name, user name, language, or concise response style.

The `friday` package separates the Tkinter app, local Qwen brain, profile, bounded conversation history, and route labels. GENERAL, CODING, RESEARCH, and MEMORY use Qwen. COMPUTER uses a deterministic action planner and the computer capability dispatcher. Unrecognized computer actions stop with a clear error.

For computer control, use Python 3.11 and install the two packages in `requirements-computer.txt`. Windows UI Automation uses pywinauto. Website automation uses Playwright with an already installed Chrome or Edge executable; no Playwright browser install command is needed. Its dedicated local browser profile is ignored by Git. The UI asks for approval before consequential actions.

## Brain providers

FRIDAY uses local Qwen by default. To opt into Cloudflare Workers AI, set `cloud_brain_enabled` to `true` in `profile.json`, then add these Windows user environment variables and restart FRIDAY:

- `FRIDAY_CLOUDFLARE_ACCOUNT_ID`
- `FRIDAY_CLOUDFLARE_API_TOKEN`
- `FRIDAY_CLOUDFLARE_MODEL` (optional; defaults to `@cf/qwen/qwen3-30b-a3b-fp8`)

Create a Workers AI API token from Cloudflare's Workers AI dashboard. Keep the token only in the environment variable; do not put it in `profile.json`, source files, or logs. FRIDAY calls the direct Workers AI Chat Completions endpoint and does not use AI Gateway, unified billing, or any third-party provider.

For zero-cost use, keep the Cloudflare account on the Workers Free plan. Cloudflare currently includes 10,000 Workers AI Neurons per day on Free and rejects requests after that allocation is exhausted. Workers Paid can charge for usage above the free allocation, so do not activate it or add payment information. FRIDAY falls back to local Qwen when Cloudflare rejects a request. Check [Workers AI pricing and free allocation](https://developers.cloudflare.com/workers-ai/platform/pricing/) before enabling cloud mode because Cloudflare's limits and prices can change.
