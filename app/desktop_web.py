# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""FoilPage WebView2 桌面入口、原生窗口与后台生命周期。"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import http.client
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview

log = logging.getLogger("shengjian.desktop")

from app.bridge import APP_VERSION, Bridge
from app import ci_runner
from app.controller import AppController
from app.local_api import LocalApiServer
from app.client_config import CLIENT_CONFIG_COMPATIBILITY_VERSION
from app.process_manager import SingleInstance, WorkerProcessManager, worker_command
from app.tray import TrayController
from app.task_notifications import TaskNotifications
from app.native_notifications import APP_NAME, register_identity, show_toast
from app.window_chrome import NativeDragController, install_resize_frame
from app.windows_icons import NativeWindowIcons
from app.webview_api import expose_bridge
from engine.settings import SettingsStore, default_logs_dir

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_COLOR_NONE = 0xFFFFFFFE
DWMWCP_ROUND = 2

_dwmapi = ctypes.windll.dwmapi
_dwmapi.DwmSetWindowAttribute.argtypes = [
    ctypes.wintypes.HWND, ctypes.wintypes.DWORD,
    ctypes.c_void_p, ctypes.wintypes.DWORD,
]
_dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


def _set_dwm_window_colors(hwnd: int, *, dark: bool) -> None:
    """Align the native Windows caption with the current application theme."""
    if not hwnd:
        return
    # COLORREF stores its channels as 0x00BBGGRR.
    rgb = (40, 47, 43) if dark else (231, 236, 229)
    colorref = ctypes.c_uint(rgb[0] | (rgb[1] << 8) | (rgb[2] << 16))
    border_none = ctypes.c_uint(DWMWA_COLOR_NONE)
    text = ctypes.c_uint((238 | (243 << 8) | (237 << 16)) if dark else (29 | (33 << 8) | (29 << 16)))
    immersive_dark = ctypes.c_int(1 if dark else 0)
    # Individual calls deliberately fail independently. Windows 10 lacks the
    # newer border/corner attributes, but it still benefits from dark mode.
    for attribute, value in (
        (DWMWA_USE_IMMERSIVE_DARK_MODE, immersive_dark),
        (DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.c_int(DWMWCP_ROUND)),
        (DWMWA_BORDER_COLOR, border_none),
        (DWMWA_CAPTION_COLOR, colorref),
        (DWMWA_TEXT_COLOR, text),
    ):
        try:
            _dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value),
            )
        except OSError:
            # Older Windows builds can reject individual newer attributes.
            # Keep applying the rest so one unsupported capability cannot
            # leave a focused white edge behind.
            continue


