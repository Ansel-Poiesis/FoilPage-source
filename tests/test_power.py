# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""系统休眠防护（TASK-8.22）：计数合并、异常释放、API 不可用降级。"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import power


@pytest.fixture(autouse=True)
def _clean_depth():
    power.reset_for_tests()
    yield
    power.reset_for_tests()


@pytest.fixture()
def calls(monkeypatch):
    recorded: list[int] = []
    monkeypatch.setattr(power, "_set_execution_state", lambda flags: recorded.append(flags) or 1)
    return recorded


def test_keep_awake_sets_and_clears_system_required(calls) -> None:
    with power.keep_awake("test"):
        assert calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]
        assert power.held_depth() == 1
    assert calls == [
        power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED,
        power.ES_CONTINUOUS,
    ]
    assert power.held_depth() == 0


def test_nested_holders_touch_system_state_once(calls) -> None:
    with power.keep_awake("outer"):
        with power.keep_awake("inner"):
            assert power.held_depth() == 2
        # 内层退出不交还系统状态（calls 仍只有一次进入）。
        assert calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]
    assert calls == [
        power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED,
        power.ES_CONTINUOUS,
    ]


def test_exception_still_releases(calls) -> None:
    with pytest.raises(RuntimeError):
        with power.keep_awake("boom"):
            raise RuntimeError("task failed")
    assert calls[-1] == power.ES_CONTINUOUS
    assert power.held_depth() == 0


def test_unavailable_api_degrades_to_noop(monkeypatch) -> None:
    monkeypatch.setattr(power, "_set_execution_state", lambda flags: 0)
    with power.keep_awake("unavailable") as held:
        assert held is False
    assert power.held_depth() == 0


def test_threads_hold_independent_state(calls) -> None:
    done = threading.Event()

    def other() -> None:
        with power.keep_awake("other-thread"):
            assert power.held_depth() == 1
        done.set()

    with power.keep_awake("main-thread"):
        assert power.held_depth() == 1
        worker = threading.Thread(target=other)
        worker.start()
        done.wait(timeout=5)
        worker.join(timeout=5)
        # 两个线程各自进入/退出；主线程状态不受子线程影响。
        assert power.held_depth() == 1
    assert power.held_depth() == 0
    assert calls.count(power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED) == 2
