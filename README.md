# FRIDAY Core MVP

A small Windows desktop command window backed by the local llama.cpp OpenAI-compatible endpoint. It uses only Python standard-library modules and Tkinter.

## Run

From this directory, run: `.venv\Scripts\python.exe friday_core.py` after installing `requirements-computer.txt` into a Python 3.11 venv.

The first GENERAL command checks http://127.0.0.1:8080/v1. If it is unavailable, FRIDAY starts the configured local launcher at D:\FRIDAY\scripts\friday-local-brain.ps1 with a 4096-token context. No model download or paid API is used.

Edit profile.json to change the assistant name, user name, language, or concise response style.

The `friday` package separates the Tkinter app, local Qwen brain, profile, bounded conversation history, and route labels. FAST/SMART/RESEARCH use approved provider pools with local Qwen fallback; PRIVATE stays on local Qwen; repository changes are handed to Codex. COMPUTER uses a deterministic action planner and the computer capability dispatcher. Unrecognized computer actions stop with a clear error.

For computer control, use Python 3.11 and install the two packages in `requirements-computer.txt`. Windows UI Automation uses pywinauto. Website automation uses Playwright with an already installed Chrome or Edge executable; no Playwright browser install command is needed. Its dedicated local browser profile is ignored by Git. The UI asks for approval before consequential actions.

## Brain providers

FRIDAY uses local Qwen by default. To opt into Cloudflare Workers AI, set `cloud_brain_enabled` to `true` in `profile.json`, then add these Windows user environment variables and restart FRIDAY:

- `FRIDAY_CLOUDFLARE_ACCOUNT_ID`
- `FRIDAY_CLOUDFLARE_API_TOKEN`
- `FRIDAY_CLOUDFLARE_MODEL` (optional; defaults to `@cf/qwen/qwen3-30b-a3b-fp8`)

Create a Workers AI API token from Cloudflare's Workers AI dashboard. Keep the token only in the environment variable; do not put it in `profile.json`, source files, or logs. FRIDAY calls the direct Workers AI Chat Completions endpoint and does not use AI Gateway, unified billing, or any third-party provider.

