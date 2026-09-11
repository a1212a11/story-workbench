# Story Workbench

AI 小说创作与故事工程系统。作者决定故事，AI 帮助讨论、组织设定，并将确定的章节剧情与结尾状态写成正文。

当前阶段：首个可用闭环的确定性部分已实现（2026-09-11）。保存、检查、审阅、采用、状态重算、回改复核、失败恢复、备份恢复、CLI 作者入口和 Anthropic 客户端均有代码与测试，`uv run pytest` 99 通过。真实模型调用、T05 终端人工验证、T07/T12 语义与阅读体验记录待作者完成，见[验收记录](docs/acceptance-log.md)。

交付方向：先用本地 CLI 验证创作流程，产品稳定后再做 UI。技术栈：Python 3.12、SQLite、Markdown 工作副本、prompt_toolkit 终端、Anthropic Claude Sonnet 5；uv 管理环境，pytest 测试。启动：`uv run python -m story_workbench <项目目录>`；密钥只读环境变量 `ANTHROPIC_API_KEY`。

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
