# 架构文档

英文书库（Lectern）的技术方案与评审。读者：本项目的维护者（含未来的自己）与协作的代理。所有图用 Mermaid，可在 GitHub 直接渲染；文档不放代码片段，只给文件名、函数名和行号。

| 文档 | 内容 | 何时读 |
|---|---|---|
| [overview.md](overview.md) | 系统全景、模块分层与依赖、两条主数据流、并发模型、缓存层次 | 第一次接触项目 |
| [data-model.md](data-model.md) | 七个 SQLite 库的 ER 图、每张表的用途 / 量级 / 读写方、跨库关联键 | 改任何表、查数据来源 |
| [flows.md](flows.md) | 时序图：播放一句、按章预生成、新书入库、长任务互斥 | 改朗读 / 预生成 / 入库 |
| [operations.md](operations.md) | 进程与端口、启动入口、日志、备份与可再生性、版本失效、Mac mini 访问、健康检查 | 部署、排障、备份恢复 |
| [models.md](models.md) | 本机算力实测、本地模型吞吐、各角色候选模型（本机可部署、不限开源）、运行时升级、内存预算与验收线、路线图 | 换模型、升级 Ollama / MLX |
| [review.md](review.md) | 架构与性能评审：13 条按收益 / 成本排序的建议，每条有证据与量化预期；附「核查过不是问题的疑点」 | 决定下一步做什么 |

阅读顺序建议：overview → review（先看结论）→ 按需进 data-model / flows / operations / models。

## 基线与测法

所有数字为 2026-10-17 在同一台机器上得到，环境：MacBook Pro M5 Max（18 核 CPU、40 核 GPU、128 GB），macOS 26.6.2，Python 3.12，Ollama 0.35.1，mlx 0.32.3 / mlx-audio 0.5.7，书库服务 commit `8737a57`。

| 项 | 测法 |
|---|---|
| 表体积与行数 | `.venv/bin/python` 只读打开各库，`dbstat` 虚表按表求和，`COUNT(*)` 计数，`PRAGMA page_count / freelist_count` |
| 查询计划与耗时 | `EXPLAIN QUERY PLAN` + 同一语句重复 5–20 次取均值（SQLite 内存热态） |
| HTTP 页面耗时 | `curl -w %{time_total}` 对运行中的服务，冷 = 服务重启后首次或缓存戳过期，热 = 紧接着第二次 |
| 阅读接口 | `TestClient` 进程内调用，重复取均值 |
| TTS / 对齐 / 译文冷热 | 用一句从未出现过的句子依次调 `/api/tts`、`/tts/words`、`/tts/prosody`、`/translate`、`/notes`，再各调一次取热值 |
| TTS 批量统计 | 解析 `logs/tts.log` 全部「耗时 × 句数」行，按批大小分组求均值 |
| Ollama 吞吐 | `/api/generate` 非流式，取 `prompt_eval_count / prompt_eval_duration` 与 `eval_count / eval_duration` |
| 预生成节奏 | `logs/server.log` 里连续 Ollama 调用的时间戳聚类（间隔 > 3 分钟视为另一次） |
| 语调分析 | 对缓存里 30 句直接调 `prosody.analyze` 计时 |
| 内存 | `ps`（RSS）与 `footprint`（含 GPU 内存） |
| 文件统计 | `find … -exec stat` 求和 |

复测时请沿用同样的测法，并把新数字连同日期追加到各文档的表里，不覆盖旧值。
