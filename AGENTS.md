# 仓库编码规则

适用于所有在本仓库写代码的人和 agent。现在开发的是新包 `story_agent`（写小说的 Claude Code），设计见 [docs/agent-workbench.md](docs/agent-workbench.md)，任务状态见 [docs/project-plan.md](docs/project-plan.md)。旧包 `story_workbench` 的规则在文末。

## 命令

```
uv sync                                   # 安装环境（Python 由 .python-version 固定为 3.12）
uv run python -m story_agent <作品目录>    # 启动；目录不是作品时按目录名新建
uv run pytest tests/agent                 # 新包测试
uv run pytest                             # 全部测试（含旧包）
uv run ruff check . && uv run ruff format .   # lint 与格式
```

新增依赖用 `uv add`（开发依赖 `uv add --group dev`），提交 `uv.lock`。`.venv/` 不入库。每个提交前 `uv run pytest tests/agent` 与 `uv run ruff check .` 必须通过。

## 依赖方向

只许往下 import，`tests/agent/test_structure.py` 按模块登记并核对（函数里的延迟 import 与 `TYPE_CHECKING` 块不算）：

```
__main__ → tui/ → agent → tools → review / rewrite
                                → workspace → layout
                  config → provider
叶子（不 import 包内任何模块）：layout、provider、approval、prompts、tui/theme、tui/palette
```

- 界面不认识工具名。工具怎么显示由工具表定，代理在 `tool_started` 事件里带过去。
- `tui/` 内部：`app` 是应用本体，只管线程协调、输入、审批卡、面板与撤回；对话区的显示都在 `transcript`，选项面板在 `palette`，代理线程端口在 `port`。

## 唯一来源

同一件事只写一处。别在代码里再抄一份清单。

| 什么 | 写在哪 |
|---|---|
| 作品目录里的文件名与目录名、`.story/` 里的记录名 | `layout.py` |
| 一个工具的规格、执行函数、中文名、是否付费、界面怎么显示 | `tools.py` 的 `TOOLS` 表 |
| 有哪些写作方法、每份的一句话描述 | `skills/*.md` 的首行 `# 名字：描述` |
| 发给模型的固定文案（人设、开场、压缩、审读、盲读、重写） | `prompts/*.md` |

- 提示词与方法文件里手写的文件名，由 `test_structure.py` 核对是否都在 `layout.py` 里。改了布局就一起改文案。
- `prompts/*.md` 只读不填：没有占位符，不做 `str.format`。动态内容（书名、目录树、摘要……）由代码按固定标题拼在后面。
- 新加一份方法只需放一个 md 文件；`tools._SKILL_ORDER` 只管列出的先后。

## 常量放哪

- 不建 `constants.py`。调参常量（重试间隔、压缩阈值、按键的确认时长等）放在使用它们的模块顶部，带一句注释说为什么是这个数。
- `config.Settings` 只放作者会在配置文件里改的东西；开发者调参不进配置。
- 函数体里的截断长度之类不提成常量，除非两处要保持一致。

## 不可违反的规则

- `workspace` 是唯一碰文件系统与 git 的模块，`provider` 是唯一碰网络的模块。
- 正式文件（`layout.FORMAL_FILES` 与 `FORMAL_DIRS`）的每处改动先弹审批卡，作者批准才写；写入走 `tools` 里的 edit 路径，`Workspace.write_text` 本身不判断审批。模型输出不是授权依据。
- 付费工具（`TOOLS` 里 `paid=True` 的）一轮各调一次；失败后本轮不再调，不自动重试付费请求。
- 历史交给 git；`.story/` 是程序自己的记录，不进 git。改了 `.story/` 里任何记录的结构，就把 `layout.STORY_FORMAT` 加一，并在 `Workspace._check_format` 里写迁移。
- 已有的方法文件，作者改过的不覆盖；没改过的跟着新版更新，旧的留在 `.story/技能备份/`。
- 密钥只走环境变量，不进配置文件、日志、对话记录。
- 面向作者的文案用中文叙事语言，不暴露内部 ID、文件哈希、置信度。

## 测试

- 放在 `tests/agent/`，按流程组织（`test_agent`、`test_tools`、`test_workspace`、`test_tui`、`test_config`、`test_structure`），不为每个函数建文件，不写空测试。
- 用临时目录、真实的临时 git 仓库与脚本化的模型客户端（`conftest.py`）；界面用 Textual 的 `run_test` 驱动。
- 控件上自定义的方法名别和 Textual 基类撞名（例如 `_render`、`log`），撞了不会报错，只会在渲染时崩。

## 旧包 `story_workbench`

旧包在新包通过三章验收前保留，不再开发。它的规则（SQLite、只追加的历史表、采用事务、`WorkbenchError` 错误码等）以 [docs/code-architecture.md](docs/code-architecture.md)（V0.2）为准；启动用 `uv run python -m story_workbench <目录>`。
