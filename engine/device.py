# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""设备指纹：稳定设备标识，用于许可证绑定与防一号多机滥用。

标识是数据目录下的一次性随机 uuid 文件：跨重启稳定、卸载重装即换新，
不采集硬件序列号等可追溯信息。服务端签发许可证时把 device_id 写入
payload，客户端验证时核对（engine.license.verify_license）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

DEVICE_ID_FILE = "device.id"


def device_id(data_dir: str | Path) -> str:
    """返回数据目录对应的设备标识；首次调用时生成并落盘。"""
    path = Path(data_dir) / DEVICE_ID_FILE
    existing = ""
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
    if existing:
        return existing
    value = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value
