# Video-Claw API（v2）

本文对照 main `faf5988` 列出 `api/app.py` 实际注册的 HTTP 路由，并写上该提交之后无密钥视频守卫的行为。路径、方法、查询参数和请求体与路由器、Pydantic 模型一致。没有 `/api/project`，也没有六阶段 `execute` / `continue` / `intervene` / `artifact`。

**基础 URL**：`http://127.0.0.1:8000`（`config.yaml` 的 `server.host` / `server.port`，默认 `127.0.0.1` 与 `8000`）。

当前版本无认证。CORS 允许任意来源。

库路由和执行器路由都用 `@bind_db`：每个请求在处理线程里 `Database.connect` 打开 `videoclaw.db`，返回前关闭。`get_library_connection` 本身不打开 SQLite，只让测试指定数据目录。进程启动时 `api/app.py` 的 lifespan 调用一次 `GraphExecutor.recover_stale_submit_claims`。没有超过 30 分钟的 `running` 视频认领时，这次调用什么都不改；已经回收过的认领不会被第二次启动再提交。

---

## 错误

| 来源 | HTTP | 条件 |
|------|------|------|
| 库路由 `library.py` | 404 | 项目、资产、形象、形象版本、图片、片段引用不存在 |
| 库路由 | 409 | 形象父级成环（`LookCycleError`） |
| 库路由 | 400 | 其他 `StorageError`（种类非法、缺字段、引用不匹配等） |
| 执行器路由 `executor.py` | 404 | 项目、图节点、形象不存在。删除一个已经不存在的项目也是 404 |
| 执行器路由 | 400 | `GraphError` 或 `StorageError`（含门禁 `_GateError`）。`confirm` 的查询参数或 JSON 里只要出现 `force_new`（包括 `false`）也是 400，`detail` 为 `force_new is only valid on retry_video_submit` |
| 执行器路由 | 409 | 删除时节点为 `running`，或 metadata 已有 `submit_dispatched` / 非空 `vendor_task_id`，或 `node_state` 为 `running`：`项目有尚未结束的提交，不能删除`。`stop` 一个正在运行的节点都没停成、也没有按完成入账（取消被拒、厂商不支持、超时或结果不确定、非视频节点已有厂商标记、没有任务号、没有密钥因而无法向厂商确认）：409，`detail` 是 `没有节点被停止` 加上中文原因。明确取消成功，或发现任务已完成并入账时，不是这条 409。视频节点的 `confirm`、`retry_video_submit`、`raise`、`impact/confirm`、`impact/regenerate` 在没有注入适配器且该模型供应商密钥为空时：`未配置视频厂商密钥，不会提交也不会入账`（执行器 `_NoVendorKey`，授权前抛出）。认领比较失败（`_ClaimLost`）：`状态已变化，请刷新`。若当时节点仍是 `running` 且没有 `submit_dispatched` / `vendor_task_id`，会先写回 `awaiting_user` 并释放报价 |
| 执行器路由 | 422 | `confirm` 或 `retry_video_submit` 的 JSON 把 `force_new` 设成 `null`。字段类型是 `bool`，Pydantic 拒绝 `null`，请求进不了处理函数 |
| 上传 | 400 | 扩展名不在允许列表 |
| 上传 | 500 | 落盘失败 |
| FastAPI | 422 | 查询参数或 JSON 体不符合模型（例如 `budget_cap` 缺失、`media_type` 不在枚举内） |

错误体是 FastAPI 的 `{"detail": ...}`。临时工作台多数失败不抛 HTTP 错误，而是 `{"success": false, "error": "..."}`。

静态目录：`backend/code` 挂在 `GET /code/...`。

---

## 服务

### `GET /`

```json
{"service": "Video-Claw", "version": "2.0.0", "health": "/api/health"}
```

### `GET /api/health`

```json
{"status": "ok", "timestamp": 0.0}
```

`timestamp` 是 `time.time()` 的浮点秒。

---

## 配置

键名以 `config.py` 的 `DEFAULT_CONFIG` 为准。供应商密钥是 `api_providers.<name>.api_key`，`name` 为 `openai`、`gemini`、`deepseek`、`moonshot`、`dashscope`、`ark`、`kling`。可灵只有 `api_key`，没有 `access_key` / `secret_key`。`Config.ARK_API_KEY` 就是 `api_providers.ark.api_key`。

`models` 默认值：`llm` = `deepseek-flash`，`vlm` = `qwen3.7-plus`，`image_t2i` / `image_it2i` = `doubao-seedream-5-0-260128`，`video` / `video_first_frame` / `video_start_end` = `wan2.7-i2v`，`video_reference` = `doubao-seedance-2-5-260628`。`generation.video_generation_mode` 默认 `first_frame`。图执行器在视频节点未给 `model_id` 时用的是 `video_reference` 这一个 id（Seedance 2.5 参考生视频），不读 `models.video`。

### `GET /api/config`

响应：`{"config": <整份配置>, "path": "<config.yaml 绝对路径>"}`。`config` 含各 `api_key` 的当前值。

### `PUT /api/config`

请求体：

```json
{"values": {}}
```

`values` 深合并进现有配置后写回 `config.yaml`，并立刻套用日志级别与 access log。响应形状与 GET 相同。

---

## 模型

### `GET /api/models`

查询参数（均可选）：

| 参数 | 约束 |
|------|------|
| `media_type` | `image` 或 `video` |
| `model_type` | `llm`、`vlm`、`t2i`、`i2i`、`video`、`tts` |
| `ability` | 任意字符串；按适配器能力过滤 |
| `verified_only` | 布尔，默认 false |

传了 `model_type` 时走 `get_models_by_type`，忽略 `media_type` / `ability` / `verified_only`。响应：

```json
{"models": [{"id": "", "label": "", "provider": "", "family": "", "model_type": "", "type": [], "concurrency": 0, "ability_type": "", "ability_types": [], "adapter_ability_types": [], "input_modalities": [], "adapter_input_modalities": [], "api_contract_verified": false, "capabilities": {}, "pricing": [], "currency": null}]}
```

未传 `model_type` 时走 `list_api_models`。每条用 `media_type` 而不是 `model_type`，其余能力字段相同，并经 `ModelRegistry.enrich_api_row` 加上 `pricing` 与 `currency`。没有文档价格的模型 `pricing` 为空，不会填一个数。

