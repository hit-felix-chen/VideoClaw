---
name: video-claw
description: 本地资产图驱动的 AI 视频生成。项目、资产、形象和片段引用在资产库；节点由 GraphExecutor.run_node 执行。只有视频逐次确认，然后才 ProjectBudget.authorize 并写入账本。触发词：视频生成、AI视频、AIGC、创作视频、制作视频、AI画图。
license: MIT License
metadata:
  author: Lychee
  version: "2.0"
---

# Video-Claw Agent Skill

> **本地运行**：前后端都在本机。
> - 后端：`http://127.0.0.1:8000`（`config.yaml` 的 `server.host` / `server.port`）
> - 前端：`http://localhost:3000`
> - 只请求本机。调用前先确认后端已启动。

> **架构**：没有编排器，没有六阶段流水线，没有 `/api/project`。一个项目是 SQLite 里的一行。资产组成依赖图。集是分组，不是节点。

> **接口以代码为准**：路径和请求体见 [api.md](video-claw/backend/docs/api.md)。下面每一步都指向 `references/workflow/` 里的对应文档，不要凭记忆调用已经删除的 session 接口。

---

## 项目结构

```
video-claw/                         ← skill 根目录
├── video-claw/
│   ├── backend/                    ← FastAPI，默认 127.0.0.1:8000
│   │   ├── api/routers/            ← 实际路由
│   │   ├── core/executor.py        ← GraphExecutor
│   │   ├── graph/                  ← 资产依赖图
│   │   ├── models/middle/          ← 注册表、报价、预算
│   │   ├── storage/                ← SQLite：项目、资产、账本、确认令牌
│   │   └── code/data/videoclaw.db  ← 运行时库（WAL），不入库
│   └── frontend/                   ← Next.js
├── references/
│   ├── init_project/               ← 安装依赖
│   ├── run_project/                ← 启动进程
│   ├── workflow/                   ← v2 调用步骤
│   ├── sandbox/                    ← 临时工作台说明（载荷以 api.md 为准）
│   └── send_message/               ← 飞书 / 微信发文件
└── SKILL.md
```

---

## 先看什么

| 用户要做的事 | 文档 |
|--------------|------|
| 第一次安装 | [init_all.md](references/init_project/init_all.md) |
| 后端或前端没起来 | [start_backend.md](references/run_project/start_backend.md)、[start_frontend.md](references/run_project/start_frontend.md) |
| 建项目、设预算上限 | [create_project.md](references/workflow/create_project.md) |
| 图、节点状态 | [graph.md](references/workflow/graph.md) |
| 跑一个节点 | [run_node.md](references/workflow/run_node.md) |
| 确认视频、提交标记、手动重试 | [confirm_video.md](references/workflow/confirm_video.md) |
| 超预算暂停、上调或放弃 | [budget.md](references/workflow/budget.md) |
| 形象变更的影响令牌 | [impact.md](references/workflow/impact.md) |
| 片段绑定和 `@资产名` | [segment_refs.md](references/workflow/segment_refs.md) |
| 上传文档或媒体 | [upload_file.md](references/workflow/upload_file.md) |
| 单次问答、画图、短视频 | [api.md 临时工作台](video-claw/backend/docs/api.md) |

---

## 执行顺序

1. 确认后端：`curl -s http://127.0.0.1:8000/api/health`。未运行则按 start_backend.md 启动 `python api_server.py`，等几秒再查。
2. 需要页面时再确认前端。
3. 查密钥。读 `video-claw/backend/config.yaml` 的 `api_providers.*.api_key`。DeepSeek 用 `api_providers.deepseek.api_key`，通义用 `api_providers.dashscope.api_key`，方舟 / Seedance 用 `api_providers.ark.api_key`，可灵用 `api_providers.kling.api_key`（不是 access_key + secret_key）。缺密钥就停下来告诉用户缺哪一家、配置文件位置，等用户写上再继续。
4. 按 create_project.md 创建项目。`budget_cap` 可空，空表示不封顶。
5. 资产、形象、图片走资产库。片段引用按 segment_refs.md。
6. 图必须已经在该项目的 `graph_json` 里。本提交没有写入图的 HTTP。没有图时 `run` 会 400：`project has no dependency graph`。不要改调 `/api/project`。
7. 对可执行节点 `POST /api/v2/projects/{project_id}/nodes/{node_id}/run`。
8. 只有视频停下来等用户确认报价，再 `confirm`。kind `character`、`scene`、`setting`、`keyframe`、`image` 以及 VLM、TTS 在 `run` 里直接按 `quote.max` 入账（`provider_called` 为 false 也记 `spent`）。`script`、`storyboard`、`previz` 不入账。不传 `modality` 时，`prop` 和 `final_cut` 是 `unsupported`。
9. `over_budget` 或 `gate` 为 `budget` 时，把暂停文案原文给用户（三个数字在有币种时各带 ` {currency}` 后缀）。上调走 raise，放弃走 decline。不要自己改上限。raise 不够时响应里没有 `allowed`，而是 `gate` 为 `budget`、`over_budget` 为 true。
10. 改形象后若要重跑下游，先 impact 拿令牌，用户确认后再 confirm。令牌 30 分钟、一次性、绑死当时的节点集合。

