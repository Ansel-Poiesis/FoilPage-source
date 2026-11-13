# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""ASR segment aggregation and cooperative cancellation tests."""
from __future__ import annotations

import threading
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from engine.asr import load_whisper_model, probe_cuda, transcribe_audio
from engine.contracts import EngineError
from model_cache_fixture import create_model_cache


def _segment(text: str, end: float = 0.0, start: float = 0.0):
    return SimpleNamespace(
        text=text, start=start, end=end, avg_logprob=-0.2, no_speech_prob=0.01,
    )


def _info(language: str = "en"):
    return SimpleNamespace(language=language, language_probability=0.98, duration=4.0)


def test_transcribe_aggregates_segments_without_batch_wrapper() -> None:
    model = SimpleNamespace(
        transcribe=lambda *_args, **_kwargs: (iter([_segment("hello"), _segment("world")]), _info())
    )
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="en")
    assert result["text"] == "hello world"
    assert result["model"] == "tiny"
    assert result["avg_logprob"] == pytest.approx(-0.2)
    assert result["segments"] == [
        {"start": 0.0, "end": 0.0, "text": "hello"},
        {"start": 0.0, "end": 0.0, "text": "world"},
    ]


def test_transcribe_preserves_segment_timestamps_for_subtitles() -> None:
    model = SimpleNamespace(
        transcribe=lambda *_args, **_kwargs: (
            iter([
                _segment(" 你好 ", end=1.5, start=0.2),
                _segment("世界", end=3.2, start=2.0),
                _segment("   ", end=4.0, start=3.5),
            ]),
            _info(language="zh"),
        )
    )
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert result["segments"] == [
        {"start": 0.2, "end": 1.5, "text": "你好"},
        {"start": 2.0, "end": 3.2, "text": "世界"},
    ]


def test_transcribe_reports_segment_time_progress() -> None:
    model = SimpleNamespace(
        transcribe=lambda *_args, **_kwargs: (
            iter([_segment("hello", 1.0), _segment("world", 3.0)]), _info()
        )
    )
    values: list[float] = []
    transcribe_audio(
        "sample.wav", model, model_name="tiny", language="en",
        progress=values.append,
    )
    assert values == [0.25, 0.75, 1.0]


def test_transcribe_cancels_while_consuming_segments() -> None:
    cancelled = threading.Event()

    def segments():
        yield _segment("first")
        cancelled.set()
        yield _segment("second")

    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (segments(), _info()))
    with pytest.raises(EngineError) as raised:
        transcribe_audio(
            "sample.wav", model, model_name="tiny", language="en",
            cancel_check=cancelled.is_set,
        )
    assert raised.value.code == "cancelled"


def test_cuda_model_retries_with_lower_vram_compute_type(tmp_path) -> None:
    create_model_cache(tmp_path)
    calls: list[str] = []

    class FakeWhisperModel:
        def __init__(self, *_args, compute_type: str, **_kwargs):
            calls.append(compute_type)
            if compute_type == "float16":
                raise RuntimeError("out of memory")

    modules = {
        "faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel),
        "ctranslate2": SimpleNamespace(
            get_supported_compute_types=lambda _device: {"float16", "int8_float16"}
        ),
    }
    with mock.patch.dict(sys.modules, modules):
        loaded, device = load_whisper_model("tiny", "cuda", str(tmp_path))
    assert isinstance(loaded, FakeWhisperModel)
    assert device == "cuda"
    assert calls == ["float16", "int8_float16"]


def test_load_whisper_model_uses_local_files_only(tmp_path) -> None:
    """模型只读本地缓存：未下载时不静默联网拉取。"""
    captured: dict = {}
    snapshot = create_model_cache(tmp_path)

    class FakeWhisperModel:
        def __init__(self, *_args, **_kwargs):
            captured.update(_kwargs)
            captured["path"] = _args[0]

    modules = {
        "faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel),
    }
    with mock.patch.dict(sys.modules, modules):
        load_whisper_model("tiny", "cpu", str(tmp_path))
    assert captured["local_files_only"] is True
    assert captured["path"] == str(snapshot)