注册表默认 LLM 是 `deepseek-flash`，默认 VLM 是 `qwen3.7-plus`。别名：`doubao-seedance-2.5` → `doubao-seedance-2-5-260628`；`deepseek-4.1-flash` 只是 `deepseek-flash` 的显示名。

---

## 资产库

数据在 `backend/code/data/videoclaw.db`（WAL）。响应是存储层记录，不是另一套 DTO。

项目记录字段：`id`，`name`，`created_at`，`updated_at`，`budget_cap`（可空浮点），`spent`（可空浮点），`status`（创建时为 `active`）。

资产种类只能是 `script`、`character`、`setting`、`prop`、`segment`、`final_cut`。范围 `global` 或 `project`。这与图节点种类不是同一份列表，见「图」。

资产记录：`id`，`project_id`，`scope`，`kind`，`name`，`description`，`tags`（字符串数组或 null），`episode_id`，`created_at`，`updated_at`。

形象记录：`id`，`asset_id`，`name`，`parent_look_id`，`description`，`prompt`，`attributes`（对象），`covered`，`version`，`created_at`，`updated_at`。

图片记录：`id`，`asset_id`，`look_id`，`blob_hash`，`source`（`generated` / `uploaded` / `extracted_frame`），`model`，`prompt`，`cost`，`width`，`height`，`created_at`。`asset_id` 与 `look_id` 至少一个非空。

### 项目

| 方法 | 路径 | 请求 | 响应 |
|------|------|------|------|
| POST | `/api/library/projects` | `{"name": "", "budget_cap": null}`。`name` 必填。`budget_cap` 省略则为 null，表示不封顶 | 项目记录 |
| GET | `/api/library/projects` | 无 | `{"projects": [项目记录]}` |
| GET | `/api/library/projects/{project_id}` | 无 | 项目记录 |

这三条不写依赖图。带固定一集图的创建、删除、停止和改模型在下一节。上调预算仍走执行器的 raise，不是改项目名称的接口。没有改项目名称的 HTTP。

### 资产

| 方法 | 路径 | 请求 / 查询 | 响应 |
|------|------|-------------|------|
| POST | `/api/library/assets` | `name`、`kind` 必填。`scope` 默认 `project`。`project` 范围必须有 `project_id`；`global` 不能带 `project_id`。可选 `description`、`tags`、`episode_id` | 资产记录 |
| GET | `/api/library/assets` | `project_id`、`scope`、`kind`。重复查询参数 `tag` 时改为 `AssetStore.search`，这时 `include_global`（默认 true）才生效。没有 `tag` 时走 `AssetStore.list`，`include_global` 被忽略 | `{"assets": [...]}` |
| GET | `/api/library/search` | `tag` 必填，可重复。可选 `project_id`、`include_global`（默认 true）、`kind`、`scope` | `{"assets": [...]}` |
| GET | `/api/library/assets/{asset_id}` | 无 | 资产记录 |
| PATCH | `/api/library/assets/{asset_id}` | 只更新出现过的字段：`name`、`description`、`tags`、`episode_id` | 资产记录 |
| DELETE | `/api/library/assets/{asset_id}` | 无 | `{"status": "deleted", "id": ""}` |
| POST | `/api/library/assets/{asset_id}/promote` | 无体。把项目资产升为全局库 | 资产记录 |

### 形象

| 方法 | 路径 | 请求 | 响应 |
|------|------|------|------|
| POST | `/api/library/assets/{asset_id}/looks` | `name` 必填。可选 `parent_look_id`、`description`、`prompt`、`attributes` | 形象记录 |
| GET | `/api/library/assets/{asset_id}/looks` | 无 | `{"looks": [...]}` |
| GET | `/api/library/looks/{look_id}` | 无 | 形象记录 |
| PATCH | `/api/library/looks/{look_id}` | 出现过的字段：`name`、`parent_look_id`、`description`、`prompt`、`attributes`、`reason`。`reason` 为 null 时不传入 | 形象记录。画面变化会把依赖标成 stale，并可能改项目里已有的图快照 |
| DELETE | `/api/library/looks/{look_id}` | 无 | `{"status": "deleted", "id": ""}` |
| GET | `/api/library/looks/{look_id}/versions` | 无 | `{"versions": [...]}` |
| POST | `/api/library/looks/{look_id}/rollback` | `{"version": 1}`，`version` ≥ 1 | 形象记录 |
| POST | `/api/library/looks/{look_id}/select-image` | `{"image_id": ""}` | `look_selection` 行：`look_id`，`image_id`，`selected_at`。不花钱 |

### 图片

| 方法 | 路径 | 请求 / 查询 | 响应 |
|------|------|-------------|------|
| POST | `/api/library/images` | `source` 必填。可选 `asset_id`、`look_id`、`blob_hash`、`model`、`prompt`、`cost`、`width`、`height` | 图片记录 |
| GET | `/api/library/images/{image_id}` | 无 | 图片记录 |
| GET | `/api/library/images` | `asset_id`、`look_id` 均可选 | `{"images": [...]}` |
| DELETE | `/api/library/images/{image_id}` | 无 | `{"status": "deleted", "id": ""}` |

### 重新生成报价（库，不执行、不入账）

这两条不跑节点，也不写 `ledger`。令牌是报价正文的 sha256，与执行器 `ImpactConfirmation` 的随机令牌不是同一种。

`POST /api/library/looks/{look_id}/regenerate-quote`

请求体可省略。字段均可选：`model`，`unit_cost`。

响应字段：`look_id`，`look_version`，`model`，`unit_cost`，`currency`（固定 `"CNY"`），`artifact_count`，`artifacts`，`estimated_cost`，`cost_known`，`unknown_models`，`price_source`，`requires_confirmation`，`confirmed`（false），`executed`（false），`will_spend`（false），`confirm_token`，`pricing`，`todo`。

没有文档 API 价格时不拿历史 `cost` 顶上，`estimated_cost` 为 null，`cost_known` 为 false，`unknown_models` 列出该 id。未指定 `model` 时，才把各条图片上已记录的 `cost` 相加（`price_source` = `prior_cost`）。

`POST /api/library/looks/{look_id}/regenerate-confirm`

