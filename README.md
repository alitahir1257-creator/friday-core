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

FRIDAY classifies capability and privacy locally with deterministic rules. It does not call an LLM to choose another LLM. Unknown tasks default to SMART; greetings and simple rewrite/Q&A tasks use FAST. COMPUTER continues through the deterministic action dispatcher and makes no LLM call. RESEARCH uses the bounded Phase 7A search/fetch/evidence flow below, then synthesizes with BrainManager and approved RESEARCH routing. VISION and VOICE are metadata-only placeholders. Repository edits are handed to Codex and are not silently sent to a chat model.

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

Obsidian desktop is optional: no account, login, plugin, or Sync is required. To use its editor later, open `D:\FRIDAY\vault` in Obsidian as a vault. Public/cloud requests can receive only public vault notes; private and local-only knowledge stays on the local PRIVATE route. No remote provider is used for ordinary vault search. Phase 7C.1 adds an optional local semantic index for persistent SQLite memory; vault and one-off document files are not automatically embedded.

Reserved locations include `D:\FRIDAY\data\research`, `D:\FRIDAY\data\logs`, `D:\FRIDAY\data\cache`, `D:\FRIDAY\models`, `D:\FRIDAY\runtime`, and `D:\FRIDAY\scripts`; existing runtime assets are not moved there automatically.

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

No external knowledge connectors are implemented. Phase 7A research uses its own bounded SearchProvider and evidence pipeline; it does not mix private Memory, Vault, or local-file contents into external queries. Phase 6A adds Tool Registry/MCP; Phase 7B adds PDF/Office/image parsing. Run `python -B scripts/phase5c_smoke.py` for temporary-only checks.

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

Workers invoke tools only through `ToolRegistry` and its `ApprovalPolicy`; receipts and task events omit arguments and prompt text. Consequential/destructive steps pause with a four-field approval request. Runs support explicit approval/skip, cooperative cancellation between tool calls, deterministic verification, dry-run, and one validated repair attempt. A run is in-memory and bounded to 8 steps, 12 registry calls, one retry per safe step, and one repair. It never spawns another planner or continues indefinitely; unsupported capabilities become BLOCKED. Coding returns handoff metadata without launching Codex; Phase 7A research requests resolve to the registered `research.run` tool.

Scheduling/background automation is Phase 6C, voice conversation is Phase 9A, and the HUD is Phase 10A. Run offline Phase 6B checks with `python -B scripts/phase6b_smoke.py`; its harmless MCP check uses the existing local stdio test server and makes no external network calls.

Run the offline Phase 6A acceptance and Phase 5 regressions with `python -B scripts/phase6a_smoke.py` from the project root. Its MCP server is test-only and communicates over local stdio.

## Local-first Research Engine (Phase 7A)

The source-backed flow is `RESEARCH -> ResearchPlanner -> SearchProvider -> ResearchFetcher -> extraction/ranking/deduplication -> evidence -> ResearchSynthesizer -> claims with citations and provenance`. `research.search`, `research.fetch`, and `research.run` are read-only ToolRegistry tools; Planner and AutomationRunner use the existing WorkerExecutor path. Research is bounded to 4 search calls, 8 fetched sources, 1 fetch per source, 2 browser render fallbacks, 2 MB per page, 4 redirects, and a 45 second default run budget.

Live research backends are tried in this order: an explicitly configured SearXNG endpoint, DDGS direct Python library mode using its explicit Bing backend, then the existing browser search fallback. SearXNG and DDGS are optional. Install DDGS into the Python 3.11 venv with `.venv\Scripts\python.exe -m pip install -r requirements-research.txt`; FRIDAY imports it only when a search is attempted, and continues to the browser provider if it is missing or fails. DDGS does not use its API or MCP server, a daemon, Docker, or a browser. Its single backend setting avoids Google and does not attempt to bypass CAPTCHA or other access challenges. Configure `FRIDAY_RESEARCH_SEARXNG_URL` only when an owned SearXNG instance is available. It uses SearXNG's JSON response and preserves title, URL, snippet, engine metadata, and rank. A local instance should bind only to `127.0.0.1`, enable JSON, and disable its limiter; never expose it on the LAN or through a public proxy. Only this explicit search-backend setting may target localhost; `ResearchFetcher` continues to block loopback, private, and metadata URLs.

