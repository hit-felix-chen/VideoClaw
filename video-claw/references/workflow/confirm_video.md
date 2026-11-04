# 确认视频并提交

只有视频要逐次确认。`run` 给出报价之后，把 `quote`（币种、min、max、`source_url`）和 `message` 给用户。用户同意后再调用：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/confirm"
```

不要带 `force_new`。查询参数或 JSON 里只要出现这个字段（包括 `false`）就是 400：`force_new is only valid on retry_video_submit`。JSON `{"force_new": null}` 目前是 422。省略该字段才确认。前端送的是 `{}`。

没有密钥、也没有注入适配器时，这条和 `retry_video_submit`、视频节点的 `raise`、`impact/confirm`、`impact/regenerate` 都是 409：`未配置视频厂商密钥，不会提交也不会入账`。账本不会多出视频行，也不写 `submit_dispatched`。进程内同一个检查，默认也拒绝。`GraphExecutor(..., allow_offline_video=True)` 才按 `quote.max` 入账，而且不要求先有结果 URL。没有被这道 409 拦住时，这一步才 `authorize` 并写入账本。入账金额是 `quote.max`，没有结果 URL 也会记。

用户拒绝、且还没有账本行：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/decline"
```

节点变为 `skipped`。若已经因为超预算写下 `awaiting_user` 账本行，decline 会把该行标为 `skipped`。

## 授权之后

未超预算：认领节点（`awaiting_user` → `running`），然后提交。成功则节点 `done`，账本 `spent`，`actual` 等于报价的 `max`。`result_id` 是远程 URL 或本地路径。本地文件在 `code/result/video/<project_id>/<node_id>.mp4`。裸任务 id 不算完成。

超预算：不提交。`gate` 为 `budget`。见 [budget.md](budget.md)。

并发的第二个 confirm 会 400，不会再授权、也不会再提交。

`confirm`、`retry_video_submit`、视频节点的 `raise`、`impact/confirm` 和 `impact/regenerate` 在没有注入适配器且供应商密钥为空时，授权前就是 409：不入账，也不写 `submit_dispatched`。见上文。执行器只把 `ark`、`dashscope`、`kling` 的 `api_key` 当成已配置。Seedance 2.5 走方舟，读 `api_providers.ark.api_key`。图像节点不走这道 409。

## 标记

提交前先把 `submit_dispatched` 写成 true 并落库。拿到任务 id 后写入 `vendor_task_id`。拿到结果 URL 后先落库 `result_obtained`，再在同一事务里记账并把节点标完成。

缺密钥且没有注入适配器是上面的 409：不写标记，也不入账。`allow_offline_video=True` 的进程内 `confirm` 不写标记，但会花掉已保留的 `quote.max`。认领比较失败（`_ClaimLost`）时释放这次报价；若节点仍是 `running` 且没有 `submit_dispatched` / `vendor_task_id`，写回 `awaiting_user`，不用等 30 分钟回收。这次回写如果比较并保存失败，会打 warning，节点会留在 `running`，直到 `recover_stale_submit_claims`（30 分钟）回收。

标记只有在已经写成 true、并且随后的 `submit` 被 `_request_was_not_sent` 判定为没发出 HTTP 时才清掉：`FileNotFoundError`、`UnsupportedParam`、`SubmitNotSent`，或错误文本含 `reference image file is missing`、`model is not registered`、`ARK_API_KEY not set`。`ARK_API_KEY not set` 只属于已经进入 `submit` 的适配器，不是 HTTP 409。这时报价改为 `skipped`，节点回到 `awaiting_user`。轮询失败、下载失败、供应商 failed 或 expired、所有 HTTP 4xx、超时，都保留标记。4xx 体里若有 `id`，会记成 `vendor_task_id`。之后普通 confirm 不会再 POST。`submit_dispatched` 落库后，视频 `stop` 按方舟文档尝试取消，见下文「提交之后停止」。