请求体：`confirm_token` 必填，可选 `model`、`unit_cost`。令牌必须等于按当前参数重算的 sha256，且 `requires_confirmation` 为 true。响应是同一份报价，`confirmed` 改为 true，`executed` 仍为 false。

---

## 片段引用（SegmentRefStore）

片段资产的库种类必须是 `segment`。名称匹配是整词相等。

| 方法 | 路径 | 请求 | 响应 |
|------|------|------|------|
| POST | `/api/library/segments/{segment_asset_id}/refs` | `asset_id` 必填。可选 `look_id`、`role`、`alias`。`look_id` 必须属于该资产 | `segment_ref` 行：`id`，`segment_asset_id`，`asset_id`，`look_id`，`role`，`alias`，`created_at` |
| GET | `/api/library/segments/{segment_asset_id}/refs` | 无 | `{"refs": [...]}` |
| DELETE | `/api/library/refs/{ref_id}` | 无 | `{"status": "deleted", "id": ""}` |
| POST | `/api/library/resolve` | `{"text": "", "project_id": null}`。`text` 必填 | `{"mentions": [...]}` |

`resolve` 扫描 `@资产名` 与 `@资产名/形象名`。带 `project_id` 时，同名项目资产优先于全局资产，不搜其他项目。多条可见匹配时 `resolved` 为 false、`ambiguous` 为 true，不挑一条。找不到资产时 `error` 为 `asset not found`；资产在但形象不在时为 `look not found`。单条匹配：

```json
{"raw": "@名/形象", "asset_name": "名", "look_name": "形象", "resolved": true, "ambiguous": false, "error": null, "matches": [{"asset_id": "", "asset_name": "", "look_id": "", "look_name": "", "kind": "", "scope": "", "project_id": ""}]}
```

视频节点跑起来时，`CharacterDesignerAgent.resolve_calls` 用的就是这个解析。参考图 id 只在 `adapter.submit` 之前换成磁盘文件。

---

## 图与节点状态

图快照存在该项目的 `graph_json`（`GraphStore`）。`POST /api/v2/projects` 会写入下面那份固定的一集图。没有通用的「替换整张图」路由。只通过 `POST /api/library/projects` 建出来的项目没有 `nodes`，`run_node` 仍返回 400，`detail` 为 `project has no dependency graph`。`GET /api/v2/projects/{project_id}/status` 在有图时返回 `edges`。

图节点种类（已知值，也接受其他非空字符串）：`script`、`character`、`scene`、`shot_segment`、`final_video`、`previz`、`keyframe`、`draft_video`。

边上的种类：`hard`、`ref`、`continuity`、`inherit`。`blocks` 为 true 且前驱不是 `done` 时挡住后继。

图上存储的状态以及允许的迁移：

| 从 | 到 |
|----|----|
| `pending` | `running`、`skipped` |
| `running` | `done`、`failed`、`awaiting_user`、`pending`、`stale` |
| `awaiting_user` | `done`、`failed`、`running`、`pending`、`stale` |
| `done` | `stale`、`pending` |
| `stale` | `running`、`pending`、`skipped` |
| `failed` | `running`、`pending`、`stale` |
| `skipped` | `pending` |

`ready` 不是图状态。`GET .../status` 里的 `executable` 是算出来的：状态为 `pending`、`stale` 或 `failed`，每条挡住它的前驱都是 `done`，并且该集显式依赖的集都已完成。`node_state` 表允许字面量 `ready`，只用于预算上调之后、下一次 confirm 认领之前。

节点 JSON：`id`，`kind`，`episode_id`，`status`，`version`，`input_versions`，`pinned`，`label`，`metadata`。

---

## 执行器

路由都在 `api/routers/executor.py`，调用 `GraphExecutor`。无适配器且供应商密钥为空时，拒绝发生在 `GraphExecutor.confirm` 里面、授权之前（`_require_video_vendor`，`confirm` 一进来就调用）。进程内直接 `confirm` 同样抛 `_NoVendorKey`，不入账，也不写 `submit_dispatched`。HTTP 的 `confirm`、`retry_video_submit`、`raise`、`impact/confirm`、`impact/regenerate` 把这个异常映射成 409：`未配置视频厂商密钥，不会提交也不会入账`。只有构造时显式 `allow_offline_video=True` 才跳过这道检查，并按 `quote.max` 离线入账。图像、VLM、TTS 不走这道拒绝。真正的厂商 POST 只发生在授权之后，并且要有注入的适配器或已配置的密钥。

### `POST /api/v2/projects`

创建项目并写入固定的一集图（`core/episode.py` 的 `seed_episode_graph`，`episode_id` 恒为 `ep1`）。请求体 `EpisodeProjectCreate`：`name` 必填；可选 `budget_cap`、`idea`、`llm_model`、`image_model`、`video_model`、`tts_model`。

图里六个节点，边是 `script → storyboard`，`storyboard → character`，`storyboard → reference`，`character → video`，`reference → video`，`video → tts`：

| id | kind | 写入的模型字段 |
|----|------|----------------|
| `script` | `script` | `llm_model`，正文是 `idea` |
| `storyboard` | `storyboard` | 同上 |
| `character` | `character` | `image_model`，`resolution` 为 `720p` |
| `reference` | `keyframe` | 同上 |
| `video` | `shot_segment` | `video_model`，`resolution` `720p`，`includes_video_input` false，`completion_tokens` 1000000，`shot_duration` 6 |
| `tts` | `tts` | `tts_model`，`quantity` 10000，`unit` `per_10k_characters` |

空字符串不当作模型 id。播种失败会删掉刚插入的项目行。响应：`project`（项目记录）、`episode_id`（`ep1`）、`graph`（存进去的 JSON）、`nodes`、`edges`。

前端 `createEpisode`（`frontend/lib/episodeApi.ts`）只提交非空的这几个字段。

### `DELETE /api/v2/projects/{project_id}`

没有请求体。图上任一节点 `status` 为 `running`，或 metadata 里 `submit_dispatched` 为真，或 `vendor_task_id` 是非空字符串，或 `node_state.status` 为 `running`，则 409，`detail` 为 `项目有尚未结束的提交，不能删除`，项目还在。否则 `ProjectStore.delete`。成功：`{"status": "deleted", "id": "<project_id>"}`。项目本就不存在：404。

