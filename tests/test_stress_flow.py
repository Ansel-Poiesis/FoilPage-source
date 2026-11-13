# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""压力混流：先生问「程序在压力中是否有效」。

并发线程对 20 个独立视频各连点 5 次（100 次提交）+ 30 个一次性新视频，
断言：每个 URL 只有一行、无异常、状态机全部处于合法活跃态；
随后批量转终态后再混流重试一轮验证去重不拦终态后的新任务。
"""
from __future__ import annotations

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator, _DEDUP_ACTIVE_STATUSES
from engine.settings import SettingsStore
from tests.test_download_pipeline import FakeAdapter


def test_rapid_duplicate_and_mixed_submits_hold_under_pressure() -> None:
    with tempfile.TemporaryDirectory(prefix="stress_dedup_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        try:
            dup_urls = [f"https://www.douyin.com/video/{7300000000000 + i}" for i in range(20)]
            fresh_urls = [f"https://www.bilibili.com/video/BV1stress{i:03d}" for i in range(30)]
            errors: list[Exception] = []

            def submit_many(url: str, times: int) -> None:
                for _ in range(times):
                    try:
                        coordinator.submit(
                            url,
                            title="stress",
                            options={"transcribe_after_download": False},
                        )
                    except Exception as exc:  # noqa: BLE001 - 压力测试收集全部异常
                        errors.append(exc)

            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(submit_many, url, 5) for url in dup_urls]
                futures += [pool.submit(submit_many, url, 1) for url in fresh_urls]
                for future in futures:
                    future.result()

            assert not errors, f"压力提交出现异常: {errors[:3]}"
            jobs = controller.downloads.list(limit=500)
            assert len(jobs) == len(dup_urls) + len(fresh_urls), (
                f"期望 {len(dup_urls) + len(fresh_urls)} 行，实际 {len(jobs)}（去重失效）"
            )
            by_url: dict[str, int] = {}
            for job in jobs:
                by_url[job["url"]] = by_url.get(job["url"], 0) + 1
            assert all(count == 1 for count in by_url.values()), "同一 URL 出现多行"
            assert all(job["status"] in _DEDUP_ACTIVE_STATUSES for job in jobs)

            # 终态后重试不拦：批量取消（终态），重提交应产生新行。
            for job in jobs:
                coordinator.store.transition(job["id"], "cancelled")
            first_fresh = dup_urls[0]
            recreated = coordinator.submit(
                first_fresh, title="after-terminal",
                options={"transcribe_after_download": False},
            )
            assert recreated["id"] not in {job["id"] for job in jobs}, "终态后应允许新建"
        finally:
            coordinator.stop()
            controller.close()


def test_concurrent_duplicate_clicks_single_row_and_lock_safety() -> None:
    """20 个线程同一瞬间点击同一 URL：必须恰好一行且无锁异常。"""
    with tempfile.TemporaryDirectory(prefix="stress_single_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        try:
            url = "https://www.douyin.com/video/7312345678901234567"
            barrier = threading.Barrier(20)
            results: list[object] = []
            lock = threading.Lock()

            def click() -> None:
                barrier.wait()
                item = coordinator.submit(
                    url, title="同秒连点",
                    options={"transcribe_after_download": False},
                )
                with lock:
                    results.append(item["id"])

            threads = [threading.Thread(target=click) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            assert len(results) == 20
            assert len(set(results)) == 1, f"同 URL 并发点击产生了多行: {set(results)}"
        finally:
            coordinator.stop()
            controller.close()
