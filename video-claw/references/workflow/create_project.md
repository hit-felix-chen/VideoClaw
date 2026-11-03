# 创建项目

项目是 SQLite 里的一行，不是 session 文件。没有 `POST /api/project/start`。

要一集能跑的图，用执行器这条。它创建项目并写入固定的 `ep1`（`core/episode.py`）。

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects" \
  -H "Content-Type: application/json" \
  -d '{"name": "第一集", "budget_cap": 100, "idea": "林夏走进教室"}'
```

`name` 必填。可选 `budget_cap`（省略或 null 表示不封顶）、`idea`、`llm_model`、`image_model`、`video_model`、`tts_model`。空字符串不会写成模型 id。

响应含 `project`、`episode_id`（恒为 `ep1`）、`graph`、`nodes`、`edges`。节点是 `script`、`storyboard`、`character`、`reference`（kind `keyframe`）、`video`（kind `shot_segment`）、`tts`。边是剧本到分镜，分镜到角色和参考图，两者再到视频，视频再到配音。视频节点带上 `resolution` `720p`、`includes_video_input` false、`completion_tokens` 1000000、`shot_duration` 6，所以这条默认视频可以被报价。播种失败会删掉刚建的项目行。图不能按集数展开，永远这一集。

## 删除

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v2/projects/{project_id}"
```

成功：`{"status": "deleted", "id": "..."}`。任一节点正在 `running`，或已有 `submit_dispatched` / 非空 `vendor_task_id`，或 `node_state` 为 `running`：409，`项目有尚未结束的提交，不能删除`。项目不存在：404。没有改项目名称的 HTTP。预算上限仍用 raise 改。

## 只建空项目

```bash
curl -X POST "http://127.0.0.1:8000/api/library/projects" \
  -H "Content-Type: application/json" \
  -d '{"name": "第一集", "budget_cap": 100}'
```

这条不写图。随后 `run` 会 400：`project has no dependency graph`。列出：`GET /api/library/projects`。打开：`GET /api/library/projects/{project_id}`。

资产用 `POST /api/library/assets`。种类只能是 `script`、`character`、`setting`、`prop`、`segment`、`final_cut`。`scope` 默认 `project`，此时 `project_id` 必填。全局资产不要带 `project_id`。图的形状见 [graph.md](graph.md)。

## 停止、改模型、改提示词

`POST /api/v2/projects/{project_id}/stop` 只把尚未发给厂商的 `running` 节点标成 `failed`（`stop_reason` 为 `用户停止`），并在同一事务里把该节点 `quoted` 且还没有实际金额的账本行改成 `skipped`。已有 `submit_dispatched` 或任务号的节点停不掉，全部如此时接口是 409。不能靠 stop 取消厂商调用。

`PATCH /api/v2/projects/{project_id}/models` 的体可以含 `llm_model`、`image_model`、`video_model`、`tts_model`，以及 `nodes`（节点 id 到模型 id）。未注册的 id 是 400。`running`、`awaiting_user`、或仍留着厂商任务的节点不改。

`PATCH /api/v2/projects/{project_id}/nodes/{node_id}/prompt` 的体是 `{"text": "..."}`。节点正在运行时不写。