### `POST /api/v2/projects/{project_id}/stop`

没有请求体。还没有 `submit_dispatched` 或 `vendor_task_id` 的 `running` 节点，在同一事务里改成 `failed`，metadata `stop_reason` 写成 `用户停止`，并把该节点仍为 `quoted`、尚未有 `actual_amount` 的账本行改成 `skipped`。已是 `spent` 的行不动。

视频节点已经落库 `submit_dispatched` 或非空 `vendor_task_id` 时，调用该模型适配器的 `cancel`（测试注入的适配器优先；否则要有供应商密钥）。方舟实现只按官方文档：

- 查询：`GET https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}`。状态为 `queued`、`running`、`cancelled`、`succeeded`、`failed`、`expired`。来源：https://ark.volcengine.com/region:cn-beijing/docs/get-video-generation-task-api
- 取消：`DELETE` 同一路径。官方写明成功是 HTTP 200，返回 `{}`。`queued` 变为 `cancelled`。`running` 不支持。`succeeded` / `failed` / `cancelled` / `expired` 的 DELETE 是删除记录，之后不能查询。来源：https://www.volcengine.com/docs/82379/1521720 与 https://ark.volcengine.com/region:cn-beijing/docs/cancel-or-delete-video-generation-tasks-api 。该页没有写拒绝时的 HTTP 状态码。
- 只有查询已是 `cancelled`，或对 `queued` 的 DELETE 返回 HTTP 200 且随后查询为 `cancelled`，才释放报价（账本 `skipped`）并把节点标为 `skipped`（`stop_reason` 为 `用户取消`），同时清掉 `submit_dispatched` 和 `vendor_task_id`。
- 查询到 `succeeded`，或节点上已有 `result_obtained`，按完成处理：`record_actual` 用原来的 `quote.max`，节点 `done`，不释放报价。DELETE 返回 200 之后若查到 `succeeded`，同样按完成处理，不把 HTTP 200 单独当成取消成功（同一个 200 也表示已完成记录被删掉）。
- `running` 不调用 DELETE。`failed` / `expired` 不调用 DELETE。非 200、超时、未知状态、200 之后记录无法再查询：不释放报价，节点状态不变。
- 没有任务号、没有可用适配器或密钥、适配器没有 `cancel`、取消抛错、返回的 `outcome` 无法识别：同样不释放。没有密钥时的原因是 `未配置视频厂商密钥，无法向厂商确认取消结果，报价保持保留，节点状态不变。` 这不是确认接口那句 `未配置视频厂商密钥，不会提交也不会入账`。

非视频节点即使带了 `submit_dispatched` 也不调用取消，原因仍是 `生成请求可能已经发给厂商，不能把它标成已停止。请使用重试提交。`

一个都没停成、也没有 `settled`：409，`detail` 是 `没有节点被停止` 再加上各节点原因，用 `；` 连接。有节点被停或有任务按完成入账：200。体为 `project_id`、`stopped`（节点 id 列表）、`refused`（`node_id` 与 `reason`）、`settled`（按完成入账的 `node_id` 与 `reason`）、`message`。当前没有 `running` 节点：200，`stopped` 与 `refused` 为空，`message` 含 `当前没有正在运行的节点`。再次 stop 一个已经取消的项目走这条，不会第二次释放报价。

### `PATCH /api/v2/projects/{project_id}/models`

只处理请求里出现过的字段。角色对应的 kind：`llm_model` → `script` / `storyboard` / `previz`；`image_model` → `character` / `scene` / `setting` / `keyframe` / `image`；`video_model` → 视频 kind；`tts_model` → `tts`。可选 `nodes`：`{节点 id: 模型 id}`，覆盖到该节点。空字符串 400。`ModelRegistry.get` 找不到的 id：400，`detail` 含 `未知模型`。

`running`、`awaiting_user`，或仍有 `submit_dispatched` / 非空 `vendor_task_id` 的节点不改，放进 `blocked`，原因是 `节点正在运行、等待确认，或仍保留着厂商任务，模型没有更换`。一个节点都没写上时 400。写上了则 200：`project_id`、`updated`（`node_id` 与 `model_id`）、`blocked`、`unapplied`（图里没有对应 kind 的角色名）。

### `PATCH /api/v2/projects/{project_id}/nodes/{node_id}/prompt`

请求体 `{"text": ""}`，`text` 必填字符串。节点正在运行（`node_state` 或图状态为 `running`）则 400：`节点正在运行，修改没有写入`。否则写入 metadata `text`；若已有 `pending_call`，里面的 `text` 一并改掉。响应：`project_id`，`node_id`，`text`，`status`。

### `POST /api/v2/projects/{project_id}/nodes/{node_id}/retry_video_submit`

请求体可省略，省略等于 `force_new` false：续接已有 `vendor_task_id` 或 `result_obtained`，花原来的报价，不新开 POST。`RetryVideoSubmitRequest.force_new` 默认 false，并按 Pydantic 的 `bool` 解析，不是只有 `true` 和 `"true"`。当前环境实测：JSON `true`、`1`，以及 `"true"` / `"True"` / `"yes"` / `"on"` / `"1"` / `"y"` / `"t"`（大小写不限）才是新开：先释放旧报价，重新 `authorize`，成功之后才丢掉任务 id，然后才新开 POST。JSON `false`、`0`，以及 `"false"` / `"no"` / `"off"` / `"0"` / `"n"` / `"f"` 仍是续接。`null` 在校验阶段就是 422（`bool_type`），到不了这个默认值。带首尾空格的字符串同样 422。前端送的是布尔值。视频节点没有可用适配器时，和 confirm 一样先 409，不入账。

节点运行请求体 `NodeRunRequest`（可省略，省略等于 `{}`）：

