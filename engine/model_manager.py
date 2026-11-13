# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Whisper model cache inspection, download, verification and removal."""
from __future__ import annotations

import os
import json
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo_id: str
    size_label: str
    description: str


@dataclass(frozen=True)
class ModelState:
    spec: ModelSpec
    installed: bool
    verified: bool
    path: Optional[str]
    bytes_on_disk: int
    verification_error: str = ""


MODELS = (
    ModelSpec("tiny", "Systran/faster-whisper-tiny", "约 75 MB", "快速验证与短音频"),
    ModelSpec("base", "Systran/faster-whisper-base", "约 145 MB", "日常轻量转写"),
    ModelSpec("small", "Systran/faster-whisper-small", "约 488 MB", "质量与速度平衡"),
    ModelSpec("medium", "Systran/faster-whisper-medium", "约 1.5 GB", "更高中文质量"),
    ModelSpec("large-v3", "Systran/faster-whisper-large-v3", "约 3 GB", "最高质量，耗时较长"),
)


class ModelManagerError(RuntimeError):
    pass


class ModelManager:
    def __init__(self, cache_root: str | None = None, *, endpoint: str = "") -> None:
        configured = cache_root or os.environ.get("HF_HOME")
        self.cache_root = Path(configured or (Path.home() / ".cache" / "huggingface")).resolve()
        # faster-whisper passes an explicit download_root directly as cache_dir;
        # HF_HOME itself instead contains the default hub subdirectory.
        self.hub_cache_root = self.cache_root if cache_root else self.cache_root / "hub"
        self.endpoint = endpoint.strip()

    def list_models(self) -> list[ModelState]:
        return [self.state(spec.name) for spec in MODELS]

    def state(self, name: str) -> ModelState:
        spec = self._spec(name)
        repo_root = self._repo_root(spec)
        snapshot = self._resolve_snapshot(repo_root)
        installed = snapshot is not None
        error = self._verification_error(snapshot) if snapshot else "未下载"
        verified = snapshot is not None and not error
        return ModelState(
            spec=spec,
            installed=installed,
            verified=verified,
            path=str(snapshot) if snapshot else None,
            bytes_on_disk=self._directory_size(repo_root) if repo_root.exists() else 0,
            verification_error=error,
        )

    def download(
        self,
        name: str,
        *,
        progress: Callable[[str, Optional[float]], None] | None = None,
        local_files_only: bool = False,
    ) -> ModelState:
        spec = self._spec(name)
        repo_root = self._repo_root(spec)
        callback = progress or (lambda _message, _value: None)
        callback("准备模型缓存", 0.0)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        try:
            from huggingface_hub import HfApi, hf_hub_download

            api = HfApi(endpoint=self.endpoint) if self.endpoint else HfApi()
            info = api.model_info(spec.repo_id, revision="main")
            revision = info.sha
            if (not isinstance(revision, str) or not revision or len(revision) > 128
                    or any(not (char.isascii() and (char.isalnum() or char in "_-")) for char in revision)):
                raise ValueError("模型版本标识无效")
            snapshot = repo_root / "snapshots" / revision
            repair = snapshot.is_dir() and bool(self._verification_error(snapshot))
            files = [item.rfilename for item in info.siblings]
            total_bytes = sum(
                item.size for item in info.siblings if item.size is not None
            )
            downloaded_bytes = 0
            for index, rfilename in enumerate(files, start=1):
                callback(f"正在下载 {rfilename}（{index}/{len(files)}）", None)
                local_path = hf_hub_download(
                    repo_id=spec.repo_id,
                    filename=rfilename,
                    revision=revision,
                    cache_dir=str(repo_root.parent),
                    force_download=repair,
                    # HF's cache branch leaves an existing Windows file copy
                    # untouched even after downloading its blob again. Repair
                    # this unavailable snapshot via the public local_dir path;
                    # valid older snapshots and their shared blobs stay intact.
                    local_dir=str(snapshot) if repair else None,
                    local_files_only=local_files_only,
                    endpoint=self.endpoint or None,
                )
                try:
                    downloaded_bytes += Path(local_path).stat().st_size
                except OSError:
                    pass
                if total_bytes:
                    callback(
                        f"已下载 {index}/{len(files)} 个文件",
                        min(0.99, downloaded_bytes / total_bytes),
                    )
        except Exception as exc:
            raise ModelManagerError(f"模型 {name} 获取失败: {exc}") from exc
        # Downloads pinned to a commit do not update Hugging Face's refs/main.
        # Verify this attempt before changing the pointer; an older valid cache
        # must never turn a broken new download into a successful upgrade.
        snapshot = repo_root / "snapshots" / revision
        error = self._verification_error(snapshot)
        if error:
            raise ModelManagerError(f"模型 {name} 缓存不完整: {error}")
        self._commit_main_reference(repo_root, revision)
        state = ModelState(spec, True, True, str(snapshot), self._directory_size(repo_root))
        callback("模型已就绪", 1.0)
        return state

    @staticmethod
    def _commit_main_reference(repo_root: Path, revision: str) -> None:
        temporary = None
        try:
            refs = repo_root / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=refs,
                                             prefix=".main-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(revision)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, refs / "main")
        except OSError as exc:
            raise ModelManagerError("模型版本引用保存失败，请重试") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def delete(self, name: str) -> bool:
        repo_root = self._repo_root(self._spec(name))
        if not repo_root.exists():
            return False
        resolved = repo_root.resolve()
        try:
            resolved.relative_to(self.cache_root)
        except ValueError as exc:
            raise ModelManagerError("拒绝删除缓存根目录之外的路径") from exc
        shutil.rmtree(resolved)
        return True

    def delete_all(self) -> int:
        """删除缓存根内所有已安装模型（含不完整缓存），返回删除数量。"""
        removed = 0
        for state in self.list_models():
            if not state.installed:
                continue
            try:
                if self.delete(state.spec.name):
                    removed += 1
            except ModelManagerError:
                continue
        return removed

    def _repo_root(self, spec: ModelSpec) -> Path:
        dirname = "models--" + spec.repo_id.replace("/", "--")
        preferred = self.hub_cache_root / dirname
        legacy = (self.cache_root / "hub" if self.hub_cache_root == self.cache_root else self.cache_root) / dirname
        return preferred if preferred.exists() or not legacy.exists() else legacy

    @staticmethod
    def _verification_error(snapshot: Path) -> str:
        """Inspect local structure without loading or hashing tensor payloads.

        This catches missing files, invalid metadata and truncated serialized
        tensors. Successful runtime loading remains the final compatibility
        check; this is not a claim that every weight byte is authentic.
        """
        try:
            config = snapshot / "config.json"
            if not config.is_file() or config.stat().st_size > 1024 * 1024:
                return "模型配置缺失或损坏，请重新下载"
            if not isinstance(json.loads(config.read_text(encoding="utf-8")), dict):
                return "模型配置损坏，请重新下载"
            ModelManager._check_binary(snapshot / "model.bin")
            tokenizer = snapshot / "tokenizer.json"
            if not tokenizer.is_file() or tokenizer.stat().st_size > 16 * 1024 * 1024:
                return "分词器缺失或损坏，请重新下载"
            parsed = json.loads(tokenizer.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), dict):
                return "分词器损坏，请重新下载"
            from tokenizers import Tokenizer
            Tokenizer.from_file(str(tokenizer))
            vocabulary = snapshot / "vocabulary.json"
            if vocabulary.is_file():
                if vocabulary.stat().st_size > 16 * 1024 * 1024:
                    return "词表损坏，请重新下载"
                words = json.loads(vocabulary.read_text(encoding="utf-8"))
                if not isinstance(words, list) or not words or not all(isinstance(word, str) for word in words):
                    return "词表损坏，请重新下载"
            else:
                vocabulary = snapshot / "vocabulary.txt"
                if not vocabulary.is_file() or not 0 < vocabulary.stat().st_size <= 16 * 1024 * 1024:
                    return "词表缺失或损坏，请重新下载"
                if not vocabulary.read_text(encoding="utf-8").strip():
                    return "词表损坏，请重新下载"
        except (OSError, ValueError, UnicodeError, ImportError, RuntimeError):
            return "缓存不完整或损坏，请重新下载"
        except Exception:
            # tokenizers raises its own exception for malformed tokenizer data.
            return "分词器损坏，请重新下载"
        return ""

    @staticmethod
    def _check_binary(path: Path) -> None:
        """Validate CT2 v2-v6 record boundaries, seeking past tensor bytes.

        The layout follows CTranslate2's model_spec.py/model.cc. Older tensor
        item-size serialization and current float/int quantization are accepted.
        """
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            def read(fmt: str):
                width = struct.calcsize(fmt)
                data = handle.read(width)
                if len(data) != width:
                    raise ValueError("模型权重不完整")
                return struct.unpack(fmt, data)[0]
            def text():
                length = read("<H")
                if not 1 < length <= 65535:
                    raise ValueError("模型权重元数据损坏")
                data = handle.read(length)
                if len(data) != length or data[-1] != 0:
                    raise ValueError("模型权重元数据不完整")
                return data[:-1].decode("utf-8")
            version = read("<I")
            if not 2 <= version <= 6 or text() != "WhisperSpec" or not 1 <= read("<I") <= 3:
                raise ValueError("模型权重格式不支持")
            count = read("<I")
            if not 1 <= count <= 100000:
                raise ValueError("模型权重元数据损坏")
            for _ in range(count):
                text()
                rank = read("<B")
                elements = 1
                for _ in range(rank):
                    elements *= read("<I")
                dtype = read("<B")
                if version >= 4:
                    widths = (4, 1, 2, 4, 2, 2)
                    if dtype >= len(widths):
                        raise ValueError("模型权重类型损坏")
                    length = read("<I")
                    expected = elements * widths[dtype]
                else:
                    if dtype not in (1, 2, 4):
                        raise ValueError("模型权重类型损坏")
                    length = read("<I") * dtype
                    expected = elements * dtype
                if length != expected or handle.tell() + length > size:
                    raise ValueError("模型权重不完整")
                handle.seek(length, os.SEEK_CUR)
            if version >= 3:
                aliases = read("<I")
                if aliases > 100000:
                    raise ValueError("模型权重元数据损坏")
                for _ in range(aliases):
                    text(); text()
            if handle.tell() != size:
                raise ValueError("模型权重元数据损坏")

    @staticmethod
    def _resolve_snapshot(repo_root: Path) -> Optional[Path]:
        ref = repo_root / "refs" / "main"
        if ref.is_file():
            commit = ref.read_text(encoding="utf-8").strip()
            candidate = repo_root / "snapshots" / commit
            if commit and commit not in (".", "..") and "/" not in commit and "\\" not in commit and candidate.is_dir():
                return candidate
        snapshots = repo_root / "snapshots"
        if snapshots.is_dir():
            choices = sorted(
                (path for path in snapshots.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if choices:
                return choices[0]
        return None

    @staticmethod
    def _directory_size(root: Path) -> int:
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        return total

    @staticmethod
    def _spec(name: str) -> ModelSpec:
        for spec in MODELS:
            if spec.name == name:
                return spec
        raise ModelManagerError(f"不支持的模型: {name}")
