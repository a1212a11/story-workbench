# 项目计划与进度

更新日期：2026-09-18。当前阶段：M5 对话式工作台被作者否定，改按 [M6 代理式工作台](agent-workbench.md) 重做，旧 CLI 与 M5 门槛流程退役。此前：真实 DeepSeek 三章写作、独立判读、CLI 采用与回改复核已在作者作品副本完成，V01 有可核对的正文、检查和快照引用。T05 的实机中文输入法、T07 的 M1–M7 全量语义变体及 T12 的真实诊断/试改仍有缺口；详见[验收记录](acceptance-log.md#2026-09-16-deepseek-三章与回改复核)。

`[x]` 表示已有对应交付证据；`[ ]` 表示尚未完成，包含未开始、进行中和待验证。写出设计、测试规格或纸面模拟，不等于代码实现和验收通过。任务只有满足完成条件后才能打勾；届时在该任务后补提交号、测试结果或验证记录链接。没有实际基线前不承诺工期或完成百分比。

执行以[代码架构 V0.2](code-architecture.md)为准；产品范围见[需求基线](requirements.md)与[实施方案](implementation.md)，T01–T12 的准确要求见[验收规格](../.omx/plans/test-spec-story-framework.md)。本文维护任务状态，不复制一套数据与接口合同。

## 已交付的规划文档

- [x] D01 整理需求与作者权限边界。证据：[需求基线](requirements.md)。
- [x] D02 确定 CLI 优先的交付顺序和 M0–M4 里程碑。证据：[实施方案](implementation.md)。
- [x] D03 完成连续三章、编辑、采用与失败恢复的纸面走查。证据：[三章走查](three-chapter-walkthrough.md)；非应用实测。
- [x] D04 编写框架验收规格 T01–T12。证据：[验收规格](../.omx/plans/test-spec-story-framework.md)；尚未执行。
- [x] D05 补齐代码架构 V0.2 的逻辑职责、数据表、接口、事务与恢复合同。证据：[代码架构](code-architecture.md)。
- [x] D06 完成代码架构独立审阅并落实修订。证据：[架构验证记录](code-architecture.md#12-修订与验证记录)；不代表严格 Architect→Critic 共识门或应用验收。
- [x] D07 将架构及相关文档提交并推送到 main。证据：提交 `f16f125`；仅指该次架构交付，不表示后续工作自动提交。
- [x] D08 建立按证据更新的项目任务清单。证据：本文。

- [x] D09 确定测试与 Python 包管理工具：pytest + uv。证据：作者明确指定；工具链初始化仍属于 P01–P02。
- [x] D10 整理技术选型、替代方案与扩展条件。证据：[技术选型](technology-decisions.md)；推荐依赖与资料/自主审稿扩展尚未实施。

## 阶段依赖与交付顺序

| 阶段 | 前置条件 | 交付物与阶段出口 |
| --- | --- | --- |
| P 开发准备 | D01–D06 | 项目规则、可运行测试入口；各接入决策有明确完成时点 |
| S 保存与采用 | P01–P02 | 真实 SQLite/文件上的保存、审阅、采用、恢复；T01–T04 |
| C 作者入口 | S 阶段、P03 | CLI 普通文字创作入口；T05 手动验证 |
| G 生成与上下文 | S 阶段；真实调用另依赖 P04；G06 另依赖 R01 | 固定客户端先验证任务合同，再接供应商；有正式状态后验收 T06–T08 确定性部分 |
| R 回改与失败恢复 | S 阶段、G01–G04；不等待 G05/G06 | 重试、复核、备份恢复；T09–T11 |
| V 首个可用闭环 | C/G/R、P04–P05 | 真实三章流程、语义样本和作者反馈记录；首期验收报告 |
| M3/M4 后续功能 | V；逐项补充设计与验收 | 发布、分支、完整影响分析及长篇能力；完整 V1 验收 |

C 与固定客户端 G 在存储接口稳定后可独立推进；采用用例、业务规则与数据库事务保持单一负责人。默认串行完成可验证切片，不因目录存在而提前铺全部功能。模型选型不阻塞 S；编辑组件选择不阻塞数据库与规则验证。

首期推荐路径：P01–P02 → S → C 与 G01–G04 → R01 → G06 → R02–R05 → V；P03 在 C 前完成，P04/G05 与 P05 在 V 前完成。三章续写依赖 R01 的正式状态，不能仅凭生成任务存在就勾选 G06。

## P 开发准备

- [x] P01 将项目专属编码规则写入仓库级 AGENTS.md，并使用 uv 建立最小 Python 项目和启动入口。完成条件：规则引用 V0.2 边界；创建 `pyproject.toml` 与 `uv.lock`，环境目录不入库；`uv run python -m story_workbench --help` 可运行；不生成尚未使用的模块。证据：提交 `e820e89`；`.python-version` 固定 3.12。
- [x] P02 建立 pytest 测试入口和首个有效检查。完成条件：pytest 作为开发依赖由 uv 管理，更新锁文件；`uv run pytest` 实际执行至少一个有行为断言的测试；记录首轮 Python/macOS 版本；不把空测试通过当作验证。证据：提交 `e820e89`；Python 3.12.12（uv 管理，系统自带 3.9.6 不满足要求）、pytest 9.1.1、ruff 0.16.7、macOS 26.1。
- [x] P03 选择并验证终端编辑组件。完成条件：中文多行输入、粘贴、光标移动、保存/取消可操作；新增依赖按项目规则处理。必须在 C 阶段实现编辑器前完成。证据：提交 `99519a5`；prompt_toolkit 3.0.53 经 `uv add` 锁定；`tests/test_cli.py` 中 test_editor_accepts_multiline_chinese_and_saves、test_editor_accepts_pasted_multiline_chinese_at_once、test_editor_cursor_moves_then_inserts_in_the_middle、test_editor_save_returns_modified_text_keeping_untouched_lines、test_editor_cancel_returns_none_and_leaves_original_untouched 通过；流式共存 test_stream_output_keeps_editor_input_and_cancel_responsive 通过，架构 4.1 的 asyncio 方案成立。输入法（IME）体验待人工 T05 验证。
- [x] P04 确定模型供应商、模型、密钥配置、上下文和输出限制、超时及预算。完成条件：记录非敏感配置和费用控制方式，密钥置于项目外；真实调用前完成。证据：[技术选型第 5 节](technology-decisions.md#5-落地与状态更新)的 P04 决定；`bootstrap.Settings` 默认值与配置文件规则；费用按用量展示，实测数字待 G05 真实调用。
- [ ] P05 准备真实试用故事和人工核对样本。完成条件：作者提供或认可素材，记录已知事实、传闻、持久设定及预期知识边界；V 阶段前完成。进度：作者已指定魔法世界、冷战谍战、前三章传递情报失败及加强世界描写；已整理[《寒灯协定》工作设定、三章初稿与 M1–M7 核对样本](magic-cold-war-trial.md)。扩展设定与初稿待作者审阅，未执行正式采用。

## S 保存、审阅与采用

触点：domain、application/authoring/adoption/projects、infrastructure/database/files；按首次使用建立所需表与函数。模型输出先用固定数据。

- [x] S01 实现项目打开、数据库初始化、schema 版本、项目锁及作品/章节关系约束。完成条件：重开项目数据仍在，跨作品/跨章引用被拒绝。证据：提交 `4eb5c93`；`tests/test_adoption.py` 与 `tests/test_recovery.py` 中重开、锁、跨作品/跨章、中断标记断言通过。
- [x] S02 实现计划与正文不可变版本、当前编辑稿指针和历史恢复。完成条件：两稿均可读取；恢复创建新稿，既有历史不变。证据：提交 `4eb5c93`；两稿并存、基准冲突保留双稿不移指针、恢复稿带 restored_from 断言通过。文件对账在 S03 接入 save_draft。
- [x] S03 实现外部文件对账和独立文件写入。完成条件：数据库稿与外部稿冲突时均保留；文件删除不变空稿；保存失败可恢复输入；不覆盖已有工作文件。 证据：`tests/test_recovery.py` 的 S03 段，覆盖导入、双稿冲突、文件删除、非 UTF-8、已有文件先导入、保存失败恢复文件、导出失败与不覆盖。
- [x] S04 实现检查材料、已读声明与具体问题处理的准确版本绑定。完成条件：修改正文或相关依据后，旧检查/审阅不能授权新采用。 证据：`database.commit_check_result` 事务内校验 `body[start:end] == quote` 与来源归属，无效结果不写入；`rules.validate_adoption` 比较 check/basis/plan/design_revision/前章快照；`tests/test_adoption.py` 的 T01、计划变更、假引用断言通过。
- [x] S05 实现采用事务、授权校验和幂等操作。完成条件：一次提交保存审阅/采用/失效标记；重复请求不重复采用；失败无部分正式写入。 证据：`database.commit_adoption` 一次事务写 review/resolution/adoption/candidate snapshot 并使后章快照 stale、登记 impact；T02 幂等、T04 跨作品无部分写入、新增设定授权、回改后章待复核断言通过。
- [x] S06 执行 T01–T04 并保存结果。完成条件：真实临时 SQLite 和文件上的版本、外部编辑、故障与隔离断言全部通过。 证据：合并后 `uv run pytest` 73 通过；T01–T04 对应 `tests/test_adoption.py` 的 test_t01–test_t04，T03 文件部分见 `tests/test_recovery.py` S03 段。

## C 作者交互入口

触点：cli、启动装配与 authoring/projects；AI 讨论接入在 G 阶段完成。

- [x] C01 实现作品/章节选择、消息保存、候选想法和作者决定、计划编辑与全文查看。完成条件：作者可通过入口整理故事，候选不会自动成为决定或正式事实。 证据：作品/章节、消息、计划、全文查看在 `cli.py`（提交 `90a0117`）；候选想法（d 记为候选 → `authoring.record_item` AUTHOR_IDEA/CANDIDATE）与作者决定（f 确定 → `decide_item`，改已确定内容须作者输入「授权」）见 `tests/test_cli.py::test_cli_candidate_and_decision_entries_require_authorization_to_change`；AI 讨论回复经 `generation.discuss` 流式打印，见 `test_cli_discussion_streams_reply_and_saves_messages`（回复只存 message，不自动抽取想法）。
- [x] C02 接入普通多行编辑器。完成条件：保存/取消行为正确，保存失败保留缓冲，全程不要求 Markdown 语法或手工文件路径。 证据：提交 `d08d24d`/`90a0117`；`cli_editor.edit_text` 基于 prompt_toolkit 3.0.53，`tests/test_cli.py` 覆盖多行中文、粘贴、光标、保存、取消、保存失败保留缓冲与流式共存。
- [x] C03 实现检查展示、准确稿件选择与明确采用入口。完成条件：展示全文、新增设定、冲突及偏差；歧义“采用那版”不写正式记录；一次明确动作提交已读与必要授权。 证据：`cli.py` 的 w 写 / x 试写（`generation.write_chapter`，on_text 逐块输出，结束展示全文与检查）、k 检查、i 查看检查（实际剧情与结尾、六类问题中文标签、证据原文、建议、候选事实）、a 采用（绑定最近展示的稿，一句「已读，采用」后逐条处理授权，构造 AuthorAction 交 `adoption.adopt`）、j 拒绝；`tests/test_cli.py`：test_cli_plan_write_review_adopt_and_state_ready（计划→写→查看检查→采用→状态完成，同一稿重复采用提示无需重复）、test_cli_rejects_overreaching_draft_then_rewrites_and_adopts（越界稿不授权→列出未处理问题、零采用→拒绝→重写→采用）、test_cli_authorizes_new_setting_in_one_action、test_cli_exploratory_draft_requires_accepting_plot_and_ending、test_cli_ambiguous_adopt_lists_candidates_and_writes_nothing（两份候选稿“采用那版”零写入）、test_cli_stale_basis_after_plan_change_needs_recheck、test_cli_without_model_explains_and_unknown_command_is_explained；保存失败另存恢复文件见 test_cli_keeps_buffer_when_save_fails_and_allows_retry。2026-09-14 已接入 CheckRecord 的已检查/未检查范围展示，见 tests/test_cli.py 阅读诊断流程断言。
- [ ] C04 人工执行 T05 并保存交互记录。完成条件：中文编辑与取消可用，CLI 操作绑定正确版本。

## G AI 任务与上下文

触点：application/generation、ai/context/tasks/prompts、infrastructure/provider；用注入的固定客户端验证，不绕过任务解析与证据校验。

- [x] G01 实现纯上下文构建与依据持久化。完成条件：来源和遗漏可追溯；拒绝稿、未来章节知识和过时状态不能混入正式续写；新增规则使旧依据过时。 证据：提交 `7507c6a`；`ai/context.build_context` 纯函数，拒绝稿/未来章隔离、容量先裁讨论、必需依据超限报错、遗漏可追溯（`tests/test_generation.py` G01 段）。依据持久化由 `database.commit_basis` 提供，与 ContextDraft 的接线在 G04。
- [x] G02 实现讨论、正式写作和试写任务合同。完成条件：最小输入可写，试写先展示缺口与假设，委托限本次请求本章；正常请求一次生成、一次检查，不自动润色。 证据：提交 `f030f77`；`ai/tasks.generate/discuss`，试写提示含缺口与假设，委托范围限本次本章，模板版本受校验。
- [x] G03 实现检查与候选事实提取。完成条件：必填字段、枚举和原文引用受校验；假引用/截断结果不能标检查成功；提取不自动成为 Canon。 证据：提交 `f030f77`/`4312fce`；`ai/tasks.inspect/diagnose` 用 Pydantic strict 校验，缺字段、多余字段、假引用、截断均为 MODEL_FAILED；`database.commit_check_result` 事务内再次逐条校验。
- [x] G04 实现 run 阶段记录、流保存、取消与迟到结果保护。完成条件：只有已保存流块可恢复；取消先落库；迟到结果不能切换当前稿；同项目最多一个模型请求在途。 证据：`application/generation.py` 的 write_chapter/check_draft/discuss/cancel_run/retry_run；`database.commit_run_chunk/commit_run_result/commit_run_cancel` 只在 running 时生效；`tests/test_workflow.py` 覆盖取消先落库、迟到块丢弃、截断为失败、在途拒绝、生成期间编辑保存为历史稿。schema v2 增加 draft_rejection 与 story_item.chapter_id。
- [ ] G05 实现单个供应商客户端并接入真实配置（依赖 P04）。完成条件：协议、流、超时、取消及用量得到验证；不自动重试付费请求，密钥不进入项目数据和日志。 进度：`provider.make_anthropic_clients`（anthropic 1.5.0，max_retries=0，取消关闭流，max_tokens 归一为 truncated，密钥只在闭包）与 `bootstrap.py`（配置文件不得含密钥，密钥只读环境变量）已实现；2026-09-14 加入 provider=deepseek，复用 Anthropic 兼容接口，密钥隔离、默认 deepseek-flash、关闭思考。配置校验、假 SDK、真实 SDK 本地模拟、DeepSeek 小样本真实流式/普通响应/取消与引用定位复测已通过；2026-09-16 又用真实 CLI 完成三章 12 次请求，10 次成功且记录用量、2 次 MODEL_FAILED 且没有自动重试。schema v4 起 `run` 记录供应商、模型与终态时间，失败/取消请求的耗时与最终用量可查，历史 12 次请求不回填。真实超时故障与账单费用仍缺证据，G05 保留未完成。详见[验收记录](acceptance-log.md#2026-09-16-deepseek-三章与回改复核)。
- [x] G06 用固定客户端执行 T06–T08 的确定性部分（依赖 R01）。完成条件：三章调用次数、拒绝稿隔离、无效检查与容量边界断言通过；语义准确性留待 V02。 证据：`tests/test_workflow.py::test_t06_three_chapters_with_rejection_and_exploratory`（一次生成一次检查、拒绝稿不入依据、试写记缺口与假设、三章事实链）及无效检查只重试检查；T07 语义部分留 V02。

## R 状态、回改与失败恢复

触点：adoption、generation、projects、domain/rules、database/files。

- [x] R01 实现正式快照计算、事实纠正和提交前依据复核。完成条件：只使用已采用且已审阅的候选及有效修订；保留传闻与主体认知，过时计算只存历史。 证据：`rules.compute_snapshot` 纯计算；`database.commit_snapshot` 比较采用、前章快照与全部 fact_head 后提交，链变化只标 stale；`commit_fact_correction` 追加修订并使依赖快照过时；`tests/test_adoption.py` 中重算失败保留采用、纠正后重算引用修订、迟到提交只存历史三条断言通过。
- [x] R02 实现前章回改后的下游失效与逐章复核。完成条件：不自动改后章正文；未改正文可更新检查/审阅/状态，不新增正文或重复采用。 证据：`adoption.revalidate_chapter` 与 `database.commit_revalidation`：只针对当前已采用版本，保存新审阅与候选快照、解除影响、不新增正文或采用；`tests/test_workflow.py` 的 T10 断言（回改第一章后二三章待复核、旧检查不能复核、复核后采用与版本计数不变、第四章续写从阻断到可写、未授权设定保持待复核）。
- [x] R03 实现重启恢复与阶段重试。完成条件：未完成 run 标中断；检查失败只重试检查，重算失败保留采用；状态未恢复时阻止依赖续写。 证据：`projects.open_project` 把 pending/running 标 interrupted 并报告；`generation.retry_run` 检查失败只重跑检查、生成失败为新任务；`adoption.recompute_state` 对 failed/stale 建新候选快照不新增采用；`tests/test_recovery.py` 的中断重试与重启后重算断言，`tests/test_workflow.py` 的状态未就绪阻断续写断言。
- [x] R04 实现一致性备份与新目录恢复。完成条件：正文、数据库、来源与任务进度一致；包含恢复文件且不含密钥；变化或校验失败不得报告成功，不覆盖原项目。 证据：`projects.backup`（SQLite backup API、前后文件摘要复核、清单、变化即删除输出）与 `projects.restore_backup`（只到空目录、清单/完整性/外键/引用校验、失败清理）；`tests/test_recovery.py` 的往返一致、备份期间变化拒绝、篡改拒绝、不覆盖原项目断言。
- [x] R05 执行 T09–T11 并保存故障注入结果。完成条件：保存失败、取消迟到、重算竞争、后章复核和备份恢复均通过。 证据：T09 见 `tests/test_workflow.py`（取消先落库、迟到块丢弃、截断失败、生成期间编辑、在途拒绝）与 `tests/test_adoption.py`（重算失败保留采用、迟到快照提交只存历史、纠正后重算）；T10 见 `tests/test_workflow.py` T10 段；T11 见 `tests/test_recovery.py` 备份恢复段。全量 `uv run pytest` 通过。

## V 首个可用闭环验收

首个闭环包含 M1/M2 和必要的基础恢复保护；尚不等于完整 V1。

- [x] V01 用真实模型和 CLI 完成连续三章创作、拒绝稿重写、试写、编辑与采用。完成条件：保留操作记录及准确的正文/采用/状态引用。证据：2026-09-16 三章正式正文、检查和 ready 快照，以及第一章回改后第二、三章逐章复核，见[三章执行记录](../.trial-output/三章执行记录.md)；质量与语义验收仍分别在 V02/V03。
- [ ] V02 人工执行 T07 语义样本，核对传闻、持久设定和推理不足。完成条件：逐条记录命中与遗漏；不以格式测试或模型自评代替人工判断。
- [ ] V03 实现按请求的阅读体验诊断与试改，并执行 T12。完成条件：有问题证据、修改选项和作者保留/拒绝理由；不覆盖原稿、不擅自变成整书规则；允许评价为不满意。 进度：`generation.diagnose_draft`（问题证据、修改选项、需授权设定，反馈只存消息）与 `generation.revise_draft`（候选稿不替换当前稿）已实现，`tests/test_workflow.py` 的 T12 确定性断言通过；2026-09-14 已接入 CLI b 入口：证据、选项、按选择试改、保留/拒绝与理由记录；原稿保留。tests/test_cli.py 覆盖保留、拒绝、仅保存候选及不试改路径。作者真实阅读体验记录仍待完成，故不勾选 V03。
- [ ] V04 汇总首期验收报告。完成条件：T01–T11 确定性结果、T05 手动记录、T07/T12 人工记录、每章耗时/用量及已知限制齐全；未解决阻塞已处理，功能缺口明确。

## 下一阶段执行计划（2026-09-14）

起点：实现已提交并推送至 `main`，提交 `efcdda3`；140 项本地测试通过、2 项真实调用默认跳过。DeepSeek 的流式、普通响应、首段后取消与一次真实检查均已有证据。阶段目标是完成首期可用闭环的验收；第 1、2 项的代码与自动化验证已完成，实机记录待 T05；其余任务仍待执行。

| 顺序 | 任务与对应原计划 | 工作范围 | 完成证据 |
| --- | --- | --- | --- |
| 1 | 补齐模型任务取消入口（C04/T05、G04、R03；代码与自动化已完成） | 模型等待期间接收 Ctrl-C，复用现有任务记录与 `cancel_run`；覆盖正文生成及随后检查、讨论、诊断。SQLite 写入保持原连接所属线程，先落取消状态再通知网络客户端。中断后回到可操作界面，部分正文可恢复，不自动重试。 | `tests/test_cli.py::test_cli_cancel_records_before_stopping_and_waits_for_late_client` 覆盖六条流程、正常取消与客户端返回迟到结果两种情况：取消先落库、连接关闭后才接受下一条命令、部分输出保留、无 running 残留。真实 SDK 本地模拟响应覆盖异步流与普通请求，并确认连接关闭。实机终端记录仍待 T05。 |
| 2 | 补齐回改后的复核与失败重试入口（R02/R03、T09/T10；代码与自动化已完成） | 显示章节待复核状态；作者查看准确的已采用正文及新检查后显式复核，复用 `AuthorAction`、`revalidate_chapter` 与问题授权收集。检查失败只重试检查，状态计算失败只重算；已采用但过时不能提示无需操作。不新增数据库业务接口。 | 已接入 `u 复核`、`y 重试` 与章节待复核提示。`tests/test_cli.py` 验证第一章回改后二、三章依次复核，复核绑定已采用正文并保留另存编辑稿；旧检查拒绝、未授权设定仍待复核；检查重试只检查，状态失败只重算，生成失败只在明确选择后新建尝试。复核不新增正文或重复采用。 |
| 3 | 准备三章试用材料（P05；初稿已准备） | 按作者新方向改用《寒灯协定》：魔法世界中的冷战谍战，同一夜的情报递送逐步失败，重点描写日常魔法、公共设施与边境生活。已提供工作设定、三章初稿、知识边界及 M1–M7 样本；旧《井下回声》保留为历史走查。 | 见 [三章试用材料](magic-cold-war-trial.md)。作者给定方向已记录；扩展设定与正文待审阅，不由脚本代签采用或语义验收。 |
| 4 | 收尾接口验证并执行真实三章（G05、V01、T06/T09/T10） | 使用 DeepSeek：第一章正式写作；第二章检查越界样本、拒绝、重写、手动编辑后重新检查；第三章按假设试写并采用。再回改第一章，检查下游复核流程。补超时失败与无自动重试证据；真实网络故障注入仅在确有验证必要时单独安排。 | 保留准确的正文、检查、采用、快照引用与操作记录；每次记录用量、耗时及重试次数。正文生成正常每次一次生成加一次检查；新增诊断、重写或复核单独计数。失败先保存证据并停止该付费路径，不自动反复请求。账单费用不可获得时明确记录缺口，不猜测。 |
| 5 | 作者终端与内容验收（C04、V02/V03、T05/T07/T12） | 实机验证中文输入法、粘贴、光标、保存和取消；逐条核对新素材的 M1–M7（旧 S1–S7 保留为历史样本）；对一稿提出阅读反馈，经诊断、试改后由作者决定保留或拒绝并记录理由。 | 作者填写终端与语义/阅读记录。引用定位成功只证明证据能对齐，不证明事实提取正确；保留漏检、误报与不满意结论。 |
| 6 | 首期验收报告（V04） | 汇总自动化、实机、三章、语义、用量证据；把发现的问题区分为已修复、阻塞与已知限制。 | 每一完成项关联提交或验证记录；阻塞未关闭时不宣布首期验收完成。完成后再拆 M3 的发布记录、完整影响分析、分支与伏笔任务。 |

执行方式：第 4 项真实三章与回改复核已完成；第 5 项保留实机输入法、M1–M7 逐条语义判读和 T12 诊断/试改缺口。付费失败不自动重试，后续只在能定位失败合同并有具体样本时单独执行。

2026-09-16 执行更新：作者提供作品压缩包、DeepSeek 密钥，并委托单人执行及独立子 agent 判读。副本中的第一章先正式采用；第二章真实生成越界稿被拒，显式重写、CLI 编辑后采用；第三章按假设试写、CLI 修结尾后采用。第一章回改并再次采用使第二、三章待复核；旧检查被拒，新的 `k`/`u` 逐章恢复 ready。12 次新增真实请求中 2 次 `MODEL_FAILED`，未自动重试；备份及新目录恢复验证通过。准确版本、用量与缺口见[本次验收记录](acceptance-log.md#2026-09-16-deepseek-三章与回改复核)。

保留边界：备份恢复底层已有确定性验证，本阶段如试用实际需要入口，再复用既有用例补最小入口；不引入新依赖、后台服务、供应商框架或新的产品里程碑。作者认可素材、已读采用及审美判断分别保留为作者动作，不由测试脚本代签。

## M3/M4 后续产品能力

M3 本地闭环已进入可验收状态；M4 仍按长篇能力与导出体验继续推进。

- [x] M3-01 实现准确版本的发布记录与已发布补救流程。完成条件：A14；后续解释不删除已发布历史，本地修订不表示平台同步。证据：`publication` 只追加，已发布版本后续采用需 `AuthorAction.allow_published_revision` 并记录 `publication_divergence`；补救计划由作者消息绑定。
- [x] M3-02 实现完整影响分析。完成条件：A06/A13；可定位证据、区分待检查范围与已确认无关内容，不自动重写全书。证据：`generation.analyze_impact` 强制逐章记录 affected/unrelated/unchecked 与 coverage；CLI `z` 只在作者确认无关后原子重建 ready 快照，依据变化时整体回滚。
- [x] M3-03 实现分支与历史状态隔离。完成条件：A10 的分支部分；分支改写不影响主分支，来源链独立；先补相应数据迁移与隔离测试。证据：Project 显式 current branch，fork 生成独立 novel_id，分支工作副本位于 `work/branches/<branch_id>/`，切换前对账并拒绝 running。
- [x] M3-04 实现伏笔追踪。完成条件：R11；埋设、发展、解释和回收有出处，不强迫固定步骤。证据：`foreshadow_event` 绑定 adopted/version/quote，CLI `o` 入口校验出处原文。
- [ ] M4-01 完善时间线与阅读信息分析。完成条件：故事时间与章节顺序可区分，人物与读者信息可追溯。
- [ ] M4-02 执行代表性长篇样本评估。完成条件：先记录规模基线，再测上下文遗漏、知识泄露、影响遗漏、耗时与成本，形成质量阈值建议。
- [ ] M4-03 完成小说导出与 CLI 体验修正。完成条件：导出可用、正文完整，修复已记录的实际编辑与恢复问题。
- [ ] M4-04 完整 V1 验收。完成条件：R01–R12、A01–A19 均有对应证据，未覆盖部分不能标完成。

## M5 对话式工作台

2026-09-16 作者评价现有单字母命令 CLI 体验很差，要求做成 Codex CLI 那样的对话式代理加全屏界面；设计见[对话式工作台设计](conversational-workbench.md)。作者权限规则不变。

2026-09-18 作者看过结果后否定了这一版："应该是 AI 和人的对话来控制一切，不是现在这种要人来控制、读取对话的意志、操控软件，我要做的是 agent。"M5-03、M5-04 不再验收，由 M6 取代；M5-01 与 M5-02 的供应商工具调用、引擎骨架保留。

- [x] M5-01 供应商层工具调用。完成条件：`ChatMessage` 支持内容块，`CallOptions.tools`，`TOOL_USE` 事件；假 SDK 与真实往返测试通过；不传 tools 时请求不变。证据：`tests/test_recovery.py::test_deepseek_tool_use_round_trip_without_network`（真实 SDK + 本地模拟传输，同步与异步：tools 参数、流式 input_json_delta 解析、非流式 tool_uses、tool_result 消息序列化、不传 tools 时无 tools 字段）；`tests/test_provider_live.py::test_live_tool_use_round_trip` 默认跳过，待真实密钥执行。
- [x] M5-02 门槛流程抽取、工具目录与代理引擎。完成条件：旧 CLI 改用 `console/workbench` 后全部测试仍通过；引擎测试覆盖已读声明、失败不自动重试、拒绝零记录、取消先落库、单轮调用上限；schema v5 允许 `assist` 任务。证据：`console/port.py`、`console/workbench.py`（从 cli.py 抽出，旧 CLI 只剩命令表与端口）、`ai/assistant.py`（27 个工具定义与系统提示）、`console/tools.py`、`console/engine.py`、`application/assistant.py`（assist run 记录用量与终态）；`tests/test_assistant.py` 17 项：一轮内三次调度全部记 run、采用需「已读，采用」、模型口头宣称采用零记录、付费失败后同轮拒绝所有付费工具、无密钥不发请求、单轮上限、取消先落库且不存回复、未知工具与坏参数不执行、编辑器与计划确认；`tests/test_m3.py` v4→v5 迁移。
- [ ] ~~M5-03~~ 已被 M6 取代，不再验收。原目标：Textual 全屏界面。完成条件：三章走查可在新界面完成；T05 对话版人工记录。进度：`tui/app.py` 已实现对话记录、状态栏、多行输入、问题卡、全屏编辑器、Esc 取消与引擎线程；`tests/test_tui.py` 5 项通过（写作流式回显、采用问题卡、编辑器保存与取消、取消先落库、新项目询问标题）。真实终端三章走查与输入法记录待作者执行。
- [ ] ~~M5-04~~ 已被 M6-03 取代。原目标：入口切换与旧 CLI 退役。完成条件：默认进入新界面，`--classic` 保留旧版；文档更新；一轮真实三章对话验收后删除旧 CLI。进度：`python -m story_workbench` 默认进入新界面，`--classic` 进旧版；README、架构、技术选型、AGENTS 分层规则已更新。删除旧 CLI 待真实验收。

## M6 代理式工作台

2026-09-18 作者否定 M5，要求做成真正的 agent：代理主导创作流程，对话即控制；正式变更像 Codex 和 Claude Code 那样弹审批卡，作者批准、不批准或直接打字说意见；没有斜杠命令、固定短语和逐项问答表单。设计见[代理式工作台设计](agent-workbench.md)。作者仍是唯一授权来源：`AuthorAction` 只由审批卡上的批准构造。

- [ ] M6-01 审批端口、工具原语、系统提示与开场轮。完成条件：`console/port.py` 以 `approve(card)` 取代 `ask`；`console/tools.py` 直接调用 application 用例、不再 import `workbench`，审批工具只在批准后构造 `AuthorAction`；系统提示改为协作者人格；引擎支持开场轮；`tests/test_agent.py` 覆盖设计第 12 节的引擎与工具项（批准采用、不批准零记录且意见回到模型、口头宣称零记录、未展示不能采用、问题处理不全拒绝执行、`edit_draft` 改动卡与新稿、`decide` 修改授权、付费失败阻断、取消先落库、8 次上限、无密钥零请求）。
- [ ] M6-02 界面。完成条件：审批卡（Enter 批准、n 不批准、打字带意见、Esc 不批准）；去掉斜杠命令与问题卡；开场轮显示；`tests/test_tui.py` 覆盖设计第 12 节的界面项。
- [ ] M6-03 退役与文档。完成条件：删除 `cli.py`、`cli_editor.py`、`cli_model.py`、`console/workbench.py`、`tests/test_cli.py` 与 `--classic`，移除 `prompt_toolkit` 依赖；旧 CLI 测试覆盖的流程移植到 `tests/test_agent.py`；全部测试与 ruff 通过；README、AGENTS、代码架构第 12 节、技术选型更新。
- [ ] M6-04 真实三章对话验收。完成条件：作者在真实终端用对话完成三章的写、改、采用、回改与复核；T05 对话版人工记录填入[验收记录](acceptance-log.md)；发现的问题回修并记录。

UI 在 CLI 产品流程验证后另行规划；云同步、社区、发布平台集成和全自动长篇不纳入本计划。本文不启动应用开发，也不把待选择的模型或依赖写成已批准事项。
