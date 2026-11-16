# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Hidden Windows worker lifecycle from CLI and windowed Desktop parents."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest import mock

import pytest

from app.process_manager import _send_windows_stop_signal

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console lifecycle")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("has_console", [False, True])
def test_stop_signal_only_detaches_a_console_it_attached(monkeypatch, has_console):
    kernel = mock.Mock()
    kernel.GetConsoleProcessList.return_value = 1 if has_console else 0
    kernel.AttachConsole.return_value = True
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    process = mock.Mock(pid=123)
    process.send_signal.side_effect = SystemError("signal failed")
    with pytest.raises(SystemError, match="signal failed"):
        _send_windows_stop_signal(process)
    process.send_signal.assert_called_once_with(signal.CTRL_BREAK_EVENT)
    if has_console:
        kernel.AttachConsole.assert_not_called()
        kernel.FreeConsole.assert_not_called()
    else:
        kernel.AttachConsole.assert_called_once_with(123)
        kernel.FreeConsole.assert_called_once_with()


def test_stop_signal_attach_failure_is_not_swallowed(monkeypatch):
    kernel = mock.Mock()
    kernel.GetConsoleProcessList.return_value = 0
    kernel.AttachConsole.return_value = False
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    process = mock.Mock(pid=123)
    with pytest.raises(OSError) as error:
        _send_windows_stop_signal(process)
    assert error.value.winerror == 5
    process.send_signal.assert_not_called()
    kernel.FreeConsole.assert_not_called()


def test_windowed_parent_stops_real_hidden_worker_gracefully(tmp_path):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("This Windows Python distribution has no pythonw executable")
    report = tmp_path / "report.json"
    script = tmp_path / "windowed_parent.py"
    script.write_text(f'''
import ctypes, json, sys, threading, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from app.process_manager import WorkerProcessManager, worker_command
lines=[]
ready=threading.Event()
def receive(line):
    lines.append(line)
    if "[worker] 启动" in line: ready.set()
command=worker_command(Path({str(ROOT)!r}), {str(tmp_path / "queue.db")!r}, poll=.05)
command[0]={sys.executable!r}
manager=WorkerProcessManager(command, log_callback=receive, auto_restart=False)
result={{"parent_console_before":ctypes.windll.kernel32.GetConsoleWindow()}}
manager.start()
process=manager._process
original_signal=process.send_signal
def observed_signal(value):
    console=ctypes.windll.kernel32.GetConsoleWindow()
    result["attached_console"]=console
    result["console_visible"]=bool(ctypes.windll.user32.IsWindowVisible(console))
    return original_signal(value)
process.send_signal=observed_signal
try:
    result["ready"]=ready.wait(5)
finally:
    manager.stop(timeout=3)
time.sleep(.15)
result.update(exit_code=process.returncode, running=manager.running, logs=lines,
              parent_console_after=ctypes.windll.kernel32.GetConsoleWindow())
Path({str(report)!r}).write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
''', encoding="utf-8")
    subprocess.run([str(pythonw), str(script)], check=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ready"] and not result["running"], result
    assert result["exit_code"] == 0, result
    assert result["parent_console_before"] == result["parent_console_after"] == 0, result
    assert result["attached_console"] and not result["console_visible"], result
    assert any("[worker] 已停止" in line for line in result["logs"]), result


def test_crash_restarts_back_off_then_give_up():
    """先生 2026-11-16：守护重启必须退避且有上限，不能无限进程风暴。"""
    import threading
    import time
    from app.process_manager import WorkerProcessManager

    states: list[str] = []
    logs: list[str] = []
    manager = WorkerProcessManager(
        [sys.executable, "-c", "pass"],  # 启动即退的假 Worker。
        state_callback=states.append,
        log_callback=logs.append,
        auto_restart=True,
    )
    manager._RESTART_BACKOFF = (0.05, 0.05)
    manager._MAX_CONSECUTIVE_RESTARTS = 3
    manager._STABLE_UPTIME_SECONDS = 3600.0
    try:
        manager.start()
        deadline = time.time() + 15
        while time.time() < deadline and "disabled" not in states:
            time.sleep(0.05)
        assert "disabled" in states, states
        # 初次崩溃 + 3 次重启各崩一次 = 4 次 crashed；之后放弃。
        assert states.count("crashed") == 4, states
        assert states.count("running") == 4, states
        assert any("停止自动重启" in line for line in logs), logs
        assert not manager.running
    finally:
        manager.stop(timeout=1)


def test_stop_during_backoff_window_aborts_restart():
    """退避等待期间收到退出意图：不得再复活进程。"""
    import time
    from app.process_manager import WorkerProcessManager

    states: list[str] = []
    manager = WorkerProcessManager(
        [sys.executable, "-c", "pass"],
        state_callback=states.append,
        log_callback=lambda _line: None,
        auto_restart=True,
    )
    manager._RESTART_BACKOFF = (1.0,)
    manager._MAX_CONSECUTIVE_RESTARTS = 5
    manager._STABLE_UPTIME_SECONDS = 3600.0
    try:
        manager.start()
        deadline = time.time() + 10
        while time.time() < deadline and "crashed" not in states:
            time.sleep(0.02)
        manager.stop(timeout=1)  # 退避 1s 内下达退出意图。
        time.sleep(1.5)
        assert states.count("running") == 1, states
        assert not manager.running
    finally:
        manager.stop(timeout=1)
