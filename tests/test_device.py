# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""设备指纹：稳定性、首建与重装语义。"""
from __future__ import annotations

from pathlib import Path

from engine.device import device_id


def test_device_id_is_stable_across_calls(tmp_path: Path):
    first = device_id(tmp_path)
    assert len(first) == 32
    assert device_id(tmp_path) == first, "同一数据目录必须返回同一标识"
    assert (tmp_path / "device.id").is_file()


def test_device_id_differs_per_data_dir(tmp_path: Path):
    a = device_id(tmp_path / "a")
    b = device_id(tmp_path / "b")
    assert a != b, "卸载重装（新数据目录）应产生新标识，服务端按设备数风控"
