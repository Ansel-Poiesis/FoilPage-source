# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""生成任务终态提示音预设组（1.3.12 自定义提示音）。

纯合成（正弦 + 谐波 + 指数衰减包络），无第三方版权负担；输出
assets/sounds/<pack>/{success,error}.wav，22050 Hz / 16-bit / 单声道，
单个文件约 6-12 KB。重新生成：

    python -X utf8 build/generate_sounds.py

音色设计：
- crisp 清脆：高音区短促双音，成功上扬（E6→A6），失败下坠（B4→E4）
- soft  柔和：中音区慢包络纯音，成功 A5→C#6，失败 F5→C5
- deep  低沉：低音区 + 倍低频衬底，成功 G4→D5，失败 E3→B2
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 22050

# (pack, kind) -> [(频率 Hz, 时长 s, 谐波系数列表[(倍频, 幅度)], 衰减 tau, 攻击 ms), ...]
SOUNDS: dict[tuple[str, str], list[dict]] = {
    ("crisp", "success"): [
        dict(freq=1318.51, dur=0.09, harmonics=[(1.0, 1.0), (2.0, 0.35)], tau=0.045, attack_ms=4),
        dict(freq=1760.00, dur=0.16, harmonics=[(1.0, 1.0), (2.0, 0.35)], tau=0.060, attack_ms=3),
    ],
    ("crisp", "error"): [
        dict(freq=493.88, dur=0.10, harmonics=[(1.0, 1.0), (3.0, 0.45)], tau=0.050, attack_ms=4),
        dict(freq=329.63, dur=0.18, harmonics=[(1.0, 1.0), (3.0, 0.45)], tau=0.070, attack_ms=3),
    ],
    ("soft", "success"): [
        dict(freq=880.00, dur=0.16, harmonics=[(1.0, 1.0), (2.0, 0.12)], tau=0.100, attack_ms=14),
        dict(freq=1108.73, dur=0.24, harmonics=[(1.0, 1.0), (2.0, 0.12)], tau=0.130, attack_ms=10),
    ],
    ("soft", "error"): [
        dict(freq=698.46, dur=0.15, harmonics=[(1.0, 1.0)], tau=0.100, attack_ms=14),
        dict(freq=523.25, dur=0.22, harmonics=[(1.0, 1.0)], tau=0.120, attack_ms=10),
    ],
    ("deep", "success"): [
        dict(freq=392.00, dur=0.15, harmonics=[(1.0, 1.0), (0.5, 0.40), (2.0, 0.18)], tau=0.090, attack_ms=10),
        dict(freq=587.33, dur=0.22, harmonics=[(1.0, 1.0), (0.5, 0.40), (2.0, 0.18)], tau=0.110, attack_ms=8),
    ],
    ("deep", "error"): [
        dict(freq=164.81, dur=0.17, harmonics=[(1.0, 1.0), (2.0, 0.22)], tau=0.100, attack_ms=10),
        dict(freq=123.47, dur=0.26, harmonics=[(1.0, 1.0), (2.0, 0.22)], tau=0.130, attack_ms=8),
    ],
}


def _render_part(spec: dict) -> list[float]:
    total = int(RATE * spec["dur"])
    attack = max(1, int(RATE * spec["attack_ms"] / 1000))
    samples: list[float] = []
    for i in range(total):
        t = i / RATE
        envelope = (1.0 - math.exp(-t / spec["tau"])) * math.exp(-t / spec["tau"]) * math.exp(1.0)
        if i < attack:
            envelope *= i / attack
        value = 0.0
        for multiple, amplitude in spec["harmonics"]:
            value += amplitude * math.sin(2 * math.pi * spec["freq"] * multiple * t)
        samples.append(0.42 * envelope * value)
    return samples


def _write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    peak = max(abs(v) for v in samples) or 1.0
    scale = 0.92 / peak
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(
            struct.pack("<h", int(max(-32768, min(32767, round(v * scale * 32767)))))
            for v in samples
        ))


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "assets" / "sounds"
    for (pack, kind), parts in SOUNDS.items():
        samples: list[float] = []
        for part in parts:
            samples.extend(_render_part(part))
        target = root / pack / f"{kind}.wav"
        _write_wav(target, samples)
        print(f"{target.relative_to(root.parent.parent)}  {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
