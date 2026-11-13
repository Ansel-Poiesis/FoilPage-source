# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Lifecycle manager for the independent queue Worker process."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Sequence


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
    ) -> None:
        self.command = list(command)
        self.log_callback = log_callback or (lambda _line: None)
        self.state_callback = state_callback or (lambda _state: None)
        self.auto_restart = auto_restart
        self._process: subprocess.Popen[str] | None = None
        self._stop_requested = False
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self.running and self._process else None

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop_requested = False
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
            )
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
        self.state_callback("stopped" if expected else "crashed")
        if should_restart:
            self.log_callback(f"Worker 异常退出 ({code})，正在重启")
            with self._lock:
                if not self._stop_requested:
                    self.start()


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
