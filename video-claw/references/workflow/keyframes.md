# 关键帧：首帧与首尾帧

视频节点的 `run` 可以指定资产库里已经存在的图片，而不是参考图。

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/nodes/{node_id}/run" \
  -H "Content-Type: application/json" \
  -d '{"first_frame_asset_id": "<资产 id>", "last_frame_asset_id": "<资产 id>"}'
```

`last_frame_asset_id` 可以省略。省略一个字段会沿用节点 metadata 里已有的 id。空字符串会清掉它。

## 资产从哪来

只能是 `ImageStore` 里已经挂在某个资产上的图片，并且 `BlobStore` 里的文件还在磁盘上。不接受临时上传，不抽视频帧，也不调用方舟的 `return_last_frame`。剧集节点卡片只列出「已经有图片」的资产。

## 什么时候拒绝

这些都发生在报价和 `authorize` 之前。HTTP 400，`detail` 是中文，账本没有新行。

| 情况 | `detail` |
|------|----------|
| 模型的适配器能力里没有 `first_frame_i2v` | `模型 {id} 不支持首帧生视频` |
| 没有 `start_end_frame_i2v` 却给了尾帧 | `模型 {id} 不支持首尾帧生视频` |
| 只给了尾帧 | `只指定了尾帧，没有首帧` |
| 资产没有可用图片 | `首帧资产不在资产库中` 或 `尾帧资产不在资产库中` |
| 提示词里的 `@资产名` 已经解析成参考图 | `首帧与参考图不能同时使用` |

能力看注册表的 `adapter_abilities`。这张表为空时才看 `abilities`。Seedance 2.5（`doubao-seedance-2-5-260628`）两种都有。`wan2.7-r2v` 两种都没有。`wan2.6-i2v-flash` 只有首帧。

没有文档 API 价格的模型，在关键帧检查通过之后仍然 `unavailable`，不编价格。静帧不算 `includes_video_input`。

无密钥时，`confirm` 仍是 409：`未配置视频厂商密钥，不会提交也不会入账`。`allow_offline_video=True` 才会按 `quote.max` 入账，存下的仍是资产 id，不是文件路径。

## 提交给厂商的字段

授权之后、`adapter.submit` 之前，资产 id 换成磁盘路径。Seedance 2.5 的创建任务体使用官方字段，不另起名字：

- `content[].type` = `image_url`
- `content[].image_url.url` = 图片 URL 或 `data:image/<格式>;base64,...`
- `content[].role` = `first_frame`，尾帧再加一条 `last_frame`
- `ratio` = `adaptive`（首帧和首尾帧都只能是这个值）

`reference_image`、`reference_video`、`reference_audio` 与上面两种任务互斥，所以关键帧请求不会再带参考图。

依据：

- https://ark.volcengine.com/region:cn-beijing/docs/seedance-2-5
- https://ark.volcengine.com/region:cn-beijing/docs/create-video-generation-task-api