When SearXNG is unset or unavailable, DDGS is tried first; if DDGS is missing, empty, or unavailable, `BrowserSearchProvider` uses the existing Playwright dependency and installed system Chrome in one temporary headless context. Set `FRIDAY_RESEARCH_BROWSER_SEARCH_ENGINE=duckduckgo` to select the initial supported public engine; this is the default and requires no key, subscription, or paid request. FRIDAY does not download Chromium, keep a research browser running, bypass CAPTCHA or bot challenges, or use hidden search APIs. Challenges and timeouts fail with explicit search statuses. Search snippets are discovery hints only: source text, evidence records, citations, and synthesis come from pages fetched through `ResearchFetcher`. `local_only` requests never search externally, and private query sanitization remains in force. If no configured provider can produce results, `research.run` returns an explicit unavailable, challenge, timeout, or no-results status.

Before an external search, FRIDAY reduces a request to a minimal public query. Private context, Memory, Vault notes, local files, credentials, and conversation history are not added to search or synthesis inputs. If private intent cannot be separated safely, the run fails closed; `local_only` never performs external search or fetches. Public synthesis uses the existing BrainManager RESEARCH policy and local Qwen fallback. Existing optional providers remain governed by their explicit enablement and routing settings.

Each evidence source retains requested/final/canonical URLs, title, domain, publication/retrieval dates, normalized text, content hash, source classification, and a citation ID. Source-derived facts require exact supporting text; model synthesis requires valid source IDs and supporting quotes; unsupported claims remain marked `UNSUPPORTED_UNVERIFIED`. Access walls, CAPTCHA/anti-bot responses, and paywalls are recorded as inaccessible. FRIDAY does not bypass them or download arbitrary binaries. PDFs are identified and deferred to Phase 7B. Research results are not automatically saved to Memory or the Vault; evidence bundles persist only when explicitly requested under `D:\FRIDAY\data\research` or `FRIDAY_RESEARCH_DATA_PATH`.

Run offline deterministic checks with `python -B scripts/phase7a_smoke.py`; it uses an in-process loopback fixture server and mocked search/synthesis, with no public network calls. A live search acceptance needs a configured zero-cost SearXNG endpoint.

## Local documents (Phase 7B)

PDF, DOCX, XLSX, and PPTX parsing uses small format-specific Python libraries installed from `requirements-documents.txt` into the Python 3.11 venv. Install with `.venv\Scripts\python.exe -m pip install -r requirements-documents.txt`. MarkItDown is not used: direct parsers preserve the requested page, heading/paragraph/table, sheet/cell, and slide locations. Docling, OCR engines, OCR models, and vision models are deferred. A textless scanned PDF returns `OCR_REQUIRED`; image-only slides return `VISION_REQUIRED`.

Document roots use the same approved-root, symlink, protected-memory/vault, and read-only checks as local files. By default FRIDAY recognizes `D:\FRIDAY\documents` when that directory already exists. Configure additional private roots in `profile.json` with `"document_roots": ["D:\\FRIDAY\\documents", "D:\\Work\\Agreements"]`; explicitly public source roots may be listed separately as `"public_document_roots": ["D:\\PublicDocs"]`. Roots are not created or scanned automatically. Tool paths are root-indexed relative IDs such as `0:agreement.pdf`; path traversal and links are rejected. Private document actions are routed to local Qwen, fail closed when local Qwen is unavailable, and do not add text to Memory, Vault, knowledge context, or public queries. Cross-document web research is available only for explicitly public roots and public requests; local evidence and web evidence are labeled separately, and only the sanitized user query goes to search.

Parsers are read-only and reject macro or executable attachments. XLSX formulas are returned as text with cached values where available; FRIDAY never evaluates formulas or follows external workbook links. Spreadsheet sum, average, min, max, count, and equal-range comparisons are deterministic. Default bounds are 20 MiB per file, 200 PDF pages, 30 workbook sheets, 100 presentation slides, 100 tables, 50,000 worksheet cells, 100,000 extracted characters, and four comparison documents. Office ZIP parts are checked for unsafe paths, excessive expansion, and embedded executable content. Results preserve `DocumentSourceRef` provenance; opening a document never creates persistent memory. Run the deterministic acceptance script with `.venv\Scripts\python.exe -B scripts\phase7b_smoke.py`.

## Lightweight Semantic Memory (Phase 7C.1)

