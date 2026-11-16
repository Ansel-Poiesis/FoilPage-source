# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""WebView2 desktop lifecycle regression tests."""
from __future__ import annotations

import threading
from unittest import mock

from app import desktop_web


def _lifecycle_app() -> desktop_web.WebDesktopApp:
    app = desktop_web.WebDesktopApp.__new__(desktop_web.WebDesktopApp)
    app._closed = False
    app._close_lock = threading.RLock()
    app._push_timer = mock.Mock()
    app.worker = mock.Mock()
    app.tray = mock.Mock()
    app.local_api = mock.Mock()
    app.controller = mock.Mock()
    app._window = None
    app._notification_stop = threading.Event()
    app._notification_thread = None
    # 测试默认不布看门狗（避免 30s 后 os._exit 杀掉测试进程）；
    # 看门狗用例显式置 False 并 monkeypatch os._exit。
    app._quit_watchdog_armed = True
    return app


def _expiry_app():
    app = _lifecycle_app()
    app.controller.settings = {"subscription_expires_at": "2026-10-16T00:00:00Z", "account_user_id": "fixture"}
    app.controller.save_settings.side_effect = app.controller.settings.update
    app.bridge = mock.Mock()
    app.bridge.get_subscription_status.return_value = {"plan_expiry_notice": {"days": 0, "date": "2026-10-16", "expired": False}}
    app._notify_bubble = mock.Mock(return_value=True)
    return app


def test_expiry_notification_uses_verified_status_and_successful_delivery():
    app = _expiry_app()
    app._notify_bubble.return_value = False
    app._plan_expiry_notice_once()
    app.controller.save_settings.assert_not_called()
    assert "24 小时内" in app._notify_bubble.call_args.args[1]
    assert "本地功能登录即可继续使用" in app._notify_bubble.call_args.args[1]
    app._plan_expiry_notice_attempt = ("", 0.0)
    app._notify_bubble.return_value = True
    app._plan_expiry_notice_once()
    app.controller.save_settings.assert_called_once()
    app._plan_expiry_notice_once()
    assert app._notify_bubble.call_count == 2


def test_expiry_notifications_respect_disabled_and_suppressed_states():
    app = _expiry_app()
    app.controller.settings["task_notifications"] = False
    app._plan_expiry_notice_once()
    app.bridge.get_subscription_status.assert_not_called()
    app.controller.settings["task_notifications"] = True
    app.bridge.get_subscription_status.return_value = {"plan_expiry_notice": None}
    app._plan_expiry_notice_once()
    app._notify_bubble.assert_not_called()


def test_expiry_daily_dedup_is_scoped_to_account_and_outcome():
    app = _expiry_app()
    app._plan_expiry_notice_once()
    app.controller.settings["account_user_id"] = "other-fixture"
    app._plan_expiry_notice_once()
    assert app._notify_bubble.call_count == 2
    app.controller.settings["account_user_id"] = "fixture"
    app._plan_expiry_notice_once()
    assert app._notify_bubble.call_count == 2
    app.controller.settings["subscription_server_url"] = "https://different.invalid"
    app._plan_expiry_notice_once()
    assert app._notify_bubble.call_count == 3
    app.bridge.get_subscription_status.return_value["plan_expiry_notice"]["expired"] = True
    app._plan_expiry_notice_once()
    assert app._notify_bubble.call_count == 4


def test_close_is_idempotent_and_stops_every_owned_component() -> None:
    app = _lifecycle_app()
    timer = app._push_timer
    tray = app.tray
    local_api = app.local_api

    app._close()
    app._close()

    timer.cancel.assert_called_once_with()
    # 2026-11-16：优雅窗口默认从固定 2s 加宽到 worker_stop_timeout（8s）。
    app.worker.stop.assert_called_once_with(timeout=8)
    tray.stop.assert_called_once_with()
    local_api.stop.assert_called_once_with(timeout=2)
    app.controller.close.assert_called_once_with()
    assert app._closed is True
    assert app.tray is None
    assert app.local_api is None


def test_main_closes_app_when_webview_loop_returns() -> None:
    fake_instance = mock.Mock()
    fake_app = mock.Mock()
    fake_app.start.return_value = None

    with (
        mock.patch.object(desktop_web, "_setup_logging"),
        mock.patch.object(desktop_web, "SingleInstance", return_value=fake_instance),
        mock.patch.object(desktop_web, "WebDesktopApp", return_value=fake_app),
    ):
        assert desktop_web._main([]) == 0

    fake_app.start.assert_called_once_with(debug=False)
    fake_app._close.assert_called_once_with()


