# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""队列 worker（阶段 2 执行链）：从 SQLite 领取 queued 作业并驱动引擎落库。

状态机：queued --claim_next--> running --run_engine--> done | failed | retryable | cancelled

职责：
- `claim_next` 用 `BEGIN IMMEDIATE` 原子抢占，并写入租约、心跳和尝试次数。
- 调用 `app/run_engine.run`（阶段 1 已接运行时校验：落盘前过 validate）。
- 成功：`store.save_manifest(manifest)` → jobs 行置 done 并写回产物路径。
- EngineError：按 code 和最大次数分终态失败 / 可重试；取消单独收敛。
- 其他未预期异常：终态失败（code=unexpected）。
- 常驻模式支持心跳、崩溃恢复和 SIGINT/SIGTERM/SIGBREAK 优雅退出。

注意：worker 不负责模型下载，只使用 model_dir 或本机已有 Hugging Face 缓存；
发布时模型获取方式见 SPIKE-报告（非阻塞发布门槛）。
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import sys
import threading
import uuid
from pathlib import Path
from typing import Mapping

# 让 worker 能 import 项目根的 engine 包，以及同目录的 run_engine 入口。
_ROOT = Path(__file__).resolve().parent.parent
_APP = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_APP))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from engine import queue_store, service_http
from engine.power import keep_awake
from engine.contracts import EngineError, Job
from engine.settings import SettingsError, SettingsStore
from engine.library_location import LibraryLocationError, preflight_library, validate_storage_path
from app.backup_restore import (
    BackupRestoreError, RECOVERY_DIRECTORY,
)

# 可瞬时重试的 EngineError code（环境抖动/资源暂不可用）。
# 其余 EngineError 视为终态失败。团队可按真实错误码扩展本集合。
_RETRYABLE_CODES: frozenset[str] = frozenset({"model_load_failed", "llm_failed", "media_timeout"})


def classify_error(code: str) -> str:
    """把 EngineError code 映射到终态：retryable / failed。"""
    return "retryable" if code in _RETRYABLE_CODES else "failed"


def _require_restore_ready(database, settings_store=None) -> None:
    if settings_store is not None:
        directory = settings_store.base_dir / RECOVERY_DIRECTORY
    else:
        value = str(database)
        if value == ":memory:" or value.startswith("file:"):
            return
        # This only identifies a log beside the explicitly selected DB. A
        # custom database path cannot establish some other settings root.
        directory = Path(value).expanduser().resolve().parent / RECOVERY_DIRECTORY
    if directory.exists() or directory.is_symlink():
        raise BackupRestoreError("备份导入待恢复，Worker已停止；请关闭当前实例并重启FoilPage完成恢复后再启动Worker（原设置根由 --settings-dir 指定）")


