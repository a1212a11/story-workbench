"""首期终端交互：作品与章节、讨论、计划、候选与决定、写作与试写、检查、采用、拒绝、编辑与恢复。

只调用 application 用例和 infrastructure/database 的读函数；不写 SQL。模型客户端由启动入口
注入，接入前（G05）为 None，相关命令提示“模型尚未接入”。面向作者的文案是中文叙事语言，
不显示内部 ID。采用只绑定最近一次完整展示过的稿件；无法绑定时列出候选稿，不写任何正式记录。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession

from story_workbench.application import adoption, authoring, generation, projects
from story_workbench.application.projects import Project, create_project, open_project
from story_workbench.cli_editor import edit_text
from story_workbench.domain.records import (
    AuthorAction,
    Authority,
    ChapterRecord,
    CheckRecord,
    ErrorCode,
    Expected,
    FactKind,
    IssueKind,
    IssueRecord,
    IssueResolution,
    Lifecycle,
    ResolutionAction,
    SnapshotStatus,
    Target,
    VersionOrigin,
    VersionRecord,
    WorkbenchError,
    WriteMode,
)
from story_workbench.infrastructure import database

_DIGITS = "零一二三四五六七八九"
_ORIGIN_LABEL = {
    VersionOrigin.AUTHOR: "修改稿",
    VersionOrigin.GENERATED: "写作稿",
    VersionOrigin.EXTERNAL: "外部稿",
    VersionOrigin.RESTORE: "恢复稿",
}
_ISSUE_LABEL = {
    IssueKind.NEW_PERSISTENT_SETTING: "新增持久设定",
    IssueKind.DECISION_CONFLICT: "与既有决定冲突",
    IssueKind.CANON_CONFLICT: "与正文冲突",
    IssueKind.PLAN_DEVIATION: "计划偏差",
    IssueKind.RUMOR_OR_BELIEF: "传闻或人物认知",
    IssueKind.SUSPECTED_ERROR: "疑似误判",
}
_FACT_LABEL = {
    FactKind.WORLD: "世界事实",
    FactKind.CHARACTER_BELIEF: "人物认知",
    FactKind.READER_INFORMATION: "读者已知",
    FactKind.NARRATION: "叙述",
    FactKind.UNCERTAIN: "不确定",
}
_LIFECYCLE_LABEL = {
    Lifecycle.DRAFT: "模型建议",
    Lifecycle.CANDIDATE: "候选想法",
    Lifecycle.DECIDED: "已确定",
    Lifecycle.REJECTED: "已否定",
    Lifecycle.ARCHIVED: "已归档",
}
READ_DECLARATIONS = frozenset({"已读，采用", "已读,采用", "已读采用", "已读 采用"})

HELP = (
    "可用操作：\n"
    "  n 新建章节    c 选章          l 章节列表\n"
    "  t 讨论        p 剧情与结尾    d 记为候选      f 确定\n"
    "  w 写          x 试写          v 查看全文      e 编辑正文\n"
    "  k 检查        i 查看检查      a 采用          j 拒绝\n"
    "  b 阅读诊断与试改\n"
    "  r 恢复历史版本  h 帮助        q 退出"
)


def chinese_number(number: int) -> str:
    """1 → 一，12 → 十二，105 → 一百零五；章号只用于展示。"""
    if number < 10:
        return _DIGITS[number]
    if number < 20:
        return "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        return _DIGITS[number // 10] + "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 1000:
        hundreds = _DIGITS[number // 100] + "百"
        rest = number % 100
        if rest == 0:
            return hundreds
        if rest < 10:
            return hundreds + "零" + _DIGITS[rest]
        return hundreds + chinese_number(rest)
    return str(number)


def chapter_label(chapter: ChapterRecord) -> str:
    return f"第{chinese_number(chapter.number)}章"


def local_time(created_at: str) -> str:
    return datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d %H:%M")


def draft_label(
    chapter: ChapterRecord, version: VersionRecord, adopted_version_id: str | None = None
) -> str:
    """形如「第一章 · 修改稿 · 未采用」；不显示内部 ID。"""
    adoption_mark = "已采用" if version.id == adopted_version_id else "未采用"
    return f"{chapter_label(chapter)} · {_ORIGIN_LABEL[version.origin]} · {adoption_mark}"


def first_line(body: str, limit: int = 30) -> str:
    line = body.strip().splitlines()[0] if body.strip() else "（空）"
    return line if len(line) <= limit else line[:limit] + "……"


def print_inline(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


@dataclass(frozen=True, slots=True)
class Shown:
    """最近一次向作者完整展示的稿件；explicit 表示作者从候选列表中明确选定。"""

    chapter_id: str
    version_id: str
    explicit: bool = False


class Workbench:
    """一个已打开作品的交互循环。say 输出整行文字，emit 输出流式片段，便于测试捕获。"""

    def __init__(
        self,
        project: Project,
        session: PromptSession[str],
        say: Callable[[str], None] = print,
        *,
        clients: generation.Clients | None = None,
        config: generation.GenerationConfig | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self.project = project
        self.session = session
        self.say = say
        self.emit = emit or print_inline
        self.clients = clients
        self.config = config
        self.chapter: ChapterRecord | None = None
        self.shown: Shown | None = None
        self.commands: dict[str, Callable[[], bool]] = {}
        for keys, handler in (
            (("n", "新建章节", "新建"), self.new_chapter),
            (("c", "选章"), self.choose_chapter),
            (("l", "章节列表", "列表"), self.show_chapters),
            (("t", "讨论"), self.discuss),
            (("p", "剧情与结尾", "计划"), self.set_plan),
            (("d", "记为候选", "候选"), self.record_candidate),
            (("f", "确定", "就这么定"), self.decide),
            (("w", "写", "写作"), self.write),
            (("x", "试写"), self.explore),
            (("v", "查看全文", "全文"), self.show_body),
            (("e", "编辑正文", "编辑"), self.edit_body),
            (("k", "检查"), self.check),
            (("i", "查看检查"), self.show_latest_check),
            (("b", "阅读诊断与试改", "阅读诊断", "诊断"), self.diagnose),
            (("a", "采用", "采用那版", "采用此稿"), self.adopt),
            (("j", "拒绝", "不要这版"), self.reject),
            (("r", "恢复历史版本", "恢复"), self.restore),
            (("h", "帮助", "?"), self.help),
        ):
            for key in keys:
                self.commands[key] = handler

    # ------------------------------------------------------------ 输入输出

    def ask(self, prompt: str) -> str:
        return self.session.prompt(prompt).strip()

    def refresh(self) -> ChapterRecord | None:
        if self.chapter is not None:
            self.chapter = authoring.get_chapter(self.project, self.chapter.id)
        return self.chapter

    def require_chapter(self) -> ChapterRecord | None:
        chapter = self.refresh()
        if chapter is None:
            self.say("还没有选中章节。先用 n 新建章节，或用 c 选章。")
        return chapter

    def model_ready(self, what: str) -> bool:
        if self.clients is None or self.config is None:
            self.say(f"模型尚未接入，暂时不能{what}。其余操作不受影响。")
            return False
        return True

    def version(self, chapter: ChapterRecord, version_id: str) -> VersionRecord:
        return authoring.get_version(
            self.project, Target(self.project.novel_id, chapter.id, version_id)
        )

    def current_draft(self, chapter: ChapterRecord) -> VersionRecord | None:
        if chapter.current_draft_id is None:
            return None
        return self.version(chapter, chapter.current_draft_id)

    def shown_draft(self, chapter: ChapterRecord) -> VersionRecord | None:
        if self.shown is None or self.shown.chapter_id != chapter.id:
            return None
        return self.version(chapter, self.shown.version_id)

    def target_draft(self, chapter: ChapterRecord) -> VersionRecord | None:
        """检查、查看检查、拒绝的对象：最近展示的稿；没有展示过则是现在的编辑稿。"""
        return self.shown_draft(chapter) or self.current_draft(chapter)

    def adopted_version_id(self, chapter: ChapterRecord) -> str | None:
        return authoring.adopted_version_id(self.project, chapter.id)

    def display_draft(
        self, chapter: ChapterRecord, version: VersionRecord, *, explicit: bool = False
    ) -> None:
        self.say(draft_label(chapter, version, self.adopted_version_id(chapter)))
        self.say("")
        self.say(version.body)
        self.say("")
        self.shown = Shown(chapter.id, version.id, explicit)

    def latest_check(self, version: VersionRecord) -> CheckRecord | None:
        return database.latest_check_for_version(
            self.project.conn, self.project.novel_id, version.id
        )

    # ------------------------------------------------------------ 主循环

    def loop(self) -> None:
        self.say(f"已打开《{self.project.novel.title}》。")
        self.show_chapters()
        self.say(HELP)
        while True:
            where = chapter_label(self.chapter) if self.chapter else "未选章"
            try:
                command = self.ask(f"{where} > ")
            except (EOFError, KeyboardInterrupt):
                self.say("已退出。")
                return
            if command in ("q", "退出"):
                self.say("已退出，所有保存过的内容都在。")
                return
            handler = self.commands.get(command)
            if handler is None:
                if command:
                    self.say(f"不认识「{command}」。输入 h 查看可用操作。")
                continue
            try:
                handler()
            except WorkbenchError as exc:
                self.say(f"没有完成：{exc.message}")
            except (EOFError, KeyboardInterrupt):
                self.say("已中止这一步；保存过的内容都在。")

    def help(self) -> bool:
        self.say(HELP)
        return True

    # ------------------------------------------------------------ 作品与章节

    def show_chapters(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        if not chapters:
            self.say("这部作品还没有章节。用 n 新建第一章。")
            return True
        self.say(f"《{self.project.novel.title}》的章节：")
        for chapter in chapters:
            marks = ["有正文" if chapter.current_draft_id else "还没有正文"]
            if chapter.current_plan_id:
                marks.append("已有剧情与结尾")
            if chapter.adopted_id:
                marks.append("已采用")
            if self.chapter is not None and chapter.id == self.chapter.id:
                marks.append("当前")
            self.say(f"  {chapter.number:>3}. {chapter_label(chapter)} · {' · '.join(marks)}")
        return True

    def new_chapter(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        number = max((c.number for c in chapters), default=0) + 1
        self.chapter = authoring.create_chapter(self.project, number)
        self.say(f"已新建{chapter_label(self.chapter)}，现在就在这一章。")
        return True

    def choose_chapter(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        if not chapters:
            self.say("这部作品还没有章节。用 n 新建第一章。")
            return True
        self.show_chapters()
        answer = self.ask("要去第几章（输入数字，回车取消）：")
        if not answer:
            return True
        by_number = {c.number: c for c in chapters}
        if not answer.isdigit() or int(answer) not in by_number:
            self.say("没有这一章。")
            return True
        self.chapter = by_number[int(answer)]
        self.say(f"现在在{chapter_label(self.chapter)}。")
        return True

    # ------------------------------------------------------------ 讨论、计划、想法与决定

    def discuss(self) -> bool:
        chapter = self.refresh()
        text = self.ask("你说：")
        if not text:
            return True
        if chapter is None or self.clients is None or self.config is None:
            authoring.record_message(self.project, text, chapter.id if chapter else None)
            if chapter is None:
                self.say("已记录你的话。选章后可以和 AI 讨论这一章。")
            else:
                self.say("已记录你的话。模型尚未接入，之后会在这里回复。")
            return True
        self.say("AI：")
        try:
            generation.discuss(
                self.project, chapter.id, text, self.clients, self.config, on_text=self.emit
            )
        except WorkbenchError as exc:
            self.emit("\n")
            self.say(f"讨论回复没有完成：{exc.message} 你的话已记录。")
            return True
        self.emit("\n")
        return True

    def set_plan(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        self.say(f"为{chapter_label(chapter)}写下剧情与结尾状态；留空表示这一项暂不确定。")
        plot = self.ask("本章剧情：")
        ending = self.ask("本章结尾状态：")
        if not plot and not ending:
            self.say("两项都为空，计划没有改变。")
            return True
        self.save_plan(chapter, plot or None, ending or None)
        return True

    def save_plan(self, chapter: ChapterRecord, plot: str | None, ending: str | None) -> None:
        note = f"本章剧情：{plot or '（暂不确定）'}\n本章结尾状态：{ending or '（暂不确定）'}"
        message_id = authoring.record_message(self.project, note, chapter.id)
        plan = authoring.save_plan(
            self.project,
            chapter.id,
            plot,
            ending,
            Expected(plan_id=chapter.current_plan_id),
            message_id,
        )
        self.refresh()
        self.say(f"{chapter_label(chapter)}的剧情与结尾已记录（第 {plan.revision} 次）。")

    def record_candidate(self) -> bool:
        chapter = self.refresh()
        text = self.ask("候选想法（一句话）：")
        if not text:
            return True
        chapter_id = chapter.id if chapter else None
        message_id = authoring.record_message(self.project, f"记为候选：{text}", chapter_id)
        authoring.record_item(
            self.project,
            kind="idea",
            body=text,
            authority=Authority.AUTHOR_IDEA,
            lifecycle=Lifecycle.CANDIDATE,
            source_message_id=message_id,
            chapter_id=chapter_id,
        )
        self.say("已记为候选想法。候选不会自动成为决定或正式事实；用 f 可以确定它。")
        return True

    def decide(self) -> bool:
        chapter = self.refresh()
        chapter_id = chapter.id if chapter else None
        items = [
            item
            for item in authoring.list_story_items(self.project)
            if item.lifecycle in (Lifecycle.DRAFT, Lifecycle.CANDIDATE, Lifecycle.DECIDED)
        ]
        if items:
            self.say("已有的想法与决定：")
            for index, item in enumerate(items, start=1):
                self.say(f"  {index:>3}. {_LIFECYCLE_LABEL[item.lifecycle]} · {item.body}")
        answer = self.ask("要确定哪一条（输入序号），或直接写下新的决定（回车取消）：")
        if not answer:
            return True
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            item = items[int(answer) - 1]
            if item.lifecycle is Lifecycle.DECIDED:
                return self.revise_decision(item, chapter_id)
            message_id = authoring.record_message(
                self.project, f"就这么定：{item.body}", chapter_id
            )
            authoring.decide_item(
                self.project, item, lifecycle=Lifecycle.DECIDED, source_message_id=message_id
            )
            self.say(f"已确定：{item.body}")
            return True
        message_id = authoring.record_message(self.project, f"确定：{answer}", chapter_id)
        authoring.record_item(
            self.project,
            kind="decision",
            body=answer,
            authority=Authority.AUTHOR_DECISION,
            lifecycle=Lifecycle.DECIDED,
            source_message_id=message_id,
            chapter_id=chapter_id,
        )
        self.say(f"已确定：{answer}")
        return True

    def revise_decision(self, item, chapter_id: str | None) -> bool:
        new_body = self.ask("这条已经确定。要改成什么（回车保持不变）：")
        if not new_body or new_body == item.body:
            self.say("已确定的内容保持不变。")
            return True
        grant = self.ask(
            "修改已确定的内容会让之前依据它的检查过时。输入「授权」确认，其他表示放弃："
        )
        if grant != "授权":
            self.say("没有授权，已确定的内容保持不变。")
            return True
        message_id = authoring.record_message(
            self.project, f"授权修改决定：{item.body} → {new_body}", chapter_id
        )
        authoring.decide_item(
            self.project,
            item,
            lifecycle=Lifecycle.DECIDED,
            source_message_id=message_id,
            body=new_body,
            authorize_change=True,
        )
        self.say(f"已按你的授权修改这条决定：{new_body}")
        return True

    # ------------------------------------------------------------ 写作与试写

    def write(self) -> bool:
        return self.generate(WriteMode.FORMAL)

    def explore(self) -> bool:
        return self.generate(WriteMode.EXPLORATORY)

    def generate(self, mode: WriteMode) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        if not self.model_ready("写作"):
            return True
        label = chapter_label(chapter)
        plan = authoring.get_current_plan(self.project, chapter.id)
        plot = plan.plot if plan else None
        ending = plan.ending if plan else None
        assumptions: tuple[str, ...] = ()
        if mode is WriteMode.FORMAL:
            if not plot or not ending:
                missing = [
                    name for name, value in (("剧情", plot), ("结尾状态", ending)) if not value
                ]
                self.say(f"正式写作需要本章剧情和结尾状态，现在还缺{'和'.join(missing)}。")
                plot = plot or self.ask("本章剧情：") or None
                ending = ending or self.ask("本章结尾状态：") or None
                if not plot or not ending:
                    self.say("仍有缺项，没有开始写作。想先探索可以用 x 试写。")
                    return True
                self.save_plan(chapter, plot, ending)
                chapter = self.refresh() or chapter
            request = self.ask("这次写作还有什么要求（回车直接开始）：")
            text = f"按当前剧情与结尾写{label}。" + (f"要求：{request}" if request else "")
        else:
            gaps = [name for name, value in (("剧情", plot), ("结尾状态", ending)) if not value]
            if gaps:
                self.say(
                    f"试写：{label}的{'和'.join(gaps)}还没有确定，这次先探索，不作为正式计划。"
                )
            else:
                self.say(f"试写：{label}已有剧情与结尾，这次生成只作探索。")
            assumption = self.ask("这次试写的假设（一句话，回车留空）：")
            assumptions = (assumption,) if assumption else ()
            text = f"试写{label}。" + (f"假设：{assumption}" if assumption else "")
        message_id = authoring.record_message(self.project, text, chapter.id)
        self.say("开始写作，输出会逐段显示；结束后展示全文与检查。")
        try:
            result = generation.write_chapter(
                self.project,
                chapter.id,
                mode,
                message_id,
                self.clients,
                self.config,
                assumptions=assumptions,
                on_text=self.emit,
            )
        except WorkbenchError as exc:
            self.emit("\n")
            self.say(f"写作没有完成：{exc.message}")
            return True
        self.emit("\n")
        fresh = self.refresh() or chapter
        version = self.version(fresh, result.version_id)
        if result.based_on_old_inputs:
            self.say(
                "生成期间这一章的编辑稿已经变化；这一稿已保存为另一稿，现在的编辑稿没有被替换。"
            )
        self.say("生成结束。以下是完整正文：")
        self.display_draft(fresh, version)
        if result.check_id is None:
            self.say("检查没有完成，这一稿保留；可以用 k 重新检查。")
        else:
            self.show_check(
                database.get_check(self.project.conn, self.project.novel_id, result.check_id)
            )
        return True

    # ------------------------------------------------------------ 检查

    def check(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        if not self.model_ready("检查"):
            return True
        target = self.target_draft(chapter)
        if target is None:
            self.say(f"{chapter_label(chapter)}还没有正文，没有可检查的稿件。")
            return True
        label = draft_label(chapter, target, self.adopted_version_id(chapter))
        message_id = authoring.record_message(self.project, f"检查{label}。", chapter.id)
        self.say(f"正在检查 {label}……")
        try:
            check_id = generation.check_draft(
                self.project,
                Target(self.project.novel_id, chapter.id, target.id),
                message_id,
                self.clients,
                self.config,
            )
        except WorkbenchError as exc:
            self.say(f"检查没有完成：{exc.message} 可以再次 k 重试。")
            return True
        self.say(f"已完成对 {label} 的检查。")
        self.show_check(database.get_check(self.project.conn, self.project.novel_id, check_id))
        return True

    def show_latest_check(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        target = self.target_draft(chapter)
        if target is None:
            self.say(f"{chapter_label(chapter)}还没有正文。")
            return True
        check = self.latest_check(target)
        label = draft_label(chapter, target, self.adopted_version_id(chapter))
        if check is None:
            self.say(f"{label} 还没有检查。用 k 检查。")
            return True
        self.say(f"{label} 的最新检查：")
        self.show_check(check)
        return True

    def show_check(self, check: CheckRecord) -> None:
        conn, novel_id = self.project.conn, self.project.novel_id
        self.say(f"  实际剧情：{check.actual_plot}")
        self.say(f"  实际结尾：{check.actual_ending}")
        self.say(f"  已检查范围：{'、'.join(check.coverage_checked) or '未说明'}")
        self.say(f"  未检查范围：{'、'.join(check.coverage_unchecked) or '未列出'}")
        if not check.issues:
            self.say("  本次检查未发现需要处理的问题；未检查范围不代表没有问题。")
        for index, issue in enumerate(check.issues, start=1):
            self.say(f"  问题 {index} · {_ISSUE_LABEL[issue.kind]}：{issue.explanation}")
            for _, _, quote in database.list_issue_evidence(conn, novel_id, issue.id):
                self.say(f"    原文：「{quote}」")
            if issue.proposed_change:
                self.say(f"    建议：{issue.proposed_change}")
        facts = database.list_candidate_facts(conn, novel_id, check.id)
        if not facts:
            self.say("  候选事实：无。")
            return
        self.say("  候选事实：")
        for fact in facts:
            subject = f"{fact.subject}：" if fact.subject else ""
            self.say(
                f"    · {_FACT_LABEL[fact.kind]} · {subject}{fact.proposition}"
                f"（原文：「{fact.quote}」）"
            )

    def list_issues(self, issues: list[IssueRecord]) -> None:
        for issue in issues:
            self.say(f"  · {_ISSUE_LABEL[issue.kind]}：{issue.explanation}")

    # ------------------------------------------------------------ 阅读诊断与试改

    def diagnose(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None or not self.model_ready("阅读诊断"):
            return True
        version = self.target_draft(chapter)
        if version is None:
            self.say(f"{chapter_label(chapter)}还没有正文。")
            return True
        current_id = chapter.current_draft_id
        self.display_draft(chapter, version, explicit=True)
        feedback = self.ask("读起来哪里不满意，或想重点看什么（回车取消）：")
        if not feedback:
            return True
        target = Target(self.project.novel_id, chapter.id, version.id)
        _, payload = generation.diagnose_draft(
            self.project, target, feedback, self.clients, self.config
        )
        self.say(f"阅读诊断：{payload.verdict}")
        for problem in payload.problems:
            self.say(f"  问题：{problem.explanation}")
            for evidence in problem.evidence:
                self.say(f"    原文：「{evidence.quote}」")
        for index, option in enumerate(payload.options, start=1):
            flag = "（需新增设定授权）" if option.requires_new_setting else ""
            self.say(f"  {index}. {option.description}{flag}")
        for setting in payload.new_settings_requiring_authorization:
            self.say(f"  需授权的新设定：{setting}")
        if not payload.options:
            self.say("没有可选修改，原稿保留。")
            return True
        answer = self.ask("按哪个选项试改（输入序号，回车只看诊断）：")
        if not answer.isdigit() or not 1 <= int(answer) <= len(payload.options):
            self.say("没有开始试改，诊断已记录，原稿保留。")
            return True
        option = payload.options[int(answer) - 1]
        message_id = authoring.record_message(
            self.project, f"仅对这一稿试改：{option.description}", chapter.id
        )
        self.say("开始试改，生成候选稿；新增设定仍需在采用时明确授权。")
        try:
            result = generation.revise_draft(
                self.project,
                target,
                option.description,
                message_id,
                self.clients,
                self.config,
                on_text=self.emit,
            )
        except WorkbenchError as exc:
            self.emit("\n")
            self.say(f"试改没有完成：{exc.message} 原稿保留。")
            return True
        self.emit("\n")
        candidate = self.version(chapter, result.version_id)
        self.display_draft(chapter, candidate, explicit=True)
        if result.check_id:
            self.show_check(
                database.get_check(self.project.conn, self.project.novel_id, result.check_id)
            )
        else:
            self.say("试改稿已保存，检查尚未完成；采用前请用 k 检查。")
        choice = self.ask("怎样处理试改稿（保留为编辑稿 / 拒绝 / 回车仅保存候选）：")
        if choice in ("保留", "保留为编辑稿"):
            reason = self.ask("保留的理由（回车留空）：")
            authoring.record_message(self.project, f"请求保留试改稿为编辑稿。{reason}", chapter.id)
            try:
                authoring.choose_current_draft(
                    self.project,
                    Target(self.project.novel_id, chapter.id, candidate.id),
                    Expected(current_draft_id=current_id),
                )
            except WorkbenchError as exc:
                self.say(f"没有切换编辑稿：{exc.message} 试改稿已保存，请重新查看后选择。")
                return True
            self.say("已保留试改稿为编辑稿，原稿仍在历史里；尚未采用。")
        elif choice == "拒绝":
            return self.reject()
        else:
            self.say("试改稿仅作为候选保存，当前编辑稿保持不变。")
        self.refresh()
        return True

    # ------------------------------------------------------------ 采用

    def candidate_drafts(self, chapter: ChapterRecord) -> list[VersionRecord]:
        rejected = database.list_rejected_version_ids(self.project.conn, self.project.novel_id)
        adopted = self.adopted_version_id(chapter)
        return [
            v
            for v in authoring.list_versions(self.project, chapter.id)
            if v.id not in rejected and v.id != adopted
        ]

    def bind_target(self, chapter: ChapterRecord) -> VersionRecord | None:
        """确定“这一稿”指哪一份；无法确定时列出候选并返回 None，不写任何记录。"""
        candidates = self.candidate_drafts(chapter)
        shown = self.shown if self.shown and self.shown.chapter_id == chapter.id else None
        if shown is not None and (shown.explicit or shown.version_id == chapter.current_draft_id):
            return self.version(chapter, shown.version_id)
        if shown is None and len(candidates) == 1:
            return candidates[0]
        if not candidates:
            self.say(f"{chapter_label(chapter)}没有可以采用的稿件。")
            return None
        self.say(
            f"{chapter_label(chapter)}有多份稿件，无法确定“那版”指哪一稿；没有做任何采用记录："
        )
        for index, version in enumerate(candidates, start=1):
            marks = [local_time(version.created_at), _ORIGIN_LABEL[version.origin]]
            if version.id == chapter.current_draft_id:
                marks.append("现在的编辑稿")
            if shown is not None and version.id == shown.version_id:
                marks.append("最近展示过")
            self.say(f"  {index:>3}. {' · '.join(marks)} · {first_line(version.body)}")
        answer = self.ask("要查看哪一稿（输入序号，回车取消）：")
        if not answer:
            return None
        if not answer.isdigit() or not 1 <= int(answer) <= len(candidates):
            self.say("没有这个序号。")
            return None
        self.display_draft(chapter, candidates[int(answer) - 1], explicit=True)
        self.say("看完后用 a 采用这一稿。")
        return None

    def adopt(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        target = self.bind_target(chapter)
        if target is None:
            return True
        if target.id == self.adopted_version_id(chapter):
            self.report_already_adopted(chapter)
            return True
        self.say("采用前请再看一遍要采用的稿件与检查：")
        self.display_draft(chapter, target, explicit=True)
        check = self.latest_check(target)
        if check is None:
            self.say("这一稿还没有检查，不能采用。先用 k 检查。")
            return True
        self.show_check(check)
        answer = self.ask("读完全文和检查后，输入「已读，采用」完成采用（回车取消）：")
        if answer not in READ_DECLARATIONS:
            self.say("没有采用。采用需要明确声明已读全文并采用。")
            return True
        notes: list[str] = [f"已阅读{chapter_label(chapter)}全文与检查，采用这一稿。"]
        resolutions, plot, ending, declined = self.resolve_issues(check, notes)
        if declined:
            self.say("这一稿有未处理的问题，没有采用：")
            self.list_issues(declined)
            self.say("可以用 e 修改稿件、j 拒绝这一稿，或再次 a 采用并给出授权。")
            return True
        basis = database.get_basis(self.project.conn, self.project.novel_id, check.basis_id)
        if basis.purpose == "exploratory" and not (plot and ending):
            plot, ending = self.accept_plot_and_ending(
                check, "这是试写稿，采用时需要接受探索出的剧情与结尾。", notes
            )
            if plot is None:
                self.say("没有采用。试写稿需要明确接受剧情与结尾。")
                return True
        chapter = self.refresh() or chapter
        previous = [
            c for c in authoring.list_chapters(self.project) if c.number == chapter.number - 1
        ]
        message_id = authoring.record_message(self.project, "\n".join(notes), chapter.id)
        action = AuthorAction(
            request_id=uuid.uuid4().hex,
            source_message_id=message_id,
            target=Target(self.project.novel_id, chapter.id, target.id),
            expected=Expected(
                adopted_id=chapter.adopted_id,
                plan_id=chapter.current_plan_id,
                previous_snapshot_id=previous[0].current_snapshot_id if previous else None,
            ),
            check_id=check.id,
            read_declared=True,
            issue_resolutions=tuple(resolutions),
            accepted_plot=plot,
            accepted_ending=ending,
        )
        try:
            result = adoption.adopt(self.project, action)
        except WorkbenchError as exc:
            self.explain_adoption_failure(exc, check)
            return True
        self.refresh()
        if result.already_adopted:
            self.say("当前稿已采用，无需重复操作。")
        elif result.state_status is SnapshotStatus.READY:
            self.say(f"已采用{chapter_label(chapter)}这一稿，故事状态更新完成。")
        else:
            self.say("已采用这一稿，但故事状态更新失败；正文已是采用版本，再次 a 采用可重试更新。")
        return True

    def resolve_issues(
        self, check: CheckRecord, notes: list[str]
    ) -> tuple[list[IssueResolution], str | None, str | None, list[IssueRecord]]:
        """逐条处理需要作者表态的问题；返回 (授权列表, 接受的剧情, 接受的结尾, 未处理问题)。"""
        resolutions: list[IssueResolution] = []
        declined: list[IssueRecord] = []
        plot: str | None = None
        ending: str | None = None
        for issue in check.issues:
            label = _ISSUE_LABEL[issue.kind]
            self.say(f"问题 · {label}：{issue.explanation}")
            if issue.kind is IssueKind.NEW_PERSISTENT_SETTING:
                answer = self.ask("接受这项新增设定进入正式故事记录？（接受 / 不接受）：")
                if answer == "接受":
                    resolutions.append(
                        IssueResolution(issue.id, ResolutionAction.AUTHORIZE_SETTING, "作者接受")
                    )
                    notes.append(f"接受新增设定：{issue.explanation}")
                else:
                    declined.append(issue)
            elif issue.kind is IssueKind.DECISION_CONFLICT:
                answer = self.ask("这与既有决定冲突。授权变更既有决定？（授权 / 不授权）：")
                if answer == "授权":
                    resolutions.append(
                        IssueResolution(
                            issue.id, ResolutionAction.AUTHORIZE_DECISION_CHANGE, "作者授权变更"
                        )
                    )
                    notes.append(f"授权变更决定：{issue.explanation}")
                else:
                    declined.append(issue)
            elif issue.kind is IssueKind.CANON_CONFLICT:
                answer = self.ask(
                    "这与已采用正文直接冲突，不能忽略。写下修正依据（回车表示放弃）："
                )
                if answer:
                    resolutions.append(
                        IssueResolution(issue.id, ResolutionAction.CORRECT_EXTRACTION, answer)
                    )
                    notes.append(f"正文冲突的修正依据：{answer}")
                else:
                    declined.append(issue)
            elif issue.kind is IssueKind.PLAN_DEVIATION:
                answer = self.ask("正文偏离了计划。接受实际剧情与结尾？（接受 / 放弃）：")
                accepted = (None, None)
                if answer == "接受":
                    accepted = self.accept_plot_and_ending(check, "请确认实际剧情与结尾。", notes)
                if accepted[0] is None:
                    declined.append(issue)
                else:
                    plot, ending = accepted
                    resolutions.append(
                        IssueResolution(issue.id, ResolutionAction.ACCEPT_DEVIATION, "作者接受偏差")
                    )
            else:
                self.say("  保留疑问，不需要现在处理。")
                resolutions.append(
                    IssueResolution(issue.id, ResolutionAction.KEEP_OPEN, "保留疑问")
                )
        return resolutions, plot, ending, declined

    def accept_plot_and_ending(
        self, check: CheckRecord, intro: str, notes: list[str]
    ) -> tuple[str | None, str | None]:
        self.say(intro)
        self.say(f"  检查所述实际剧情：{check.actual_plot}")
        self.say(f"  检查所述实际结尾：{check.actual_ending}")
        plot = self.ask("本章实际剧情（回车沿用检查所述）：") or check.actual_plot
        ending = self.ask("本章实际结尾（回车沿用检查所述）：") or check.actual_ending
        answer = self.ask("接受以上剧情与结尾作为本章采用结果？（接受 / 放弃）：")
        if answer != "接受":
            return None, None
        notes.append(f"接受实际剧情：{plot}")
        notes.append(f"接受实际结尾：{ending}")
        return plot, ending

    def report_already_adopted(self, chapter: ChapterRecord) -> None:
        if chapter.current_snapshot_id is not None:
            snapshot = database.get_snapshot(
                self.project.conn, self.project.novel_id, chapter.current_snapshot_id
            )
            if snapshot.status is SnapshotStatus.READY:
                self.say("当前稿已采用，无需重复操作。")
                return
        self.say("当前稿已采用，但上次故事状态更新没有完成，正在重试……")
        try:
            status = adoption.recompute_state(self.project, chapter.id)
        except WorkbenchError as exc:
            self.say(f"状态更新仍未完成：{exc.message} 正文仍是已采用版本，可稍后再试。")
            return
        if status is SnapshotStatus.READY:
            self.say("故事状态更新完成。")
        else:
            self.say("状态更新仍未完成，可稍后再试；正文仍是已采用版本。")

    def explain_adoption_failure(self, exc: WorkbenchError, check: CheckRecord) -> None:
        if exc.code is ErrorCode.REVIEW_REQUIRED:
            self.say(f"没有采用：{exc.message} 请先用 k 对这一稿重新检查，再采用。")
        elif exc.code is ErrorCode.STALE_BASIS:
            self.say(f"没有采用：相关依据已经变化。{exc.message} 请用 k 重新检查后再采用。")
        elif exc.code is ErrorCode.WORKCOPY_CONFLICT:
            self.say("没有采用：发现尚未展示的外部修改稿，已保存为新稿。")
            self.say("请先用 v 查看，再决定采用哪一稿。")
            self.shown = None
        elif exc.code is ErrorCode.AUTHORIZATION_REQUIRED:
            self.say("没有采用：还有未处理的问题：")
            issue_id = exc.details.get("issue_id")
            unresolved = [i for i in check.issues if issue_id is None or i.id == issue_id]
            self.list_issues(unresolved)
        else:
            self.say(f"没有采用：{exc.message}")

    # ------------------------------------------------------------ 拒绝

    def reject(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        target = self.target_draft(chapter)
        if target is None:
            self.say(f"{chapter_label(chapter)}还没有正文。")
            return True
        if target.id == self.adopted_version_id(chapter):
            self.say("这一稿已经采用，不能标记拒绝；要更换正式正文，请采用另一稿。")
            return True
        self.say(f"要拒绝的是：{draft_label(chapter, target)} · {first_line(target.body)}")
        reason = self.ask("拒绝的原因（回车留空）：")
        message_id = authoring.record_message(self.project, f"不要这版。{reason}", chapter.id)
        authoring.reject_draft(
            self.project, Target(self.project.novel_id, chapter.id, target.id), message_id, reason
        )
        self.shown = None
        fresh = self.refresh() or chapter
        self.say("这一稿已标记拒绝，不会进入正式故事记录；历史里仍然保留。")
        if fresh.current_draft_id is None:
            self.say("这一章现在没有编辑稿；可以用 w 重写或 e 编辑。")
        return True

    # ------------------------------------------------------------ 正文

    def show_body(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        draft = self.current_draft(chapter)
        if draft is None:
            self.say(f"{chapter_label(chapter)}还没有正文。用 e 开始编辑。")
            return True
        self.display_draft(chapter, draft)
        return True

    def edit_body(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        draft = self.current_draft(chapter)
        text = edit_text(draft.body if draft else "", f"{chapter_label(chapter)} 正文")
        if text is None:
            self.say("已取消编辑，正文没有改变。")
            return True
        self.save_with_retry(chapter, text)
        return True

    def save_with_retry(self, chapter: ChapterRecord, text: str) -> None:
        """保存失败时作者的文字仍在内存里，并另存恢复文件：可以重试、回到编辑器，或明确放弃。"""
        title = f"{chapter_label(chapter)} 正文"
        recovered: str | None = None
        while True:
            try:
                result = authoring.save_draft(
                    self.project,
                    chapter.id,
                    text,
                    Expected(current_draft_id=chapter.current_draft_id),
                )
            except WorkbenchError as exc:
                self.say(f"保存没有成功：{exc.message} 你刚写的文字还在，没有丢失。")
                if text != recovered:
                    recovered = self.write_recovery(chapter, text)
                choice = self.ask("r 重试保存 · e 回到编辑器继续修改 · q 放弃这次修改：")
                if choice in ("e", "编辑"):
                    edited = edit_text(text, title)
                    if edited is not None:
                        text = edited
                elif choice in ("q", "放弃"):
                    self.say("已放弃这次修改；之前保存过的正文没有改变。")
                    return
                continue
            break
        fresh = self.refresh() or chapter
        if result.became_current:
            self.shown = Shown(chapter.id, result.version_id)
            self.say(
                f"已保存，这是{chapter_label(fresh)}现在的编辑稿。文字已变化，采用前需要重新检查。"
            )
            return
        self.say("在你编辑期间，这一章的编辑稿已经变化。你的修改已保存为另一稿，两稿都在。")
        mine = self.version(chapter, result.version_id)
        other = self.current_draft(fresh)
        self.say("稿一 · 你刚保存的修改稿：")
        self.say(mine.body)
        self.say("稿二 · 现在的编辑稿：")
        self.say(other.body if other else "（空）")
        choice = self.ask("要以哪一稿继续？1 或 2（回车保留稿二）：")
        if choice == "1":
            restored = authoring.restore_version(
                self.project,
                Target(self.project.novel_id, chapter.id, mine.id),
                Expected(current_draft_id=fresh.current_draft_id),
            )
            if restored.became_current:
                self.shown = Shown(chapter.id, restored.version_id)
                self.say("已把你的修改稿作为现在的编辑稿；另一稿保留在历史里。")
            else:
                self.say("编辑稿又变化了，请用 r 查看历史版本后再选择。")
        else:
            self.shown = Shown(chapter.id, other.id) if other else None
            self.say("保留稿二为编辑稿；你的修改稿留在历史里，可以用 r 恢复。")

    def write_recovery(self, chapter: ChapterRecord, text: str) -> str | None:
        try:
            path = projects.write_recovery(self.project, chapter.id, text)
        except WorkbenchError as exc:
            self.say(f"恢复文件也没有写成：{exc.message}")
            return None
        self.say(f"已把这段文字另存为恢复文件：{self.project.root / path}")
        return text

    def restore(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        versions = authoring.list_versions(self.project, chapter.id)
        if not versions:
            self.say(f"{chapter_label(chapter)}还没有任何正文版本。")
            return True
        adopted = self.adopted_version_id(chapter)
        self.say(f"{chapter_label(chapter)}的历史版本：")
        for index, version in enumerate(versions, start=1):
            marks = [local_time(version.created_at), _ORIGIN_LABEL[version.origin]]
            if version.id == chapter.current_draft_id:
                marks.append("现在的编辑稿")
            if version.id == adopted:
                marks.append("已采用")
            self.say(f"  {index:>3}. {' · '.join(marks)}")
        answer = self.ask("恢复第几个版本（输入序号，回车取消）：")
        if not answer:
            return True
        if not answer.isdigit() or not 1 <= int(answer) <= len(versions):
            self.say("没有这个序号。")
            return True
        chosen = versions[int(answer) - 1]
        result = authoring.restore_version(
            self.project,
            Target(self.project.novel_id, chapter.id, chosen.id),
            Expected(current_draft_id=chapter.current_draft_id),
        )
        self.shown = None
        if result.became_current:
            self.say("已用这个版本创建恢复稿，并作为现在的编辑稿。历史版本都还在。")
        else:
            self.say("恢复稿已保存，但编辑稿在此期间变化了；请查看历史版本后再选择。")
        return True


def open_or_create(path: Path, session: PromptSession[str], say: Callable[[str], None]) -> Project:
    """目录里已有作品就打开；没有则询问标题并创建。"""
    try:
        project, notice = open_project(path)
    except WorkbenchError as exc:
        if exc.code is not ErrorCode.NOT_FOUND:
            raise
        say(f"这里还没有作品：{path}")
        while True:
            title = session.prompt("作品标题：").strip()
            if title:
                return create_project(path, title)
            say("标题不能为空。")
    for note in notice.notes:
        say(note)
    return project


def run(
    path: Path,
    say: Callable[[str], None] = print,
    *,
    clients: generation.Clients | None = None,
    config: generation.GenerationConfig | None = None,
    emit: Callable[[str], None] | None = None,
) -> int:
    """clients/config 由启动入口装配；为 None 时写作、检查与 AI 讨论提示模型尚未接入。"""
    session: PromptSession[str] = PromptSession()
    try:
        project = open_or_create(path, session, say)
    except (EOFError, KeyboardInterrupt):
        say("已退出。")
        return 1
    except WorkbenchError as exc:
        say(f"无法打开作品：{exc.message}")
        return 1
    try:
        Workbench(project, session, say, clients=clients, config=config, emit=emit).loop()
    finally:
        project.close()
    return 0
