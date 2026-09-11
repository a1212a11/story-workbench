"""P02：启动入口的最小行为断言。"""

from __future__ import annotations

import subprocess
import sys

from story_workbench.__main__ import main


def test_help_exits_zero_without_project(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "小说项目目录" in out


def test_project_argument_is_not_yet_served(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert str(tmp_path) in err


def test_module_is_runnable_via_python_m():
    result = subprocess.run(
        [sys.executable, "-m", "story_workbench", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "story_workbench" in result.stdout