| 字段 | 说明 |
|------|------|
| `model_id` | 可选。视频节点为空时用 `doubao-seedance-2-5-260628` |
| `modality` | 可选。`video`、`image`、`vlm`、`tts` 才会进入计费门。否则按节点种类推断 |
| `resolution` | 可选 |
| `includes_video_input` | 可选布尔。Seedance 2.5 含视频输入与不含视频输入的单价不同 |
| `asset_count` | 可选。图片默认按 1 个资产、每次 1..6 次调用估价 |
| `quantity` | 可选。视频节点不用调用方给的时长当数量 |
| `unit` | 可选 |
| `completion_tokens` | 可选。Seedance 2.5 按百万 token 计价时，没有它就无法缩放报价 |
| `input_tokens`、`output_tokens` | 可选。文本模型要两边都有才出单价 |
| `text` | 可选。提示词，也是 `@资产名` 的解析文本 |
| `asset_id`、`look_id` | 可选。用来列出已有图片版本 |
| `first_frame_asset_id` | 可选。资产库里已有图片的资产 id，作为首帧。空字符串清掉节点上已存的 id；省略则沿用 |
| `last_frame_asset_id` | 可选。同上，作为尾帧。必须和首帧一起给出 |
| `continue_from_tail` | 可选布尔。为 true 时，用上一镜头已保存的 `last_frame_asset_id` 作为本镜头首帧。省略则沿用节点上的值；`false` 清掉 |
| `duration` | 可选。视频节点接受该字段并忽略。时长只看模型能力与节点 metadata 的 `shot_duration` |

`run_node` / `regenerate` / `confirm` / `decline` / `raise_budget` / `decline_budget` 的节点响应来自 `_pack`，字段为：

`project_id`，`node_id`，`status`，`submitted`，`spent`，`provider_called`，`gate`（`none` / `confirm` / `budget`），`call_confirmed`，`why_blocked`，`quote`，`entry_id`，`message`，`duration`，`references`，`versions`，`over_budget`，`successor_status`。

视频 `run_node` 在等待确认时 `entry_id` 为 null，账本还没有行。`confirm` 成功入账后才有 `entry_id`。

### 谁需要逐次确认

`_modality`（`executor.py`）先看请求或节点 metadata 里的 `modality`。没有的话按 kind 归类。归类和入账如下。

| kind 或 modality | `run` 做什么 |
|------------------|--------------|
| kind `shot_segment`、`draft_video`、`final_video`、`segment`、`video`，或 `modality=video` | 视频确认门。只报价，节点 `awaiting_user`，`gate` 为 `confirm`，账本还没有行。用户 `confirm` 之后才 `authorize`。结算时 `actual` 等于 `quote.max`。 |
| kind `character`、`scene`、`setting`、`keyframe`、`image`，或 `modality=image`；kind 或 modality 为 `vlm`、`tts` | 不确认。`run` 里直接 `ProjectBudget.authorize`。未超预算则 `_finish` 把 `actual` 记成 `quote.max`（`this_quote`），节点 `done`，`spent` true。节点执行器返回 `provider_called: false` 时也记这一笔，响应里的 `provider_called` 仍是 false。超预算则 `gate` 为 `budget`，不跑节点。 |
| kind `script`、`storyboard`、`previz` | 不报价、不入账，跑完即 `done`。 |

`character`、`scene`、`setting`、`keyframe`、`image` 不是 `unsupported`。不传 `modality` 时，不在上表里的 kind 才是 `unsupported`（`status: "unsupported"`，`message` 为 `node has no billable modality`）。库种类里这是 `prop` 和 `final_cut`。给这种节点显式带上 `modality`（`video` / `image` / `vlm` / `tts`）则改走对应的那一列。

前驱未完成：`status` 为 `blocked`，带 `why_blocked`，不写账本。

### `POST /api/v2/projects/{project_id}/nodes/{node_id}/run`

体：`NodeRunRequest` 或空。

视频且报价不可用时：`status` 为 `unavailable`，`gate` 为 `none`，不写账本，节点保持 `pending`。下面几种都是这样：

- 没有文档价格。
- 有多条文档价格，但选不中唯一的一条。Seedance 2.5 按分辨率和是否含视频输入各有一行。`resolution` 和 `includes_video_input` 缺一个，`PriceBook.quote` 就匹配不到恰好一行，原因是 `no single documented price matches this request`。
- 选出了一行百万 token 价格，但没有可缩放的数量。`duration.default` 是 -1，不会被当成计费秒数；不传 `completion_tokens` 时 `quantity` 仍是 null。

调用方传的 `duration` 不改变上面的结果。响应里的 `duration` 仍是能力说明：`min` 4，`max` 30，`default` -1，`allow_auto` true，`verified` true。

带了 `first_frame_asset_id`（以及可选的 `last_frame_asset_id`）时，在报价之前先查关键帧。资产必须已经在 `ImageStore` / `BlobStore` 里，并且文件在磁盘上。模型的 `adapter_abilities`（空则看 `abilities`）没有 `first_frame_i2v` 或 `start_end_frame_i2v` 时，HTTP 400，`detail` 为 `模型 {id} 不支持首帧生视频` 或 `模型 {id} 不支持首尾帧生视频`。只给尾帧是 `只指定了尾帧，没有首帧`。资产没有图片是 `首帧资产不在资产库中` 或 `尾帧资产不在资产库中`。提示词里已经解析出的参考图和关键帧不能一起交：`首帧与参考图不能同时使用`。这些拒绝不写账本，也不写 `submit_dispatched`。无密钥 409 仍是确认时的另一道检查，文案不变。没有文档价格的模型仍然 `unavailable`，不出数字；有价格的模型不因为多了一张静帧就改 `includes_video_input`。

`continue_from_tail: true` 在报价前把上一镜头 metadata 里的 `last_frame_asset_id` 抄成这一镜的首帧。不从成片抽帧。只有一条 `continuity` 前驱时用那一镜；多条是 400：`有多个上一镜头，不能确定尾帧`。没有这种边时，用同一集拓扑序里的上一个视频节点。没有上一镜头是 `没有上一镜头，不能续接尾帧`。上一镜头没存尾帧资产是 `上一镜头没有尾帧资产，不能续接`。同时又手选了首帧是 `续接尾帧时不能再手选首帧`。抄过来的 id 再走上面的能力和资产检查。挡住本节点的边仍然要先完成；尾帧资产本身不要求上一镜头已经 `done`。

Seedance 2.5 首帧 / 首尾帧提交使用官方字段：`content[].type` = `image_url`，`content[].image_url.url`，`content[].role` = `first_frame` 或 `last_frame`。这两种任务的 `ratio` 只能是 `adaptive`（注册表 `ratio_by_mode`）。和 `reference_image` 互斥。依据是火山方舟《Seedance 2.5 教程》和《创建视频生成任务 API》。提交前才把资产 id 换成磁盘路径。