def test_main_normalizes_foilpage_protocol_launch_to_background() -> None:
    """2026-11-16 品牌统一：主协议 foilpage://，行为与旧 shengjian:// 一致。"""
    fake_instance = mock.Mock()
    fake_app = mock.Mock()
    fake_app.start.return_value = None
    captured: dict[str, object] = {}

    def fake_app_factory(**kwargs):
        captured.update(kwargs)
        return fake_app

    with (
        mock.patch.object(desktop_web, "_setup_logging"),
        mock.patch.object(desktop_web, "SingleInstance", return_value=fake_instance),
        mock.patch.object(desktop_web, "WebDesktopApp", side_effect=fake_app_factory),
    ):
        assert desktop_web._main(["foilpage://launch"]) == 0

    assert captured.get("initially_hidden") is True, "foilpage:// 拉起应归一为后台静默启动"
    fake_instance.close.assert_called_once_with()


def test_main_normalizes_shengjian_protocol_launch_to_background() -> None:
    fake_instance = mock.Mock()
    fake_app = mock.Mock()
    fake_app.start.return_value = None
    captured: dict[str, object] = {}

    def fake_app_factory(**kwargs):
        captured.update(kwargs)
        return fake_app

    with (
        mock.patch.object(desktop_web, "_setup_logging"),
        mock.patch.object(desktop_web, "SingleInstance", return_value=fake_instance),
        mock.patch.object(desktop_web, "WebDesktopApp", side_effect=fake_app_factory),
    ):
        assert desktop_web._main(["shengjian://launch"]) == 0

    assert captured.get("initially_hidden") is True, "协议拉起应归一为后台静默启动"
    fake_instance.close.assert_called_once_with()


def test_explicit_worker_stop_does_not_trigger_auto_restart() -> None:
    """The stop flag must win over the monitor's crash-restart path."""
    from app.process_manager import WorkerProcessManager

    states: list[str] = []
    manager = WorkerProcessManager(
        ["unused"], state_callback=states.append, auto_restart=True,
    )
    process = mock.Mock()
    process.stdout = None
    process.poll.return_value = 0
    process.wait.return_value = 0
    with manager._lock:
        manager._process = process
        manager._stop_requested = True

    with mock.patch.object(manager, "start") as restart:
        manager._monitor_loop()

    restart.assert_not_called()
    assert states == ["stopped"]




def test_close_uses_configured_worker_stop_timeout() -> None:
    app = _lifecycle_app()
    app.controller.settings = {"worker_stop_timeout": 5}
    app._close()
    app.worker.stop.assert_called_once_with(timeout=5)


def test_close_signals_notification_loop_and_waits_for_it_to_exit() -> None:
    """先生 2026-11-16：退出先叫停轮询并 join（≤3s）再取锁。

    轮询每秒全程持 _close_lock，数据库抖动时单轮可持锁数秒且立即重入；
    不先 join，_close 会被反复插队饿死（2026-11-14 磁盘满风暴形态）。
    """
    app = _lifecycle_app()
    app._notification_events = []
    app._notifications = mock.Mock()
    app._notifications.update.return_value = []
    app.bridge = mock.Mock()
    in_poll = threading.Event()
    release_poll = threading.Event()

    def blocking_tasks():
        in_poll.set()
        release_poll.wait(5)
        return []

    app.bridge.get_notification_tasks.side_effect = blocking_tasks
    app.controller.settings = {"task_notifications": True}
    app.controller.media_action_lock = threading.RLock()
    app.controller.failed_task_retention = mock.Mock()
    app._plan_expiry_notice_once = mock.Mock()
    loop = threading.Thread(target=app._notification_loop, daemon=True)
    loop.start()
    assert in_poll.wait(2), "轮询应已进入一次库读"
    close_done = threading.Event()
    closer = threading.Thread(target=lambda: (app._close(), close_done.set()), daemon=True)
    closer.start()
    # join(3) 到期时阻塞中的轮询仍在（5s 上限），_close 必须继续取锁完成
    # 收尾——等待有界，不因轮询悬挂而卡死退出。
    assert close_done.wait(6), "退出不得被持锁轮询无限拖延"
    release_poll.set()
    loop.join(3)
    assert app._closed is True
    assert not loop.is_alive(), "退出后轮询线程必须已结束"
    app.worker.stop.assert_called_once_with(timeout=8)
    # 停止置位后不再开启新的轮询轮次。
    assert app.bridge.get_notification_tasks.call_count <= 2


