# 跑一个节点

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/run" \
  -H "Content-Type: application/json" \
  -d '{"model_id": "doubao-seedance-2-5-260628", "text": "参考 @林夏/日"}'
```

体可以省略。字段见 api.md 的 `NodeRunRequest`。视频节点上的 `duration` 会被接受然后忽略。

## 默认模型

视频节点没有 `model_id` 时用 Seedance 2.5：`doubao-seedance-2-5-260628`，参考生视频（`reference_to_video`）。别名 `doubao-seedance-2.5` 指向同一个 id。

配置里的 `models.llm` 默认 `deepseek-flash`，`models.vlm` 默认 `qwen3.7-plus`。密钥在 `api_providers.*.api_key`。图执行器不读取 `models.video`（默认仍是 `wan2.7-i2v`）来填空的视频节点。

## 门禁

| 节点 | `run` 做什么 |
|------|----------------|
| 视频确认门：`shot_segment`、`draft_video`、`final_video`、`segment`、`video`，或 `modality` 为 `video` | 报价后停在 `awaiting_user`，`gate` 为 `confirm`。账本还没有行。等用户同意再 confirm。结算金额是 `quote.max`。 |
| 直接入账：`character`、`scene`、`setting`、`keyframe`、`image`，或 `modality` 为 `image`；kind 或 modality 为 `vlm`、`tts` | 不逐次确认。`run` 里直接 `ProjectBudget.authorize`。未超预算则节点 `done`，`spent` true，`actual` 等于 `quote.max`。`provider_called` 为 false 时也记这一笔。超预算则 `gate` 为 `budget`，不跑。 |
| `script`、`storyboard`、`previz` | 不报价、不入账，跑完为 `done`。 |
| 不在上面、且没有显式 `modality` | `unsupported`，`node has no billable modality`。库种类里是 `prop` 和 `final_cut`。`character` / `scene` / `setting` / `keyframe` / `image` 不是这一行。 |
| 前驱挡住 | `blocked`，不写账本。 |

节点执行器（剧本、分镜、角色、参考图、TTS）返回 `provider_called: false`，不自己打供应商。视频的 HTTP 只发生在 confirm 授权之后的 `adapter.submit`。

`@资产名` 和 `@资产名/形象名` 在这次调用里解析。见 [segment_refs.md](segment_refs.md)。

视频节点还可以在请求体里带 `first_frame_asset_id` 和 `last_frame_asset_id`（资产库已有图片）。`continue_from_tail: true` 改用上一镜头已保存的尾帧资产，不能再手选首帧。能力不够、资产不存在、没有上一镜头，或和参考图混用时，`run` 直接 400，中文 `detail`，不写账本。见 [keyframes.md](keyframes.md)。

## 报价

没有文档 API 价格（未核实、没有币种、或没有 http(s) 来源）时，`min` / `max` 为 null，`unknown_models` 带上该 id。不要编一个 0。

Seedance 2.5 有文档价格，单位是元 / 百万 token：无视频输入时 480p 与 720p 为 70，1080p 为 77；有视频输入时 480p 与 720p 为 42，1080p 为 46。要选出唯一一行，请求里必须同时有 `resolution` 和 `includes_video_input`。缺一个就匹配不到恰好一条价，`run` 返回 `unavailable`。选出一行之后，`duration.default` 仍是 -1，不会被当成计费秒数；不传 `completion_tokens` 时价格还是缩放不了，同样 `unavailable`，节点保持 `pending`，不写账本。响应里的 `duration` 仍说明能力：最短 4 秒，最长 30 秒，允许 auto。

镜头真正提交的秒数来自节点 metadata 的 `shot_duration`，且必须落在该模型已核实的区间里。Seedance 2.5 区间外发送 -1。其他已核实区间见 [confirm_video.md](confirm_video.md)。

## 只重跑这一个

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/regenerate" \
  -H "Content-Type: application/json" \
  -d '{"text": "改过的提示词"}'
```

后继标成 `stale`，不会被这次调用启动。`done`、`stale`、`failed`、`pending` 可以重跑；`running` 和 `awaiting_user` 会 400。

看整体：`GET /api/v2/projects/{project_id}/status`。
