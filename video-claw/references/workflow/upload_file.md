# 上传文件

上传只负责把文件放进 `backend/code/temp`。没有接口把 `file_path` 自动写成剧本或写进图。

## 文档

`POST /api/upload_file`，表单字段名 `file`。允许 `.docx`、`.doc`、`.txt`、`.md`、`.pdf`。

```bash
curl -X POST "http://127.0.0.1:8000/api/upload_file" \
  -F "file=@/path/to/local/file.txt"
```

响应：

```json
{"filename": "file.txt", "file_path": "1720392941_file.txt"}
```

`file_path` 是 temp 目录下的文件名，不是绝对路径。

## 媒体

`POST /api/upload_media`，字段名同样是 `file`。允许 `.jpg`、`.jpeg`、`.png`、`.webp`、`.bmp`、`.mp4`、`.mov`、`.avi`、`.mkv`、`.webm`。

响应里的 `file_path` 是绝对路径，和文档上传不一样。

## 清空 temp

```bash
curl -X DELETE "http://127.0.0.1:8000/api/cache/temp"
```

响应含 `deleted`、`freed_bytes`、`freed_mb` 和删失败时的 `errors`。
