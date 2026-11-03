# 项目预算

上限在创建项目时放进 `budget_cap`。null 表示不封顶，不会暂停。

判断是 `已用 + 已占用 + 本次报价 max` 是否大于上限。暂停文案形如：

`已用 {used} {currency} / 本次报价 {金额} {currency} / 当前上限 {cap} {currency}。是否上调预算上限？`

`{currency}` 来自报价。有币种时每个数字后面加一个空格再加币种代码，例如 `已用 0 CNY / 本次报价 1.32 CNY / 当前上限 1 CNY。是否上调预算上限？`。`min` 与 `max` 相等时中间只写一个数；不相等时写成 `{min}~{max}`，币种仍加在这段后面。没有币种则不加后缀。

把这句原文给用户，不要自己上调。

## 视频和图片差在哪

视频：`run` 只预告 `over_budget`，账本仍为空。用户 `confirm` 时才授权；超了就把账本行写成 `awaiting_user`，`gate` 为 `budget`，不提交。

图片、VLM、TTS：`run` 里直接授权。超了同样停在 `gate: "budget"`，节点不跑。

币种和项目里已有账本不一致时不提交，说明是「报价币种与项目账本不一致，不提交调用。」

## 上调

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/budget/{entry_id}/raise" \
  -H "Content-Type: application/json" \
  -d '{"budget_cap": 200}'
```

新上限仍盖不住这笔报价：旧上限不变，调用仍不执行。够用：改项目上限，同一笔报价继续（`confirmed` true），视频会接着提交。文案是「已上调预算上限，可以继续。」

节点上已有 `submit_dispatched` 或 `vendor_task_id` 时，raise 会拒绝，避免第二笔生成。例外是 `force_new` 因预算停住的那一次（见 confirm_video.md）。

## 放弃

```bash
curl -X POST "http://127.0.0.1:8000/api/v2/projects/{project_id}/budget/{entry_id}/decline"
```

账本行变为 `skipped`，节点 `skipped`，不提交。文案是「未上调预算上限，本次调用不执行。」

也可以对仍在等待的节点调用 `POST .../nodes/{node_id}/decline`。有等待中的账本行时，它会找到那一行并同样跳过。
