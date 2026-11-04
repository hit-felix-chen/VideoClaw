# AGENTS.md

VideoClaw 2.0 在 fork `hit-felix-chen/VideoClaw` 上开发。接手前先读：

1. `docs/HANDOFF.md`：状态、红线、下一步。
2. `docs/DEVELOPMENT.md`：架构、代码地图、命令、已知缺口。
3. `video-claw/CLAUDE.md` 与 `video-claw/video-claw/backend/docs/api.md`：后端行为细节。

硬规则：只推 fork，不动上游 `HITsz-TMG/VideoClaw`；用 `hit-felix-chen` 账号；不在任何地方写入或索取密钥；没有用户提供的密钥和同意，不做真实厂商调用；视频调用必须先人工确认且受预算约束；不弱化已有测试；改行为就同步改文档。

测试：`cd video-claw/video-claw/backend && python3 -m pytest`；前端 `cd video-claw/video-claw/frontend && npx tsc --noEmit && npx vitest run`。
