# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from unittest.mock import Mock

from app.task_notifications import TaskNotifications


def task(status, key="one", kind="transcription", stamp="1"):
    return {"task_id": key, "status": status, "kind": kind, "updated_at": stamp, "display_title": "测试素材"}


def test_history_is_quiet_but_fast_and_normal_tasks_notify_once():
    notify = Mock(return_value=True)
    monitor = TaskNotifications(notify)
    assert monitor.update([task("done", "history"), task("running")]) == []
    events = monitor.update([task("done", "history"), task("done"), task("done", "fast", "download")])
    assert [event["title"] for event in events] == ["转写成功", "下载成功"]
    assert monitor.update([task("done"), task("done", "fast", "download")]) == []
    assert notify.call_count == 2


def test_retry_can_notify_again_and_muted_tasks_do_not_replay():
    monitor = TaskNotifications(Mock(return_value=True))
    monitor.update([task("running")])
    assert monitor.update([task("failed")])[0]["title"] == "转写失败"
    monitor.update([task("queued")])
    assert monitor.update([task("done")])[0]["title"] == "转写成功"
    assert monitor.update([task("done", "muted")], enabled=False) == []
    assert monitor.update([task("done", "muted")], enabled=True) == []


def test_system_failure_is_reported_for_in_app_fallback():
    monitor = TaskNotifications(Mock(return_value=False))
    monitor.update([])
    assert monitor.update([task("done")])[0]["delivered"] is False


def test_linked_download_completion_stays_visible_to_notification_monitor():
    from app.bridge import Bridge
    controller = Mock()
    controller.job_rows.return_value = [{"id": "asr", "status": "running", "display_title": "示例"}]
    controller.downloads.list.return_value = [{"id": "dl", "status": "transcribing", "downloaded_path": "sample.mp4", "job_id": "asr", "title": "示例"}]
    bridge = Bridge(controller, Mock(), Mock(), lambda: None, lambda: None)
    rows = bridge.get_notification_tasks()
    assert len(rows) == 2
    assert rows[0]["kind"] == "transcription" and rows[0]["status"] == "running"
    assert rows[1]["kind"] == "download" and rows[1]["status"] == "done"


def test_active_progress_and_handoffs_remain_quiet_until_success():
    notify = Mock(return_value=True)
    monitor = TaskNotifications(notify)
    monitor.update([])
    for index, status in enumerate(["received", "downloading", "awaiting_browser", "received", "downloading"]):
        assert monitor.update([task(status, key="dl:1", kind="download", stamp=str(index))]) == []
    assert notify.call_count == 0
    events = monitor.update([task("done", key="dl:1", kind="download", stamp="final")])
    assert [e["title"] for e in events] == ["下载成功"]
    assert notify.call_count == 1
    assert monitor.update([task("done", key="dl:1", kind="download", stamp="later")]) == []


def test_terminal_beep_differs_between_success_and_failure():

    from app.task_notifications import TaskNotifications

    def task(status, task_id="t1", kind="transcription"):
        return {"task_id": task_id, "status": status, "kind": kind, "display_title": "标题", "updated_at": "u"}

    beeps = []
    monitor = TaskNotifications(lambda *a: True, beep=beeps.append)
    monitor.update([task("running")], enabled=True)
    monitor.update([task("done", "a")], enabled=True)
    monitor.update([task("failed", "b")], enabled=True)
    assert beeps == ["success", "error"], "成功/失败必须走不同提示音"


def test_terminal_notification_uses_full_app_title():
    from unittest import mock

    from app.task_notifications import TaskNotifications

    def task(status, task_id="t1", kind="download"):
        return {"task_id": task_id, "status": status, "kind": kind, "display_title": "标题", "updated_at": "u"}

    notify = mock.Mock(return_value=True)
    monitor = TaskNotifications(notify)
    monitor.update([task("running")], enabled=True)
    monitor.update([task("done")], enabled=True)
    assert notify.call_args[0][0] == "下载成功"


def test_stage_snapshots_select_exact_completion_and_keep_original_video_title():
    notify = Mock(return_value=True)
    monitor = TaskNotifications(notify)
    monitor.update([])
    cases = [(True, False, "done", "disabled", "正文转写成功"),
             (False, True, "disabled", "done", "标题转写成功"),
             (True, True, "done", "done", "正文与标题转写成功"),
             (True, True, "done", "failed", "正文转写成功，标题处理失败")]
    for index, (body, title, bs, ts, label) in enumerate(cases):
        row = {**task("done", str(index)), "body_enabled_snapshot": body, "title_enabled_snapshot": title,
               "body_status": bs, "title_status": ts, "original_source_title": "视频原题", "display_title": "生成的新题"}
        events = monitor.update([row])
        assert events[0]["title"] == label
        assert notify.call_args[0][1].startswith("视频原题")


def test_quota_exhausted_terminal_label_names_the_reason_and_action():
    """积分用完（正文/标题整理被跳过）必须点名原因与去处，弹窗直达用户。"""
    monitor = TaskNotifications(Mock(return_value=True))
    monitor.update([{**task("running", "quota")}])  # 首轮预热历史基线
    row = {**task("done", "quota"), "body_enabled_snapshot": True, "title_enabled_snapshot": True,
           "body_status": "skipped", "title_status": "skipped", "body_quota_exceeded": True}
    events = monitor.update([row])
    assert events[0]["title"] == "转写完成，云端积分已用完，请到订阅页充值后重试整理"
    assert "任务页" in events[0]["message"]  # failed 语义：详情引导