def test_quit_watchdog_forces_process_exit_when_cleanup_hangs() -> None:
    """清理链卡死时看门狗兜底强制退出——进程必死、互斥量必释放。"""
    app = _lifecycle_app()
    app._quit_watchdog_armed = False
    block_cleanup = threading.Event()
    app.worker.stop.side_effect = lambda timeout: block_cleanup.wait(10)
    forced = threading.Event()

    with (
        mock.patch.object(desktop_web.os, "_exit", side_effect=lambda code: forced.set()) as exit_mock,
        mock.patch.object(desktop_web.time, "sleep", side_effect=lambda _seconds: None),
    ):
        quit_thread = threading.Thread(target=app._quit, daemon=True)
        quit_thread.start()
        assert forced.wait(2), "看门狗应在清理卡死时触发强制退出"
        block_cleanup.set()
        quit_thread.join(3)
        exit_mock.assert_called_once_with(0)
    assert app._quit_watchdog_armed is True


def test_dwm_attributes_are_attempted_independently() -> None:
    """An unsupported Win11-only attribute must not prevent border removal."""
    calls: list[int] = []

    def set_attribute(hwnd, attribute, value, size):
        calls.append(attribute)
        if attribute == desktop_web.DWMWA_WINDOW_CORNER_PREFERENCE:
            raise OSError("unsupported")
        return 0

    with mock.patch.object(
        desktop_web._dwmapi, "DwmSetWindowAttribute", side_effect=set_attribute,
    ):
        desktop_web._set_dwm_window_colors(101, dark=True)

    assert desktop_web.DWMWA_BORDER_COLOR in calls
    assert desktop_web.DWMWA_CAPTION_COLOR in calls
    assert desktop_web.DWMWA_TEXT_COLOR in calls


def test_tray_quit_destroys_window_after_cleanup_failure() -> None:
    app = _lifecycle_app()
    app._window = mock.Mock()
    app._close = mock.Mock(side_effect=SystemError("invalid worker handle"))
    destroyed = threading.Event()
    app._window.destroy.side_effect = destroyed.set

    app._request_quit()
    assert destroyed.wait(2), "tray quit should finish outside the menu callback thread"

    app._window.destroy.assert_called_once_with()


def test_cleanup_failure_does_not_leave_tray_or_api_running() -> None:
    app = _lifecycle_app()
    tray, api = app.tray, app.local_api
    app.worker.stop.side_effect = RuntimeError("worker already exited")
    api.stop.side_effect = RuntimeError("listener already stopped")
    app._close()
    tray.stop.assert_called_once()
    app.controller.close.assert_called_once()


def test_close_hides_only_when_tray_is_usable() -> None:
    app = _lifecycle_app()
    app._window = mock.Mock()
    app.controller.settings = {"close_to_tray": True}
    app.tray.available = False
    assert app._on_closing() is None
    app._window.hide.assert_not_called()
    app.tray.available = True
    assert app._on_closing() is False
    app._window.hide.assert_called_once()


def test_worker_stop_recovers_from_invalid_control_break_handle() -> None:
    from app.process_manager import WorkerProcessManager

    states: list[str] = []
    manager = WorkerProcessManager(["unused"], state_callback=states.append)
    process = mock.Mock()
    process.poll.return_value = None
    process.send_signal.side_effect = SystemError("invalid handle")
    process.wait.return_value = 0
    with manager._lock:
        manager._process = process

    assert manager.stop(timeout=1) is True
    process.terminate.assert_called_once_with()
    assert states == ["stopped"]


def test_native_window_initialization_failure_is_logged_once_after_retries(caplog) -> None:
    app = _lifecycle_app()
    app._native_hwnd = mock.Mock(side_effect=OSError("native handle unavailable"))
    with mock.patch.object(desktop_web.time, "sleep"):
        app._fix_native_window()
    assert app._native_hwnd.call_count == 15
    failures = [record for record in caplog.records if "Native window icon/frame initialization" in record.message]
    assert len(failures) == 1 and failures[0].exc_info is not None


def test_manual_activation_of_background_instance_returns_to_setup() -> None:
    app = _lifecycle_app()
    app._launch_context = {"mode": "background", "sequence": 0}
    app._window = mock.Mock()
    navigated = threading.Event()
    app._window.evaluate_js.side_effect = lambda js: navigated.set()
    app._show_from_api()
    assert navigated.wait(2)
    assert app._launch_context == {"mode": "manual", "sequence": 1}
    assert "switchView('setup')" in app._window.evaluate_js.call_args.args[0]
    app._window.show.assert_called_once()
    app._window.restore.assert_called_once()


def test_extension_deep_link_keeps_requested_page_and_does_not_fake_manual_launch() -> None:
    app = _lifecycle_app()
    app._launch_context = {"mode": "background", "sequence": 0}
    app._window = mock.Mock()
    navigated = threading.Event()
    app._window.evaluate_js.side_effect = lambda js: navigated.set()
    app._show_from_api('browser')
    assert navigated.wait(2)
    assert "switchView('browser')" in app._window.evaluate_js.call_args.args[0]
    assert app._launch_context["mode"] == "background"
