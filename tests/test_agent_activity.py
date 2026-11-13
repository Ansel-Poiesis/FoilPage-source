# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Only safe operation summaries may leave the local activity journal."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from engine.agent_activity import AgentActivity


def test_activity_records_only_operation_outcome_and_revision_not_arguments(tmp_path):
    activity = AgentActivity(tmp_path / "activity.json")
    activity.record("settings.saved", True, "settings", "a" * 64)
    activity.record("documents.read", False)
    activity.record("private-marker-with-spaces", True)
    summary = activity.summary(enabled=True)
    assert summary["enabled"] is True and summary["last_revision"] == "a" * 64
    assert [r["operation"] for r in summary["records"]] == ["documents.read", "settings.saved"]
    assert summary["records"][0]["ok"] is False
    assert all(set(r) == {"operation", "at", "ok", "category"} for r in summary["records"])
    assert "private-marker" not in json.dumps(summary)


@pytest.mark.parametrize("persisted", [[], {"records": {}}, {"records": [None, [], "private-marker"]},
    {"records": [{"operation": "documents.read", "at": {"token": "private-marker"}, "ok": True, "category": "private-marker"}],
     "last_saved_at": {"token": "private-marker"}, "last_revision": "private-marker", "token": "private-marker"},
    {"records": [{"operation": "documents.read", "at": "not-a-time-private-marker", "ok": True, "category": "application"}]},
])
def test_tampered_journal_does_not_leak_unknown_fields_or_invalid_metadata(tmp_path, persisted):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(persisted), encoding="utf-8")
    public = AgentActivity(path).summary(enabled=False)
    assert public["enabled"] is False
    assert "private-marker" not in json.dumps(public)
    assert public["last_revision"] in (None, "")
    assert public["last_saved_at"] in (None, "")


def test_valid_persisted_record_discards_injected_payload(tmp_path):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps({"records": [{"operation": "documents.read", "at": "2026-10-09T00:00:00+00:00",
        "ok": True, "category": "application", "arguments": {"text": "private-marker"}}], "token": "private-marker"}), encoding="utf-8")
    public = AgentActivity(path).summary(enabled=True)
    assert len(public["records"]) == 1 and "private-marker" not in json.dumps(public)
    assert set(public["records"][0]) == {"operation", "at", "ok", "category"}


def test_activity_bounded_and_invalid_inputs_cannot_break_the_operation(tmp_path):
    activity = AgentActivity(tmp_path / "activity.json")
    for index in range(205):
        activity.record(f"tasks.read_{index}", True)
    summary = activity.summary(enabled=True)
    assert len(summary["records"]) == 200
    assert summary["records"][0]["operation"] == "tasks.read_204"
    assert summary["records"][-1]["operation"] == "tasks.read_5"
    for invalid in (None, {}, 3, "", "TOKEN=private-marker", "a" * 97):
        activity.record(invalid, True)
    assert len(activity.summary(enabled=True)["records"]) == 200
    activity.record("settings.update", True, revision="invalid")
    assert activity.summary(enabled=True)["last_revision"] == ""


def test_two_instances_do_not_lose_parallel_records_and_release_file_handles(tmp_path):
    path = tmp_path / "activity.json"
    one, two = AgentActivity(path), AgentActivity(path)
    def write(activity, prefix):
        for index in range(40):
            activity.record(f"{prefix}.read_{index}", True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: write(*args), [(one, "one"), (two, "two")]))
    records = one.summary(enabled=True)["records"]
    assert len(records) == 80
    assert len({record["operation"] for record in records}) == 80
    # Windows rename is a real check that journal handles are closed after read/write.
    moved = tmp_path / "moved.json"
    path.rename(moved)
    assert moved.is_file()


def test_agent_control_business_rejection_is_not_logged_as_success(tmp_path):
    from types import SimpleNamespace
    from app.agent_control import AgentControl
    activity = AgentActivity(tmp_path / "activity.json")
    class Rejected(AgentControl):
        def _call(self, method, params):
            return {"ok": False, "code": "model_not_ready", "error": "模型未完成下载"}
    control = Rejected(SimpleNamespace(agent_activity=activity), None)
    result = control.call("operations.call", {"operation": "settings.update", "arguments": {}})
    assert result["ok"] is False
    records = activity.summary(enabled=True)["records"]
    assert records[0]["operation"] == "settings.update" and records[0]["ok"] is False


def test_agent_control_outer_record_preserves_real_saved_revision(tmp_path):
    from types import SimpleNamespace
    from app.agent_control import AgentControl
    activity = AgentActivity(tmp_path / "activity.json")
    class Saved(AgentControl):
        def _call(self, method, params):
            # The public Bridge save records this before the outer call records
            # its completion. Both real logging layers share the same journal.
            activity.record("settings.saved", True, "settings", "b" * 64)
            return {"ok": True, "revision": "b" * 64}
    control = Saved(SimpleNamespace(agent_activity=activity), None)
    assert control.call("operations.call", {"operation": "settings.update", "arguments": {}})["ok"] is True
    summary = activity.summary(enabled=True)
    assert summary["last_revision"] == "b" * 64
    assert summary["last_saved_at"] is not None
    assert [record["operation"] for record in summary["records"]] == ["settings.update", "settings.saved"]
