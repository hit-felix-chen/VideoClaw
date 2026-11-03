# 片段引用

`SegmentRefStore` 把一条库资产（种类必须是 `segment`）绑到另一个资产，以及可选的那个资产的一个形象。

## 绑定

```bash
curl -X POST "http://127.0.0.1:8000/api/library/segments/{segment_asset_id}/refs" \
  -H "Content-Type: application/json" \
  -d '{"asset_id": "<角色资产 id>", "look_id": "<形象 id>", "role": "主角"}'
```

`look_id`、`role`、`alias` 可选。形象必须属于该资产。同一片段、同一资产、同一形象不能绑第二次。

列出：`GET /api/library/segments/{segment_asset_id}/refs`，响应 `{"refs": [...]}`。

删除：`DELETE /api/library/refs/{ref_id}`。

## 从正文解析

```bash
curl -X POST "http://127.0.0.1:8000/api/library/resolve" \
  -H "Content-Type: application/json" \
  -d '{"text": "参考 @林夏/日 走进房间", "project_id": "<project_id>"}'
```

匹配 `@资产名` 和 `@资产名/形象名`，名称必须完全相等。带 `project_id` 时，同名的项目资产优先于全局资产，不搜其他项目。两条都看得见则 `ambiguous` 为 true，不挑一条。没有资产时 `error` 为 `asset not found`；资产在、形象不在时为 `look not found`。

节点 `run` 会用同一套解析读 `text`。视频提交前才把解析到的资产 id 换成磁盘上的图片文件。文件不在则跳过报价，节点回到等待，不会把资产 id 当成 URL 送出去。
