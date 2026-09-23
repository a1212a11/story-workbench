# Story Workbench

AI 小说创作与故事工程系统。作者决定故事，AI 帮助讨论、组织设定，并将确定的章节剧情与结尾状态写成正文。

当前阶段：首期核心流程已有实现与自动化测试（2026-09-14）。本次补齐 CLI 阅读诊断、试改与检查范围展示，并加入 DeepSeek 配置选择。真实模型调用、T05 终端人工验证、T07/T12 语义与阅读体验记录仍待验证，其他界面限制见[验收记录](docs/acceptance-log.md)。

交付方向：先用本地终端验证创作流程，产品稳定后再做图形界面。技术栈：Python 3.12、SQLite、Markdown 工作副本、Textual 全屏终端界面（旧版命令界面基于 prompt_toolkit 保留）；uv 管理环境，pytest 测试。启动：`uv run python -m story_workbench <项目目录>`；加 `--classic` 进入旧版单字母命令界面。

## 新包 story_agent（M6，开发中）

按[新包设计](docs/agent-workbench.md)重做的"写小说的 Claude Code"：作品是一个 Markdown 目录（作品.md、大纲.md、设定/、人物/、章节/、决定.md 为正式文件，故事状态.md、草稿/、技能/ 为自由文件），代理用读、搜、改、历史、核对引用、独立审读、独立重写、编辑器、查方法、请作者定十个工具工作，正式文件的每处改动先弹 diff 审批卡，批准后写入并记一次 git 历史。启动：`uv run python -m story_agent <作品目录>`，目录不是作品时按目录名新建。模型配置与下节相同。旧包 `story_workbench` 在新包通过三章验收前保留。

## 模型配置

配置文件默认位于 `~/.config/story-workbench/config.json`，也可通过 `STORY_WORKBENCH_CONFIG` 指定。文件只放非敏感配置。选择 DeepSeek：

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "max_output_tokens": 8000,
  "timeout_seconds": 120,
  "context_tokens": 100000,
  "template_reserve": 2000
}
```

| provider | 省略 model 时的默认值 | 密钥环境变量 |
| --- | --- | --- |
| `anthropic` | `claude-sonnet-5` | `ANTHROPIC_API_KEY` |
| `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| `openrouter` | `google/gemini-3.8-flash` | `OPENROUTER_API_KEY` |

例如仅写 `{"provider": "deepseek"}` 即使用 DeepSeek 默认配置；`model` 可改成供应商支持的其他模型。没有配置文件时沿用 Anthropic。密钥在启动前通过对应环境变量提供，缺少密钥时仍可编辑和保存。配置错误会在启动时提示，不会自动换用另一供应商。

新包 `story_agent` 还可选填 `"writing_model"`（同一供应商的另一个模型名）：独立重写与审读用它，对话与摘要仍用 `model`，例如 `{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "writing_model": "anthropic/claude-opus-5"}`；状态行显示为"对话 ▸ 写作"。

