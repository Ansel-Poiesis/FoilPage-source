# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""无效转写检测（1.3.12）：拦截"非空但无效"的转写。

硬拦截在 asr.transcribe_audio 内：过短/密度过低（no_speech）、幻听循环
（repetitive_hallucination）、疑似非语音（suspected_noise）。阈值保守，
真实语音（含高重复内容、带单段坏置信度的长音频）不得被误杀。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.asr import low_confidence_warning, transcribe_audio
from engine.contracts import EngineError


def _segment(text: str, end: float = 0.0, start: float = 0.0,
             avg_logprob: float = -0.2, no_speech_prob: float = 0.01):
    return SimpleNamespace(
        text=text, start=start, end=end,
        avg_logprob=avg_logprob, no_speech_prob=no_speech_prob,
    )


def _info(language: str = "zh", duration: float = 300.0):
    return SimpleNamespace(
        language=language, language_probability=0.98, duration=duration
    )


def _model(segments, info=None):
    return SimpleNamespace(
        transcribe=lambda *_a, **_k: (iter(segments), info or _info())
    )


def test_empty_transcription_still_raises_no_speech() -> None:
    model = _model([_segment("   "), _segment(" ")])
    with pytest.raises(EngineError) as raised:
        transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert raised.value.code == "no_speech"


def test_short_clip_with_single_word_passes() -> None:
    """<10s 的短音频哪怕只有一两个字也放行（如口令、单句应答）。"""
    model = _model([_segment("好。", 8.0)], _info(duration=8.0))
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert result["text"] == "好。"


def test_tiny_text_on_normal_clip_raises_no_speech() -> None:
    model = _model([_segment("嗯。", 20.0)], _info(duration=20.0))
    with pytest.raises(EngineError) as raised:
        transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert raised.value.code == "no_speech"
    assert "有效字符" in raised.value.message


def test_low_density_long_audio_raises_no_speech() -> None:
    """5 分钟音频只有 20 个字：1 字/15 秒，判定无对白。"""
    model = _model(
        [_segment("谢谢观看。", 300.0)], _info(duration=300.0)
    )
    with pytest.raises(EngineError) as raised:
        transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert raised.value.code == "no_speech"
    assert "分钟" in raised.value.message


def test_normal_speech_density_passes() -> None:
    segments = [_segment(f"这是第{i}段正常的语音内容，用于密度检查。", 5.0 * (i + 1))
                for i in range(10)]
    model = _model(segments)
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert result["no_speech_prob_mean"] == pytest.approx(0.01)


def test_hallucination_loop_with_poor_confidence_raises() -> None:
    """纯音乐幻听循环：同一句占绝对多数 + 置信度差。"""
    segments = [_segment("谢谢观看请订阅频道", 5.0 * (i + 1), avg_logprob=-0.9)
                for i in range(10)]
    model = _model(segments)
    with pytest.raises(EngineError) as raised:
        transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert raised.value.code == "repetitive_hallucination"


def test_repetitive_real_speech_with_good_confidence_passes() -> None:
    """真实的高重复语音（口号/跟读）：置信度正常，必须放行。"""
    segments = [_segment("祝大家新年快乐万事如意", 5.0 * (i + 1))
                for i in range(10)]
    model = _model(segments)
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert "祝大家" in result["text"]


def test_extreme_bad_confidence_raises_suspected_noise() -> None:
    segments = [_segment(f"环境音片段内容{index}", 5.0 * (index + 1), avg_logprob=-1.3)
                for index in range(10)]
    model = _model(segments)
    with pytest.raises(EngineError) as raised:
        transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    assert raised.value.code == "suspected_noise"


def test_single_bad_segment_does_not_fail_long_audio() -> None:
    """长音频里单个坏段（max 高）不算非语音：门用段均值，不用最大值。"""
    segments = [
        _segment("这是一段完全正常的语音内容用于测试。", 30.0,
                 no_speech_prob=0.05)
        for _ in range(9)
    ] + [_segment("间奏音乐。", 300.0, no_speech_prob=0.95)]
    model = _model(segments)
    result = transcribe_audio("sample.wav", model, model_name="tiny", language="zh")
    # 最大值 0.95 超过警告线：非阻断警示应为 True。
    assert result["no_speech_prob"] == pytest.approx(0.95)
    assert low_confidence_warning(result["avg_logprob"], result["no_speech_prob"]) is True


def test_low_confidence_warning_thresholds() -> None:
    assert low_confidence_warning(-1.0, None) is True
    assert low_confidence_warning(None, 0.7) is True
    assert low_confidence_warning(-0.5, 0.2) is False
    assert low_confidence_warning(None, None) is False


def test_run_engine_persists_asr_metrics_into_manifest(tmp_path) -> None:
    """置信度指标随 manifest 持久化；低置信度落非阻断警示标记。"""
    from unittest import mock

    from app import run_engine

    source = tmp_path / "sample.wav"
    source.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    result = {
        "text": "你好世界",
        "language": "zh",
        "language_probability": 0.99,
        "duration_sec": 2.0,
        "avg_logprob": -0.95,
        "no_speech_prob": 0.01,
        "backend": "local",
        "model": "tiny",
    }
    with mock.patch.object(
        run_engine,
        "_transcribe_with_device_fallback",
        return_value=(result, "cpu"),
    ):
        manifest = run_engine.run(
            str(source), str(tmp_path / "out"), model="tiny",
            device="cpu", language="zh", model_dir="",
        )
    assert manifest["asr_avg_logprob"] == pytest.approx(-0.95)
    assert manifest["asr_no_speech_prob"] == pytest.approx(0.01)
    assert manifest["asr_low_confidence"] is True

    result_good = dict(result, avg_logprob=-0.3)
    with mock.patch.object(
        run_engine,
        "_transcribe_with_device_fallback",
        return_value=(result_good, "cpu"),
    ):
        manifest = run_engine.run(
            str(source), str(tmp_path / "out2"), model="tiny",
            device="cpu", language="zh", model_dir="",
        )
    assert manifest["asr_avg_logprob"] == pytest.approx(-0.3)
    assert manifest["asr_low_confidence"] is False
