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

## Optional remote Laya System-1

Laya is an optional structured route classifier for genuinely ambiguous requests. It does not answer conversational prompts, execute computer actions, or replace `BrainManager`. Deterministic computer plans and policy-sensitive actions bypass it. On eligible requests, FRIDAY sends only the current command (up to 512 characters) and a compact route-choice question; it does not send conversation history, environment values, or local files. Requests containing credential markers or recognizable token formats are kept local. The footer shows whether remote Laya is disabled, configured, healthy, or unavailable.

Laya is disabled by default. Opt in through `laya_enabled: true` in `profile.json` or `FRIDAY_LAYA_ENABLED=true`, then set `FRIDAY_LAYA_BASE_URL` to an HTTPS service URL. HTTP is accepted only for loopback development. `FRIDAY_LAYA_API_KEY` is optional and is sent only as a bearer header to `/v1/systemone`; `FRIDAY_LAYA_TIMEOUT_SECONDS` defaults to `5`. If Laya is missing, unreachable, times out, or returns an invalid decision, FRIDAY keeps the existing built-in route and continues through `BrainManager` as usual. No Laya dependency or model is installed in the Windows FRIDAY environment.

The official `laya[serve]` deployment template is isolated in [`deploy/laya`](deploy/laya/README.md). It pins Laya 0.3.26, runs the official `laya-serve` entry point on CPU, and preloads only the English checkpoint. The template binds to loopback by default; use a TLS reverse proxy or private tunnel before remote access. Supply `LAYA_API_KEY` through the deployment environment. No host is selected or deployed by this project.

### Optional hosted Jev System-One provider

Hosted Jev is disabled by default. It is used only for ambiguous typed route decisions, after deterministic routing and after configured Remote Laya. When enabled, FRIDAY sends only the current request and the route-choice question to `https://api.typesafe.ai/v1/systemone`; the request includes model `jev-latest` by default. Jev decisions cannot initiate COMPUTER actions. Invalid, low-confidence, timed-out, or failed decisions fall back to the existing deterministic route and `BrainManager`.

Set `FRIDAY_JEV_ENABLED=true` and `FRIDAY_JEV_API_KEY` in the environment to opt in. Optional settings are `FRIDAY_JEV_BASE_URL` (defaults to `https://api.typesafe.ai/v1/systemone`), `FRIDAY_JEV_MODEL` (defaults to `jev-latest`), and `FRIDAY_JEV_TIMEOUT_SECONDS` (defaults to `5`). The API key is never stored in `profile.json` or logged. Hosted Jev sends user text off-device; verify account access and pricing before enabling it. This project does not create an account, configure billing, or assume a free tier. See the [TypeSafe System-One API documentation](https://api.typesafe.ai/redoc).
