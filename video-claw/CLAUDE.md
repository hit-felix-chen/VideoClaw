# CLAUDE.md

Guidance for working in this skill directory. Claims below were checked against the backend on commit `0c6f9f4`. There is no orchestrator and no `/api/project` router.

## Project Overview

Video-Claw 2.0 is an asset-driven dependency graph plus a local asset library. A project is a row in SQLite, not a session file. Episodes group assets; they are not graph nodes. Nodes are assets. An edge points from the asset that must exist to the asset that depends on it.

`video-claw/` (this directory) is an OpenClaw agent skill:

- `video-claw/` — FastAPI backend and Next.js frontend
- `SKILL.md` — rules for the agent that drives the local API
- `references/` — how to start the stack and how to call the workflow
- `video-claw/backend/docs/api.md` — the HTTP routes that exist on this commit

Defaults from `config.py`: backend `127.0.0.1:8000`, frontend dev server `3000`. The API process only binds the host in `config.yaml` (`server.host`, default `127.0.0.1`).

## Commands

### Backend

```bash
cd video-claw/backend
python api_server.py
```

`api_server.py` is the uvicorn entry. It loads `api.app:app` (`title="Video-Claw"`, `version="2.0.0"`).

### Frontend

```bash
cd video-claw/frontend
npm install   # first time only
npm run build
npm start
```

### Health

```bash
curl http://127.0.0.1:8000/api/health
```

### Tests

The suite does not call a real model or ffmpeg. From this skill directory:

```bash
cd video-claw/backend
python3 -m pytest
```

From the repository root: `cd video-claw/video-claw/backend && python3 -m pytest`. Dev dependencies are the `dev` extra in `video-claw/backend/pyproject.toml` (`pytest`, `pytest-asyncio`). `.github/workflows/tests.yml` runs that command and `npx tsc --noEmit` in `video-claw/frontend`.

## Architecture

### Asset graph

`graph/dependency_graph.py` and `graph/model.py`.

Known `AssetKind` strings (any non-empty string is accepted, so a plugin can add a node): `script`, `character`, `scene`, `shot_segment`, `final_video`, `previz`, `keyframe`, `draft_video`.

Edge kinds, all of which can block: `hard`, `ref`, `continuity`, `inherit`. An edge blocks while `blocks` is true and the predecessor is not `done`.

Stored node statuses and the only legal transitions (`NodeStatus`, `graph/model.py`):

| From | To |
|------|----|
| `pending` | `running`, `skipped` |
| `running` | `done`, `failed`, `awaiting_user`, `pending`, `stale` |
| `awaiting_user` | `done`, `failed`, `running`, `pending`, `stale` |
| `done` | `stale`, `pending` |
| `stale` | `running`, `pending`, `skipped` |
| `failed` | `running`, `pending`, `stale` |
| `skipped` | `pending` |

`ready` is not a graph status. `DependencyGraph.executable_nodes` computes it: status is `pending`, `stale`, or `failed`, every blocking predecessor is `done`, and every episode this node's episode explicitly depends on is complete. `why_blocked` returns `{node_id, executable, reasons}` with reason types `status`, `predecessor`, and `episode`.

The SQLite `node_state` table does allow `ready`. `ProjectBudget.raise_cap` writes that value after a cap increase. The graph snapshot does not.

`GraphStore` persists `DependencyGraph.to_dict()` on `project.graph_json`. `POST /api/v2/projects` writes one fixed episode (`ep1`: script, storyboard, character, keyframe reference, shot_segment video, tts). There is no general route that replaces an arbitrary graph. A project created only with `POST /api/library/projects` has no `nodes`, and `GraphExecutor` still returns `project has no dependency graph`. `GET /api/v2/projects/{id}/status` returns `edges` plus per-node `node_state`, and for an awaiting video node `quote`, `duration`, and when a ledger row is waiting `entry_id` and `budget_hold`. `edge` and `node_state` stay a separate relational shape; saving the JSON does not fill those tables. `provider_called: false` is stored on the node metadata (and on `metadata.result`) and repeated by `status`. A true value is not written back onto that field.