## 手动重试

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/retry_video_submit" \
  -H "Content-Type: application/json" \
  -d '{"force_new": false}'
```

省略请求体是续接。`force_new` 按 Pydantic 的 `bool` 解析，不是只有 `true` 和 `"true"`。当前环境实测：JSON `true`、`1`，以及 `"true"` / `"True"` / `"yes"` / `"on"` / `"1"` / `"y"` / `"t"`（大小写不限）才新开并重新授权；JSON `false`、`0`，以及 `"false"` / `"no"` / `"off"` / `"0"` / `"n"` / `"f"` 仍是续接。`null` 是 422。带首尾空格的字符串也解析失败。前端送的是布尔值。新开时先释放旧报价，重新授权成功后才丢掉 `vendor_task_id`，然后 POST。无密钥时这条 HTTP 也是 409，不入账。

默认续接：标记保持，有 `vendor_task_id` 就再轮询并花原来的报价（过期的 running 认领也一样），有 `result_obtained` 就直接结算、不再 POST。新报价放不进预算时，id 和标记都留着，用 raise 继续。

认领 TTL 是 30 分钟。Seedance 整次提交在 20 分钟停下，避免轮询活过认领。超过 30 分钟且没有标记的 `running` 视为提交前就死了，未结算报价被跳过，节点可以再认领。有标记的过期认领不再次提交。进程启动时 `recover_stale_submit_claims` 跑一次；没有过期行时返回空列表，再启动一次仍是空操作。下一次 confirm 在 TTL 之后也会回收。

认领在写入完成之前被别人改掉（例如同时 `stop`，且当时还没有 `submit_dispatched`）时，confirm 返回 409：`状态已变化，请刷新`。这次 `stop` 会在同一事务里把该节点仍为 `quoted` 的账本行改成 `skipped`。

## 提交之后停止

`POST /api/v2/projects/{project_id}/stop`。还没写上 `submit_dispatched` 的 `running` 节点仍标成 `failed`（`stop_reason` 为 `用户停止`），并释放未结算报价。

视频节点已经写上标记或任务号时，调用适配器的 `cancel`。方舟只按官方文档做：

- 查询 `GET /api/v3/contents/generations/tasks/{id}`。来源：https://ark.volcengine.com/region:cn-beijing/docs/get-video-generation-task-api
- 只有状态 `queued` 才 `DELETE` 同一路径。成功页写的是 HTTP 200 且体为 `{}`，排队任务变为 `cancelled`。来源：https://www.volcengine.com/docs/82379/1521720 与 https://ark.volcengine.com/region:cn-beijing/docs/cancel-or-delete-video-generation-tasks-api
- 随后再查询，状态确是 `cancelled`（或一开始就是 `cancelled`）才把报价改成 `skipped`，节点改成 `skipped`，`stop_reason` 为 `用户取消`。
- `running` 官方写明不支持删除，不调用 DELETE。
- `succeeded`，或节点上已经有 `result_obtained`，按完成入账（`spent` + `done`），不释放报价。取消请求发出后若查到已成功，同样按完成处理。
- `failed` / `expired` 不是取消。对它们调用 DELETE 会删掉记录且之后不能查询，因此不调用。
- 非 200、超时、未知状态、DELETE 之后记录消失：结果不确定，不释放报价，节点状态不变。取消接口文档没有写拒绝时的 HTTP 状态码。

没有任务号、没有密钥、或该厂商没有已核实的取消接口：同样不释放，HTTP 409，原因写明报价保持保留。这和确认视频时的 409 `未配置视频厂商密钥，不会提交也不会入账` 不是同一句：那里是授权前拒绝，这里是报价已经占着、不能把它当成已经取消。

非视频节点即使带了标记也不会去调用取消。一个都没停成、也没有按完成入账时，409 的 `detail` 是 `没有节点被停止` 加上上述原因。

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
