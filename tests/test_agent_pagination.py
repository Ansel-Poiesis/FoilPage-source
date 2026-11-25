# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Stable Agent pages over synthetic records, without workers or providers."""
from __future__ import annotations

from threading import RLock
from types import SimpleNamespace

import pytest

from app.agent_control import AgentControl, AgentControlError
from app.controller import AppController
from engine.settings import SettingsStore
from test_agent_control import completed


@pytest.fixture
def local(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    try:
        yield controller, AgentControl(controller, None)
    finally:
        controller.close()


def test_same_title_documents_and_tasks_can_be_fully_traversed(local):
    controller, control = local
    expected = {completed(controller, f"{i:012x}")[0] for i in range(201)}
    # Tie every timestamp so the task ID must resolve the page boundary.
    controller.queue._conn.execute("UPDATE jobs SET updated_at=?", ("2026-11-22T10:00:00",))
    controller.queue._conn.commit()
    for method in ("tasks.list", "documents.list"):
        first = control.call(method, {"query": "文稿标题", "limit": 200})
        assert first["total"] == 201 and len(first["items"]) == 200
        assert isinstance(first["next_cursor"], str)
        second = control.call(method, {"query": "文稿标题", "limit": 200, "cursor": first["next_cursor"]})
        assert second["total"] == 201 and len(second["items"]) == 1
        assert second["next_cursor"] is None
        ids = [item["task_id"] for item in first["items"] + second["items"]]
        assert len(ids) == len(set(ids)) == 201 and set(ids) == expected
        assert ids == sorted(expected, reverse=True)
        # Omitting cursor remains the original first-page operation.
        assert control.call(method, {"query": "文稿标题", "limit": 200}) == first
        assert len(control.call(method, {})["items"]) == 50


def test_download_pages_are_not_limited_by_the_desktop_preview_cap(local):
    controller, control = local
    expected = {"dl:" + controller.downloads.create(
        f"https://example.invalid/synthetic/{i}", "同名下载", "youtube", {"transcribe": False},
    )["id"] for i in range(501)}
    ids = []
    params = {"query": "同名下载", "status": "received", "limit": 200}
    while True:
        page = control.call("tasks.list", params)
        assert page["total"] == 501
        ids.extend(item["task_id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        params = {**params, "cursor": page["next_cursor"]}
    assert len(ids) == len(set(ids)) == 501 and set(ids) == expected


@pytest.fixture
def listing():
    rows = [{"task_id": f"{i:012x}", "kind": "transcription", "status": "done",
             "source_title": "同名资料", "updated_at": "2026-11-22T10:00:00"} for i in range(3)]
    controller = SimpleNamespace(media_action_lock=RLock(), task_rows=lambda **_: rows)
    return AgentControl(controller, None), rows


@pytest.mark.parametrize("changes", [{"query": "其他资料"}, {"status": "failed"}])
def test_cursor_cannot_cross_filters(listing, changes):
    control, _ = listing
    params = {"query": "同名资料", "status": "done", "limit": 1}
    cursor = control.call("tasks.list", params)["next_cursor"]
    with pytest.raises(AgentControlError) as failed:
        control.call("tasks.list", {**params, **changes, "cursor": cursor})
    assert (failed.value.status, failed.value.code) == (400, "invalid_cursor")


def test_cursor_cannot_cross_methods_and_equivalent_filters_can_continue(listing):
    control, _ = listing
    cursor = control.call("tasks.list", {"query": " 同名资料 ", "limit": 1})["next_cursor"]
    page = control.call("tasks.list", {"query": "同名资料", "limit": 200, "cursor": cursor})
    assert len(page["items"]) == 2 and page["next_cursor"] is None
    with pytest.raises(AgentControlError) as failed:
        control.call("documents.list", {"query": "同名资料", "cursor": cursor})
    assert failed.value.code == "invalid_cursor"


@pytest.mark.parametrize("change", ["insert", "move", "delete"])
def test_changed_membership_or_order_requires_restart_instead_of_losing_rows(listing, change):
    control, rows = listing
    params = {"query": "同名资料", "limit": 1}
    first = control.call("tasks.list", params)
    if change == "insert":
        rows.append({**rows[0], "task_id": "000000000003"})
    elif change == "move":
        rows[0]["updated_at"] = "2026-11-22T11:00:00"
    else:
        rows.pop(0)
    with pytest.raises(AgentControlError) as failed:
        control.call("tasks.list", {**params, "cursor": first["next_cursor"]})
    assert (failed.value.status, failed.value.code) == (409, "cursor_stale")
    assert control.call("tasks.list", params)["total"] == len(rows)


@pytest.mark.parametrize("cursor", [None, True, 123, "not-a-cursor", "W10", "a", "a" * 1025,
                                   "bad=cursor", "\ud800"])
def test_invalid_cursor_is_rejected_without_echo(listing, cursor):
    control, _ = listing
    with pytest.raises(AgentControlError) as failed:
        control.call("tasks.list", {"cursor": cursor})
    assert failed.value.status == 400
    assert "not-a-cursor" not in str(failed.value)


def test_empty_page_and_exact_limit_have_no_next_cursor(listing):
    control, _ = listing
    assert control.call("tasks.list", {"query": "无匹配"}) == {"items": [], "total": 0, "next_cursor": None}
    assert control.call("tasks.list", {"limit": 3, "cursor": ""})["next_cursor"] is None
