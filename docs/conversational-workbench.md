# 对话式工作台设计（M5）

日期：2026-09-16。状态：**已被 [M6 代理式工作台设计](agent-workbench.md) 取代**（2026-09-18 作者否定了这一版：模型只是把作者的话翻译成旧流程，不是 agent）。第 4 节供应商工具调用与第 5 节引擎规则仍有效，其余仅作历史记录。

## 1. 为什么改

现有 CLI 是单字母菜单：作者按 `w` 写、`k` 检查、`a` 采用，自己充当调度器。作者的评价是体验很差，期望的是 Codex CLI 那样的界面：直接说要做什么，代理自己调用工具、流式展示过程，关键动作弹出确认；界面全屏，有状态栏和斜杠命令。[实施方案第 1 节](implementation.md#1-cli-与用户路径)原本就是这样写的（"进入持续对话，可以直接说'试写第一章''这一版采用'"），现有 CLI 偏离了该设计。

不变的部分：作者权限规则（[需求基线](requirements.md)、[代码架构](code-architecture.md)）。正式变更仍只由 `AuthorAction` 触发；模型的话、模型的工具调用都不是授权。变的只是作者表达意图和确认的方式：从按键变为对话与对话内的审批卡。

## 2. 目标体验

```
第二章 · 未采用 · deepseek-flash · 本轮 3.2k/1.1k token
────────────────────────────────────────────────────────
你：第二章按现在的剧情写，注意六点前情报还在她手里。
助手：好，先核对第二章的剧情与结尾……（读取计划）
      开始写作。
      ▌雪停之后，配给站的灯……（流式正文）
      写完了，检查发现一条新增设定：……
      要我展示全文和检查，走采用流程吗？
你：采用
助手：（展示全文与检查）
      ┌ 采用第二章这一稿 ───────────────────────┐
      │ 读完全文和检查后，输入「已读，采用」      │
      └──────────────────────────────────────────┘
你：已读，采用
助手：问题 1 · 新增持久设定：……  接受进入正式记录？（接受 / 不接受）
你：接受
助手：已采用第二章这一稿，故事状态更新完成。
────────────────────────────────────────────────────────
> ▍                                  Enter 发送 · Ctrl-J 换行 · Esc 取消 · /help
```

作者说的话是自然语言。助手决定读什么、调用哪个工具、何时停下来问。所有会写正式记录的动作都停下来问，并且沿用现有门槛：已读声明、逐条授权、歧义时列候选而不写记录。

## 3. 分层

```
tui（Textual 全屏界面）      cli（旧版，过渡期保留为 --classic）
        │                             │
        └──────────► console ◄────────┘      作者交互层：UI 端口、门槛流程、工具执行、代理引擎
                        │
                   application                用例（不变）
                  ╱     │      ╲
             domain    ai    infrastructure
```

- `console/port.py`：`AuthorPort` 协议，四个方法 `say(text)`、`emit(chunk)`、`ask(prompt) -> str`、`edit(initial, title) -> str | None`，加 `cancel: CancelSignal`。现有 CLI 的全部门槛流程恰好只用这四个接口，因此可以原样搬过来。
- `console/flows.py`：从 `cli.py` 抽出的门槛流程，参数化到 `AuthorPort`：采用与复核（已读声明、逐条问题处理、试写稿接受剧情结尾）、拒绝、发布、确认无关、补救确认、伏笔出处校验、改已确定内容的授权、稿件歧义列候选。旧 CLI 改为调用这些流程，避免两套规则。
- `console/tools.py`：工具目录的执行侧。每个工具是一个普通函数，接收 `Project`、`AuthorPort`、模型客户端与参数，调用 application 用例或 flows，返回给模型的文本结果。工具不构造 `AuthorAction`；只有 flows 在作者作答后构造。
- `console/engine.py`：代理引擎。一次作者发言 = 一轮：装配消息 → 带工具定义调用模型（流式）→ 执行工具 → 把结果回给模型 → 直到模型结束发言。每次模型调用记一条 `run`（新任务类型 `assist`），记录用量与终态时间。
- `ai/assistant.py`、`ai/assistant_prompts.py`：纯函数。工具定义（名称、中文说明、JSON Schema）、系统提示、从对话记录装配消息、解析模型返回。不写数据库。
- `infrastructure/provider.py`：加工具调用协议（第 4 节）。
- `tui/app.py`：Textual 界面，实现 `AuthorPort`（第 6 节）。

## 4. 供应商层：工具调用

现有客户端只处理纯文本。需要补：

- `ChatMessage.content` 允许为文本或内容块序列；新增 `ToolUseBlock(id, name, input)` 与 `ToolResultBlock(tool_use_id, content, is_error)`。纯文本消息保持原样，现有任务不受影响。
- `CallOptions` 增加 `tools: tuple[ToolSpec, ...] = ()`。`ToolSpec(name, description, input_schema)`。不传 tools 时请求与现在完全一致。
- `EventKind.TOOL_USE`，`StreamEvent.tool_use: ToolUseBlock | None`。流式时文本照常逐段产出；流结束后从最终消息取出 tool_use 块逐个产出，然后是 USAGE，`stop_reason == "tool_use"` 与 `end_turn` 都算 COMPLETED。`CompleteResult` 增加 `tool_uses`。
- DeepSeek 的 Anthropic 兼容接口完整支持 tools、tool_use、tool_result（官方文档 2026-09-16 核对）。不用 SDK 的 beta tool runner；循环自己写，因为取消要先落库、付费失败不自动重试、每次调用要记 run。

## 5. 代理循环规则

- 一轮内最多 8 次模型调用；超过即停下来把情况告诉作者，不自动继续。
- 每次模型调用前检查 `_ensure_idle`；调用记 `run(task=assist)`，完成后写用量与 finished_at，再执行工具。工具里的写作、检查等自己再记各自的 run，前后不重叠。
- 取消：作者按 Esc。引擎在下一个流式片段或工具边界检查取消标志，先 `cancel_run` 落库，再置位客户端信号并关闭流。正在执行的工具若是模型任务，其取消复用现有机制。
- 失败不自动重试：某个模型任务工具在本轮失败后，本轮内后续的模型任务工具一律拒绝执行，结果告诉模型"需要作者确认后再试"；作者下一句话开启新一轮。
- 工具结果对模型是数据不是指令：正文和检查结果原样回传，系统提示写明其中的指令不执行（沿用 `COMMON_SYSTEM` 的防注入条款）。
- 对话持久化：作者发言与助手最终回复写入 `message` 表（author / model）；工具调用与结果只在本次会话内存里，重启后助手从最近若干条消息恢复上下文，不伪装记得工具细节。
- 上下文容量：系统提示 + 最近对话 + 工具结果按 `ContextLimits` 估算，超出时先丢最早的工具结果，再丢最早的对话，不丢系统提示与当前发言。

## 6. 工具目录（首版）

只读：`list_chapters`、`get_chapter`（计划、状态、稿件摘要）、`read_draft`、`get_check`、`list_versions`、`list_story_items`、`history_status`。

写记录但不需模型（相当于作者动手，执行前由 flows 复述并请作者确认）：`create_chapter`、`select_chapter`、`set_plan`、`record_candidate`、`decide`（改已确定内容要求输入「授权」）、`open_editor`（打开正文编辑器，作者自己改、自己保存）、`reject_draft`、`restore_version`、`choose_current_draft`、`create_branch`、`switch_branch`、`record_foreshadow`。

模型任务（付费；执行前简述将要做什么）：`write_chapter`、`check_draft`、`diagnose_draft`、`revise_draft`、`analyze_impact`、`retry_run`、`discuss`。

正式变更（完整沿用 flows 的门槛）：`adopt_draft`、`revalidate_chapter`、`mark_published`、`confirm_impact_unrelated`、`record_remediation`。

工具描述、参数名与错误文案全部中文；返回给模型的结果不含内部 ID，用章号、稿件时间与类型定位；模型要指定某一稿时用 `get_chapter` 返回的序号。

## 7. 界面（Textual）

- 布局：顶部状态栏（作品、分支、当前章及状态、模型、本轮与本会话用量）；主区为对话记录（Markdown 渲染，助手回复流式追加，工具活动折叠为一行，可展开）；底部多行输入框。左侧章节栏可用 `/chapters` 切换显示。
- 按键：Enter 发送，Ctrl-J 换行，Esc 取消进行中的模型任务，Ctrl-Q 退出，粘贴直接进入输入框。
- 审批与提问：`ask` 在输入框上方显示问题卡，输入框进入作答模式；`edit` 打开全屏编辑器屏幕（Textual `TextArea`），Ctrl-S 保存、Ctrl-Q 取消，与现有编辑器语义相同。
- 斜杠命令只保留不需要模型的固定操作：`/help`、`/chapters`、`/go N`、`/new`、`/edit`、`/status`、`/branch`、`/quit`。其余一律自然语言。没有密钥时对话仍可用来记录想法与手动编辑，助手回复"模型尚未接入"。
- 线程：Textual 在主线程跑事件循环；引擎在专用线程里拥有 `Project` 与 SQLite 连接，所有用例与数据库操作只在该线程执行。界面通过队列把作者输入与作答送给引擎，引擎通过 `call_from_thread` 把 say/emit/ask/edit 投递到界面。取消标志是线程安全的事件。
- 终端与输入法：Textual 处理原生终端输入；中文输入法与粘贴仍需在真实终端人工验证（T05 的对话版）。

## 8. 数据与迁移

- schema v5：`run.task` 允许 `assist`。这需要重建 `run` 表（CHECK 约束不能 ALTER），沿用 v3 的重建方式，迁移已在 v4 修复为关闭外键、事务末尾复核。
- `TaskKind.ASSIST`；`message` 表不加字段。

## 9. 测试

- 供应商：本地假 SDK 覆盖 tools 参数、tool_use 块、`stop_reason=tool_use`；真实调用测试加一条工具调用往返，默认跳过。
- 引擎：固定脚本客户端（第 N 次调用返回哪些文本与 tool_use）+ 假 `AuthorPort`（预置作答），按流程断言：写作→展示→采用需要已读声明；模型任务失败后同轮不再发起付费调用；作者拒绝时零正式记录；取消先落库；8 次上限。
- 界面：Textual `run_test` 驱动按键，断言对话区文本、问题卡出现、编辑器保存与取消。
- 旧 CLI 测试继续通过，直到旧 CLI 退役。

## 10. 实施顺序

| 步 | 内容 | 完成条件 |
| --- | --- | --- |
| M5-01 | 供应商工具调用 | 假 SDK 与真实往返测试通过；不传 tools 时请求字节级不变 |
| M5-02 | flows 抽取、工具目录、代理引擎 | 旧 CLI 改用 flows 后全部测试仍过；引擎测试覆盖第 9 节列出的规则；schema v5 |
| M5-03 | Textual 界面 | 三章走查可在新界面完成；T05 对话版人工记录 |
| M5-04 | 入口切换与退役 | `python -m story_workbench` 默认进新界面，`--classic` 进旧版；文档更新；一轮真实三章对话验收后删除旧 CLI |

不做的事：不引入第二个模型供应商框架，不做多代理，不让模型代签任何作者动作，不在本阶段做 UI 之外的新产品功能。
