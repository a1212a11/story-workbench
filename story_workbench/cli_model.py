"""模型等待期间接收取消；asyncio 和 SQLite 均在 CLI 所在线程运行。"""

from __future__ import annotations

import asyncio
import signal
from contextlib import ExitStack
from dataclasses import replace

from prompt_toolkit.input.typeahead import get_typeahead, store_typeahead
from prompt_toolkit.keys import Keys

from story_workbench.application import generation
from story_workbench.domain.records import ErrorCode, WorkbenchError
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import CancelSignal


def run_model(project, session, say, clients, config, operation, *args, **kwargs):
    """同步用例在网络边界让出事件循环；旧请求和资源关闭后才返回命令提示。"""
    cancel = CancelSignal()
    if clients.async_stream is None or clients.async_complete is None:
        return operation(project, *args, clients=clients, config=config, cancel=cancel, **kwargs)

    say("模型任务进行中，按 Ctrl-C 取消；已保存的内容会保留。")
    with asyncio.Runner() as runner, ExitStack() as cleanup:
        interrupted = False
        active_reader = None

        def interrupt(signum, frame):
            # Signal handlers defer database work until the event loop resumes.
            nonlocal interrupted
            interrupted = True
            if active_reader is not None:
                runner.get_loop().call_soon(active_reader)

        previous_handler = signal.signal(signal.SIGINT, interrupt)
        cleanup.callback(signal.signal, signal.SIGINT, previous_handler)

        async def waiting(awaitable, *, closing=False):
            nonlocal active_reader
            task = asyncio.ensure_future(awaitable)

            def read_keys():
                nonlocal interrupted
                keys = get_typeahead(session.input) + session.input.read_keys()
                store_typeahead(session.input, [key for key in keys if key.key != Keys.ControlC])
                requested = (
                    interrupted
                    or session.input.closed
                    or any(key.key == Keys.ControlC for key in keys)
                )
                interrupted = False
                if requested:
                    run_id = database.any_run_in_flight(project.conn, project.novel_id)
                    if run_id is not None and generation.cancel_run(project, run_id, cancel):
                        say("取消已记录，正在结束模型请求……")
                        if not closing and not task.done():
                            task.cancel()

            with session.input.raw_mode(), session.input.attach(read_keys):
                active_reader = read_keys
                read_keys()
                try:
                    result = await task
                except asyncio.CancelledError:
                    raise WorkbenchError(ErrorCode.MODEL_FAILED, "本次模型任务已取消。") from None
                finally:
                    active_reader = None
                if cancel.is_set():
                    raise WorkbenchError(ErrorCode.MODEL_FAILED, "本次模型任务已取消。")
                return result

        def stream(messages, options, signal):
            events = clients.async_stream(messages, options, signal)
            try:
                while True:
                    try:
                        yield runner.run(waiting(anext(events)))
                    except StopAsyncIteration:
                        break
            finally:
                runner.run(waiting(events.aclose(), closing=True))

        def complete(messages, options, signal):
            return runner.run(waiting(clients.async_complete(messages, options, signal)))

        adapted = replace(clients, stream=stream, complete=complete)
        return operation(project, *args, clients=adapted, config=config, cancel=cancel, **kwargs)
