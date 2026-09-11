# 仓库编码规则

适用于所有在本仓库写代码的人和 agent。设计合同以 [docs/code-architecture.md](docs/code-architecture.md)（V0.2）为准；`.omx/plans/prd-story-framework.md` 中的 workflow.py / store.py / model.py 是历史提案文件名，不作数。产品边界见 [docs/requirements.md](docs/requirements.md)，任务状态见 [docs/project-plan.md](docs/project-plan.md)。

## 命令

```
uv sync                                   # 安装环境（Python 由 .python-version 固定为 3.12）
uv run python -m story_workbench <目录>   # 启动
uv run pytest                             # 测试
uv run ruff check . && uv run ruff format .   # lint 与格式
```

新增依赖用 `uv add`（开发依赖 `uv add --group dev`），提交 `uv.lock`。`.venv/` 不入库。

## 依赖方向

`cli` → `application` → { `domain`, `ai`, `infrastructure` }。

- `domain`（records、rules）：纯函数与记录，不读文件、数据库、网络、时钟，不 import 其他包。
- `ai/context`、`ai/tasks`：纯构建与解析，只通过传入的客户端函数调用模型，不写数据库。
- `infrastructure/database`：唯一的 SQL 写入口；`infrastructure/files`：唯一的文件 I/O；`infrastructure/provider`：唯一的网络 I/O。三者互不 import，也不 import `application`。
- `application` 用例协调以上模块，决定阶段顺序；不写 SQL、不解析供应商协议。
- 只有 `__main__` 装配客户端、项目资源与 CLI。不用服务定位器、依赖注入容器、通用 Repository、事件总线。

## 不可违反的数据规则

- 历史表只追加：version、plan、story_item、adoption、review、resolution、fact_revision、snapshot 等不提供 UPDATE/DELETE 业务接口。恢复、纠正、重新采用都是新记录。
- 正式变更只由作者动作（AuthorAction）触发；模型输出、模型置信度、文件修改时间都不是授权依据。
- 采用、快照提交在 database 的命名提交函数内完成：事务内重读依据、调用 `domain/rules` 复核、原子写入。文件读取与网络请求不进数据库事务。
- 已有外部工作文件不自动覆盖；新文件排他创建。
- 不自动重试付费模型请求；取消先落库再通知客户端；迟到结果不能更新当前稿。
- 密钥只走环境变量，不进 run 记录、日志、导出、备份。
- 检查载荷必须通过结构与证据校验（`body[start:end] == quote`），无效结果记 MODEL_FAILED，不算"无问题"。

## 代码组织

- 按逻辑职责放文件，不按表、不按模型角色建类；共享记录随首次使用加入 `domain/records.py`，只允许追加字段。
- schema 变更只通过 `infrastructure/database.py` 的迁移；其他泳道需要字段时提需求，不直接改。
- 测试按流程组织（`tests/test_adoption.py`、`test_generation.py`、`test_recovery.py`、`test_cli.py`），使用临时目录、真实临时 SQLite 与固定客户端函数；不为每个函数建测试文件，不写空测试。
- 错误统一 `WorkbenchError(code, message, details)`，code 限定在架构第 4.1 节列出的集合。
- 面向作者的文案用中文叙事语言，不暴露内部 ID、表名、置信度。

## 并行泳道（开工后）

A 核心与事务（domain、adoption、projects、database、files）单一负责人；B 作者入口（cli、authoring）；C AI 与上下文（ai、generation、provider）。每线独立分支或 worktree，合并前 `uv run pytest` 与 `uv run ruff check .` 必须通过。