The library uses a different kind list (`storage/schema.py`): `script`, `character`, `setting`, `prop`, `segment`, `final_cut`, scope `global` or `project`. `SegmentRefStore` requires the segment asset's library kind to be `segment`.

### GraphExecutor.run_node

`core/executor.py`. HTTP wrappers live in `api/routers/executor.py` under `/api/v2/projects/...`.

`run_node` loads the graph, refuses a blocked node (`status: "blocked"`, no ledger write), then dispatches by modality:

`_modality` uses an explicit `modality` on the request or the node metadata when it is `video`, `image`, `vlm`, or `tts`. Otherwise it classifies by kind.

| Kind / modality | Gate |
|-----------------|------|
| `shot_segment`, `draft_video`, `final_video`, `segment`, `video`, or modality `video` | Video confirm. Quote, then wait at `awaiting_user` / `gate: "confirm"`. The ledger row is written only in `confirm`, after `ProjectBudget.authorize`. The settled actual is `quote.max`. |
| `character`, `scene`, `setting`, `keyframe`, `image`, or modality `image`; kind or modality `vlm` or `tts` | No per-call confirm. `run_node` calls `ProjectBudget.authorize` immediately. When that is allowed, `_finish` writes `actual` = `quote.max` and marks the node `done` with `spent: true`. That charge is written even when the runner returns `provider_called: false` (the HTTP body keeps `provider_called: false`). Over budget pauses at `gate: "budget"` and does not run the node. |
| `script`, `storyboard`, `previz` | No quote and no ledger row. The runner result is stored and the node is marked `done`. |
| any other kind with no explicit modality | `status: "unsupported"`, message `node has no billable modality`. Library kinds `prop` and `final_cut` land here. `character`, `scene`, `setting`, `keyframe`, and `image` do not. An explicit modality on one of these kinds follows the row above. |

An empty video `model_id` becomes `doubao-seedance-2-5-260628` (`DEFAULT_REFERENCE_VIDEO_MODEL`). That is the reference-to-video default. It is not `config.yaml` `models.video` (that key still defaults to `wan2.7-i2v`).

`core/nodes/dispatch.py` picks the node runner. Those `execute_node` methods return `provider_called: false` and do not open a vendor connection. `GraphExecutor._submit` rejects a runner that sets `provider_called` true. The vendor HTTP call for video is `adapter.submit` after authorize, not the agent.

Dispatch:

- video modality or video kinds above → `VideoDirectorAgent.execute_node` (builds a reference-to-video request)
- `character` → `CharacterDesignerAgent.execute_node`
- other image kinds → `ReferenceGeneratorAgent.execute_node`
- `tts` → local stub, model id `edge-tts` when omitted
- `script` → `ScriptWriterAgent.execute_node`
- `storyboard` → `StoryboardAgent.execute_node`
- else → generic stub

`VideoEditorAgent.execute_node` is not on this dispatch path. A `final_video` node is treated as video.

`regenerate` reruns that one node (`done` goes to `pending`; `stale` or `failed` is revised) and leaves successors stale. It does not start them.

### Quote, confirm, budget, ledger

`models/middle/pricing.py` `quote`, `models/middle/budget.py` `ProjectBudget`, `storage/ledger.py` `LedgerStore`.

Order for a video node:

1. `run_node` prices the call and stores `pending_call` on the node. Status becomes `awaiting_user`, `gate` is `confirm`. No ledger row yet. `over_budget` is a preview against the project cap.
2. `confirm` claims the node (`awaiting_user` → `running`, `BEGIN IMMEDIATE`) and calls `ProjectBudget.authorize`. That is the first ledger write for the call.
3. Authorize either reserves (`ledger.status = quoted`, `allowed` true) or pauses (`awaiting_user`) when `used + committed + quote.max` would pass `budget_cap`. A null cap never pauses.
4. A reserved quote is spent with `ProjectBudget.record_actual` at `quote.max` in the same transaction as `done`. A URL is not required. When `provider_called` is true and the result is a URL or path, that URL is committed as `result_obtained` first; the spend still uses `quote.max` if there is no URL. The HTTP routes `confirm`, `retry_video_submit`, and `raise` return 409 (`未配置视频厂商密钥，不会提交也不会入账`) before `confirm` when no adapter is injected and the provider key is empty, so they do not write a ledger row. In-process `GraphExecutor.confirm` with no adapter still spends `quote.max`.