状态行会按已知价格估算本次花费；模型不在内置价格表里时，可在配置里加 `"input_price"` 与 `"output_price"`（美元 / 百万 token）。OpenRouter 同样走其 Anthropic 兼容端点，`model` 用 OpenRouter 的写法（如 `google/gemini-3.8-flash`），只对新包 `story_agent` 生效。DeepSeek 复用现有 SDK，通过[官方 Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)访问，显式关闭思考模式；[模型名称](https://api-docs.deepseek.com/quick_start/pricing/)与[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)按 2026-09-14 官方文档核对。付费请求不自动重试。

真实调用验证：配置供应商和密钥后执行 `STORY_WORKBENCH_LIVE=1 uv run pytest tests/test_provider_live.py -s`，会产生费用。普通测试默认跳过真实调用。

## 对话式工作台（默认界面）

打开作品后直接用一句话说要做什么，例如「写第二章」「这稿采用」「第一章读起来太赶，帮我看看」。助手通过工具读取作品、发起写作与检查、记录你的决定；过程流式显示。所有正式变更（计划、决定、采用、拒绝、发布、复核、影响确认）都会停下来在问题卡里请你亲自确认，规则与旧界面完全相同：采用要输入「已读，采用」，新增设定要逐条接受或不接受，稿件不明确时只列候选不写记录。模型的话和模型的工具调用都不是授权。

外观与交互仿照 Claude Code：你的话显示为灰色的「> 你的话」，助手回复带「⏺」并按 Markdown 渲染；工具调用显示为「⏺ 写作(第二章 · 正式)」，其下缩进显示过程输出（正文流式出现），结束后一行「⎿ 完成 · N 行」，Ctrl-O 折叠或展开；工作时输入框上方有旋转指示与已用秒数。按键：Enter 发送，Ctrl-J 换行，Esc 中断进行中的模型任务，Ctrl-C 中断、清空输入或退出，Ctrl-D 退出。固定操作用斜杠命令：`/chapters`、`/go 2`、`/new`、`/edit`（打开全屏正文编辑器，Ctrl-S 保存、Ctrl-Q 取消）、`/status`、`/branch`、`/help`、`/quit`。输入框下方一行左侧是快捷键，右侧是当前章、模型与本次用量。没有密钥时对话仍可记录想法与编辑正文，助手会说明模型尚未接入。

助手的每次调度调用都记为 `assist` 任务，带用量与终态时间；一轮最多 8 次调用；某个付费工具失败后，本轮不再发起任何付费请求，由你决定是否重试。设计见[对话式工作台设计](docs/conversational-workbench.md)。

## 阅读诊断与试改（旧界面命令；对话界面里直接说出反馈即可）

选章并查看正文后输入 `b`（或 `阅读诊断`），填写反馈，查看带原文证据的问题与修改选项。选择序号才会生成试改候选稿，随后可保留为编辑稿、拒绝并记录理由，或回车仅保存候选。原稿留在历史中；正式采用仍通过 `a` 完成。检查展示同时列出已检查和未检查范围。

模型任务进行时可按 `Ctrl-C` 取消（写作、试改、讨论、诊断与检查均支持）。取消先保存，再停止请求；已经生成的完整稿和已落库的部分输出保留，迟到结果不替换当前稿。生成后的检查被取消时，正文仍可查看，并可用 `k` 重新检查。

## 回改后的复核与失败恢复

前章重新采用后，章节列表会标出后章“待复核”。用 `u` 查看该章已采用正文；需要新检查时回车返回，用 `k` 检查这份正文，再用 `u` 审阅并输入“已读，复核”，逐条处理新增设定或冲突。从最早受影响的章节依次复核。当前另存的编辑稿保留，复核不新增正文或重复采用。

用 `y` 选择本章失败、中断或取消的历史阶段：检查只重新检查原稿；写作重新发起生成；故事状态更新只重算，不调用模型。选择序号后才重试，不自动重试；状态更新失败也可通过已采用稿的 `a` 或 `u` 重算。

## 文档

- [技术选型](docs/technology-decisions.md)：已确定工具、推荐依赖及选择理由，含审稿 Agent 与资料查阅的扩展方向。
- [项目计划与进度](docs/project-plan.md)：任务依赖、交付物、完成条件与勾选状态；已完成项附提交号与测试名。
- [验收记录](docs/acceptance-log.md)：确定性检查结果，以及 T05、T07、T12 人工项与用量的记录表。
- [代码架构设计 V0.2](docs/code-architecture.md)：逻辑职责、数据所有权、依赖边界、模块接口、状态转换与恢复；已按此实现首期代码，偏差在第 12 节记录。
- [需求基线](docs/requirements.md)：产品边界、功能需求、权限规则与验收场景。
- [实施方案](docs/implementation.md)：CLI 交互、本地数据、AI 职责、交付顺序与待定事项。
- [魔法冷战三章试用材料](docs/magic-cold-war-trial.md)：《寒灯协定》工作设定、三章初稿、知识边界与人工核对样本。
- [对话式工作台设计](docs/conversational-workbench.md)：M5 的代理循环、工具目录、Textual 界面与实施顺序。
- [三章创作交互走查](docs/three-chapter-walkthrough.md)：含完整短章的纸面模拟，覆盖普通文字编辑、试写、采用、回改和失败恢复；非应用实测。

## 核心闭环

想法讨论 → 作者确定 → 故事设计 → 本章剧情与结尾状态 → 写作草稿 → 作者采用 → 故事状态更新 → 下一章。

## 来源与状态

整理日期：2026-09-08。

需求依据：[ChatGPT 分享对话《设计小说大纲》](https://chatgpt.com/share/6a9f6801-e16c-83e8-9463-7ba317d4622c)，以对话末尾的章节最小输入确认及「需求方案 V1」为基线。

文档是整理与工程化建议，并非逐字转录。明确区分原讨论原则、为消除歧义提出的规则和待确认选择。仓库名称为暂定名；初始可见性为私有。