视频报价可用：节点改为 `awaiting_user`，`gate` 为 `confirm`，`call_confirmed` false，`submitted` false。若 `used + committed + quote.max` 已超过 `budget_cap`，`over_budget` 为 true。`message` 是 `pause_message`：三个数字在报价有币种时各带 ` {currency}` 后缀（`min` 等于 `max` 时中间只有一个数）。此时仍没有账本行。`budget_cap` 为 null 时不判超支。

### `POST /api/v2/projects/{project_id}/nodes/{node_id}/confirm`

查询参数 `force_new` 或 JSON 字段 `force_new` 只要出现就 400，包括 `false`、查询串 `force_new=false` 和 `force_new=true`。省略该字段才是普通确认（前端 `confirmRequest()` 送的是 `{}`）。JSON `{"force_new": null}` 目前是 422，不是 400。

没有 `force_new` 时：视频节点先做无密钥 409（见上，不入账）。这道检查在 `GraphExecutor.confirm` 里面、授权之前，HTTP 和进程内共用。通过之后才认领并 `authorize`。这是该次调用的第一次账本写入。结算不要求先拿到 URL：`record_actual` 用的是已保留的 `quote.max`。有 URL 时会先把 `result_obtained` 单独提交，但没有 URL 也会入账并把节点标 `done`。无适配器且密钥为空时不会走到这一步，除非构造执行器时传入 `allow_offline_video=True`。

认领在授权之前：`awaiting_user`（或预算上调留下的 `node_state.status = ready`）变成 `running`。并发的第二个 confirm 立刻 400，不会授权也不会提交。

授权被预算挡住：节点回到 `awaiting_user`，`gate` 为 `budget`，账本行是 `awaiting_user`。上调用 raise，放弃用 decline。

授权通过：跑节点。有供应商密钥时再 `adapter.submit`。成功后节点 `done`，账本 `spent`，`actual` 等于保留报价的 `max`。`result_id` 是远程 URL 或本地路径，不是裸任务 id。

节点不是 `awaiting_user` 或 `running`、没有 `pending_call`、或 `submit_dispatched` 已置位时，400。已置位时普通 confirm 不会再次 POST。见下方提交标记。

### `POST /api/v2/projects/{project_id}/nodes/{node_id}/decline`

无请求体。

还没有账本行、节点在 `awaiting_user`：跳过节点，清掉 `pending_call`，`message` 为「未确认本次调用，节点已跳过。」

已经有一条该节点的 `awaiting_user` 账本行：等同于对该行 decline，行变为 `skipped`，节点 `skipped`。

### `POST /api/v2/projects/{project_id}/nodes/{node_id}/regenerate`

体：`NodeRunRequest` 或空。只重跑这一个节点，后继标 `stale`，不启动后继。`done` 先回到 `pending`；`stale` / `failed` 走 `revise`；已经是 `pending` 则直接跑。其他状态 400：`cannot regenerate a node that is {status}`。响应多一个 `regenerated`（节点 id）。

### `POST /api/v2/projects/{project_id}/budget/{entry_id}/raise`

请求体：`{"budget_cap": 0.0}`，必填浮点。

新上限仍盖不住 `used + committed + 本次报价`：保持原上限，调用仍不提交。HTTP 响应是 `_pack`，没有 `allowed` 字段。此时 `gate` 为 `budget`，`over_budget` 为 true，`status` 为 `awaiting_user`，`submitted` 与 `spent` 为 false。

够用：项目 `budget_cap` 改为新值，该行改为 `quoted` 且 `confirmed` true，然后继续同一次报价（视频会接着提交）。`message` 为「已上调预算上限，可以继续。」

`submit_dispatched` 或 `vendor_task_id` 已存在时拒绝，除非这次暂停是 `force_new` 的预算出口（`force_new_pending`）。否则会 400，避免第二笔生成。

### `POST /api/v2/projects/{project_id}/budget/{entry_id}/decline`

无请求体。该行必须是 `awaiting_user`。行改为 `skipped`，节点 `skipped`，不提交。`message` 为「未上调预算上限，本次调用不执行。」

### `GET /api/v2/projects/{project_id}/status`

无图或图里没有 `nodes` 时，`nodes`、`edges`、`awaiting_user`、`executable`、`stale` 都是空数组。预算和账本仍返回。有图时 `edges` 是快照里的边。每个节点除了图字段，还有 `node_state`（没有行则是 null）。`provider_called` 只在 metadata 或 `metadata.result` 里存了 false 时出现在节点上，并保持 false；成功打到厂商时，当次响应可以是 true，但 status 不会把 true 写回这个字段。节点为 `awaiting_user` 且是视频时，附上重新算出的 `quote` 和 `duration`。该节点有一条 `awaiting_user` 账本行时，再附上 `entry_id` 和 `budget_hold: true`。

```json
{
  "project_id": "",
  "budget": {"used": 0, "committed": 0, "remaining": null, "cap": null, "currency": null, "mixed_currency": false},
  "ledger": [],
  "nodes": [],
  "edges": [],
  "awaiting_user": [],
  "executable": [],
  "stale": []
}
```

`remaining` 在 `cap` 为 null 时为 null，否则是 `cap - used - committed`。币种多于一种时 `currency` 为 null 且 `mixed_currency` 为 true。

账本行：`id`，`project_id`，`node_id`，`kind`，`model`，`quote_amount`，`actual_amount`，`currency`，`confirmed`，`status`（`quoted` / `awaiting_user` / `skipped` / `spent` / `unavailable`），`detail`，`created_at`。

### 形象影响与令牌

`POST /api/v2/projects/{project_id}/looks/{look_id}/impact`

体可省略。可选 `model`，`unit_cost`。不跑节点，不花钱。在库报价上附加：

- `graph_impact`：该形象所属资产在图里的下游（无图或不在图中则为 null）
- `confirm_token`：`impact_confirmation` 表里的随机令牌，不是报价 sha256
- `impact_nodes`：要重跑的下游节点 id（钉住的不在内；pending / skipped 不在内；已经 stale 的在内）
- `expires_at`：签发后 30 分钟（`IMPACT_TOKEN_TTL_SECONDS`）
- `submitted` / `spent` / `provider_called`：false

