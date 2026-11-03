# 临时工作台视频

`POST /api/sandbox/video` 的请求体没有 `project_id`。通过校验的请求一律 HTTP 409，体为 `{"success": false, "error": "沙盒视频需在项目内生成"}`。不调用 `VideoClient`，不写历史，不入账。有项目上下文的生成还没做，不要给这个请求体加 `project_id`。

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
| model | 是 | 无 | 仍要校验，不会传给厂商 |
| prompt | 是 | 无 | 提示词 |
| image | 否 | null | |
| ratio | 否 | `16:9` | |
| resolution | 否 | `720P` | |
| duration | 否 | `5` | 不会按 4–30 秒改写，因为请求到不了厂商 |

## 响应

缺 `model` 或 `prompt`：422。字段齐了：

```json
{"success": false, "error": "沙盒视频需在项目内生成"}
```

HTTP 409。不生成文件，也没有 `video_path`。沙盒页把 `error` 显示出来。

可选模型以 `GET /api/models?media_type=video` 为准，不要用这份旧清单里没注册的 id。
