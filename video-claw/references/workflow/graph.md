# 资产依赖图

节点是资产。边从必须先存在的资产指向依赖它的资产。集只是分组，不是节点。

本提交没有保存或读取图的 HTTP。`GraphStore` 把 `DependencyGraph.to_dict()` 放在项目的 `graph_json`。没有这份快照时：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/run"
```

返回 400，`detail` 为 `project has no dependency graph`。不要改去调 `/api/project`。

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
