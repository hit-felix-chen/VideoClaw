# 确认视频并提交

只有视频要逐次确认。`run` 给出报价之后，把 `quote`（币种、min、max、`source_url`）和 `message` 给用户。用户同意后再调用：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/confirm"
```

没有请求体。这一步才 `ProjectBudget.authorize` 并写入账本。

用户拒绝、且还没有账本行：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/decline"
```

节点变为 `skipped`。若已经因为超预算写下 `awaiting_user` 账本行，decline 会把该行标为 `skipped`。

## 授权之后

未超预算：认领节点（`awaiting_user` → `running`），然后提交。成功则节点 `done`，账本 `spent`，`actual` 等于报价的 `max`。`result_id` 是远程 URL 或本地路径。本地文件在 `code/result/video/<project_id>/<node_id>.mp4`。裸任务 id 不算完成。

超预算：不提交。`gate` 为 `budget`。见 [budget.md](budget.md)。

并发的第二个 confirm 会 400，不会再授权、也不会再提交。

该模型的供应商没有密钥，也没有注入适配器时，不打供应商，仍会把准备好的请求存进节点并花掉已保留的报价。执行器只把 `ark`、`dashscope`、`kling` 的 `api_key` 当成已配置。Seedance 2.5 走方舟，读 `api_providers.ark.api_key`。

## 标记

提交前先把 `submit_dispatched` 写成 true 并落库。拿到任务 id 后写入 `vendor_task_id`。拿到结果 URL 后先落库 `result_obtained`，再在同一事务里记账并把节点标完成。

上面「没有密钥」的路径不写标记，也不把标记清掉：不 POST，直接花掉已保留的报价。

标记只有在已经写成 true、并且随后的 `submit` 被 `_request_was_not_sent` 判定为没发出 HTTP 时才清掉：`FileNotFoundError`、`UnsupportedParam`、`SubmitNotSent`，或错误文本含 `reference image file is missing`、`model is not registered`。这时报价改为 `skipped`，节点回到 `awaiting_user`。轮询失败、下载失败、供应商 failed 或 expired、所有 HTTP 4xx、超时，都保留标记。4xx 体里若有 `id`，会记成 `vendor_task_id`。之后普通 confirm 不会再 POST。

## 手动重试（无 HTTP）

`GraphExecutor.retry_video_submit(project_id, node_id, force_new=False)` 没有路由。不要发明 URL。`r9/frontend-v2` 可能会补一条；在那之前只能在进程内调用。

默认不新开任务：标记保持，有 `vendor_task_id` 就再轮询并花原来的报价（过期的 running 认领也一样），有 `result_obtained` 就直接结算、不再 POST。

`force_new=True` 只在这次重试上合法。它先释放旧报价，新报价授权成功之后才丢掉任务 id，然后才 POST。新报价放不进预算时，id 和标记都留着，用 raise 继续。把 `force_new` 传给普通 confirm 会 400。

认领 TTL 是 30 分钟。Seedance 整次提交在 20 分钟停下，避免轮询活过认领。超过 30 分钟且没有标记的 `running` 视为提交前就死了，未结算报价被跳过，节点可以再认领。有标记的过期认领不再次提交。启动脚本不会自动跑回收；下一次 confirm 在 TTL 之后会做同样的事。

## 各模型已核实时长

请求体 `duration` 无效。用节点 metadata 的 `shot_duration`，且必须落在下面的区间内，否则该模型允许 auto 时发送 -1，不允许则不发送时长。

| 模型 id | 秒 |
|---------|----|
| `doubao-seedance-2-5-260628` | 4..30，默认可 auto（-1） |
| `doubao-seedance-2-0-260128`、`doubao-seedance-2-0-fast-260128`、`doubao-seedance-2-0-mini-260615` | 2..12 |
| `wan2.6-i2v-flash`、`wan2.7-i2v` | 2..15 |
| `happyhorse-1.0-i2v`、`happyhorse-1.1-i2v`、`happyhorse-1.0-r2v`、`happyhorse-1.1-r2v`、`kling-v3` | 3..15 |
| `wan2.7-r2v` | 2..10 |

`kling-v2-6`、`kling-v2-5-turbo`、`seedance-1-0-pro`、`seedance-1-0-lite` 没有已核实时长。