For zero-cost use, keep the Cloudflare account on the Workers Free plan. Cloudflare currently includes 10,000 Workers AI Neurons per day on Free and rejects requests after that allocation is exhausted. Workers Paid can charge for usage above the free allocation, so do not activate it or add payment information. FRIDAY falls back to local Qwen when Cloudflare rejects a request. Check [Workers AI pricing and free allocation](https://developers.cloudflare.com/workers-ai/platform/pricing/) before enabling cloud mode because Cloudflare's limits and prices can change.

### Optional FreeLLMAPI gateway

FreeLLMAPI is an optional FRIDAY provider alongside OmniRoute; it does not replace FRIDAY's policy selection or local Qwen fallback. Adaptive requests use the policy-specific order documented below. Each selected backend/model target gets at most one FRIDAY attempt before fallback.

The gateway is disabled by default. To opt in, set these Windows environment variables and restart FRIDAY:

- `FRIDAY_FREELLMAPI_ENABLED=true`
- `FRIDAY_FREELLMAPI_BASE_URL` (optional; defaults to `http://127.0.0.1:3001/v1`)
- `FRIDAY_FREELLMAPI_API_KEY` (the unified gateway key; environment only)
- `FRIDAY_FREELLMAPI_MODEL` (optional; defaults to the documented `auto` router model)
- `FRIDAY_FREELLMAPI_TIMEOUT_SECONDS` (optional; defaults to `15`)

FRIDAY makes one non-streaming OpenAI-compatible chat-completions request. A disabled, unconfigured, failed, timed-out, rate-limited, or malformed gateway response falls through to local Qwen. Use FreeLLMAPI's free provider configuration only; do not enable premium features or billing. Keep individual downstream provider keys in FreeLLMAPI, never in FRIDAY. The unified gateway key is never stored in `profile.json` or written to logs.

### Optional OmniRoute orchestration gateway

OmniRoute is a separate optional OpenAI-compatible FRIDAY provider. It is disabled by default. FRIDAY does not install or start OmniRoute, enable its MCP/A2A/Memory/Skills features, or configure downstream providers. Phase 4D sends explicit allowlisted Gemini free model IDs for FAST/SMART; the app does not use OmniRoute `auto` for adaptive requests.

Set these Windows environment variables to opt in, then restart FRIDAY:

- `FRIDAY_OMNIROUTE_ENABLED=true`
- `FRIDAY_OMNIROUTE_BASE_URL` (optional; defaults to `http://127.0.0.1:20128/v1`)
- `FRIDAY_OMNIROUTE_API_KEY` (optional unless the local OmniRoute instance requires API-key authentication; environment only)
- `FRIDAY_OMNIROUTE_MODEL` (legacy setting; adaptive routing ignores it and sends an approved explicit model)
- `FRIDAY_OMNIROUTE_TIMEOUT_SECONDS` (optional; defaults to `15`, maximum `60`)

FRIDAY sends one non-streaming request to `/chat/completions` per selected model attempt. Timeout, HTTP errors, or malformed responses proceed to the next approved target; attempts are bounded and are not recursively routed. The API key is not stored in `profile.json` or written to logs. Requests classified `private` or `local_only` skip System-One, use local Qwen only, and are excluded from shared conversation history. Sensitive prompt bodies are not logged.

The OmniRoute base URL is restricted to a loopback IP address, so this integration cannot silently send requests to a remote gateway.

FreeLLMAPI and OmniRoute remain parallel FRIDAY providers. FRIDAY does not configure FreeLLMAPI inside OmniRoute or create a custom recursive provider chain. The Kilo route previously used through FreeLLMAPI may log prompts and outputs for training; private data stays on local Qwen.

### Adaptive model policies (Phase 4D)

FRIDAY classifies capability and privacy locally with deterministic rules. It does not call an LLM to choose another LLM. Unknown tasks default to SMART; greetings and simple rewrite/Q&A tasks use FAST. COMPUTER continues through the deterministic action dispatcher and makes no LLM call. RESEARCH currently uses the SMART model chain and does not perform web search. VISION and VOICE are metadata-only placeholders. Repository edits are handed to Codex and are not silently sent to a chat model.

- **FAST:** OmniRoute `gemini/gemini-3.1-flash-lite` → OmniRoute `gemini/gemini-2.5-flash-lite` → FreeLLMAPI → local Qwen.
- **SMART / RESEARCH:** OmniRoute `gemini/gemini-3-flash-preview` → OmniRoute `gemini/gemini-2.5-flash` → FreeLLMAPI → explicitly enabled direct Cloudflare → local Qwen.
- **PRIVATE:** local Qwen only. If local Qwen fails, FRIDAY returns a local/private route error.
- **CODING:** Codex handoff metadata; no automatic chat-model fallback.
- **COMPUTER:** deterministic OS/browser dispatcher only.

Optional model overrides are `FRIDAY_FAST_MODEL`, `FRIDAY_FAST_FALLBACK_MODEL`, `FRIDAY_SMART_MODEL`, and `FRIDAY_SMART_FALLBACK_MODEL`. Defaults are listed above. Overrides outside the four Gemini model IDs verified as free for this phase are ignored, so an unknown or paid model cannot enter the adaptive OmniRoute pool through these variables. Provider free-tier availability and account limits remain controlled by the provider account.

The official project documents MCP (`omniroute --mcp`), A2A, Memory, and Skills, but those features are deferred and are not part of FRIDAY's integration. If OmniRoute is not installed, its official Windows npm path is to use a Node.js version allowed by package metadata (`>=20.20.2 <21`, `>=22.22.2 <23`, or `>=24.0.0 <25`), run `npm install -g omniroute`, then run `omniroute` in PowerShell. The server and dashboard use port `20128` by default. Create an API key in Dashboard → Endpoints only if the instance requires one, then set it in `FRIDAY_OMNIROUTE_API_KEY`; do not paste the key into `profile.json` or source files. See the [official OmniRoute README](https://github.com/Xchat1/omniroute), [User Guide](https://github.com/Xchat1/omniroute/blob/main/docs/USER_GUIDE.md), and [runtime package metadata](https://raw.githubusercontent.com/Xchat1/omniroute/main/package.json) for current install requirements.

## Local storage (Phase 5A / 5B)

FRIDAY's persistent root is `D:\FRIDAY`. SQLite operational memory stays separate from the human-readable Markdown knowledge vault:

- Memory: `D:\FRIDAY\data\memory\friday-memory.sqlite3` (`FRIDAY_MEMORY_DB_PATH` overrides it). SQLite/FTS5 retrieval is local and deterministic. An old database under `%LOCALAPPDATA%\FRIDAY\memory` is never migrated or deleted automatically; migrate it manually only if needed.
- Knowledge: `D:\FRIDAY\vault` (`FRIDAY_VAULT_PATH` overrides it). Notes are ordinary UTF-8 Markdown with optional editable frontmatter and Obsidian-style wikilinks. Vault initialization is explicit; FRIDAY does not import conversations or populate personal data.

Obsidian desktop is optional: no account, login, plugin, or Sync is required. To use its editor later, open `D:\FRIDAY\vault` in Obsidian as a vault. Public/cloud requests can receive only public vault notes; private and local-only knowledge stays on the local PRIVATE route. No remote provider is used for ordinary vault search. Semantic embeddings remain a future optional capability.

Reserved future locations are `D:\FRIDAY\data\logs`, `D:\FRIDAY\data\cache`, `D:\FRIDAY\models`, `D:\FRIDAY\runtime`, and `D:\FRIDAY\scripts`; existing runtime assets are not moved there automatically.

## Unified Knowledge (Phase 5C)

`UnifiedKnowledgeManager` merges **Memory + Vault + Connectors** into ranked, deduplicated,
namespace-aware snippets. SQLite memory and the Markdown vault remain independent sources.
`BrainManager` consumes one privacy-filtered context; source failures leave other sources available.
Result provenance and per-source counts are available for a future HUD, while prompts omit internal IDs and paths.

Connectors are opt-in. `LocalFilesConnector` defaults to disabled with no roots. Configure
explicit absolute `ApprovedRoot(path, namespace="friday", privacy="private")` directories,
register the enabled connector in `ConnectorRegistry`, and pass that registry to the unified manager.
Local files are private by default; marking a root public explicitly permits its text in cloud context.
Private/local-only context requires a PRIVATE/local Qwen route and cannot fall back to remote providers.

Search is on demand: no whole-disk scanning, daemon, embedding model, or persistent connector index.
Each scan visits at most 5,000 entries and reads at most 256 KiB per supported UTF-8 text file.
Traversal, symlinks/junctions, overlapping roots, credential names/content, hidden directories,
dependency directories, `.obsidian`, the real vault, and the operational memory directory are excluded.
These limits bound ordinary local usage; concurrent hostile filesystem mutation is outside the single-user trust model.

No external connectors are implemented. Future research connectors can declare `search` and `research`
capabilities using the same result contract. Phase 6A adds Tool Registry/MCP; Phase 7A adds research;
Phase 7B adds PDF/Office/image parsing. Run `python -B scripts/phase5c_smoke.py` for temporary-only checks.

## Optional remote Laya System-1

Laya is an optional structured route classifier for genuinely ambiguous requests. It does not answer conversational prompts, execute computer actions, or replace `BrainManager`. Deterministic computer plans and policy-sensitive actions bypass it. On eligible requests, FRIDAY sends only the current command (up to 512 characters) and a compact route-choice question; it does not send conversation history, environment values, or local files. Requests containing credential markers or recognizable token formats are kept local. The footer shows whether remote Laya is disabled, configured, healthy, or unavailable.

Laya is disabled by default. Opt in through `laya_enabled: true` in `profile.json` or `FRIDAY_LAYA_ENABLED=true`, then set `FRIDAY_LAYA_BASE_URL` to an HTTPS service URL. HTTP is accepted only for loopback development. `FRIDAY_LAYA_API_KEY` is optional and is sent only as a bearer header to `/v1/systemone`; `FRIDAY_LAYA_TIMEOUT_SECONDS` defaults to `5`. If Laya is missing, unreachable, times out, or returns an invalid decision, FRIDAY keeps the existing built-in route and continues through `BrainManager` as usual. No Laya dependency or model is installed in the Windows FRIDAY environment.

The official `laya[serve]` deployment template is isolated in [`deploy/laya`](deploy/laya/README.md). It pins Laya 0.3.26, runs the official `laya-serve` entry point on CPU, and preloads only the English checkpoint. The template binds to loopback by default; use a TLS reverse proxy or private tunnel before remote access. Supply `LAYA_API_KEY` through the deployment environment. No host is selected or deployed by this project.

### Optional hosted Jev System-One provider

Hosted Jev is disabled by default. It is used only for ambiguous typed route decisions, after deterministic routing and after configured Remote Laya. When enabled, FRIDAY sends only the current request and the route-choice question to `https://api.typesafe.ai/v1/systemone`; the request includes model `jev-latest` by default. Jev decisions cannot initiate COMPUTER actions. Invalid, low-confidence, timed-out, or failed decisions fall back to the existing deterministic route and `BrainManager`.

Set `FRIDAY_JEV_ENABLED=true` and `FRIDAY_JEV_API_KEY` in the environment to opt in. Optional settings are `FRIDAY_JEV_BASE_URL` (defaults to `https://api.typesafe.ai/v1/systemone`), `FRIDAY_JEV_MODEL` (defaults to `jev-latest`), and `FRIDAY_JEV_TIMEOUT_SECONDS` (defaults to `5`). The API key is never stored in `profile.json` or logged. Hosted Jev sends user text off-device; verify account access and pricing before enabling it. This project does not create an account, configure billing, or assume a free tier. See the [TypeSafe System-One API documentation](https://api.typesafe.ai/redoc).
# Tool Bus (Phase 6A)

FRIDAY routes tool calls through `ToolRegistry`, `ApprovalPolicy`, native or MCP adapters, `ToolResult`, and a safe in-memory receipt. COMPUTER adapters continue to use the existing deterministic dispatcher; knowledge search uses `UnifiedKnowledgeManager` and its existing privacy filters, provenance, approved roots, and result limits.

MCP defaults to no configured servers. Phase 6A supports local stdio only; it does not enable remote MCP discovery. Each server must be explicitly configured with an executable, separate arguments, an allowed-tool list, blocked tools, environment allowlist, timeout, and any locally reviewed risk mappings. Credentials are referenced by environment-variable name and are passed only when explicitly allowlisted. Tools without a local risk mapping are treated as consequential and require approval. MCP server risk claims are ignored.

Read-only and ordinary reversible tools may run under policy. Consequential and destructive tools require explicit approval. Private/local-only invocations cannot use tools configured to transmit data. FRIDAY does not register an unrestricted shell or command-execution tool, and it never bypasses operating-system prompts or provider permissions.

PersonalJarvis is an architecture reference only. OpenHuman is not a runtime or dependency. Phase 6B adds bounded in-memory planning and sequential workers; coding handoff metadata does not launch Codex automatically.

## Bounded Planner and Workers (Phase 6B)

Multi-step requests follow `Planner -> PlanValidator -> WorkerExecutor -> ToolRegistry -> ApprovalPolicy -> ToolResult`. Simple COMPUTER actions and one-call knowledge lookups keep their direct routes. Planning uses the existing BrainManager: public plans request SMART, while private/local-only plans use PRIVATE and local Qwen only.

Workers invoke tools only through `ToolRegistry` and its `ApprovalPolicy`; receipts and task events omit arguments and prompt text. Consequential/destructive steps pause with a four-field approval request. Runs support explicit approval/skip, cooperative cancellation between tool calls, deterministic verification, dry-run, and one validated repair attempt. A run is in-memory and bounded to 8 steps, 12 registry calls, one retry per safe step, and one repair. It never spawns another planner or continues indefinitely; unsupported capabilities become BLOCKED. Coding returns handoff metadata without launching Codex; Research stays unavailable until Phase 7A.

Scheduling/background automation is Phase 6C, voice conversation is Phase 9A, and the HUD is Phase 10A. Run offline Phase 6B checks with `python -B scripts/phase6b_smoke.py`; its harmless MCP check uses the existing local stdio test server and makes no external network calls.

Run the offline Phase 6A acceptance and Phase 5 regressions with `python -B scripts/phase6a_smoke.py` from the project root. Its MCP server is test-only and communicates over local stdio.

## Local automations (Phase 6C)

`AutomationManager` stores automation definitions and compact run receipts in SQLite at
`D:\FRIDAY\data\automations\friday-automations.sqlite3`. Set
`FRIDAY_AUTOMATION_DB_PATH` to override the database path. The store initializes
idempotently and keeps timestamps timezone-aware.

An explicit in-process `tick()` checks one-time, interval, daily, weekly, and
local event triggers. Missed recurring intervals coalesce into at most one run;
event and timed occurrences have persistent duplicate protection. There is no
Windows service or automatic startup integration yet; Phase 10B owns that work.
Event providers are local and deterministic; internet monitoring is not included.

Execution follows `AutomationManager -> Scheduler -> AutomationRunner -> Planner /
WorkerExecutor -> ToolRegistry -> ApprovalPolicy`. A single deterministic
computer action may use FRIDAY's existing direct dispatcher, which still uses
ToolRegistry and ApprovalPolicy. Private and local-only plans use the PRIVATE
BrainManager route; private prompt text and tool arguments are not copied into
run history. Consequential and destructive actions wait for explicit approval,
which expires after 24 hours by default. Routine failures do not trigger an
automation retry or replay storm. The bounded queue dispatches work sequentially.

The manager exposes list/get, pause/resume, disable/archive, edit, next-run
inspection, `run_now`, approval, history, and cancellation methods for later
conversation and voice controls. Voice integration is deferred to Phase 9A.
Automation history is operational SQLite data and is not written to the
Obsidian-compatible Knowledge Vault or long-term memory.