Image kinds (`character`, `scene`, `setting`, `keyframe`, `image`), VLM, and TTS skip step 1's wait: `run_node` calls `authorize` immediately and, when allowed, spends `quote.max` even if `provider_called` is false. Over budget returns `gate: "budget"` and does not run the node. Raising the cap (`raise_budget`) continues the same quote. Declining (`decline_budget`, or `decline` on the node) sets the ledger row to `skipped` and skips the node.

Pause text is built by `pause_message`. Each of the three numbers gets a ` {currency}` suffix when the quote has a currency (a leading space, then the code). Equal min and max are printed as one number, not a range: `已用 {used} {currency} / 本次报价 {min} {currency} / 当前上限 {cap} {currency}。是否上调预算上限？` A range is `{min}~{max}` in the middle slot, still with the suffix. No currency means no suffix.

Ledger statuses: `quoted`, `awaiting_user`, `skipped`, `spent`, `unavailable`.

A model with no documented API price is not given a number. `documented()` requires `verified`, a currency, and an `http(s)` `source_url`. Amounts copied from `MODEL_CONFIG` fail that test. `quote` then returns `available: false`, `min`/`max` null, and lists the id in `unknown_models`. `ProjectBudget.authorize` writes `unavailable` and does not submit. Message when the model is unknown: `没有文档价格，不计算报价，不提交调用。`

Seedance 2.5 does have documented CNY prices (`per_million_tokens`, see Model registry). The executor still will not invent a billed length from a caller `duration`. See shot duration below.

### ImpactConfirmation

`storage/confirmations.py`. A look-impact token is one row in `impact_confirmation`: one project, the exact downstream node id list, TTL `IMPACT_TOKEN_TTL_SECONDS` (30 minutes), single-use `claim`.

- `POST .../looks/{look_id}/impact` builds a `RegenerationQuote` (does not spend) and replaces that quote's hash token with `ImpactConfirmation.issue`.
- `POST .../impact/confirm` claims the token and runs the stored node set through `run_node`. A video node then `confirm`s inside that batch. The first node that is still awaiting the user stops the rest, so a later node is not reserved in that call. A runner failure releases that node's unsettled quote and stores the failure on the consumed token.
- `POST .../impact/regenerate` reruns a subset. Unknown nodes, nodes outside the set, and a missing, expired, stale, or already-consumed token fail before any node runs and before any ledger write.

The library routes `POST /api/library/looks/{look_id}/regenerate-quote` and `regenerate-confirm` are a different token: a sha256 of the quote payload. Confirming that token records acceptance and still does not generate or spend.

Pinned nodes are listed by graph `impact` and are not regenerated. Pending and skipped nodes are absent from that list. An already-stale node is included.

### SegmentRefStore

`storage/references.py`. Binds a library asset of kind `segment` to another asset and, when set, one look of that asset. `resolve` finds `@资产名` and `@资产名/形象名` in text. Matching is exact. With a `project_id`, a project asset wins over a global asset of the same name. Other projects are not searched. Two visible matches stay `ambiguous` instead of picking one.

`VideoDirectorAgent.execute_node` turns resolved mentions into reference asset ids, in mention order, first match only. It does not search file-name prefixes. `GraphExecutor` turns those ids into on-disk image files immediately before `adapter.submit`. A missing file skips the quote and leaves the node awaiting the user. The asset id is not sent as a URL.

### Model registry and keys

`models/middle/registry.py`, loaded from `models/config_model.py` `MODEL_CONFIG`, then builtin `SpecCatalog.seedance_25()` and `SpecCatalog.edge_tts()`, then `models/middle/official.py`.

Config defaults (`config.py` `DEFAULT_CONFIG["models"]`):

| Key | Default |
|-----|---------|
| `llm` | `deepseek-flash` |
| `vlm` | `qwen3.7-plus` |
| `image_t2i`, `image_it2i` | `doubao-seedream-5-0-260128` |
| `video`, `video_first_frame`, `video_start_end` | `wan2.7-i2v` |
| `video_reference` | `doubao-seedance-2-5-260628` |