def test_load_whisper_model_missing_cache_gives_download_guidance(tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self, *_args, **_kwargs):
            raise FileNotFoundError("Model not found in cache (local_files_only)")

    modules = {
        "faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel),
    }
    with (
        mock.patch.dict(sys.modules, modules),
        pytest.raises(EngineError) as raised,
    ):
        load_whisper_model("tiny", "cpu", str(tmp_path))
    assert raised.value.code == "model_missing"
    assert "设置" in raised.value.message


def test_cuda_probe_is_serializable_when_backend_fails() -> None:
    broken = SimpleNamespace(get_cuda_device_count=mock.Mock(side_effect=OSError("driver")))
    with (
        mock.patch.dict(sys.modules, {"ctranslate2": broken}),
        mock.patch("engine.dll.add_runtime_dll_dirs", return_value=["C:/cuda"]),
    ):
        report = probe_cuda()
    assert report["available"] is False
    assert report["dll_dirs"] == ["C:/cuda"]
    assert "driver" in report["error"]


# ------------------------------------------------------------- 升级维护改造

def _capturing_model(**transcribe_result):
    """Fake faster-whisper model that records the kwargs it was called with."""
    calls: list[dict] = []

    def transcribe(_audio, **kwargs):
        calls.append(kwargs)
        return transcribe_result.get(
            "segments", iter([_segment("内容", 1.0)])
        ), transcribe_result.get("info", _info(language="zh"))

    model = SimpleNamespace(transcribe=transcribe)
    model.calls = calls
    return model


def test_default_zh_prompt_is_neutral_and_hotwords_forwarded() -> None:
    model = _capturing_model()
    transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    prompt = model.calls[0]["initial_prompt"]
    # 内置默认提示只做标点引导，不得夹带话题偏向（任何领域的视频都可能出现）。
    assert "标点符号" in prompt
    assert "产品评测" not in prompt and "生活分享" not in prompt

    model = _capturing_model()
    transcribe_audio(
        "sample.wav", model, model_name="tiny", language="zh",
        initial_prompt="自定义领域提示", hotwords="GLM Deno CTranslate2",
    )
    assert model.calls[0]["initial_prompt"] == "自定义领域提示"
    assert model.calls[0]["hotwords"] == "GLM Deno CTranslate2"


def test_empty_prompt_disables_prompt_and_non_zl_default_has_no_prompt() -> None:
    model = _capturing_model()
    transcribe_audio("sample.wav", model, model_name="tiny", language="zh", initial_prompt="")
    assert model.calls[0]["initial_prompt"] is None

    model = _capturing_model()
    transcribe_audio("sample.wav", model, model_name="tiny", language="en")
    assert model.calls[0]["initial_prompt"] is None


def test_batched_inference_is_used_and_falls_back_before_first_segment() -> None:
    import types as _types

    model = _capturing_model()

    received: list[dict] = []

    class FakeBatched:
        def __init__(self, model):
            self.inner = model

        def transcribe(self, audio, **kwargs):
            received.append(dict(kwargs))
            if "batch_size" in kwargs:
                # 模拟批量分块在产出任何分段前失败（显存不足/分块不兼容）。
                raise RuntimeError("CUDA out of memory during chunking")
            return self.inner.transcribe(audio, **kwargs)

    fake_module = _types.ModuleType("faster_whisper")
    fake_module.BatchedInferencePipeline = FakeBatched
    with mock.patch.dict(sys.modules, {"faster_whisper": fake_module}):
        result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh", use_batched=True)

    assert result["text"] == "内容"
    # 批量轨调用带 batch_size；回退的逐段轨调用不带，且不再二次回退。
    assert "batch_size" in received[0]
    assert len(model.calls) == 1
    assert "batch_size" not in model.calls[0]


def test_batched_failure_after_segments_produced_does_not_replay() -> None:
    import types as _types

    class FakeBatched:
        def __init__(self, model):
            self.inner = model

        def transcribe(self, audio, **kwargs):
            def _gen():
                yield _segment("前半", 1.0)
                raise RuntimeError("device lost mid-batch")
            return _gen(), _info(language="zh")

    fake_module = _types.ModuleType("faster_whisper")
    fake_module.BatchedInferencePipeline = FakeBatched
    model = SimpleNamespace(
        transcribe=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not replay"))
    )
    with mock.patch.dict(sys.modules, {"faster_whisper": fake_module}):
        with pytest.raises(RuntimeError, match="device lost"):
            transcribe_audio("sample.wav", model, model_name="tiny", language="zh", use_batched=True)
