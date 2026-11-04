# VideoClaw 2.0 开发文档

基于 fork `hit-felix-chen/VideoClaw` 的 main `b78e4ca`。更细的行为说明在三处，本文不重复，只给入口和判断依据：

- `video-claw/CLAUDE.md`：后端行为的权威说明（图、执行器、报价、预算、提交标记、关键帧、取消），逐条对着代码核实过。
- `video-claw/video-claw/backend/docs/api.md`：HTTP 路由、请求体、错误码和「已知遗留」。
- `video-claw/references/workflow/*.md`：给调用 API 的 agent 看的流程说明。

## 1. 这是什么

VideoClaw 2.0 是「资产驱动的依赖图 + 本地资产库」。项目是 SQLite 里的一行，不再是 session 文件。节点就是资产，边表示先后依赖。视频生成默认走 Seedance 2.5 的参考图生视频，每次调用厂商之前必须有人确认，且受预算上限约束。首帧、首尾帧、尾帧接续是可选插件，不是主路径。

目录：

- `video-claw/video-claw/backend`：FastAPI 后端（`python api_server.py`，默认 `127.0.0.1:8000`）。
- `video-claw/video-claw/frontend`：Next.js 前端（开发服务器 3000）。
- `video-claw/SKILL.md`、`video-claw/references/`：驱动本地 API 的 agent 技能。
- `FilmAgent/`：仓库里原有的另一个项目目录，2.0 开发不涉及，不要改动。

## 2. 红线（设计原则，改代码前先读）

1. 先确认，再调用厂商。视频节点 `run` 只报价并停在 `awaiting_user`，只有 `confirm` 才会授权预算并提交。
2. 预算不自动花钱。超过上限就暂停，等人 `raise` 或 `decline`。
3. 不编造价格。没有文档价格（已核实、有币种、有 http(s) 来源链接）的模型不出数字，也不提交。
4. 没有厂商密钥就不提交也不入账。视频节点在 `_require_video_vendor` 处直接 409，只有显式 `allow_offline_video=True` 才允许离线入账（仅供测试）。
5. 只有可以确认的结果才释放报价。取消只对排队中的 Ark 任务生效；状态不确定、超时、记录消失一律保留报价和节点状态。
6. 只用本地 SQLite，不接别的数据库，不兼容旧 session。
7. 不弱化已有测试。改测试断言要在提交说明里写明原因。

## 3. 环境与命令

后端（Python 3.13 已验证；依赖见 `backend/requirements.txt`，开发依赖是 `pytest` 和 `pytest-asyncio`，缺 `pytest-asyncio` 会有一个测试因 async 配置失败）：

```bash
cd video-claw/video-claw/backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest          # 当前 303 个全过，约 5 秒，不联网
.venv/bin/python api_server.py      # 启动后端
```

前端：

```bash
cd video-claw/video-claw/frontend
npm ci
npx tsc --noEmit                    # 当前通过
npx vitest run                      # 当前 10 个通过
npm run build && npm start
```

CI：`.github/workflows/tests.yml` 跑后端 pytest 和前端 `tsc --noEmit`。

跑测试时不要配置真实厂商密钥（`ARK_API_KEY`、`DASHSCOPE_API_KEY` 等），测试套件本身不联网。密钥只放在 `config.yaml` 的 `api_providers.<provider>.api_key`，`config.yaml` 不进仓库，`config.yaml.example` 是模板。

## 4. 代码地图

| 位置 | 作用 |
|------|------|
| `backend/graph/` | `DependencyGraph`、节点状态机、边类型（hard/ref/continuity/inherit） |
| `backend/core/executor.py` | `GraphExecutor`：`run_node`、`confirm`、`stop`、`retry_video_submit`、影响面再生成、提交标记与恢复 |
| `backend/core/keyframes.py` | 首帧、首尾帧、尾帧接续插件 |
| `backend/core/episode.py` | 创建固定的一集图（`ep1`） |
| `backend/core/agents/`、`core/nodes/dispatch.py` | 各节点的 runner；runner 不调用厂商，只构造请求 |
| `backend/models/middle/` | 模型中间层：注册表、能力规格、价格表 `pricing.py`、预算 `budget.py`、适配器 `adapter.py` |
| `backend/models/video_seedance.py` | `ArkVideoAdapter`：提交、轮询、下载、取消 |
| `backend/storage/` | SQLite、资产库、项目、账本 `ledger.py`、影响面令牌 `confirmations.py` |
| `backend/api/routers/` | HTTP 路由（`executor.py` 是 `/api/v2/projects/...`） |
| `frontend/components/episode/` | 单集运行界面 `EpisodeStudio`、`NodeCard`、`RetryPanel` |
| `frontend/lib/episodeFlow.ts`、`episodeApi.ts` | 前端流程辅助与 API 调用 |