def _system_theme_is_dark() -> bool:
    """跟随系统：读取 Windows 应用深浅色偏好，失败时默认深色。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        finally:
            winreg.CloseKey(key)
    except Exception:
        return True


def _resource_path(*parts: str) -> Path:
    """Resolve bundled assets inside a PyInstaller one-folder build."""
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base.joinpath(*parts)


WEBVIEW_UI_DIR = _resource_path("app", "webview_ui")

PUSH_INTERVAL_SECONDS = 2.0


def _activate_running_instance(data_dir: str | None, *, show: bool) -> bool:
    if not show:
        return True
    store = SettingsStore(base_dir=data_dir) if data_dir else SettingsStore()
    settings = store.load()
    token = settings.get("local_api_token", "")
    if not token:
        return False
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(settings.get("local_api_port", 18765)), timeout=2,
    )
    try:
        connection.request(
            "POST", "/v1/show", body=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8") or "{}")
        return response.status == 200 and data.get("shown") is True
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


class WebDesktopApp:
    """WebView2 版桌面应用：组装引擎组件并托管桥接窗口。"""

    def __init__(
        self,
        *,
        data_dir: str | None = None,
        demo: bool = False,
        start_worker: bool = True,
        window_title: str = APP_NAME,
        initially_hidden: bool = False,
        ui_dir: str | None = None,
        create_window: bool = True,
    ) -> None:
        log.info("WebDesktopApp.__init__ start")
        self.settings_store = SettingsStore(base_dir=data_dir) if data_dir else SettingsStore()
        self.controller = AppController(self.settings_store)
        settings = self.controller.settings
        self.demo = demo
        self._closed = False
        self._close_lock = threading.RLock()
        self._window = None
        self._launch_context = {"mode": "background" if initially_hidden else "manual", "sequence": 0}
        self._resize_callback = None
        self._native_drag = None
        self._native_icons = None
        self._notification_identity = register_identity(_resource_path("assets", "icons", "foilpage.png")) if not demo else False
        self.tray: TrayController | None = None
        self.local_api: LocalApiServer | None = None
        self._push_timer: threading.Timer | None = None
        self._notification_stop = threading.Event()
        self._notification_events: list[dict] = []
        _init_theme = settings.get("theme")
        self._native_theme = "light" if _init_theme == "paper" else "dark"

        command = worker_command(
            ROOT,
            settings["database_path"],
            model_dir=settings.get("model_dir", ""),
            settings_dir=str(self.settings_store.base_dir),
            poll=settings["poll_seconds"],
            heartbeat=settings["heartbeat_seconds"],
            lease_timeout=settings["lease_timeout_seconds"],
            concurrency=self._effective_concurrency(),
        )
        self.worker = WorkerProcessManager(
            command,
            auto_restart=settings.get("worker_auto_restart", True),
            log_callback=self._worker_log_line,
        )
        self.bridge = Bridge(
            self.controller,
            self.worker,
            self.settings_store,
            window_provider=lambda: self._window,
            on_quit=self._quit,
            local_api_provider=lambda: self.local_api,
            native_theme_applier=self.set_native_theme,
            notifier=self._notify_bubble,
            launch_context_provider=lambda: self._launch_context,
            native_drag_provider=lambda: bool(self._native_drag and self._native_drag.enabled),
        )
        self._notifications = TaskNotifications(self._notify_bubble, beep=self._notify_beep)
        self._notifications.update(self.bridge.get_notification_tasks())

        # Dedicated caption surface; controls are outside the drag region.
        webview.settings["DRAG_REGION_SELECTOR"] = ".window-drag-fallback"
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = False

        if create_window:
            index_path = Path(ui_dir) / "index.html" if ui_dir else WEBVIEW_UI_DIR / "index.html"
            if not index_path.is_file():
                raise FileNotFoundError(f"前端入口缺失: {index_path}")
            self._window = webview.create_window(
                window_title,
                url=str(index_path),
                js_api=None,
                width=1280,
                height=800,
                min_size=(560, 500),
                background_color=("#e7ece5" if self._native_theme == "light" else "#282f2b"),
                hidden=initially_hidden,
                frameless=True,
                resizable=True,
                easy_drag=False,
            )
            expose_bridge(self._window, self.bridge)
            self._window.events.closing += self._on_closing
            self._window.events.closed += self._on_closed
            self._window.events.before_show += self._prepare_native_drag
            self._window.events.loaded += lambda: self.bridge._bind_media_drop(self._window)
            self._window.events.closed += self._release_native_icons

        if demo:
            self._seed_demo()
        if not demo and settings.get("local_api_enabled", True):
            try:
                self.local_api = LocalApiServer(
                    self.controller,
                    show_callback=self._show_from_api,
                    quit_callback=self._quit,
                    desktop_login_callback=self.bridge._web_login_proof,
                    agent_operations=self.bridge.agent_operations_facade(),
                    app_version=CLIENT_CONFIG_COMPATIBILITY_VERSION,
                )
                self.local_api.start()
            except OSError:
                log.exception("本地连接服务启动失败，扩展任务将等待应用恢复")
                self.local_api = None
        if start_worker and settings.get("worker_auto_start", True) and not demo:
            log.info("starting worker")
            self.worker.start()
            log.info("worker started")
        if not demo:
            tray = TrayController(
                icon_path=_resource_path("assets", "icons", "foilpage.ico"),
                show=self._show_from_api,
                quit_app=self._request_quit,
                version=APP_VERSION,
            )
            if tray.start():
                self.tray = tray
            log.info("tray started=%s", self.tray is not None)

    # ------------------------------------------------------------ lifecycle
    def _worker_log_line(self, line: str) -> None:
        """Worker 子进程 stdout 落盘（logs/worker.log 随 desktop.log 轮转）。"""
        logging.getLogger("shengjian.worker").info("%s", line)

    def start(self, *, debug: bool = False) -> None:
        log.info("app.start, launching webview")
        # push_state() 依赖已绑定的 GUI：在 webview.start() 进入 GUI 循环前
        # 直接调用 evaluate_js 会阻塞（pywebview 等待 GUI 线程），因此延迟到
        # 循环启动后在后台线程再开启 2s 状态推送。
        threading.Thread(target=self._launch_push_timer_later, daemon=True).start()
        threading.Thread(target=self._notification_loop, daemon=True).start()
        threading.Thread(target=self._ci_runner_wakeup_once, daemon=True).start()
        log.info("webview.start entering")
        webview.start(self._fix_native_window, debug=debug)
        log.info("webview.start returned")

    def _ci_runner_wakeup_once(self) -> None:
        """启动时唤起本机 CI Runner 服务（设置 ci_runner_wakeup 控制；任何异常不影响启动）。"""
        try:
            if self.demo or not self.controller.settings.get("ci_runner_wakeup", False):
                return
            result = ci_runner.wake_runner_service()
            if result in ("started", "start-pending", "start-failed", "probe-failed"):
                log.info("ci runner service wakeup: %s", result)
        except Exception:
            log.exception("ci runner service wakeup failed")

    # -------------------------------------------------- native titlebar
    def _release_native_icons(self) -> None:
        owner = getattr(self, "_native_icons", None)
        if owner is not None:
            owner.close()

    def _prepare_native_drag(self) -> None:
        """before_show runs on the GUI thread before WebView2 navigation."""
        if os.name != "nt" or self._window is None or self._native_drag is not None:
            return
        try:
            self._native_drag = NativeDragController(self._window.native.webview)
        except Exception:
            log.warning("Could not initialize native window dragging", exc_info=True)

    def _native_hwnd(self) -> int | None:
        """当前窗口的 Win32 HWND。"""
        window = self._window
        if window is None:
            return None
        try:
            native = getattr(window, "native", None)
            if native is None:
                return None
            handle = getattr(native, "Handle", None)
            if handle is not None:
                return int(handle.ToInt64())
        except Exception:
            pass
        return None

    def _fix_native_window(self) -> None:
        """Install native sizing behind the custom caption on the GUI thread."""
        last_error = None
        for _ in range(15):
            try:
                hwnd = self._native_hwnd()
                if hwnd:
                    _set_dwm_window_colors(hwnd, dark=self._native_theme == "dark")
                    import System.Windows.Forms as WinForms
                    from System.Drawing import Icon
                    native = self._window.native

                    def apply_icon() -> None:
                        icon_path = _resource_path("assets", "icons", "foilpage.ico")
                        native.Icon = Icon(str(icon_path))
                        if self._native_icons is None:
                            self._native_icons = NativeWindowIcons(hwnd, icon_path)
                        native.Text = APP_NAME
                        if self._resize_callback is None:
                            self._resize_callback = install_resize_frame(hwnd)

                    native.Invoke(WinForms.MethodInvoker(apply_icon))
                    return
            except Exception as exc:
                last_error = (type(exc), exc, exc.__traceback__)
            time.sleep(0.2)
        log.warning("Native window icon/frame initialization did not complete after startup", exc_info=last_error)


    def set_native_theme(self, theme: str) -> None:
        """设置原生标题栏主题色（dark/paper），未安装时先记录待用。"""
        raw = str(theme or "").lower()
        resolved = "light" if raw in {"paper", "light"} else "dark"
        self._native_theme = resolved
        hwnd = self._native_hwnd()
        if hwnd:
            _set_dwm_window_colors(hwnd, dark=resolved == "dark")

    def _launch_push_timer_later(self) -> None:
        import time

        time.sleep(1.0)
        self._start_push_timer()

    def _start_push_timer(self) -> None:
        def loop() -> None:
            if self._closed:
                return
            try:
                events, self._notification_events = self._notification_events, []
                self.bridge.push_state(events)
            finally:
                if not self._closed:
                    self._push_timer = threading.Timer(PUSH_INTERVAL_SECONDS, loop)
                    self._push_timer.daemon = True
                    self._push_timer.start()

        loop()

    def _plan_expiry_notice_once(self) -> None:
        """套餐到期预警（先生 2026-10-14）：距到期不足 3 天或已过期时，每天
        至多一条系统通知；去重键写回设置。本地转写不受影响，仅云端权益。"""
        try:
            from datetime import date
            if not self.controller.settings.get("task_notifications", True):
                return
            notice = self.bridge.get_subscription_status().get("plan_expiry_notice")
            if not notice:
                return
            today = date.today().isoformat()
            import hashlib
            scope = [str(self.controller.settings.get("subscription_server_url") or "").rstrip("/"),
                str(self.controller.settings.get("account_user_id") or "local"), str(notice["date"]), str(notice["expired"])]
            notice_key = hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode("utf-8")).hexdigest()
            stored = self.controller.settings.get("plan_expiry_last_notice_day")
            sent = stored.get("keys", []) if isinstance(stored, dict) and stored.get("day") == today else []
            sent = [key for key in sent if isinstance(key, str)] if isinstance(sent, list) else []
            if notice_key in sent:
                return
            last_key, last_attempt = getattr(self, "_plan_expiry_notice_attempt", ("", 0.0))
            if last_key == notice_key and time.monotonic() - last_attempt < 60:
                return
            self._plan_expiry_notice_attempt = (notice_key, time.monotonic())
            local_note = "本地功能登录即可继续使用"
            countdown = "24 小时内" if notice["days"] == 0 else f"{notice['days']} 天后"
            message = (f"套餐已到期（{notice['date']}）：云端整理暂停；{local_note}，可在订阅页续费"
                       if notice["expired"] else
                       f"套餐将于 {countdown}（{notice['date']}）到期；到期后云端整理暂停；{local_note}。可在订阅页续费")
            if self._notify_bubble("FoilPage 套餐即将到期" if not notice["expired"] else "FoilPage 套餐已到期", message):
                self.controller.save_settings({"plan_expiry_last_notice_day": {"day": today, "keys": [*sent, notice_key][-64:]}})
        except Exception:
            log.exception("套餐到期提醒失败")

    def _notification_loop(self) -> None:
        while not self._notification_stop.wait(1.0):
            try:
                # Serialize database reads with shutdown; the last poll must
                # finish before controller.close releases its connections.
                with self._close_lock:
                    if self._closed:
                        return
                    events = self._notifications.update(
                        self.bridge.get_notification_tasks(),
                        enabled=self.controller.settings.get("task_notifications", True),
                    )
                    self.bridge.sync_subscription_in_background()
                    self.bridge.sync_product_notifications_in_background()
                    with self.controller.media_action_lock:
                        self.controller.failed_task_retention.run_if_due(self.controller.settings)
                    self._plan_expiry_notice_once()
                self._notification_events.extend(events)
                self._notification_events[:] = self._notification_events[-10:]
            except Exception:
                log.exception("任务通知检查失败")

    def _on_closing(self) -> bool | None:
        """驻留托盘时，关闭窗口仅隐藏；否则允许关闭并清理。"""
        if (
            not self._closed
            and self.controller.settings.get("close_to_tray", True)
            and self.tray is not None
            and self.tray.available
        ):
            try:
                self._window.hide()
                self._notify_bubble("FoilPage 仍在运行", "任务会在后台继续。右键托盘图标可退出 FoilPage。")
            except Exception:
                return None
            return False
        return None

    def _on_closed(self) -> None:
        self._close()

    def _show_from_api(self, page: str = "") -> None:
        window = self._window
        if window is None:
            return
        if not page:
            page = "workbench" if self.controller.settings.get("setup_completed") is True else "setup"
        if page in {"setup", "workbench"}:
            self._launch_context = {"mode": "manual", "sequence": self._launch_context["sequence"] + 1}
        try:
            window.show()
            window.restore()
        except Exception:
            pass
        # 插件唤起时可直达指定页（如浏览器连接页）；页面键白名单防注入。
        if page in {"setup", "subscription", "workbench", "videos", "library", "browser", "links", "import", "settings"}:
            def _navigate() -> None:
                js = (f"if (window.__appReady && typeof switchView === 'function') {{ switchView('{page}'); }} "
                      f"else {{ window.__pendingNativeView = '{page}'; }} true;")
                for _ in range(6):
                    try:
                        window.evaluate_js(js)
                        return
                    except Exception:
                        pass
                    time.sleep(0.25)
            threading.Thread(target=_navigate, name="shengjian-show-page", daemon=True).start()

    def _effective_concurrency(self) -> int:
        """云端套餐并发独立；已验签的本地试用与解锁使用三任务并发。"""
        try:
            from engine.subscription import FREE_CONCURRENCY, status as subscription_status
            from engine.device import device_id

            st = subscription_status(self.settings_store.load(), device_id=device_id(self.settings_store.base_dir))
            if st.get("active"):
                return int(st.get("max_concurrency") or FREE_CONCURRENCY)
        except Exception:
            pass
        # TASK-8.29：解锁门槛下线——登录用户沿用原解锁档的 3 并发。
        return 3

    def _notify_bubble(self, title: str, message: str) -> bool:
        """Notification-area bubble for task completion / failure.

        True means Windows accepted the request through the live tray icon.
        Unavailable delivery is logged and exposed to the in-app fallback.
        """
        try:
            if getattr(self, "_notification_identity", False) and show_toast(title, message):
                return True
            tray = self.tray
            if tray is not None and tray.notify(title, message):
                return True
        except Exception:
            pass
        try:
            log.warning("系统通知不可用: %s", title)
            return False
        except Exception:
            return False

    def _notify_beep(self, kind: str) -> None:
        """终端态提示音：按设置的预设组/自定义 WAV 播放（非 Windows 静默跳过）。"""
        try:
            from app.notification_sounds import play_notification_sound

            play_notification_sound(kind, self.controller.settings)
        except Exception:
            pass

    def _quit(self) -> None:
        """清理并销毁窗口；可由托盘、桥接或窗口事件重复调用。"""
        try:
            self._close()
        except Exception:
            log.exception("退出清理失败，继续关闭窗口")
        finally:
            # pywebview marshals destroy_window through the WinForms GUI
            # thread. It must still run when stopping an already-dead Worker
            # raises from the tray callback thread.
            if self._window is not None:
                try:
                    self._window.destroy()
                except Exception:
                    pass

    def _request_quit(self) -> None:
        """托盘退出入口。

        pystray 回调来自独立消息线程；退出动作必须经过同一个幂等入口，
        不能只隐藏窗口或只停止托盘，否则主循环/Worker/本地 API 可能残留。
        pywebview 的 destroy_window 会在 WinForms UI 线程执行实际销毁。
        """
        # Never stop/join the pystray loop from its own menu callback thread.
        threading.Thread(target=self._quit, name="shengjian-quit", daemon=True).start()

    def _close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            stop = getattr(self, "_notification_stop", None)
            if stop is not None:
                stop.set()
            if self._push_timer is not None:
                self._push_timer.cancel()
                self._push_timer = None
            tray = self.tray
            self.tray = None
            local_api = self.local_api
            self.local_api = None
            # Each component must be stopped even if an earlier stop fails.
            cleanups = [lambda: self.worker.stop(timeout=2)]
            if local_api is not None:
                cleanups.append(lambda: local_api.stop(timeout=2))
            bridge = getattr(self, "bridge", None)
            if bridge is not None:
                cleanups.append(bridge.close_media_preview)
            cleanups.append(self.controller.close)
            if tray is not None:
                cleanups.append(tray.stop)
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    log.exception("退出组件清理失败")

    # ---------------------------------------------------------------- demo
    def _seed_demo(self) -> None:
        demo_dir = Path(self.controller.settings_store.base_dir) / "demo-media"
        demo_dir.mkdir(parents=True, exist_ok=True)
        samples = (
            ("产品访谈.mp4", b"demo-video-1"),
            ("课程录音.wav", b"RIFF-demo-audio-2"),
            ("会议片段.m4a", b"demo-audio-3"),
        )
        for name, data in samples:
            path = demo_dir / name
            if not path.exists():
                path.write_bytes(data)
        self.controller.import_files([str(demo_dir / name) for name, _data in samples])
        rows = self.controller.media.list_media()
        if rows and not self.controller.queue.list_jobs():
            self.controller.enqueue_media([rows[0]["id"]])




def _runtime_log_dir() -> Path:
    """运行数据目录下的 logs 子目录（F06：日志统一落盘并轮转）。"""
    log_dir = default_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _setup_logging() -> None:
    log_dir = _runtime_log_dir()
    level = logging.DEBUG if os.environ.get("SHENGJIAN_DEBUG_LOG") else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "desktop.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    logging.getLogger("shengjian.worker").propagate = True


def _verify_packaged_preview(app) -> None:
    """Opt-in release check, exclusively inside the supplied isolated data dir."""
    import subprocess
    import uuid
    from urllib.request import Request, urlopen
    from engine.audio import get_ffmpeg
    base = Path(app.settings_store.base_dir)
    source = base / f"smoke-preview-{uuid.uuid4().hex}.mp4"
    subprocess.run([get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=12", "-t", "1", "-pix_fmt", "yuv420p", str(source)],
                   check=True, timeout=15, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    item = app.controller.media.import_file(str(source)).item
    preview = app.bridge.get_video_preview(item["id"])
    if preview.get("ok") is not True:
        raise RuntimeError("随包视频预览初始化失败")
    with urlopen(Request(preview["preview_url"], headers={"Range": "bytes=0-31"}), timeout=5) as response:
        if response.status != 206 or len(response.read()) != 32:
            raise RuntimeError("随包视频流与进度定位失败")
    with urlopen(preview["thumbnail_url"], timeout=15) as response:
        cover = response.read()
        if response.status != 200 or not cover.startswith(b"\xff\xd8"):
            raise RuntimeError("随包封面解码失败")
    (base / "media-preview-smoke.json").write_text(json.dumps({"ok": True, "version": APP_VERSION,
        "byte_range": True, "real_cover_jpeg": True, "cover_bytes": len(cover)}, ensure_ascii=False), encoding="utf-8")
    _verify_packaged_browser_import(app, source)


def _verify_packaged_browser_import(app, source: Path) -> None:
    """Exercise the frozen receipt validator during isolated release smoke."""
    from app.download_coordinator import DownloadCoordinator
    from engine.downloader import DownloadAdapterError

    base = Path(app.settings_store.base_dir)
    coordinator = DownloadCoordinator(app.controller)
    def pending_job(label):
        job = coordinator.store.create("https://www.douyin.com/video/1234567890123456", label, "douyin", {
            "mode": "browser", "browser_fallback": True, "transcribe_after_download": False,
            "managed_download_root": str(base / "browser-managed"),
        })
        for status in ("resolving", "awaiting_browser"):
            coordinator.store.transition(job["id"], status)
        return job
    try:
        job = pending_job("packaged-browser-receipt")
        imported = coordinator.submit_local_file(str(source), download_id=job["id"],
            transcribe=False, validate_media=True)
        if (coordinator.store.get(job["id"])["status"] != "done" or imported["job_id"] is not None
                or not Path(imported["path"]).is_file()):
            raise RuntimeError("随包浏览器有效媒体接收失败")
        bad = base / "browser-denied.mp4"
        bad.write_text("<html>access denied</html>", encoding="utf-8")
        rejected = pending_job("packaged-invalid-receipt")
        media_before = len(app.controller.media.list_media())
        try:
            coordinator.submit_local_file(str(bad), download_id=rejected["id"],
                transcribe=False, validate_media=True)
        except DownloadAdapterError as exc:
            if exc.code != "media_not_video":
                raise RuntimeError("随包媒体校验组件不可用") from exc
        else:
            raise RuntimeError("随包浏览器错误正文被误当成视频")
        if len(app.controller.media.list_media()) != media_before or app.controller.queue.list_jobs():
            raise RuntimeError("随包浏览器校验创建了额外资料或处理任务")
        (base / "browser-import-smoke.json").write_text(json.dumps({
            "ok": True, "version": APP_VERSION, "frozen_receiver": bool(getattr(sys, "frozen", False)),
            "valid_video_imported": True, "same_download_task_done": True,
            "invalid_html_rejected": True, "processing_jobs": 0,
        }), encoding="utf-8")
    finally:
        coordinator.stop()


def _main(argv: list[str] | None = None) -> int:
    # 一次性品牌改名迁移（1.3.8→1.3.9）：必须先于 _setup_logging——日志句柄会
    # 打开旧数据根下的文件，随后的目录重命名必然自锁（WinError 5）。仅打包版
    # 且未显式指定数据/演示/冒烟目录时执行；源码运行与测试不触碰真实资料。
    pre_argv = [str(item) for item in (argv if argv is not None else sys.argv[1:])]
    if (getattr(sys, "frozen", False)
            and not any(item.startswith(("--data-dir", "--smoke", "--demo")) for item in pre_argv)):
        from engine.settings import migrate_legacy_data_root

        migrate_legacy_data_root()
    _setup_logging()
    log.info("desktop_web starting, args=%s", argv)
    parser = argparse.ArgumentParser(description="FoilPage 桌面工作台（WebView2）")
    parser.add_argument("--data-dir", default=None, help="覆盖运行数据目录（测试用）")
    parser.add_argument("--demo", action="store_true", help="载入演示媒体，不启动 Worker")
    parser.add_argument("--smoke", action="store_true", help="初始化后立即退出")
    parser.add_argument("--smoke-preview", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background", action="store_true", help="静默运行浏览器连接和转写服务")
    parser.add_argument("--title", default=APP_NAME, help=argparse.SUPPRESS)
    parser.add_argument("--geometry", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--webview-ui", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    # shengjian:// 协议拉起（2026-10-19）：浏览器一键启动传 URL 作 argv，
    # 归一为后台静默启动，行为与 --background 完全一致。
    if argv:
        argv = ["--background" if str(item).startswith("shengjian://") else item for item in argv]
    args = parser.parse_args(argv)
    if args.smoke_preview and (not args.smoke or not args.data_dir):
        parser.error("预览检查须使用 --smoke 和独立 --data-dir")
    if args.smoke:
        import yt_dlp

        if not getattr(yt_dlp, "version", None):
            raise RuntimeError("yt-dlp 打包验证失败")
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as downloader:
            for extractor in ("Youtube", "BiliBili", "Douyin"):
                downloader.get_info_extractor(extractor)
    instance = None
    if not args.smoke and not args.demo:
        try:
            instance = SingleInstance()
        except RuntimeError:
            return 0 if _activate_running_instance(args.data_dir, show=not args.background) else 1
    try:
        app = WebDesktopApp(
            data_dir=args.data_dir,
            demo=args.demo,
            start_worker=not (args.demo or args.smoke),
            window_title=args.title,
            initially_hidden=args.background,
            ui_dir=args.webview_ui,
            create_window=not args.smoke,
        )
        try:
            if args.smoke:
                if args.smoke_preview:
                    _verify_packaged_preview(app)
                return 0
            app.start(debug=args.debug)
            return 0
        finally:
            # GUI loop can return from window destroy, while the tray callback
            # may still be winding down. Always perform a final idempotent close.
            app._close()
    finally:
        if instance:
            instance.close()



def main(argv: list[str] | None = None) -> int:
    """Entry point with crash logging (packaged builds have no console)."""
    try:
        return _main(argv)
    except BaseException:
        import traceback
        try:
            log_dir = _runtime_log_dir()
            (log_dir / "desktop_crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