令牌绑定这一个项目和当时的节点集合，一次性。

`POST /api/v2/projects/{project_id}/looks/{look_id}/impact/confirm`

请求体：`confirm_token` 必填，可选 `model`、`unit_cost`。

令牌必须属于该项目、对应该形象、未过期，且当前下游集合与签发时一致，否则 400（`confirm_token is invalid` / `belongs to a different project` / `does not match this look` / `has expired` / `is stale: the impact set changed`）。集合为空：`nothing to regenerate`。

认领后按拓扑跑 `impact_nodes`。每个节点走 `run_node`。视频节点接着在批内 `confirm`（授权并写账本）。图片、VLM、TTS 以及 kind `character`、`scene`、`setting`、`keyframe`、`image` 仍在 `run_node` 里直接按 `quote.max` 入账，`provider_called` 为 false 也记 `spent`。第一个仍在等用户的节点停下，后面的节点这次不会占用也不会花。某个节点在占用额度之后失败：释放该节点未结算的报价，把失败写在已消费的令牌上，然后抛 `_GateError`。这条 HTTP 是 400，`detail` 形如 `impact regenerate failed on node {id}: ...`。再次 confirm 已消费且存了结果的令牌时原样返回该结果，不再花第二次，这次是 200。

响应在报价字段之外有：`confirmed`，`executed`，`will_spend`（false），`spent`，`submitted`，`provider_called`，`nodes`，`spent_node_ids`，`failed`，`failed_node_id`，`error`，`halted`。

`POST /api/v2/projects/{project_id}/impact/regenerate`

请求体是 `NodeRunRequest` 再加必填的 `confirm_token` 与 `node_ids`（非空字符串数组）。

未知节点、不在已确认集合里的节点、重复 id、令牌缺失 / 无效 / 过期 / 过期集合已变 / 已消费，都在任何节点运行之前、任何账本写入之前失败。通过后按图的拓扑序跑这些节点。响应：`project_id`，`look_id`，`confirm_token`，`impact_nodes`，`confirmed`，`nodes`，`submitted`，`spent`，`spent_node_ids`，`failed`，`failed_node_id`，`error`，`halted`，`provider_called`。路由注释写明这条本身不直接调供应商；视频供应商调用仍只发生在批内的 `confirm` 授权之后。

---

## 视频提交标记

实现在 `GraphExecutor` 与 `models/video_seedance.py`。常量：认领 TTL `VIDEO_SUBMIT_CLAIM_TTL_SECONDS` = 30 分钟；Seedance 整次提交截止 `SUBMIT_DEADLINE_SECONDS` = 20 分钟。20 分钟必须短于 30 分钟，否则 120 轮、单次 GET 超时 30 秒、间隔 5 秒的轮询（大约 74 分钟）会活过认领。

`confirm` 在 `adapter.submit` 之前把节点 metadata `submit_dispatched` 写成 true 并提交事务。供应商返回任务 id 时写入 `vendor_task_id`，标记保持 true。拿到结果 URL 后先单独提交 `result_obtained`（同时保持 `submit_dispatched`），再在同一事务里把账本记为 `spent` 并把节点标 `done`。本地文件在 `code/result/video/<project_id>/<node_id>.mp4`，URL 记在 `result_id`。

`confirm`、`retry_video_submit`、`raise`、`impact/confirm`、`impact/regenerate` 在密钥为空且没有注入适配器时，都在 `GraphExecutor._require_video_vendor` 里、授权前拒绝。HTTP 是 409，进程内是 `_NoVendorKey`。不入账，也不写 `submit_dispatched`，节点不会因此变成 `done`。只有 `allow_offline_video=True` 才离线按 `quote.max` 入账且 `provider_called` 为 false。`_ClaimLost` 把这次报价标 `skipped`；节点若仍是 `running` 且没有厂商标记，会写回 `awaiting_user`。这次回写走比较并保存，失败会被吞掉并打 warning：节点可能一直 `running`，直到 `recover_stale_submit_claims`（30 分钟）回收。

标记只有在已经写上、并且随后的 `submit` 抛错、且 `_request_was_not_sent` 为真时才清掉。那是 `FileNotFoundError`、`UnsupportedParam`、`SubmitNotSent`，或错误文本含 `reference image file is missing`、`model is not registered`、`ARK_API_KEY not set`。最后这句只属于已经进入 `submit` 的适配器（例如注入了适配器，客户端又发现没有密钥）。HTTP 缺密钥路径不进入 `submit`，所以不是清标记。清掉时报价改为 `skipped`，节点回到 `awaiting_user`。轮询错误、下载错误、供应商判定 failed / expired、所有 HTTP 4xx、超时，以及其他异常都保留 `submit_dispatched` 和 `vendor_task_id`。4xx 响应体里若有 `id`，先存成 `vendor_task_id` 再抛错。此后普通 `confirm` 不会再 POST。`submit_dispatched` 已经落库时，视频 `stop` 按上一节调用取消；只有明确 `cancelled` 才清标记并释放报价。不确定时标记和报价都留着，`recover_stale_submit_claims` 仍只把过期认领停回 `awaiting_user`，不跳过这笔报价。

镜头时长：`shot_duration` 取节点 metadata。模型 `duration.verified` 不为 true 则不发送时长。整数且落在该模型 min..max 内则用该秒数，否则若 `allow_auto` 则发送 -1。Seedance 2.5 是 4 到 30 秒，区间外为 -1。已核实的其他模型区间：

| 模型 id | 最短 | 最长 |
|---------|------|------|
| `doubao-seedance-2-5-260628` | 4 | 30（默认可 auto） |
| `doubao-seedance-2-0-260128`、`doubao-seedance-2-0-fast-260128`、`doubao-seedance-2-0-mini-260615` | 2 | 12 |
| `wan2.6-i2v-flash`、`wan2.7-i2v` | 2 | 15 |
| `happyhorse-1.0-i2v`、`happyhorse-1.1-i2v`、`happyhorse-1.0-r2v`、`happyhorse-1.1-r2v`、`kling-v3` | 3 | 15 |
| `wan2.7-r2v` | 2 | 10 |

`kling-v2-6`、`kling-v2-5-turbo`、`seedance-1-0-pro`、`seedance-1-0-lite` 没有已核实时长。