class Worker:
    def __init__(
        self,
        store: queue_store.QueueStore,
        *,
        model: str | None = None,
        device: str | None = None,
        language: str | None = None,
        model_dir: str = "",
        worker_id: str = "worker-0",
        poll: float = 2.0,
        heartbeat_interval: float = 5.0,
        lease_timeout: float = 300.0,
        agent_settings: Mapping[str, object] | None = None,
        settings_store: SettingsStore | None = None,
        concurrency: int = 1,
        network_policy: service_http.NetworkPolicy | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.device = device
        self.language = language
        self.model_dir = model_dir
        self.worker_id = worker_id
        self.poll = poll
        self.heartbeat_interval = heartbeat_interval
        self.lease_timeout = lease_timeout
        self.agent_settings = dict(agent_settings or {})
        self.settings_store = settings_store
        self.network_policy = network_policy
        if settings_store is not None:
            _require_restore_ready("", settings_store)
        # 并发档位（免费 1 / 标准 2 / 高级 4）：>1 时用线程池同时处理
        # 多条作业；claim_next 的原子抢占保证不会重复领取。
        self.concurrency = max(1, int(concurrency))
        self._stop = False
        self._stop_event = threading.Event()
        self.account_waiting = False

    def _account_ready(self) -> bool:
        _require_restore_ready(self.store.db_path, self.settings_store)
        from engine.account_access import verified_account_access
        try:
            if self.settings_store:
                settings = self.settings_store.load()
                base_dir = self.settings_store.base_dir
            else:
                settings = self.agent_settings
                database = str(self.store.db_path)
                base_dir = Path(database).resolve().parent if database != ":memory:" and not database.startswith("file:") else None
            # TASK-8.29（先生 2026-10-27）：解锁门槛整体下线，登录即开工。
            ready = verified_account_access(settings, base_dir)["allowed"]
            reason = "等待登录：任务已保留，登录后继续"
        except (OSError, TypeError, ValueError, SettingsError):
            ready = False
            reason = "等待账户验证：任务已保留"
        if self.account_waiting != (not ready):
            self.account_waiting = not ready
            print(f"[worker] {reason}" if not ready else "[worker] 账户已登录，继续队列", flush=True)
        return ready

    def _run_job(self, job: Job) -> None:
        _require_restore_ready(self.store.db_path, self.settings_store)
        # 延迟 import，避免 worker 仅做 --help / 单元测试时也拉起重型引擎依赖。
        from run_engine import run as run_engine_run

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job["id"], heartbeat_stop),
            name=f"heartbeat-{job['id']}",
            daemon=True,
        )
        heartbeat.start()
        last_progress = 0.0
        agent_settings = copy.deepcopy(self.settings_store.load() if self.settings_store else self.agent_settings)
        if self.network_policy is not None:
            agent_settings["_proxy_route"] = self.network_policy.proxy_route
        # A resumed run needs a fresh hosted operation even when its retry
        # allowance was restored after a graceful shutdown.
        agent_settings["_agent_operation_id"] = uuid.uuid4().hex
        # TASK-8.24：重新转写可按单任务覆盖 Agent 整理开关（None=沿用全局）。
        body_override = job.get("body_enabled_override")
        if isinstance(body_override, bool):
            agent_settings["agent_enabled"] = body_override
        # 2026-11-02：重新转写可按单任务覆盖整理力度（空=沿用全局设置）。
        level_override = str(job.get("agent_level_override") or "").strip()
        if level_override:
            agent_settings["agent_editing_level"] = level_override
        settlements = []
        agent_settings["_agent_stage_settlements"] = settlements
        if agent_settings.get("agent_billing_mode") == "hosted" and self.settings_store:
            from engine.device import device_id
            agent_settings["_agent_device_id"] = device_id(self.settings_store.base_dir)
        original_title = str(job.get("source_title") or "")
        stage_state = {"body_enabled_snapshot": bool(agent_settings.get("agent_enabled")),
            "title_enabled_snapshot": bool(agent_settings.get("title_optimization_enabled")),
            "original_source_title": original_title, "optimized_source_title": ""}
        for stage, enabled in (("body", stage_state["body_enabled_snapshot"]), ("title", stage_state["title_enabled_snapshot"])):
            stage_state.update({stage + "_status": "pending" if enabled else "disabled",
                stage + "_billing_mode": str(agent_settings.get("agent_billing_mode") or "byok") if enabled else "none",
                stage + "_usage_units": 0, stage + "_request_count": 0})

        def persist_stages(changes):
            stage_state.update(queue_store._stage_metadata(changes, strict=True))
            self.store.set_stage_metadata(job["id"], stage_state, worker_id=self.worker_id)

        # 先生 2026-11-16：退出排干。停止信号与用户取消同样触发引擎的协作
        # 取消检查点（ASR 在段边界快速退出），但结果分流：用户取消=终态
        # cancelled；退出中断=置回 queued（不吃尝试次数，见
        # QueueStore.requeue_interrupted），下次启动继续处理。
        def interrupt_check() -> bool:
            return self._stop or self.store.is_cancel_requested(job["id"])

        def persist_progress(value: float) -> None:
            nonlocal last_progress
            value = min(1.0, max(last_progress, float(value)))
            if value < 1.0 and value - last_progress < 0.01:
                return
            self.store.update_progress(job["id"], value, self.worker_id)
            last_progress = value

        def mark_terminal(status: str, error: dict) -> None:
            """写终态；若行已被强制删除则静默跳过（删除即终态）。"""
            try:
                pending = {stage + "_status": "cancelled" if status == "cancelled" else "skipped"
                           for stage in ("body", "title")
                           if stage_state[stage + "_status"] in ("running", "pending")}
                persist_stages(pending)
                self.store.update_status(
                    job["id"], status, error, worker_id=self.worker_id
                )
            except queue_store.JobNotFoundError:
                pass

        try:
            persist_stages({})
            if self.store.is_cancel_requested(job["id"]):
                raise EngineError("cancelled", "作业已取消")
            engine_progress = (
                (lambda value: persist_progress(float(value) * 0.9))
                if agent_settings.get("agent_enabled") or agent_settings.get("title_optimization_enabled") else persist_progress
            )
            manifest = run_engine_run(
                job["source_path"],
                job["output_dir"],
                self.model or job["whisper_model"],
                self.device or job["device"],
                self.language or job["language"],
                self.model_dir or "",
                job["id"],  # 队列拥有 job_id，回写时与 jobs 行对齐
                source_title=job.get("source_title", ""),
                cancel_check=interrupt_check,
                progress=engine_progress,
                initial_prompt=str(agent_settings.get("asr_initial_prompt") or ""),
                hotwords=str(agent_settings.get("asr_hotwords") or ""),
                use_batched=(
                    bool(agent_settings.get("asr_batched_enabled", True))
                    and (self.device or job["device"]) == "cuda"
                ),
            )
            manifest.update(stage_state)
            if original_title:
                manifest["source_title"] = original_title
            if stage_state["body_enabled_snapshot"]:
                try:
                    from app.agent_service import apply_agent_translation
                    persist_stages({"body_status": "running"})
                    manifest = apply_agent_translation(
                        manifest, agent_settings,
                        source_title=job.get("source_title", ""),
                        cancel_check=interrupt_check,
                    )
                    persist_stages({**queue_store._stage_metadata(manifest), "body_status": "done"})
                    persist_progress(0.98)
                    print(f"[agent] 完成 {job['id']} ({manifest.get('agent_profile', '')})", flush=True)
                except Exception as exc:  # Agent failure must not discard usable ASR output.
                    from engine.agent_client import agent_failure_metadata
                    failure = agent_failure_metadata(exc, "body")
                    persist_stages({"body_status": "cancelled" if self.store.is_cancel_requested(job["id"]) else "skipped",
                                    "body_usage_units": 0,
                                    "body_quota_exceeded": getattr(exc, "code", "") == "quota_exceeded",
                                    **getattr(exc, "stage_metadata", {}), **failure})
                    if self.store.is_cancel_requested(job["id"]):
                        raise EngineError("cancelled", "作业已取消") from exc
                    manifest.update(stage_state)
                    manifest["agent_status"] = "skipped"
                    print(f"[agent] 跳过 {job['id']}: {failure['body_error_code']}", flush=True)
            if stage_state["title_enabled_snapshot"]:
                from app.agent_service import apply_title_optimization
                persist_stages({"title_status": "running"})
                manifest = apply_title_optimization(manifest, agent_settings,
                    cancel_check=interrupt_check)
                persist_stages(manifest)
            manifest.update(stage_state)
            if (manifest.get("title_status") == "done" and agent_settings.get("update_title_after_transcribe", True)
                    and manifest.get("source_title")):
                self.store.set_source_title(job["id"], str(manifest["source_title"]))
            from app.agent_service import _write_manifest_file
            _write_manifest_file(manifest)
            self.store.save_manifest(manifest, worker_id=self.worker_id)
            from engine.agent_client import settle_agent_stages
            billing_metadata = settle_agent_stages(settlements, success=True)
            if billing_metadata:
                manifest.update(billing_metadata)
                try:
                    self.store.replace_manifest(manifest)
                except queue_store.QueueStoreError:
                    print(f"[agent] 结算结果待账户同步 {job['id']}", flush=True)
            if agent_settings.get("delete_video_after_transcribe"):
                self._delete_downloaded_source(job["source_path"], agent_settings)
            if agent_settings.get("delete_process_files_after_transcribe"):
                self._delete_process_files(manifest)
            if (
                agent_settings.get("delete_source_after_translation")
                and manifest.get("agent_status") == "done"
            ):
                self._delete_downloaded_source(job["source_path"], agent_settings)
            print(f"[worker] 完成 {job['id']}", flush=True)
        except queue_store.JobNotFoundError:
            # 用户已强制删除该任务：无需再写任何状态，直接静默收尾。
            print(f"[worker] {job['id']} 已从队列删除，停止处理", flush=True)
        except queue_store.CancellationRequestedError:
            mark_terminal(
                "cancelled",
                {"code": "cancelled", "message": "作业已取消"},
            )
            print(f"[worker] 已取消 {job['id']}", flush=True)
        except EngineError as e:
            # 先生 2026-11-16：停止信号触发的协作取消不是用户取消——任务
            # 置回 queued（不吃尝试次数），本次退出即排干、下次启动续跑。
            if (
                e.code == "cancelled"
                and self._stop
                and not self.store.is_cancel_requested(job["id"])
                and self.store.requeue_interrupted(job["id"], worker_id=self.worker_id)
            ):
                print(f"[worker] 退出中断，任务返回队列 {job['id']}", flush=True)
                return
            status = "cancelled" if e.code == "cancelled" else classify_error(e.code)
            if status == "retryable" and job.get("attempts", 0) >= job.get("max_attempts", 3):
                status = "failed"
            mark_terminal(
                status,
                {"code": e.code, "message": e.message},
            )
            print(f"[worker] {status} {job['id']}: {e.code}: {e.message}", flush=True)
        except Exception as e:  # noqa: BLE001
            mark_terminal(
                "failed",
                {"code": "unexpected", "message": f"{type(e).__name__}: {e}"},
            )
            print(f"[worker] failed {job['id']}: {type(e).__name__}: {e}", flush=True)
        finally:
            if settlements:
                from engine.agent_client import settle_agent_stages
                billing_metadata = settle_agent_stages(settlements, success=False)
                try:
                    self.store.set_stage_metadata(job["id"], billing_metadata)
                except queue_store.JobNotFoundError:
                    pass
            heartbeat_stop.set()
            heartbeat.join(timeout=max(self.heartbeat_interval * 2, 0.2))

    def _heartbeat_loop(self, job_id: str, stop: threading.Event) -> None:
        interval = max(self.heartbeat_interval, 0.05)
        while not stop.wait(interval):
            if not self.store.heartbeat(job_id, self.worker_id):
                return

    @staticmethod
    def _delete_downloaded_source(source_path: str, settings: Mapping[str, object]) -> None:
        """Remove only a completed browser download; never delete imported media."""
        try:
            source = Path(source_path).resolve(strict=True)
            download_dir = Path(str(settings.get("download_dir") or "")).expanduser().resolve(strict=True)
            source.relative_to(download_dir)
        except (OSError, ValueError):
            return
        try:
            source.unlink()
            # Preserve library provenance while accurately reflecting removal.
            from engine.media_store import MediaStore
            database_path = str(settings.get("database_path") or "")
            if database_path:
                media = MediaStore(database_path)
                try:
                    media.refresh_missing()
                finally:
                    media.close()
            print(f"[worker] 已删除已转译下载源文件: {source.name}", flush=True)
        except OSError as exc:
            # Output is durable; cleanup must not turn successful work into failure.
            print(f"[worker] 删除下载源文件失败: {source.name}: {exc}", flush=True)

    @staticmethod
    def _delete_process_files(manifest: Mapping[str, object]) -> None:
        """转写成功后清理任务目录中的过程文件（原始稿等），保留最终稿、
        manifest 与字幕时间轴（*_segments.json 是资产，供 SRT/VTT 导出）。

        引擎提取的中间音频在 run_engine 内部已随临时目录清理；这里兜底删除
        任务输出目录中除 final_path、*_manifest.json、*_segments.json 之外的文件，
        失败不阻断。
        """
        raw_value = manifest.get("raw_path")
        final_value = manifest.get("final_path")
        if not raw_value:
            return
        try:
            job_dir = Path(str(raw_value)).expanduser().resolve().parent
            final = Path(str(final_value)).expanduser().resolve() if final_value else None
        except OSError:
            return
        if not job_dir.is_dir():
            return
        try:
            for candidate in job_dir.iterdir():
                if not candidate.is_file():
                    continue
                if (
                    candidate == final
                    or candidate.name.endswith("_manifest.json")
                    or candidate.name.endswith("_segments.json")
                ):
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                    print(f"[worker] 已删除过程文件: {candidate.name}", flush=True)
                except OSError:
                    continue
        except OSError:
            return
    def step(self) -> bool:
        """领取并处理一条作业；队列空返回 False。"""
        _require_restore_ready(self.store.db_path, self.settings_store)
        self.store.recover_stale_jobs(self.lease_timeout)
        if not self._account_ready():
            return False
        job = self.store.claim_next(self.worker_id)
        if job is None:
            return False
        print(f"[worker] 领取 {job['id']} <- {job['source_path']}", flush=True)
        # 本地转写是长任务：期间系统休眠会无声挂起作业（用户视角=卡死）。
        # 持线程级执行锁覆盖整个 _run_job（识别→整理→落库）。
        with keep_awake(f"asr:{job['id']}"):
            self._run_job(job)
        return True

    def run_forever(self) -> None:
        self._install_signal_handlers()
        self._start_stop_watchers()
        print(
            f"[worker] 启动 id={self.worker_id} 并发={self.concurrency}，Ctrl+C 停止",
            flush=True,
        )
        if self.concurrency > 1:
            self._run_forever_concurrent()
            return
        while not self._stop:
            if not self.step():
                self._stop_event.wait(self.poll)
        print("[worker] 已停止", flush=True)

    def _run_forever_concurrent(self) -> None:
        """线程池并发：信号量限流领取，避免池队列里堆积被抢占的空租约。"""
        from concurrent.futures import ThreadPoolExecutor

        semaphore = threading.BoundedSemaphore(self.concurrency)

        def release() -> None:
            try:
                semaphore.release()
            except ValueError:
                pass

        def run_and_release(job: Job) -> None:
            try:
                print(f"[worker] 领取 {job['id']} <- {job['source_path']}", flush=True)
                # 与 step() 同款防休眠；并发池里各线程各持各的线程级状态。
                with keep_awake(f"asr:{job['id']}"):
                    self._run_job(job)
            except Exception:
                import traceback

                traceback.print_exc()
            finally:
                release()

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while not self._stop:
                if not semaphore.acquire(timeout=0.2):
                    continue
                _require_restore_ready(self.store.db_path, self.settings_store)
                self.store.recover_stale_jobs(self.lease_timeout)
                if not self._account_ready():
                    release()
                    self._stop_event.wait(self.poll)
                    continue
                job = self.store.claim_next(self.worker_id)
                if job is None:
                    release()
                    self._stop_event.wait(self.poll)
                    continue
                pool.submit(run_and_release, job)
        print("[worker] 已停止", flush=True)

    def _request_stop(self) -> None:
        self._stop = True
        self._stop_event.set()

    def _start_stop_watchers(self) -> None:
        """Listen for parent-managed stop signals without needing a console window."""
        if os.name != "nt":
            return
        # On Windows, venv launchers (.venv\Scripts\python.exe) spawn the real
        # interpreter as a child process, so process.pid in the parent equals
        # either our own PID (frozen EXE) or our immediate launcher PID (venv).
        pids = dict.fromkeys((os.getpid(), os.getppid()))
        event_names = [f"Local\\FoilPageWorkerStop-PID-{pid}" for pid in pids if pid > 0]
        if not event_names:
            return

        def _watch_windows_event() -> None:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateEventW.restype = wintypes.HANDLE
            kernel.WaitForMultipleObjects.argtypes = [
                wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
            ]
            kernel.WaitForMultipleObjects.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handles = [
                handle for name in event_names
                if (handle := kernel.CreateEventW(None, True, False, name))
            ]
            if not handles:
                return
            try:
                array = (wintypes.HANDLE * len(handles))(*handles)
                # bWaitAll=False, dwMilliseconds=INFINITE (0xFFFFFFFF)
                kernel.WaitForMultipleObjects(len(handles), array, False, 0xFFFFFFFF)
            finally:
                for handle in handles:
                    kernel.CloseHandle(handle)
            self._request_stop()

        threading.Thread(target=_watch_windows_event, name="worker-stop-event", daemon=True).start()

    def _install_signal_handlers(self) -> None:
        signals = [signal.SIGINT, signal.SIGTERM]
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signals.append(sigbreak)
        for stop_signal in signals:
            signal.signal(stop_signal, lambda *_: self._request_stop())


