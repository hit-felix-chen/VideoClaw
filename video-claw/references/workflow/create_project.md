# 创建项目

项目是 SQLite 里的一行，不是 session 文件。没有 `POST /api/project/start`。

## 请求

```bash
curl -X POST "http://127.0.0.1:8000/api/library/projects" \
  -H "Content-Type: application/json" \
  -d '{"name": "第一集", "budget_cap": 100}'
```

`budget_cap` 可以不传或传 null，表示不封顶。不封顶时不会因为超预算暂停。

## 响应

项目记录：`id`，`name`，`created_at`，`updated_at`，`budget_cap`，`spent`，`status`（新建为 `active`）。后面的路径都用这个 `id` 作为 `project_id`。

## 接着做

列出项目：`GET /api/library/projects`。打开一个：`GET /api/library/projects/{project_id}`。

没有改名、改上限（除了预算 raise）、删除项目的 HTTP。

创建项目不会生成图。节点要跑，`graph_json` 里必须已经有 `nodes`。见 [graph.md](graph.md)。

资产用 `POST /api/library/assets`。种类只能是 `script`、`character`、`setting`、`prop`、`segment`、`final_cut`。`scope` 默认 `project`，此时 `project_id` 必填。全局资产不要带 `project_id`。