SQLite remains the authoritative memory store and FTS5 remains the keyword-search and failure fallback. Model2Vec produces local CPU embeddings; `sqlite-vec` stores/query-ranks those vectors in the same SQLite database, linked by existing memory IDs. The semantic metadata table stores hashes and scope fields, not a second copy of memory text. Hybrid retrieval deduplicates by memory ID and retains existing namespace, privacy, expiry, status, secret, and ranking filters. Semantic state is reported as `AVAILABLE`, `UNAVAILABLE`, or `DEGRADED`; semantic failures leave FTS5 usable.

The optional pinned packages are `model2vec==0.9.0` and `sqlite-vec==0.1.9`. Install them into the Python 3.11 environment with `.venv\Scripts\python.exe -m pip install -r requirements-memory.txt`. The selected model is `minishlab/potion-base-4M` (English-focused, 128 dimensions; the downloaded local snapshot is about 31.15 MB / 29.71 MiB). Download it once to `D:\FRIDAY\models\embeddings\potion-base-4M` with:

```powershell
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='minishlab/potion-base-4M', local_dir=r'D:\FRIDAY\models\embeddings\potion-base-4M')"
```

Constructing `MemoryManager` does not load the model. The first semantic query or an explicit memory write/update that needs an embedding loads it from that local directory. FRIDAY runtime does not fetch models or call embedding services. `MemoryManager.close()` releases the model reference; no idle timer or embedding daemon runs. PRIVATE embeddings, indexing, and lookup stay local.

Set `FRIDAY_SEMANTIC_DISABLED=1` to force FTS5-only retrieval temporarily; writes remain authoritative in SQLite and can be indexed later with the explicit backfill command after the flag is unset.

Semantic backfill is explicit and bounded; it never runs at startup. Run `.venv\Scripts\python.exe -B scripts\phase7c1_backfill.py --batch-size 32` after installing dependencies and the model. Batches are limited to 1–256 active, unexpired rows, commit independently, print counts only, and can be safely rerun after interruption. Existing SQLite memory remains readable during backfill. Secret-detected rows are not embedded. Phase 7B documents and connector files remain ephemeral unless separately saved through the existing authorized memory API.

The English model is not a multilingual-quality guarantee. The single Phase 7C.1 Roman Urdu probe result is recorded by `scripts/phase7c1_smoke.py`; broader Roman Urdu coverage remains unmeasured. Run that offline temporary-database acceptance script to check FTS5 fallback, semantic/hybrid behavior, privacy and lifecycle rules, resumable backfill, resource measurements, and Phase 7B/7A/6B/6A/5 regressions. No large Lighthouse/browser stack, database service, Docker, WSL, or background process is required.

### API / MCP discovery reference (Phase 7A.1)

