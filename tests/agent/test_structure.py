"""守住包的结构：依赖方向、文案里的文件名、方法文件的首行。

这些规则写在 AGENTS.md 里；写下来的规则没有测试守着，迟早会漂。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from story_agent import layout, tools

PACKAGE = Path(__file__).resolve().parents[2] / "story_agent"

# 每个模块在模块顶层可以 import 的包内模块。函数里的延迟 import 与 TYPE_CHECKING 块不算。
# 新加模块时在这里登记它能依赖谁；想加一条边，先想想它是不是放错了层。
ALLOWED = {
    "layout": set(),
    "provider": set(),
    "approval": set(),
    "prompts": set(),
    "config": {"provider"},
    "workspace": {"layout"},
    "review": {"layout", "prompts", "provider"},
    "rewrite": {"layout", "prompts", "provider"},
    "tools": {"approval", "config", "layout", "provider", "workspace"},
    "agent": {"approval", "config", "layout", "prompts", "provider", "tools", "workspace"},
    "tui.theme": set(),
    "tui.palette": set(),
    "tui.text": {"config", "tui.theme"},
    "tui.widgets": {"tui.text"},
    "tui.transcript": {"tui.text", "tui.widgets"},
    "tui.port": {"approval", "provider"},
    "tui.app": {
        "agent",
        "approval",
        "config",
        "provider",
        "workspace",
        "tui.palette",
        "tui.port",
        "tui.text",
        "tui.theme",
        "tui.transcript",
        "tui.widgets",
    },
    "tui": {"tui.app", "tui.text", "tui.theme", "tui.widgets"},
    "__main__": set(),
}


def _module_name(path: Path) -> str:
    parts = path.with_suffix("").relative_to(PACKAGE).parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _top_level_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "story_agent":
                found |= {alias.name for alias in node.names if alias.name != "__version__"}
            elif node.module.startswith("story_agent."):
                found.add(node.module.removeprefix("story_agent."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("story_agent."):
                    found.add(alias.name.removeprefix("story_agent."))
    return found


def test_modules_only_import_downwards():
    modules = {
        _module_name(path): path
        for path in PACKAGE.rglob("*.py")
        if _module_name(path)  # 包本身的 __init__ 只放版本号
    }
    unregistered = sorted(set(modules) - set(ALLOWED))
    assert not unregistered, f"新模块要在 ALLOWED 里登记它能依赖谁：{unregistered}"
    for name, path in modules.items():
        extra = _top_level_imports(path) - ALLOWED[name]
        assert not extra, f"{name} 不该 import {sorted(extra)}"


# 文案里提到的文件名与目录名，必须是 layout 里定义过的；改了布局忘了改文案，这里会报。
# 一-鿿 是 CJK 统一表意文字的范围（U+4E00–U+9FFF）
_NAME = re.compile(r"[一-鿿A-Za-z0-9_-]+(?:/[一-鿿A-Za-z0-9_-]+)*(?:\.md|/)")
_CHAPTER = re.compile(r"^第(\d{2,3}|NN)章\.md$")


def _model_facing_texts() -> dict[str, str]:
    texts = {
        str(path.relative_to(PACKAGE)): path.read_text(encoding="utf-8")
        for folder in ("prompts", "skills")
        for path in (PACKAGE / folder).glob("*.md")
    }
    for spec in tools.TOOL_SPECS:
        texts[f"工具 {spec.name}"] = spec.description + str(spec.input_schema)
    return texts


def _check_name(token: str) -> str | None:
    """不认识就返回原因。中文没有空格，名字前面可能粘着别的字，所以按结尾比对。"""
    files = (*layout.FORMAL_FILES, *layout.FREE_FILES)
    dirs = (*layout.FORMAL_DIRS, *layout.FREE_DIRS, layout.TRANSCRIPT_DIR)
    head, slash, rest = token.partition("/")
    if not slash:
        return None if token.endswith(files) else "不是布局里的文件"
    directory = next((d for d in dirs if head.endswith(d)), None)
    if directory is None:
        return "不是布局里的目录"
    if directory == layout.SKILLS_DIR and rest:
        known = {f"{name}.md" for name in tools.skill_catalogue()}
        return None if rest in known else "没有这份方法"
    if directory == layout.CHAPTERS_DIR and rest:
        return None if _CHAPTER.match(rest) else "章节文件名应是 第NN章.md"
    return None


def test_file_names_in_model_facing_texts_match_the_layout():
    problems = [
        f"{where}：{token}（{reason}）"
        for where, text in _model_facing_texts().items()
        for token in _NAME.findall(text)
        if (reason := _check_name(token))
    ]
    assert not problems, "\n".join(problems)


def test_file_name_guard_notices_a_renamed_file():
    assert _check_name("大纲.md") is None and _check_name("先读大纲.md") is None
    assert _check_name("提纲.md") and _check_name("技能/写小说.md") and _check_name("正文/")


def test_every_skill_file_starts_with_name_and_description():
    for path in (PACKAGE / "skills").glob("*.md"):
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        match = re.match(r"^#\s*(.+?)：(.+)$", first)
        assert match and match.group(1) == path.stem, f"{path.name} 首行应是「# {path.stem}：描述」"