`generation.video_generation_mode` still defaults to `first_frame`. The graph video path does not read that key. `VideoDirectorAgent.execute_node` sets `ability` to `reference_to_video`. Submit uses `generation_mode` `reference` when reference files exist, otherwise `text`.

`OpenAICompatLLM.DEFAULT_MODEL` is `deepseek-flash`. Every LLM provider goes through `OpenAICompatibleAdapter.complete` (`chat.completions`). `vlm_*.py` is still the VLM path; the LLM client does not import it.

API keys are `api_providers.<provider>.api_key` for `openai`, `gemini`, `deepseek`, `moonshot`, `dashscope`, `ark`, and `kling`. `Config.ARK_API_KEY` is `api_providers.ark.api_key`. Kling is one `api_key`, not an access-key/secret pair. Video submit treats a provider as configured only for `ark`, `dashscope`, and `kling` (`_provider_configured`). Seedance 2.5 resolves to `ArkVideoAdapter`.

Aliases (`models/middle/ids.py`): `doubao-seedance-2.5` → `doubao-seedance-2-5-260628`. `deepseek-4.1-flash` is a display name for `deepseek-flash`, not a published API id.

### Pricing

`PriceBook` is the only price source. A row is an API price only when it is verified, has a currency, and has an http(s) source URL. Otherwise the model is unknown and the quote has no min/max.

Seedance 2.5 (`SpecCatalog.seedance_25`) prices are CNY per million tokens: 480p/720p without video input 70, 1080p without video input 77, 480p/720p with video input 42, 1080p with video input 46. The stored formula is `(input_video_seconds + output_video_seconds) * width * height * fps / 1024`. The executor does not evaluate that formula. Image quotes use 1..6 calls per asset (`MIN_IMAGE_CALLS_PER_ASSET` / `MAX_IMAGE_CALLS_PER_ASSET`). `unit_price is None` returns no range, not zero.

`edge-tts` is registered with an empty price list.

### Shot duration

A shot length is used only when it sits inside that model's verified `DurationLimit` (`video_agent._capability_duration`). The request field `duration` on `NodeRunRequest` is accepted and ignored for video nodes. The length comes from `node.metadata.shot_duration`.

Verified ranges on this commit (`capabilities.duration.verified: true` in `MODEL_CONFIG`, plus the builtin Seedance 2.5 spec):

| Model id | min | max |
|----------|-----|-----|
| `doubao-seedance-2-5-260628` | 4 | 30, integer, `default` -1, `allow_auto` true |
| `doubao-seedance-2-0-260128`, `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-mini-260615` | 2 | 12 |
| `wan2.6-i2v-flash`, `wan2.7-i2v` | 2 | 15 |
| `happyhorse-1.0-i2v`, `happyhorse-1.1-i2v`, `happyhorse-1.0-r2v`, `happyhorse-1.1-r2v`, `kling-v3` | 3 | 15 |
| `wan2.7-r2v` | 2 | 10 |

`kling-v2-6`, `kling-v2-5-turbo`, `seedance-1-0-pro`, and `seedance-1-0-lite` have no verified duration on the spec. Unverified limits are not a billed length and are not sent.

For Seedance 2.5, a shot length inside 4..30 is sent as that integer. Outside the range, submit uses `-1` because `allow_auto` is true. Quote quantity uses `duration.default` only when that default is an integer greater than 0. `-1` is not a length, so a Seedance 2.5 quote has no quantity unless the caller passes `completion_tokens`. The per-million-token price then cannot be scaled, `run_node` returns `unavailable`, and no ledger row is written. The node stays `pending`.

### Video submit markers

Constants: `VIDEO_SUBMIT_CLAIM_TTL_SECONDS = 30 * 60`, `video_seedance.SUBMIT_DEADLINE_SECONDS = 20 * 60`.

`confirm` claims the node before authorize. Two concurrent confirms: one wins; the loser is rejected and does not authorize or submit.

