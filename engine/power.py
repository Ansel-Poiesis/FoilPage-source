# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""任务执行期间的系统休眠防护（下载＋本地转写的长任务窗口）。

背景（TASK-8.22，2026-10-21）：本地 ASR 一跑十几分钟，机器中途自动休眠
会让任务挂起，用户看到的是「软件卡死」。竞品 4K Video Downloader Plus
默认 preventSleep；FoilPage 此前全仓没有任何防护。

实现用 Win32 ``SetThreadExecutionState``：
- ``ES_CONTINUOUS | ES_SYSTEM_REQUIRED`` 阻止系统空闲休眠（允许熄屏，
  下载/转写都不需要屏幕）；
- 该状态是**线程级**语义：进入与退出必须发生在同一线程。下载协调器
  （browser-download-worker 线程）与转写 worker（单线程或并发线程池）
  各自在本线程内成对进出即可，线程本地计数支持同线程嵌套持有；
- 退出（depth 归零）恢复 ``ES_CONTINUOUS`` 单独调用，交还休眠控制权；
- 非 Windows 平台与调用失败均为静默空操作：休眠防护是体验增强，
  绝不能反过来阻断任务本身。
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("shengjian.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_state = threading.local()


def _load_kernel32():
    if os.name != "nt":  # pragma: no cover - 开发机非 Windows 时跳过
        return None
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _set_execution_state(flags: int) -> int:
    """单点封装，便于测试替换并记录状态转换。返回 0 表示调用失败。"""
    kernel32 = _load_kernel32()
    if kernel32 is None:
        return 0
    try:
        return int(kernel32.SetThreadExecutionState(flags)) or 0
    except Exception:  # noqa: BLE001 - 防护失败不能影响任务
        return 0


@contextmanager
def keep_awake(reason: str = "") -> Iterator[bool]:
    """在 with 代码块期间阻止系统自动休眠；yield 是否真正持有锁。

    嵌套调用在同一线程上按计数合并：只有最外层进入/退出会触碰系统状态。
    同一线程内进入、退出是对称的；跨线程各持各的线程级状态，互不干扰。
    """
    depth = getattr(_state, "depth", 0)
    if depth == 0:
        if not _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            # 拿不到锁（非 Windows / API 失败）：任务照常执行，仅记录一次。
            log.debug("sleep prevention unavailable: %s", reason or "task")
            yield False
            return
        log.info("sleep prevention held: %s", reason or "task")
    _state.depth = depth + 1
    try:
        yield True
    finally:
        _state.depth = depth
        if depth == 0:
            _set_execution_state(ES_CONTINUOUS)
            log.info("sleep prevention released: %s", reason or "task")


def held_depth() -> int:
    """当前线程的持有深度（测试与诊断用）。"""
    return getattr(_state, "depth", 0)


def reset_for_tests() -> None:
    """仅测试用：清空线程本地计数。不触碰系统状态。"""
    _state.depth = 0