---

## 确认规则

| 调用 | 要不要等用户 |
|------|----------------|
| 视频 `run` | 要。返回报价，`gate` 为 `confirm`，账本还没有行。用户同意后再 `confirm`，那时才 `authorize` 并写入账本。 |
| kind `character`、`scene`、`setting`、`keyframe`、`image`，以及 `vlm`、`tts` | 不要逐次确认。未超预算就在 `run` 里 `authorize`，并按 `quote.max` 记 `spent`（`provider_called` 为 false 也记账），节点 `done`。超预算才停，等的是预算决定。`prop` 和 `final_cut` 在不传 `modality` 时是 `unsupported`，不是这条。 |
| `script`、`storyboard`、`previz` | 不要确认，也不写账本。 |
| 超预算 | 要。把暂停文案给用户。raise 继续同一笔报价；decline 跳过，不提交。 |

视频节点没给 `model_id` 时，默认是 Seedance 2.5：`doubao-seedance-2-5-260628`，参考生视频。这不是 `models.video`（那一项默认仍是 `wan2.7-i2v`）。

Seedance 2.5 已核实时长是 4 到 30 秒，区间外提交 `-1`（允许 auto）。请求体里的 `duration` 被忽略。时长来自节点 metadata 的 `shot_duration`。默认时长是 -1，不会被当成计费秒数；不另给 `completion_tokens` 时，百万 token 的文档价格缩放不了，`run` 返回 `unavailable`，不写账本，也不要编一个价。

没有文档 API 价格（未核实、无币种、或没有 http(s) 来源）的模型同样不出数字。

---

## 视频提交失败时

供应商密钥没配、也没有注入适配器时：不 POST，不写 `submit_dispatched`，已保留的报价仍按 `quote.max` 记 `spent`，节点 `done`。这不是清标记。

普通 `confirm` 在 `submit_dispatched` 已经写上时不会再次 POST。标记只有在 `submit` 已经开始、随后又被判定为没发出 HTTP 时才清掉：`FileNotFoundError`、`UnsupportedParam`、`SubmitNotSent`，或错误文本含 `reference image file is missing`、`model is not registered`。这时报价改为 `skipped`。轮询、下载、供应商 failed/expired、所有 HTTP 4xx 都保留标记。4xx 体里若有 id，会记成 `vendor_task_id`。

手动重试是 `GraphExecutor.retry_video_submit`。本提交没有这条 HTTP。`r9/frontend-v2` 可能会加路由；加之前不要猜路径。`force_new=True` 只属于这次重试：先释放旧报价，新报价授权成功后才丢掉任务 id，然后才 POST。新报价超预算时标记留着，用 raise 继续。认领 TTL 30 分钟，Seedance 整次提交 20 分钟停。细节见 confirm_video.md。

---

## 状态

图上的状态是 `pending`、`running`、`awaiting_user`、`done`、`stale`、`failed`、`skipped`。`ready` 不是图状态。`GET /api/v2/projects/{project_id}/status` 的 `executable` 才是「现在可以跑」的节点。

`blocked` 是 `run` 的响应，不是存储状态：前驱没完成，不写账本。

账本状态：`quoted`、`awaiting_user`、`skipped`、`spent`、`unavailable`。

---

## 临时工作台

用户要单独问答、看图、文生图、图生图或一条短视频时，用 `/api/sandbox/*`。这些调用不报价、不确认、不进项目账本。视频时长默认 5 秒，不使用图执行器那条 4..30 秒的规则。请求体以 api.md 为准。

---

## 把结果交给用户

每完成一件用户能感知的事，发一条简报：刚完成什么、下一步、需要用户决定什么。有文件就发文件。视频确认前必须展示报价（币种、min、max、来源 URL）和是否超预算。

影响令牌、预算上调、视频 confirm，都等用户明确回复后再调用。不要替用户确认。

飞书或微信发图片和视频时，读 [feishu.md](references/send_message/feishu.md) 或 [wechat.md](references/send_message/wechat.md)。