def _route_agent_logs_to_stdout() -> None:
    """Worker 进程不配置全局日志；保真降级等 agent 诊断行经 stdout 进入桌面端日志。"""
    target = logging.getLogger("shengjian.agent")
    if any(getattr(handler, "_worker_stdout", False) for handler in target.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._worker_stdout = True
    target.addHandler(handler)
    target.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FoilPage 队列 worker")
    parser.add_argument("--db", default=None, help="已存在的 SQLite 数据库；默认沿用应用资料库")
    parser.add_argument("--model", default=None, help="显式覆盖作业 whisper 模型")
    parser.add_argument("--device", default=None, choices=["cpu", "cuda"], help="显式覆盖作业设备")
    parser.add_argument("--language", default=None, help="显式覆盖作业语言")
    parser.add_argument("--model-dir", default="", help="模型缓存根目录（空=HF 默认）")
    parser.add_argument("--settings-dir", default="", help="应用设置目录（验证登录并自动恢复队列）")
    parser.add_argument("--poll", type=float, default=2.0, help="空闲轮询间隔(秒)")
    parser.add_argument("--oneshot", action="store_true", help="只处理一条即退出")
    parser.add_argument("--worker-id", default="worker-0")
    parser.add_argument("--heartbeat", type=float, default=5.0, help="心跳间隔(秒)")
    parser.add_argument("--lease-timeout", type=float, default=300.0, help="租约超时(秒)")
    parser.add_argument("--concurrency", type=int, default=1, help="并发处理作业数（订阅档位：免费1/标准2/高级4）")
    parser.add_argument("--probe-gpu", action="store_true", help="输出 CUDA 能力报告后退出")
    args = parser.parse_args(argv)
    _route_agent_logs_to_stdout()

    if args.probe_gpu:
        from engine.asr import probe_cuda

        print(json.dumps(probe_cuda(), ensure_ascii=False), flush=True)
        return 0

    settings_store = None
    settings: dict[str, object] = {}
    try:
        if args.settings_dir or not args.db:
            settings_store = SettingsStore(base_dir=args.settings_dir) if args.settings_dir else SettingsStore()
        if settings_store is not None:
            # Another process may still be publishing this set. Only the
            # application's exclusive startup can decide to roll it back.
            _require_restore_ready("", settings_store)
            settings = settings_store.load()
            preflight_library(settings_store, settings)
            selected = validate_storage_path(settings["database_path"])
            if args.db and validate_storage_path(Path(args.db).expanduser().resolve()) != selected:
                raise LibraryLocationError("Worker 数据库与应用记录的资料库位置不同，已停止启动")
            args.db = str(selected)
        else:
            _require_restore_ready(args.db)
        try:
            network_policy = service_http.worker_network_policy(os.environ)
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        database = validate_storage_path(Path(args.db).expanduser().resolve())
        if not database.is_file():
            raise LibraryLocationError("原资料库暂不可用，Worker 不会创建空库；请先启动 FoilPage")
        args.db = str(database)
    except (BackupRestoreError, LibraryLocationError, SettingsError, OSError) as exc:
        print(f"[worker] 无法启动: {exc}", file=sys.stderr, flush=True)
        return 2
    store = queue_store.QueueStore(args.db)
    try:
        w = Worker(
            store,
            model=args.model,
            device=args.device,
            language=args.language,
            model_dir=args.model_dir,
            worker_id=args.worker_id,
            poll=args.poll,
            heartbeat_interval=args.heartbeat,
            lease_timeout=args.lease_timeout,
            settings_store=settings_store,
            concurrency=args.concurrency,
            network_policy=network_policy,
        )
        if args.oneshot:
            w.step()
            return 0
        w.run_forever()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
