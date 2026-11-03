# 临时工作台视频

`POST /api/sandbox/video` 直接调用 `VideoClient`。不报价、不确认、不进项目账本，也不使用图执行器那条 4–30 秒的时长规则。

## 请求

`model` 与 `prompt` 必填。处理函数不填默认模型，也不会改成 `wan2.7-i2v`。示例用已注册的 `wan2.6-i2v-flash`。

```bash
curl -X POST "http://127.0.0.1:8000/api/sandbox/video" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "wan2.6-i2v-flash",
    "prompt": "一只猫在草地上奔跑"
  }'
```

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| model | 是 | 无 | 传给 `VideoClient.generate_video` 的模型 id |
| prompt | 是 | 无 | 提示词 |
| image | 否 | null | 省略或 null 时，客户端收到的 `image_path` 是空字符串 |
| ratio | 否 | `16:9` | |
| resolution | 否 | `720P` | |
| duration | 否 | `5` | 整数秒，原样交给客户端。不是 Seedance 2.5 的 4–30 |

## 响应

成功：

```json
{
  "success": true,
  "result": "",
  "video_path": "",
  "record_id": ""
}
```

`video_path` 是挂在 `/code` 下的相对路径，文件在 `code/result/sandbox/videos/`。失败时 HTTP 仍可能是 200，体为 `{"success": false, "error": "..."}`。

可选模型以 `GET /api/models?media_type=video` 为准，不要用这份旧清单里没注册的 id。
