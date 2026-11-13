# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""运行时数据校验自测（P1 物证）。不依赖 torch/faster-whisper。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.contracts import EngineError  # noqa: E402
from engine import validate  # noqa: E402


def test_validate_transcription_result_ok() -> None:
    r = {
        "text": "这是转写结果",
        "language": "zh",
        "language_probability": 0.98,
        "duration_sec": 12.5,
        "avg_logprob": -0.4,
        "no_speech_prob": 0.01,
        "backend": "local",
        "model": "tiny",
    }
    validate.validate_transcription_result(r)
    print("[OK] validate_transcription_result 通过合法样本")


def test_validate_transcription_result_empty_text() -> None:
    r = {
        "text": "   ",
        "language": "zh",
        "language_probability": 0.98,
        "duration_sec": 12.5,
        "avg_logprob": None,
        "no_speech_prob": None,
        "backend": "local",
        "model": "tiny",
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError:
        print("[OK] validate_transcription_result 拦截空 text")
    else:
        raise AssertionError("空 text 未被拦截")


def test_validate_job_illegal_status() -> None:
    j = {
        "id": "job-1", "source_path": "x.mp4", "output_dir": "out",
        "status": "weird", "whisper_model": "tiny", "device": "cpu",
        "language": "zh", "content_mode": "raw",
        "created_at": "t", "updated_at": "t",
        "error": None, "raw_path": None, "final_path": None,
    }
    try:
        validate.validate_job(j)
    except validate.ContractValidationError as e:
        assert "status" in str(e)
        print("[OK] validate_job 拦截非法 status")
    else:
        raise AssertionError("非法 status 未被拦截")


def test_validate_manifest_ok() -> None:
    m = {
        "job_id": "job-1", "source_path": "x.mp4", "source_sha256": None,
        "backend": "local", "model": "tiny", "language": "zh",
        "duration_sec": 12.5, "raw_path": "out/x_raw.md", "final_path": "out/x.md",
        "content_mode": "raw", "prompt_version": "v1", "created_at": "t",
    }
    validate.validate_manifest(m)
    print("[OK] validate_manifest 通过合法样本")


def test_assert_error_picklable() -> None:
    validate.assert_error_picklable(EngineError("no_speech", "无语音"))
    print("[OK] assert_error_picklable 接受可 pickle 的 EngineError")


def test_coerce_job_fills_optional() -> None:
    partial = {
        "id": "job-2", "source_path": "y.mp4", "output_dir": "out",
        "status": "queued", "whisper_model": "tiny", "device": "cpu",
        "language": "zh", "content_mode": "raw",
        "created_at": "t", "updated_at": "t",
    }
    full = validate.coerce_job(partial)
    assert full["error"] is None
    assert full["raw_path"] is None
    assert full["final_path"] is None
    assert full["tags"] == []
    print("[OK] coerce_job 补齐可选字段默认值")


def test_coerce_tags_normalizes_and_dedupes() -> None:
    assert validate.coerce_tags(None) == []
    assert validate.coerce_tags(["a", " a ", "b", "a", "  "]) == ["a", "b"]
    print("[OK] coerce_tags 去空、去重、去首尾空白")


def test_coerce_tags_rejects_invalid() -> None:
    for bad in ("not-list", [123], ["x" * 25], [f"t{i}" for i in range(21)]):
        try:
            validate.coerce_tags(bad)
        except validate.ContractValidationError:
            continue
        raise AssertionError(f"非法 tags 未被拦截: {bad!r}")
    print("[OK] coerce_tags 拦截非列表/非字符串/超长/超量")


def test_validate_job_rejects_bad_tags() -> None:
    j = {
        "id": "job-3", "source_path": "x.mp4", "output_dir": "out",
        "status": "queued", "whisper_model": "tiny", "device": "cpu",
        "language": "zh", "content_mode": "raw",
        "created_at": "t", "updated_at": "t",
        "error": None, "raw_path": None, "final_path": None,
        "tags": "not-a-list",
    }
    try:
        validate.validate_job(j)
    except validate.ContractValidationError as e:
        assert "tags" in str(e)
        print("[OK] validate_job 拦截非列表 tags")
    else:
        raise AssertionError("非列表 tags 未被拦截")


def test_transcription_result_missing_duration_is_caught() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "local",
        "language_probability": 0.9,
        # duration_sec 故意缺失
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "duration_sec" in str(e)
        print("[OK] 缺失 duration_sec 被拦截（非裸 KeyError）")
    else:
        raise AssertionError("缺失 duration_sec 未被拦截")


def test_transcription_result_illegal_backend() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "bogus",
        "duration_sec": 1.0, "language_probability": 0.9,
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "backend" in str(e)
        print("[OK] 非法 backend 被拦截")
    else:
        raise AssertionError("非法 backend 未被拦截")


def test_transcription_result_probability_out_of_range() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "local",
        "duration_sec": 1.0, "language_probability": 9,
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "language_probability" in str(e)
        print("[OK] language_probability=9 越界被拦截")
    else:
        raise AssertionError("language_probability 越界未被拦截")


def test_transcription_result_no_speech_prob_wrong_type() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "local",
        "duration_sec": 1.0, "language_probability": 0.9,
        "no_speech_prob": "high",
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "no_speech_prob" in str(e)
        print("[OK] no_speech_prob 类型错误被拦截")
    else:
        raise AssertionError("no_speech_prob 类型错误未被拦截")


def test_transcription_result_segments_invalid_time() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "local",
        "duration_sec": 1.0, "language_probability": 0.9,
        "segments": [{"start": 3.0, "end": 2.0, "text": "bad"}],
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "segments[0]" in str(e)
        print("[OK] segments 时间轴非法被拦截")
    else:
        raise AssertionError("segments 时间轴非法未被拦截")


def test_transcription_result_segments_blank_text_rejected() -> None:
    r = {
        "text": "x", "language": "zh", "model": "tiny", "backend": "local",
        "duration_sec": 1.0, "language_probability": 0.9,
        "segments": [{"start": 0.0, "end": 1.0, "text": "   "}],
    }
    try:
        validate.validate_transcription_result(r)
    except validate.ContractValidationError as e:
        assert "segments[0]" in str(e)
        print("[OK] segments 空文本被拦截")
    else:
        raise AssertionError("segments 空文本未被拦截")


def test_job_empty_required_string() -> None:
    j = {
        "id": "", "source_path": "y.mp4", "output_dir": "out",
        "status": "queued", "whisper_model": "tiny", "device": "cpu",
        "language": "zh", "content_mode": "raw",
        "created_at": "t", "updated_at": "t",
        "error": None, "raw_path": None, "final_path": None,
    }
    try:
        validate.validate_job(j)
    except validate.ContractValidationError as e:
        assert "id" in str(e)
        print("[OK] 空 Job.id 被拦截")
    else:
        raise AssertionError("空 Job.id 未被拦截")


def test_manifest_missing_prompt_version() -> None:
    m = {
        "job_id": "j", "source_path": "x.mp4",
        "source_sha256": None, "backend": "local", "model": "tiny",
        "language": "zh", "duration_sec": 1.0,
        "raw_path": "o/x_raw.md", "final_path": "o/x.md",
        "content_mode": "raw", "created_at": "t",
        # prompt_version 故意缺失
    }
    try:
        validate.validate_manifest(m)
    except validate.ContractValidationError as e:
        assert "prompt_version" in str(e)
        print("[OK] 缺失 prompt_version 被拦截")
    else:
        raise AssertionError("缺失 prompt_version 未被拦截")


if __name__ == "__main__":
    test_validate_transcription_result_ok()
    test_validate_transcription_result_empty_text()
    test_validate_job_illegal_status()
    test_validate_manifest_ok()
    test_assert_error_picklable()
    test_coerce_job_fills_optional()
    test_coerce_tags_normalizes_and_dedupes()
    test_coerce_tags_rejects_invalid()
    test_validate_job_rejects_bad_tags()
    test_transcription_result_missing_duration_is_caught()
    test_transcription_result_illegal_backend()
    test_transcription_result_probability_out_of_range()
    test_transcription_result_no_speech_prob_wrong_type()
    test_job_empty_required_string()
    test_manifest_missing_prompt_version()
    print("\n全部校验自测通过 ✅")
