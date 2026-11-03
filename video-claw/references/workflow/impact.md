# 形象影响与重新生成令牌

改形象的画面后，依赖可以标成 stale。重跑下游要先拿令牌。两条库接口和三条执行器接口不是同一个令牌。

## 库报价（不执行、不入账）

```bash
curl -X POST "http://127.0.0.1:8000/api/library/looks/{look_id}/regenerate-quote" \
  -H "Content-Type: application/json" \
  -d '{"model": "doubao-seedream-5-0-260128"}'
```

`model` 和 `unit_cost` 都可选。响应里的 `confirm_token` 是报价正文的 sha256。`will_spend` 为 false。没有文档价格时 `estimated_cost` 为 null，不会用图片上旧的 `cost` 顶替（只有没指定 `model` 时才加总旧 cost）。

```bash
curl -X POST "http://127.0.0.1:8000/api/library/looks/{look_id}/regenerate-confirm" \
  -H "Content-Type: application/json" \
  -d '{"confirm_token": "<sha256>"}'
```

只把 `confirmed` 设为 true。`executed` 仍是 false。不生成，不写账本。

## 执行器令牌（会跑节点）

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/looks/{look_id}/impact" \
  -H "Content-Type: application/json" \
  -d '{"model": "doubao-seedance-2-5-260628"}'
```

在上面的报价之外给出 `graph_impact`、`impact_nodes`、`expires_at`。这里的 `confirm_token` 是 `impact_confirmation` 表里的随机令牌，30 分钟有效，绑定这一个项目和当时的下游节点列表。钉住的节点不重跑。pending 和 skipped 不在列表里。已经 stale 的在列表里。

用户确认后：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/looks/{look_id}/impact/confirm" \
  -H "Content-Type: application/json" \
  -d '{"confirm_token": "<token>"}'
```

跑批之前先对每个视频节点做无密钥检查（`_require_video_vendor`，在 `claim` 和 `_run_confirmed_node` 之前）。没有注入适配器且供应商密钥为空时，HTTP 是 409：`未配置视频厂商密钥，不会提交也不会入账`。不入账，不写 `submit_dispatched`，令牌不占用。`allow_offline_video=True` 才允许这批视频按 `quote.max` 入账。通过检查之后按列表跑 `run_node`。视频节点会在这批里再 `confirm`（授权并入账）。kind `character`、`scene`、`setting`、`keyframe`、`image` 以及 VLM、TTS 在 `run` 里直接按 `quote.max` 入账，`provider_called` 为 false 也记 `spent`，界面可显示「已记账·未调用厂商」。第一个还在等用户的节点会停下，后面的节点这次不占用、也不花。某个节点占用之后失败：释放未结算报价，把失败记在已用掉的令牌上，这次 HTTP 是 400。再拿同一个令牌 confirm，若上面有结果，则原样返回（200），不再花第二次。

令牌过期、属于别的项目、对不上这个形象、或下游集合已经变了，都会 400，且不写账本。

只重跑其中几个：

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/impact/regenerate" \
  -H "Content-Type: application/json" \
  -d '{"confirm_token": "<token>", "node_ids": ["shot_1"]}'
```

`node_ids` 必须都在已确认的集合里。未知节点、集合外节点、重复、已消费、过期或集合已变，都在任何节点运行之前失败。
