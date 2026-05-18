# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/) — 大版本号(0 → 1)预告不兼容变化,小版本号(0.4 → 0.5)是 additive,补丁号(0.4.1)是 bugfix。

升级 schema 变化时,启动会自动 `sqlite3 .backup ~/.life_lens/backups/auto-vX-YYYYMMDD.db` — 出问题手动恢复就行。

---

## [Unreleased]

### 待加入下个版本
- (开发中,本节随 commit 累积,release 时迁移到下面新版本号下)

---

## [0.4.0] - 2026-05-XX(首次公开发布)

### 添加
- 完整 Phase 0-4 功能:vision 流水线 / 人脸识别 / 高德 reverse-geocoding / FTS5 全文检索 / hybrid 语义检索 / Web Chat / Apple Photos source
- 共享 Photo Viewer modal:chat / Browse 点缩略图弹大图 + "下载原图"按钮
- A 层回归测试(`pytest tests/`)
- 自动 db 备份机制(schema 升级时)
- `scripts/publish.sh` 发布到公开镜像流程
- **Web 配置 API**(给统一配置页用):`/api/setup/status` 聚合就绪状态 + `/api/ollama/ping` 探活 + `/api/config/amap-key{,/validate}` + `/api/config/llm-provider` + `/api/config/llm-default`
- 统一 config 读写模块 `life_lens/store/config.py`(原子写 + chmod 600)
- Ollama 探活 `life_lens/vision/ollama_probe.py`

### 不兼容变更 (BREAKING)
- **LLM 只支持 OpenAI 兼容 RESTful API**,移除 `kind="claude-p"` 子进程方案
  - 旧 config 含 `kind="claude-p"` provider 会被启动时**自动忽略 + 日志 WARN**(不会崩溃,但用不了那个 provider)
  - **迁移方法**:到 Web 配置页 → LLM 文本模型,删旧 claude-opus,新增 DeepSeek/OpenAI provider(填 api_key + base_url + model)。推荐 DeepSeek v4-flash(便宜 + 中文好 + 速度快):https://platform.deepseek.com/

### Schema 变化
- 加 `photos.schema_version` / `PRAGMA user_version` 跟踪
- 加 `photo_embeddings` 表(语义向量)
- 加 `scan_runs` / `amap_quota` 等运行历史表

### 已知限制
- HEIC 在浏览器不能 inline 显示(由 Safari/Chrome 决定下载或交给系统 app)
- Apple Photos iCloud-only(没下载本地)的原图 `/api/original` 404
- 视频(MOV/MP4)不进 vision pipeline
- UI 中文为主,欢迎 PR 英化
