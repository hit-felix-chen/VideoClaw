# 资产依赖图

节点是资产。边从必须先存在的资产指向依赖它的资产。集只是分组，不是节点。

`POST /api/v2/projects` 把固定的一集图写进 `project.graph_json`（见 [create_project.md](create_project.md)）。没有「任意替换整张图」的路由。只走 `POST /api/library/projects` 时没有 `nodes`，这时：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/run"
```

返回 400，`detail` 为 `project has no dependency graph`。有图时 `GET /api/v2/projects/{project_id}/status` 返回 `edges`，以及每个节点的 `node_state`。视频节点在 `awaiting_user` 时还有 `quote`；账本停在 `awaiting_user` 时还有 `entry_id` 和 `budget_hold`。`provider_called: false` 会留在节点上。

## 图节点种类

已知值：`script`、`character`、`scene`、`shot_segment`、`final_video`、`previz`、`keyframe`、`draft_video`。其他非空字符串也能当节点种类。

资产库种类是另一份：`script`、`character`、`setting`、`prop`、`segment`、`final_cut`。片段引用要求库种类为 `segment`。

## 边

`hard`、`ref`、`continuity`、`inherit`。`blocks` 为 true 且前驱不是 `done` 时，后继跑不了。

## 状态

存在节点上的状态：`pending`、`running`、`awaiting_user`、`done`、`stale`、`failed`、`skipped`。

| 从 | 只能到 |
|----|--------|
| `pending` | `running`、`skipped` |
| `running` | `done`、`failed`、`awaiting_user`、`pending`、`stale` |
| `awaiting_user` | `done`、`failed`、`running`、`pending`、`stale` |
| `done` | `stale`、`pending` |
| `stale` | `running`、`pending`、`skipped` |
| `failed` | `running`、`pending`、`stale` |
| `skipped` | `pending` |

`ready` 不是图状态。预算上调后，`node_state` 表里可能短暂写成 `ready`，下一次视频 confirm 会把它认领走。

现在可以跑的节点是算出来的：状态为 `pending`、`stale` 或 `failed`，挡住它的前驱都已 `done`，并且该集显式依赖的集都已完成。`GET /api/v2/projects/{project_id}/status` 的 `executable` 就是这份列表。`awaiting_user` 与 `stale` 也在这个响应里。

`run` 返回 `status: "blocked"` 时看 `why_blocked.reasons`。类型为 `status`、`predecessor` 或 `episode`。这种情况不写账本。
