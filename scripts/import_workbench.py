"""一次性脚本：把旧工作台的 SQLite 作品导出成 story_agent 的作品目录。

用法：uv run python scripts/import_workbench.py <story.sqlite3> <目标目录>

导出：书名 → 作品.md；每章已采用正文 → 章节/第NN章.md；每章最新计划 → 大纲.md；
作者已确定的决定 → 决定.md；不带章节的作者留言 → 作品.md 的偏好。故事状态留给代理整理。
只读数据库，不改旧库。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story_agent.workspace import (  # noqa: E402
    DECISIONS_FILE,
    OUTLINE_FILE,
    STATE_FILE,
    WORK_FILE,
    Workspace,
    chapter_file,
)

_DIGITS = "零一二三四五六七八九"


def chinese_number(number: int) -> str:
    if number < 10:
        return _DIGITS[number]
    if number < 20:
        return "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        return _DIGITS[number // 10] + "十" + (_DIGITS[number % 10] if number % 10 else "")
    return str(number)


def local_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = Path(argv[0]), Path(argv[1])
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    novel = conn.execute("SELECT id, title FROM novel LIMIT 1").fetchone()
    if novel is None:
        print("旧库里没有作品。", file=sys.stderr)
        return 1
    novel_id, title = novel["id"], novel["title"]
    ws = Workspace.create(target, title)

    chapters = conn.execute(
        "SELECT id, number, adopted_id FROM chapter WHERE novel_id = ? ORDER BY number", (novel_id,)
    ).fetchall()
    outline = ["# 大纲", "", "## 全书走向", "", "（导入自旧工作台，尚未写全书走向。）", ""]
    written = 0
    for chapter in chapters:
        number = chapter["number"]
        plan = conn.execute(
            "SELECT plot, ending FROM plan WHERE chapter_id = ? ORDER BY revision DESC LIMIT 1",
            (chapter["id"],),
        ).fetchone()
        body = None
        if chapter["adopted_id"]:
            row = conn.execute(
                "SELECT v.body FROM adoption a JOIN version v ON v.id = a.version_id "
                "WHERE a.id = ?",
                (chapter["adopted_id"],),
            ).fetchone()
            body = row["body"] if row else None
        status = "已定稿" if body else "未写"
        outline += [
            f"## 第{chinese_number(number)}章",
            "",
            f"- 剧情：{(plan['plot'] if plan else None) or '（未定）'}",
            f"- 结尾状态：{(plan['ending'] if plan else None) or '（未定）'}",
            f"- 状态：{status}",
            "",
        ]
        if body:
            ws.write_text(
                chapter_file(number), f"# 第{chinese_number(number)}章\n\n{body.strip()}\n"
            )
            written += 1
    ws.write_text(OUTLINE_FILE, "\n".join(outline))

    decisions = ["# 作者决定", ""]
    for row in conn.execute(
        "SELECT body, created_at FROM story_item WHERE novel_id = ? AND lifecycle = 'decided' "
        "ORDER BY created_at",
        (novel_id,),
    ):
        decisions.append(f"- {local_date(row['created_at'])}：{row['body'].strip()}")
    if len(decisions) == 2:
        decisions.append("（还没有决定。）")
    ws.write_text(DECISIONS_FILE, "\n".join(decisions) + "\n")

    preferences = [
        row["body"].strip()
        for row in conn.execute(
            "SELECT body FROM message WHERE novel_id = ? AND role = 'author' "
            "AND chapter_id IS NULL "
            "ORDER BY created_at",
            (novel_id,),
        )
    ]
    work = ws.read_text(WORK_FILE)
    if preferences:
        work = work.replace(
            "（喜欢什么样的文字，不要什么。）",
            "\n".join(f"- {p}" for p in preferences) + "\n\n（以上导入自旧工作台的作者留言。）",
        )
    ws.write_text(WORK_FILE, work)
    ws.write_text(
        STATE_FILE,
        "# 故事状态\n\n（导入自旧工作台，尚未整理。"
        "请按 技能/故事状态.md 读已定稿的章节后整理。）\n",
    )
    ws.commit(f"导入自旧工作台：{written} 章正文")
    print(f"已导出《{title}》到 {target}：{written} 章正文，{len(chapters)} 章计划。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
