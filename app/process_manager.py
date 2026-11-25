# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Lifecycle manager for the independent queue Worker process."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence


def _send_windows_stop_signal(process: subprocess.Popen[str]) -> None:
    """Deliver CTRL_BREAK from a windowed parent without showing a console.

    A frozen Desktop/pythonw parent has no console, unlike CLI test runners.
    Attach only to the worker's existing hidden console for signal delivery;
    never allocate a console or detach a console owned by our caller.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.FreeConsole.argtypes = []
    kernel.FreeConsole.restype = wintypes.BOOL
    attached = False
    processes = (wintypes.DWORD * 1)()
    if kernel.GetConsoleProcessList(processes, 1) == 0:
        if not kernel.AttachConsole(process.pid):
            raise ctypes.WinError(ctypes.get_last_error())
        attached = True
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    finally:
        if attached:
            kernel.FreeConsole()


class WorkerProcessManager:
    def __init__(
        self,
        command: Sequence[str],
        *,
        log_callback: Callable[[str], None] | None = None,
        state_callback: Callable[[str], None] | None = None,
        auto_restart: bool = True,
        environment_provider: Callable[[], Mapping[str, str] | None] | None = None,
    ) -> None:
        self.command = list(command)
        self.log_callback = log_callback or (lambda _line: None)
        self.state_callback = state_callback or (lambda _state: None)
        self.auto_restart = auto_restart
        self._environment_provider = environment_provider
        self._environment: dict[str, str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stop_requested = False
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None
        # 先生 2026-11-16：守护重启退避与上限。实机日志曾出现 1.18 秒内
        # "杀掉→重启→再死→再重启"的循环；无退避无上限的守护会把一次环境
        # 故障（DLL 初始化失败、磁盘满）放大成进程风暴。
        self._crash_restarts = 0
        self._started_at = 0.0

    _RESTART_BACKOFF = (2.0, 5.0, 15.0, 60.0)
    _MAX_CONSECUTIVE_RESTARTS = 5
    _STABLE_UPTIME_SECONDS = 300.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self.running and self._process else None

    def start(self) -> bool:
        """显式启动（应用启动或用户手动）：崩溃计数清零，重新计守护额度。"""
        with self._lock:
            if self.running:
                return False
            self._stop_requested = False
            self._crash_restarts = 0
            if self._environment_provider is not None:
                environment = self._environment_provider()
                self._environment = dict(environment) if environment is not None else None
            return self._spawn_locked()

    def _spawn_locked(self) -> bool:
        """在已持锁的前提下拉起进程与监控线程；不动退出意图与崩溃计数。"""
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            # CTRL_BREAK_EVENT requires the child to belong to a console
            # process group. CREATE_NO_WINDOW makes that signal undeliverable,
            # so hide the console via STARTUPINFO instead.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        self._process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            startupinfo=startupinfo,
            env=dict(self._environment) if self._environment is not None else None,
        )
        self._started_at = time.monotonic()
        self.state_callback("running")
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()
        return True

    def stop(self, timeout: float = 8.0) -> bool:
        with self._lock:
            process = self._process
            self._stop_requested = True
        if process is None or process.poll() is not None:
            self.state_callback("stopped")
            return False
        try:
            if os.name == "nt":
                _send_windows_stop_signal(process)
            else:
                process.send_signal(signal.SIGTERM)
            process.wait(timeout=timeout)
        except (OSError, SystemError, subprocess.TimeoutExpired):
            # Python's Windows subprocess wrapper can surface an invalid
            # process handle as SystemError while sending CTRL_BREAK_EVENT.
            # Treat it like any other failed graceful stop and keep the app
            # shutdown path moving.
            try:
                process.terminate()
            except (OSError, SystemError):
                pass
            try:
                process.wait(timeout=3)
            except (OSError, SystemError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except (OSError, SystemError):
                    pass
                try:
                    process.wait(timeout=3)
                except (OSError, SystemError, subprocess.TimeoutExpired):
                    pass
        self.state_callback("stopped")
        return True

    def _monitor_loop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdout is not None:
            for line in process.stdout:
                self.log_callback(line.rstrip())
        code = process.wait()
        with self._lock:
            expected = self._stop_requested
            if self._process is process:
                self._process = None
            # Keep the stop decision and restart decision under one lock. A
            # tray/window shutdown racing with process exit must not resurrect
            # a new worker after ``stop()`` has requested termination.
            should_restart = not expected and self.auto_restart and not self._stop_requested
            delay = 0.0
            if should_restart:
                uptime = time.monotonic() - self._started_at
                if uptime >= self._STABLE_UPTIME_SECONDS:
                    # 稳定运行过一段时间：这次崩溃视为新事件，重新计数。
                    self._crash_restarts = 0
                if self._crash_restarts >= self._MAX_CONSECUTIVE_RESTARTS:
                    should_restart = False
                else:
                    delay = self._RESTART_BACKOFF[
                        min(self._crash_restarts, len(self._RESTART_BACKOFF) - 1)
                    ]
                    self._crash_restarts += 1
        self.state_callback("stopped" if expected else "crashed")
        if not should_restart:
            cap_reached = (
                not expected and self.auto_restart
                and self._crash_restarts >= self._MAX_CONSECUTIVE_RESTARTS
            )
            if cap_reached:
                self.log_callback(
                    f"Worker 异常退出 ({code})，连续 {self._crash_restarts} 次重启无效，"
                    "停止自动重启；可在应用中手动启动 Worker"
                )
                self.state_callback("disabled")
            return
        self.log_callback(
            f"Worker 异常退出 ({code})，{delay:.0f} 秒后第 {self._crash_restarts} 次重启"
        )
        # 退避在锁外等：等待期间收到 stop() 则放弃重启。
        time.sleep(delay)
        with self._lock:
            if self._stop_requested or self.running:
                return
            self._spawn_locked()


def worker_command(
    project_root: Path,
    db_path: str,
    *,
    model_dir: str = "",
    settings_dir: str = "",
    poll: float = 2.0,
    heartbeat: float = 5.0,
    lease_timeout: float = 300.0,
    concurrency: int = 1,
) -> list[str]:
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        candidates = (
            app_dir / "worker" / "FoilPage-Worker.exe",
            app_dir / "worker" / "VideoToArticle-Worker.exe",
            app_dir.parent / "FoilPage-Worker" / "FoilPage-Worker.exe",
            app_dir.parent / "VideoToArticle-Worker" / "VideoToArticle-Worker.exe",
            app_dir / "FoilPage-Worker.exe",
            app_dir / "VideoToArticle-Worker.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                executable = str(candidate)
                break
        else:
            raise FileNotFoundError("安装目录中缺少 FoilPage-Worker.exe")
    else:
        executable = sys.executable

    command = [executable]
    if not getattr(sys, "frozen", False):
        command.append(str(project_root / "app" / "worker.py"))
    command.extend([
        "--db", db_path,
        "--poll", str(poll),
        "--heartbeat", str(heartbeat),
        "--lease-timeout", str(lease_timeout),
        "--concurrency", str(max(1, int(concurrency))),
    ])
    if model_dir:
        command.extend(["--model-dir", model_dir])
    if settings_dir:
        command.extend(["--settings-dir", settings_dir])
    return command


class SingleInstance:
    def __init__(self, name: str = "Local\\FoilPageDesktop") -> None:
        self._handle = None
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            self._handle = kernel32.CreateMutexW(None, False, name)
            if not self._handle or kernel32.GetLastError() == 183:
                if self._handle:
                    kernel32.CloseHandle(self._handle)
                    self._handle = None
                raise RuntimeError("应用已经在运行")

    def close(self) -> None:
        if self._handle:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