[public-apis/public-apis](https://github.com/public-apis/public-apis) is a discovery/reference catalog only, not a trusted runtime provider, tool manifest, or installer. Its community-maintained entries and metadata can be stale; inclusion does not establish that an API is currently available, free, safe, or suitable. FRIDAY does not import the catalog, register its entries as tools, install listed MCP servers, or call listed APIs automatically.

For an individually reviewed candidate, record its name, category, description, authentication requirement, HTTPS and CORS metadata where listed, and MCP transport/install metadata where applicable; preserve unknown values as unknown. Verify current provider documentation, availability, cost and limits, terms, security, and data handling before implementation. Phase 7D owns selected API/MCP integrations, each with its own explicit configuration and security review.

### Vetted public API connectors (Phase 7D)

The catalog remains discovery/reference only. FRIDAY currently registers three fixed, read-only capabilities; it does not import catalog entries, install SDKs, or discover arbitrary API hosts at runtime. Each provider call sends only the parameters listed below. All require HTTPS and no API key or account.

| Provider | Capability | Free-use status and limits | Data sent |
| --- | --- | --- | --- |
| [Open-Meteo](https://open-meteo.com/en/docs) | Current weather and up to 7 forecast days | No key. Free for non-commercial use; published free limits are 600/minute, 5,000/hour, 10,000/day, and 300,000/month. Commercial use requires a paid plan. No uptime SLA; attribution is required under CC BY 4.0. See [pricing and terms](https://open-meteo.com/en/pricing). | Place name for geocoding, then coordinates and fixed weather fields. |
| [Frankfurter](https://frankfurter.dev/) | Daily reference FX rates and conversions | No key, published daily/monthly quota, or payment requirement; abusive traffic may be throttled. Rates update daily and are not for live trading. Upstream rate-source terms still apply. | Three-letter currency codes only. |
| [Nager.Date Community API](https://nagerholidays.com/api) | Public holidays by ISO country code and year | No key. Terms allow private/non-profit use; commercial API use requires sponsorship. Docs publish no request limit and disclaim availability/accuracy. Coverage varies; Pakistan and Saudi Arabia are not listed as supported in the current [coverage list](https://nagerholidays.com/country/coverage). | ISO country code and year only. |

Open-Meteo and Nager.Date are enabled only for use allowed by their current free-use terms. In particular, do not use either for commercial workloads without the provider's required paid plan or sponsorship. Frankfurter provides daily reference rates, not trading quotes. These services can change; re-check the linked official documentation before changing this set.

REST Countries was evaluated and left out: its current API requires an API key and its free plan is limited to prototyping/evaluation and non-commercial use. No country-metadata connector is enabled in this batch. `public-apis/public-apis` was used as a catalog lead only; provider documentation and terms were checked independently.

`public_api.weather`, `public_api.currency`, and `public_api.holidays` are exposed through ToolRegistry and selected by the Planner for matching structured requests. A provider failure produces a normalized error and permits at most one explicit, bounded, public `ResearchEngine` fallback. The fallback never runs in PRIVATE or local-only mode. Public API tools are excluded from private plans; private memory, vault, documents, credentials, and unrelated conversation context are not sent. API responses carry provider, capability, request time, source endpoint, provider update time where available, and a network/cache indicator. Raw provider payloads are reduced to bounded fields.

The HTTP client uses FRIDAY's existing DNS-pinned fetcher, fixed HTTPS host/path contracts, a 6-second timeout, a 256 KiB body limit, JSON-only parsing, and no redirects, cookies, compressed responses, or retries. It normalizes transport, provider, rate-limit, malformed JSON, and policy failures. Public responses may use a bounded 64-entry in-memory cache with short capability-specific TTLs; there is no persistent cache, polling, or background service. Diagnostics are on demand and record registration plus last success/failure.

Run `.venv\Scripts\python.exe -B scripts\phase7d_smoke.py` for deterministic safety/ToolRegistry/Planner checks, separate live provider probes, and Phase 7C.2 plus its nested regressions. A live network failure is reported as BLOCKED, separately from deterministic code checks.

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

## Verified experience learning (Phase 7C.2)

`MemoryManager.experience` stores incidents, attempts, resolutions, corrections,
and playbooks in the existing `D:\FRIDAY\data\memory\friday-memory.sqlite3`.
Capture is explicit through `start_incident`, `record_attempt`,
`record_attempt_result`, and `resolve_incident`; chat or model statements are
never imported automatically. A resolution is **verified** only when a trusted
caller supplies a test, build, runtime, deterministic tool, or user confirmation
method with an observed result and a concise evidence reference. Vague claims
such as “looks fixed” remain `UNVERIFIED` and cannot make playbooks.

Failed attempts stay linked to their incident and appear only as warnings.
`promote_playbook` requires a verified reusable resolution plus explicit trigger,
diagnostics, prerequisites, successful steps, and verification steps. Identical
playbooks share source incidents by content hash; different or contradictory
evidence remains separate. A single failed attempt does not ban an approach
for another environment. `supersede_playbook` links the replacement and hides
the previous recommendation. `correct_incident` records the user's correction,
invalidates prior verification, and removes affected recommendations.
`forget_incident` hides the structured incident and removes its semantic memory
entry; linked playbooks are invalidated. Historical rows remain in SQLite for
audit until a separate deletion policy is approved.

Verified resolution and playbook text is indexed by the existing local
Model2Vec/sqlite-vec memory index. FTS5 and structured lexical matching remain
available when semantic retrieval is unavailable. Retrieval filters by namespace,
privacy, and project for private records; remote routes see public records only.
Planner receives a bounded evidence excerpt with failed attempts labelled as
warnings. Stored logs, webpages, and experience text are untrusted data and
are never executed. Evidence excerpts are limited to concise text rather than
whole logs. Run `.venv\Scripts\python.exe -B scripts\phase7c2_smoke.py` for
offline fixtures and nested Phase 7C.1 through Phase 5C regressions. The single
Roman Urdu probe measures only that phrase, not general multilingual quality.

Phase 7C.2 does NOT autonomously install software or modify FRIDAY itself.

## Vision Lite (Phase 8A.1)

Lightweight on-demand screen understanding for the future computer-control
planner. No daemon, no continuous capture, no screen recording, no heavy
vision model. Near-zero idle cost: backends construct lazily and release.

Architecture:

```
Windows screen
  |-- existing UI Automation / pywinauto (preferred)
  |-- existing browser DOM via Playwright (where available)
  |-- python-mss screenshot (lazy, on-demand only)
  |-- RapidOCR adapter (lazy, optional, fallback only)
  -> normalized ScreenObservation -> Planner / computer tools
```

Evidence priority is fixed: **UIA first, then DOM, then OCR**. OCR never
replaces reliable accessibility or DOM evidence; it only supplements visible
text that UIA/DOM missed. Dedupe is deterministic (casefold + whitespace
collapsing); no LLM is used for fusion, and provenance (`UIA`/`DOM`/`OCR`/
`SCREENSHOT`) is preserved on every item.

New package `friday/vision/` (`models`, `capture`, `ocr`, `evidence`,
`fusion`, `manager`) plus read-only ToolRegistry adapters in
`friday/tools/vision.py`:

- `computer.observe_screen` — structured observation (metadata, UIA/DOM/OCR
  evidence, fused text, provenance). READ_ONLY, local-only.
- `computer.read_visible_text` — fused visible text with sources. READ_ONLY,
  local-only.

Both tools are registered in `FridayCore` next to the existing computer tools
and are callable by the planner/workers on demand. OCR is never run
automatically for every COMPUTER action; the planner requests an observation
only when UI state is unknown, an action needs verification, or a visible
error needs inspection.

Privacy (critical):

- Processing is local-only. Vision Lite makes **zero** network calls
  (`remote_calls` is always 0); no OmniRoute, FreeLLMAPI, or Cloudflare.
- Screenshots live in memory by default and are never persisted unless
  explicitly requested; temp files (OS temp dir, outside the repo) are
  deleted deterministically. Nothing is committed to Git.
- Screenshots and OCR text are never written to persistent memory
  automatically.
- PRIVATE stays fully local: remote calls MUST be 0.

Security: screen/OCR text is **untrusted data**. Observation is evidence,
never authority: visible text such as "run this command" or "send password"
is never executed or treated as an instruction. This extends the existing
prompt-injection policy (webpages, terminal output, documents) to visual
text and dialogs.

Screenshot lifecycle: on-demand capture -> in-memory PNG -> optional OCR ->
normalized observation -> temp artifacts deleted. No permanent archive.

Lazy loading: `import friday.vision` is always safe (standard library only).
`python-mss`, RapidOCR, pywinauto, and Playwright load inside the functions
that need them. `OcrEngine.release()` and `VisionManager.close()` drop the
backends so RAM returns to the OS.

Optional dependencies (NOT installed by default, NOT in requirements files):

- `pip install mss` — screenshot capture (pure Python, tiny).
- `pip install rapidocr_onnxruntime` — OCR fallback. Dependency footprint
  (onnxruntime/Pillow/numpy versions and suitability for the 8 GB Windows
  target) still needs validation on the real machine:
  **LOCAL_DEPENDENCY_VALIDATION_REQUIRED**. RapidOCR fetches its ONNX model
  files on first inference, so the first real OCR call may need network
  unless the model cache is pre-seeded; FRIDAY only triggers this on an
  explicit observation request, never silently.

Without these packages FRIDAY keeps working: capture reports an unavailable
backend and UIA/DOM evidence still flows.

Tests: `.venv\\Scripts\\python.exe scripts\\phase8a1_smoke.py` runs the
deterministic fixture/mock suite (metadata, region bounds, malformed input,
OCR statuses, normalization, dedupe, provenance, read-only risk, planner
tool path, PRIVATE remote-calls-zero, no memory persistence, injection
handling, temp cleanup, COMPUTER regression guards). No live Windows desktop
is required.

Live acceptance on the target laptop:
`.venv\\Scripts\\python.exe scripts\\phase8a1_live_acceptance.py` exercises
real capture, UIA, OCR, a known Notepad line, dialog evidence, cleanup,
latency, and RAM. Results from GitHub alone must be reported as
**LOCAL_ACCEPTANCE_REQUIRED**, never as passed.

Limitations: no coordinate fallback yet (Phase 8A scope), no visual
targeting model, OCR quality unvalidated on the target machine, no video.