Seedance 2.5 文档单价（元 / 百万 token）：无视频输入 480p 与 720p 为 70，1080p 为 77；有视频输入 480p 与 720p 为 42，1080p 为 46。规格上的公式是 `(input_video_seconds + output_video_seconds) * width * height * fps / 1024`。执行器不算这个公式。没有文档价格的模型不出数字。

---

## 已知遗留

这些是当前代码的行为，不是还没写上的路由。

- 图像、VLM、TTS 在 `run` 里仍按 `quote.max` 入账，`provider_called` 为 false。前端在节点 `done` 且 `provider_called === false`（节点上或 `metadata.result` 上）时显示 `已记账·未调用厂商`（`episodeFlow.ts` 的 `OFFLINE_BADGE`）。视频节点没有这道离线入账，除非 `allow_offline_video=True`。
- `POST /api/sandbox/video` 的请求体没有 `project_id`。当前对所有请求返回 409：`沙盒视频需在项目内生成`。不调用 `VideoClient`，不入账。
- `POST /api/v2/projects` 的图永远是一集 `ep1` 和上面那六条边。没有按用户集数生成图的参数。
- 方舟取消只覆盖官方文档写明的行为。`running` 不能取消。取消接口没有写拒绝时的 HTTP 状态码，所以非 200 一律不当成成功。DELETE 返回 200 之后如果任务记录消失，无法区分「已取消」和「已完成的记录被删掉」，这时不释放报价。可灵、百炼视频没有在这里核实过取消接口，`cancel` 返回不支持。沙盒视频路由仍是上面的 409，不受这次 stop 影响。

## 无 HTTP 的存储调用

| 调用 | 行为 |
|------|------|
| `GraphExecutor.recover_stale_submit_claims(conn)` | 把超过 30 分钟仍为 `running` 的视频认领收回。无标记的：跳过未结算报价并释放认领。有标记的：停回 `awaiting_user`，不再次提交。进程启动时调用一次。没有可回收行时返回空列表，再启动一次也是空操作。未超过 TTL 的认领不动。下一次 `confirm` 在 TTL 之后也会做同样的回收。 |
| `GraphStore.save` / `load` | 除了 `POST /api/v2/projects` 写入那份固定图，以及执行器改节点时回写快照，没有单独的读写图路由。 |
| `ProjectStore.update` | 无通用 HTTP。预算 raise 会改 `budget_cap`。 |

`retry_video_submit` 已有 HTTP，见上文。普通 confirm 出现 `force_new` 是 400，不是再转去重试。

---

## 文件

### `POST /api/upload_file`

`multipart/form-data`，字段名 `file`。扩展名 `.docx`、`.doc`、`.txt`、`.md`、`.pdf`。保存到 `backend/code/temp`，文件名为 `{unix秒}_{原名}`。

响应：`{"filename": "原名", "file_path": "1720000000_原名.txt"}`。`file_path` 是 temp 目录下的相对名，不是绝对路径。本提交没有把这个相对名自动喂给建项或写图的接口。

### `POST /api/upload_media`

同样的表单字段 `file`。扩展名 `.jpg`、`.jpeg`、`.png`、`.webp`、`.bmp`、`.mp4`、`.mov`、`.avi`、`.mkv`、`.webm`。

响应：`{"filename": "", "file_path": "<temp 下的绝对路径>"}`。与 `upload_file` 不同，这里的 `file_path` 是绝对路径。

### `DELETE /api/cache/temp`

清空 `backend/code/temp` 下的条目。响应：`status`（`ok`），`deleted`，`freed_bytes`，`freed_mb`，`errors`（删失败的 `{path, error}` 列表）。

---

## 临时工作台

问答、看图、文生图、图生图直接调用对应客户端，不经过 `quote`、`confirm`、`ProjectBudget` 或提交标记。视频路由除外，见下节。历史在 `backend/code/result/sandbox/history.json`。进行中的任务只在进程内存里。

失败时 HTTP 仍可能是 200，体为 `{"success": false, "error": "..."}`。

### `GET /api/sandbox/history`

`{"success": true, "records": [{"id", "tool", "model", "input", "output", "created_at"}]}`

### `GET /api/sandbox/tasks`

`{"success": true, "tasks": [{"id", "tool", "model", "input", "status": "running", "progress": 1, "created_at"}]}`

### `GET /api/sandbox/history/{record_id}`

找到：`{"success": true, "record": <含 files 的完整记录>}`。没有：`{"success": false, "error": "记录不存在"}`。

### `DELETE /api/sandbox/history/{record_id}`

没有记录：`{"success": false, "error": "记录不存在"}`。删掉：`{"success": true}`，并尝试删除记录里的文件。

### `POST /api/sandbox/llm`

`{"model": "", "prompt": "", "temperature": 0.7, "web_search": false}`。`model`、`prompt` 必填。`temperature` 在模型里，当前处理函数没有把它传给客户端。

成功：`{"success": true, "result": "<文本>", "record_id": ""}`。

### `POST /api/sandbox/vlm`

`{"model": "", "prompt": "", "images": []}`。三个字段都必填，`images` 是路径字符串数组。

成功：`{"success": true, "result": "<文本>", "record_id": ""}`。

### `POST /api/sandbox/t2i`

`{"model": "", "prompt": "", "style": "anime", "ratio": "16:9"}`。`model`、`prompt` 必填。`style` 在模型里，当前处理函数没有把它传给 `generate_image`。

成功：`{"success": true, "result": ["<换成 /code 相对路径的图片>"], "record_id": ""}`。

### `POST /api/sandbox/i2i`

`{"model": "", "prompt": "", "image": "", "ratio": "16:9"}`。`model`、`prompt`、`image` 必填。

成功形状与文生图相同。

### `POST /api/sandbox/video`

`{"model": "", "prompt": "", "image": null, "ratio": "16:9", "resolution": "720P", "duration": 5}`。`model`、`prompt` 必填。请求体没有 `project_id`，处理函数也不读取额外字段。

当前对所有通过校验的请求返回 409：`{"success": false, "error": "沙盒视频需在项目内生成"}`。不调用 `VideoClient`，不写历史，不入账。有项目的生成留到以后，本路由不新增 `project_id`。