## 5. 已完成的内容

路线图在 fork 的 issues #6 到 #20，对应 R0 到 R14。**注意：分支名里的 `r9`、`r11`、`r12` 是工作批次，不等于路线图的 R9、R11、R12。** 对照如下。

路线图完成情况：

- R0 清场、R1 模型中间层、R2 LLM 统一走 OpenAI 兼容接口、R3 价格表与预算、R4 SQLite 项目、R5 资产库、R6 依赖图、R7 图驱动执行器（含 R7b 影响面再生成）、R8 参考图生视频为主路径：均已合入。issue #13、#14 仍开着，因为遗留项见第 6 节。
- R12 前端重做：只做了「单集运行界面」（`r9/frontend-v2`），项目列表、资产库界面、依赖图视图未做。

合入顺序（fork main 的关键节点）：

| main | 内容 |
|------|------|
| `a534014` | R8 参考图生视频切换 |
| `0c6f9f4` | `r8b` 真实接入 `ArkVideoAdapter.submit`，带提交标记、`vendor_task_id`、`retry_video_submit`、逐节点 compare-and-set 保存 |
| `576dbc5` | `r10` 文档改成 v2 |
| `14d5aaf` | `r9` 前端 v2 单集界面和 `/api/v2/projects` 路由 |
| `faf5988` | `r10b` 文档补 r9 的路由 |
| `d8dcfde` | `r9b` 无密钥守卫：确认、重试、上调预算、影响面确认/再生成无密钥时 409 |
| `40dc926` | `r9c` 沙盒视频在项目外一律 409，关闭预算绕过 |
| `b78e4ca` | `r11` 提交后取消排队中的 Ark 任务，`r12` 首帧/首尾帧/尾帧接续插件 |

## 6. 已知缺口和未验证项

1. **没有真实厂商验证过。** 从未用真实 Seedance 密钥提交过一次视频。提交、轮询、下载、取消、首尾帧 payload 都只靠单元测试和官方文档核对。第二次冒烟测试（真实四厂商）需要用户自己提供密钥。
2. 图永远是固定的一集：`POST /api/v2/projects` 只建 `ep1`（脚本、分镜、角色、关键帧引用、镜头视频、TTS），没有按用户集数或镜头数生成图的参数。
3. 图像、VLM、TTS 节点在 `run` 里不经确认就按 `quote.max` 入账，`provider_called` 为 false，前端显示「已记账·未调用厂商」。是否像视频一样逐次确认，用户还没决定。
4. 可灵、百炼（DashScope）视频没有核实过取消接口，`cancel` 返回不支持。
5. 沙盒视频只返回 409。要不要做「项目内的沙盒视频」（走项目预算）还没定。
6. 前端节点卡的首尾帧选择和取消流程没有在浏览器里实测，只有类型检查和少量 vitest。
7. 旧 `config.yaml` 里已退役的模型 id 不会被自动改写。
8. 路线图未开始：R9 预演接口 `previz.*`、R10 `previz.image`、R11 agent 工具化并重写 `SKILL.md`、R13 音频层（TTS、字幕、BGM）、R14 低清草稿视频与抽卡。

## 7. 常见改动的做法

- 加一个视频厂商：在 `models/middle/catalog.py` 加规格（能力、时长限制、价格，价格必须带 `verified`、币种、来源链接），在 `adapter.py` 实现 `submit`，没有核实过的取消接口就不要实现 `cancel`（缺省即「不支持」）。
- 加一种节点：`graph/model.py` 的 `AssetKind` 接受任意非空字符串；runner 在 `core/nodes/dispatch.py` 分派；要确定它属于哪一档计费（见 `CLAUDE.md` 的 run_node 表）。
- 改状态机：`NodeStatus` 的合法转换在 `graph/model.py`，改动要同步 `CLAUDE.md`、`api.md` 和测试。
- 改 HTTP 行为：同步 `api.md` 和 `references/workflow/`；CLAUDE.md 开头写了核对的提交号，改完更新它。

## 8. 提交与评审约定

- 每个改动一个分支，分支名形如 `rNN/short-name`，完成后向 fork 的 main 合入；**永远不要向上游 `HITsz-TMG/VideoClaw` 推送、提 PR 或合并。**
- 合入前评审要点：红线是否被削弱；账本和节点状态是否一致（尤其是取消、恢复、并发 confirm、`_ClaimLost` 的竞态）；HTTP 状态码和中文提示；旧测试有没有被放宽；文档是否同步。
- 合入后验证：合并提交的树要等于被评审分支的树。
