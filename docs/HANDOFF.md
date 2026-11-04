# VideoClaw 2.0 交接文档（Cursor 转 Codex）

写于 2026-11-04，基于 fork `hit-felix-chen/VideoClaw` 的 main `b78e4ca`（树 `ea2c9d3`）。架构和代码地图见 [DEVELOPMENT.md](DEVELOPMENT.md)，后端行为细节见 `video-claw/CLAUDE.md`。本文只写接手的人需要知道的事。

## 1. 先做这几件事

1. 克隆 fork：`git clone https://github.com/hit-felix-chen/VideoClaw.git`，切到 `main`，确认 HEAD 是 `b78e4ca` 或其后代。
2. 按 DEVELOPMENT.md 第 3 节建环境，跑后端 pytest（303 个）和前端 `tsc --noEmit`、`vitest run`（10 个）。三项都过，才是干净的起点。
3. 读 `video-claw/CLAUDE.md`（先读「Architecture」，约 200 行）和 `backend/docs/api.md` 的「已知遗留」。

## 2. 不能碰的东西

- **只能推到 fork，不能动上游。** 开发在 `hit-felix-chen/VideoClaw`。不要向 `HITsz-TMG/VideoClaw` 推送、提 PR、合并。
- **提交账号是 `hit-felix-chen`，不是 `hit-cxf`。** `hit-cxf` 是用户本人的账号。本机若同时登录两个 GitHub 账号，推送前确认当前用的是 `hit-felix-chen`。
- **不要把密钥写进聊天、提交、日志或测试。** 目前没有任何 Seedance 密钥。只有用户本人提供，才能做真实调用，而且每次真实调用前都要用户同意。不要去用户的机器上翻找密钥。
- 测试和评审期间不联网调用厂商。
- 用户的 Windows 笔记本上的文件不要随便删，要清理先问。

## 3. 工作方式（用户的要求）

- 每个阶段性成果（合入、评审结论、一步做完）要立即向用户汇报，其余时间不闲聊，直接做下一步。
- 一个分支合入之后，不要等指令，立即开始下一步。
- 用户用简体中文，回答用中文。
- 流程：开分支 → 做完 → 自检（全部测试、tsc、vitest）→ 评审（红线、账本一致性、状态码、旧测试、文档）→ 合入 fork main → 验证合并树等于被评审分支的树 → 汇报。
- 云端 agent 额度已经用完，所以评审和写文档都在本地做。

## 4. 现在的状态

main = `b78e4ca`，测试全过。已合入内容见 DEVELOPMENT.md 第 5 节。最后合入的两项：

- 取消（`d3f56f8`）：`stop` 对已提交的视频节点调用 Ark 取消接口，只有确认 `cancelled` 才释放报价和跳过节点；已成功则按完成入账；其余一律保留。
- 首尾帧插件（`52567da`、`eae4828`）：`run` 可带 `first_frame_asset_id`、`last_frame_asset_id`、`continue_from_tail`；报价前检查，不支持的模型直接拒绝，不自动提交。

两个分支是并行做的，合入时只在 `EpisodeStudio.tsx` 的一行 import 上冲突，已按双方保留解决。合并后的整体没有在浏览器里点过。

## 5. 建议的下一步（按价值排序）

1. **在浏览器里走一遍单集流程**（不需要密钥）：创建项目、编辑提示词、运行到视频节点停在确认、首帧选择、停止。重点看节点卡里首尾帧选择和取消提示是否显示正确。发现问题就修，这是合并后第一件事。
2. **第二次冒烟测试，真实 Seedance 提交一次。** 需要用户通过加密输入提供密钥并同意。目标：验证提交、轮询、下载、取消排队任务、首帧 payload 在真实接口上的行为，把结果回写到 `CLAUDE.md` 和 `api.md`。没有密钥就不要做。
3. 决定图像、VLM、TTS 节点是否也要逐次确认（现在不经确认直接按 `quote.max` 入账，`provider_called` 为 false）。需要用户拍板，再改。
4. 图的灵活性：`POST /api/v2/projects` 现在固定一集 `ep1`，加上按镜头数或集数生成图的参数。
5. 路线图剩余项：R9 `previz.*` 接口和 `previz.text`、R10 `previz.image`、R11 agent 工具化并重写 `SKILL.md`、R12 其余前端（项目列表、资产库、依赖图视图）、R13 音频层、R14 草稿视频与抽卡。issue 在 fork 的 #6 到 #20，#13 和 #14 仍开着。
6. 可灵、百炼的取消接口核实（目前返回「不支持」，保守且正确，只有核实后才加）；项目内沙盒视频；旧 `config.yaml` 模型 id 迁移。

## 6. 容易踩的坑

- 分支名 `rNN` 与路线图 R 编号不是一回事（DEVELOPMENT.md 第 5 节）。
- 只改代码不改文档会被评审打回。`CLAUDE.md`、`api.md`、`references/workflow/*.md` 要和行为一起改，`CLAUDE.md` 第 3 行写着它核对过的提交号，改完更新。
- 不要把任何放宽账本规则的改动混在无关提交里。账本、预算、提交标记、`_ClaimLost`、取消这几块曾经反复返工，改它们要写测试覆盖竞态（并发 confirm、提交后停止、过期认领恢复）。
- 新价格必须带来源链接，且是官方文档。不确定就不出数字。
- 默认模型：LLM `deepseek-flash`，VLM `qwen3.7-plus`，视频 `doubao-seedance-2-5-260628`（`reference_to_video`）。
- 记录在案的厂商调用接口：Ark 取消是 `DELETE /api/v3/contents/generations/tasks/{id}`，只有排队中能取消；Seedance 首尾帧用 `content[].type=image_url` 加 `role` 为 `first_frame` 或 `last_frame`，`ratio` 只能是 `adaptive`，且不能与参考图同时使用。

## 7. 分工背景

之前 Mochi 负责评审和合入，另一个 agent 负责排任务和写代码。换到 Codex 后由你同时承担这两件事，评审要在写完代码之后单独再看一遍 diff。项目是 HITsz-TMG 的 VideoClaw，2.0 在 fork 里开发，用户通过聊天下达指令和做决定（预算、密钥、是否放宽确认规则）。
