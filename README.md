# Story Workbench

AI 小说创作与故事工程系统。作者决定故事，AI 帮助讨论、组织设定，并将确定的章节剧情与结尾状态写成正文。

当前阶段：首期核心流程已有实现与自动化测试（2026-09-14）。本次补齐 CLI 阅读诊断、试改与检查范围展示，并加入 DeepSeek 配置选择。真实模型调用、T05 终端人工验证、T07/T12 语义与阅读体验记录仍待验证，其他界面限制见[验收记录](docs/acceptance-log.md)。

交付方向：先用本地 CLI 验证创作流程，产品稳定后再做 UI。技术栈：Python 3.12、SQLite、Markdown 工作副本、prompt_toolkit 终端；uv 管理环境，pytest 测试。启动：`uv run python -m story_workbench <项目目录>`。

## 模型配置

配置文件默认位于 `~/.config/story-workbench/config.json`，也可通过 `STORY_WORKBENCH_CONFIG` 指定。文件只放非敏感配置。选择 DeepSeek：

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "max_output_tokens": 8000,
  "timeout_seconds": 300,
  "context_tokens": 100000,
  "template_reserve": 2000
}
```

| provider | 省略 model 时的默认值 | 密钥环境变量 |
| --- | --- | --- |
| `anthropic` | `claude-sonnet-5` | `ANTHROPIC_API_KEY` |
| `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` |

例如仅写 `{"provider": "deepseek"}` 即使用 DeepSeek 默认配置；`model` 可改成供应商支持的其他模型。没有配置文件时沿用 Anthropic。密钥在启动前通过对应环境变量提供，缺少密钥时仍可编辑和保存。配置错误会在启动时提示，不会自动换用另一供应商。

DeepSeek 复用现有 SDK，通过[官方 Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)访问，显式关闭思考模式；[模型名称](https://api-docs.deepseek.com/quick_start/pricing/)与[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)按 2026-09-14 官方文档核对。付费请求不自动重试。

真实调用验证：配置供应商和密钥后执行 `STORY_WORKBENCH_LIVE=1 uv run pytest tests/test_provider_live.py -s`，会产生费用。普通测试默认跳过真实调用。

## 阅读诊断与试改

选章并查看正文后输入 `b`（或 `阅读诊断`），填写反馈，查看带原文证据的问题与修改选项。选择序号才会生成试改候选稿，随后可保留为编辑稿、拒绝并记录理由，或回车仅保存候选。原稿留在历史中；正式采用仍通过 `a` 完成。检查展示同时列出已检查和未检查范围。

## 文档

- [技术选型](docs/technology-decisions.md)：已确定工具、推荐依赖及选择理由，含审稿 Agent 与资料查阅的扩展方向。
- [项目计划与进度](docs/project-plan.md)：任务依赖、交付物、完成条件与勾选状态；已完成项附提交号与测试名。
- [验收记录](docs/acceptance-log.md)：确定性检查结果，以及 T05、T07、T12 人工项与用量的记录表。
- [代码架构设计 V0.2](docs/code-architecture.md)：逻辑职责、数据所有权、依赖边界、模块接口、状态转换与恢复；已按此实现首期代码，偏差在第 12 节记录。
- [需求基线](docs/requirements.md)：产品边界、功能需求、权限规则与验收场景。
- [实施方案](docs/implementation.md)：CLI 交互、本地数据、AI 职责、交付顺序与待定事项。
- [三章创作交互走查](docs/three-chapter-walkthrough.md)：含完整短章的纸面模拟，覆盖普通文字编辑、试写、采用、回改和失败恢复；非应用实测。

## 核心闭环

想法讨论 → 作者确定 → 故事设计 → 本章剧情与结尾状态 → 写作草稿 → 作者采用 → 故事状态更新 → 下一章。

## 来源与状态

整理日期：2026-09-08。

需求依据：[ChatGPT 分享对话《设计小说大纲》](https://chatgpt.com/share/6a9f6801-e16c-83e8-9463-7ba317d4622c)，以对话末尾的章节最小输入确认及「需求方案 V1」为基线。

文档是整理与工程化建议，并非逐字转录。明确区分原讨论原则、为消除歧义提出的规则和待确认选择。仓库名称为暂定名；初始可见性为私有。