Before `adapter.submit`, the same claim writes node metadata `submit_dispatched: true` and commits it. `on_task` then stores `vendor_task_id` (and keeps the marker). A result URL is written to `result_obtained` and committed before the spent-and-done transaction.

On the HTTP routes, a missing provider key and no injected adapter is a 409 before authorize: no POST, no `submit_dispatched`, no ledger row. In-process `GraphExecutor.confirm` still returns from `_video_submitter` before the marker write, does not POST, and `_finish` spends `quote.max` with `provider_called` false.

The marker is cleared only after it was written and `submit` then raises something `_request_was_not_sent` accepts: `FileNotFoundError`, `UnsupportedParam`, `SubmitNotSent`, or an error string containing `reference image file is missing`, `model is not registered`, or `ARK_API_KEY not set`. `ARK_API_KEY not set` is an adapter that already entered `submit`, not the HTTP 409. On this path the quote is skipped, the node returns to `awaiting_user`, and a plain `confirm` does not POST again. Poll and download errors, vendor `failed` or `expired`, every HTTP 4xx, timeouts, and any other exception keep both `submit_dispatched` and `vendor_task_id`. A 4xx body that contains an id is stored as `vendor_task_id` before the error is raised. `stop` refuses a node once `submit_dispatched` is stored and does not cancel the vendor call.

`POST /api/v2/projects/{id}/nodes/{node_id}/retry_video_submit` calls `confirm(..., retry=True, force_new=...)`. The default is resume: the marker stays set, a stored `vendor_task_id` is polled again, and the original quote is spent, including when the claim is stale. A stored `result_obtained` URL is settled with no new POST. `force_new` becomes true only for JSON `true` or the string `"true"` (Pydantic coerces that string to true). That path releases the old quote, authorizes a new one, and drops `vendor_task_id` only after that authorization succeeds, then POSTs. If the new quote does not fit the budget, the id and marker stay (`force_new_pending`) and `raise_budget` can continue that attempt. Any `force_new` on `confirm` (query or JSON, including `false`) is HTTP 400. JSON `null` is 422 because the field is `bool`. The in-process method still raises `force_new is only valid on retry_video_submit` if `force_new` is set without `retry`.

After 30 minutes, `recover_stale_submit_claims` and the next `confirm` do not submit a dispatched attempt again. A stale `running` row with no marker died before the vendor call: its unsettled quote is skipped and the node can be claimed again. A claim newer than the TTL is left alone. `api/app.py` calls `recover_stale_submit_claims` once at startup. A start with nothing past the TTL returns an empty list; a second start does not submit those claims again. The next `confirm` after the TTL still reclaims if startup did not.

`raise_budget` refuses while `submit_dispatched` or `vendor_task_id` is set, unless this pause is the budget exit of `force_new`.

With no injected adapter and no provider key, confirm still stores the runner's prepared request (asset ids, not files) and spends the reserved quote. That path does not call the vendor.

`adapter.submit` for Ark submits, polls, downloads to `save_path` (`code/result/video/<project_id>/<node_id>.mp4`), and returns the remote URL. `result_id` is that URL. A bare task id is not a finished result. The Seedance client stops the whole submit at 20 minutes so the poll cannot outlive the 30-minute claim (the raw poll cap is about 74 minutes: 120 rounds, 30-second HTTP timeout, 5-second pause).

## Persistence

- Database: `video-claw/backend/code/data/videoclaw.db` (WAL). `Database.connect` creates the directory. Legacy `code/data/sessions/` is not the v2 store.
- Blobs: `code/data/blobs/<sha256[:2]>/<sha256>.<ext>`. The database stores hash, mime, and kind.
- Static files: `GET /code/...` mounts `backend/code`.
- Sandbox history: `backend/code/result/sandbox/history.json`. Sandbox routes call providers directly and do not pass through `ProjectBudget`.

## Key references

- [SKILL.md](SKILL.md) — agent workflow against the v2 routes
- [docs/api.md](video-claw/backend/docs/api.md) — methods, paths, and bodies taken from the routers
- [references/workflow/](references/workflow/) — project, graph, run, confirm, budget, impact, segment refs, upload
