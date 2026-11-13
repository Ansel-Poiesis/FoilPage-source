# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""yt-dlp 版本漂移检查（依赖升级节拍的工具侧）。

yt-dlp 的本质是与平台反爬的持续对抗：锁死旧版本不会立即坏，但会随平台
改版逐渐失效。本脚本让「检查 → 升级 → 冒烟验证 → 提交」成为每月例行运维：

  python build/check_ytdlp_update.py           # 退出码 0=最新 1=有新版本 2=检查失败
  python build/check_ytdlp_update.py --json    # 机器可读输出（CI 用）
  python build/check_ytdlp_update.py --apply   # 把 pins 升到最新（requirements 两处同步改）

GitHub Actions 每月自动运行本脚本（.github/workflows/ytdlp-upgrade-check.yml），
发现新版本时自动开 issue 提醒。本地手动升级后的验证步骤见 README「依赖升级节拍」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS = _ROOT / "requirements-base.txt"
_LOCK = _ROOT / "requirements-base.lock.txt"
# extra 标记（如 yt-dlp[default]==x.y.z）是发布能力的一部分，--apply 时原样保留。
# tail 容忍 uv --generate-hashes 锁的行尾 " \" 续行符（与 test_ytdlp_pin 同口径）；
# --apply 重写时保留 tail，避免把锁文件的续行符吃掉。
_PIN_RE = re.compile(
    r"^yt-dlp(?P<extra>\[[a-z,\-]+\])?==(?P<ver>\d+\.\d+\.\d+)(?P<tail>\s*(?:\\\s*)?(?:#.*)?)$",
    re.M,
)

# PyPI 在部分网络环境被拦截（见 requirements-base.txt 头注释），依次回退。
_SOURCES = (
    ("pypi", "https://pypi.org/pypi/yt-dlp/json"),
    ("github", "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"),
)


def read_pinned_version() -> str:
    """读当前 pin；requirements-base.txt 与 lock 不一致时视为配置错误。

    extra 标记只要求 requirements-base.txt 携带（发布能力声明）；uv pip
    compile 生成的 lock 按规范不写 extra——其能力以解析出的子包（如
    yt-dlp-ejs）物化，逐行比对 extra 会永久误报。
    """
    base_match = _PIN_RE.search(_REQUIREMENTS.read_text(encoding="utf-8"))
    lock_match = _PIN_RE.search(_LOCK.read_text(encoding="utf-8"))
    if not base_match or not lock_match:
        raise RuntimeError("requirements 文件中找不到 yt-dlp pin")
    if base_match.group("ver") != lock_match.group("ver"):
        raise RuntimeError(
            f"pin 版本不一致: requirements-base.txt={base_match.group('ver')} "
            f"lock={lock_match.group('ver')}；请先手工对齐"
        )
    return base_match.group("ver")


def fetch_latest_version() -> tuple[str, str]:
    """返回 (版本号, 来源)。所有源都失败时抛出最后一个错误。"""
    last_error: Exception | None = None
    for name, url in _SOURCES:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "shengjian-ytdlp-check", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if name == "pypi":
                return str(payload["info"]["version"]), name
            tag = str(payload.get("tag_name") or "")
            return tag.lstrip("v"), name
        except Exception as exc:  # noqa: BLE001 - 逐源回退
            last_error = exc
    raise RuntimeError(f"所有版本源均不可达: {last_error}")


def apply_pin(version: str) -> list[Path]:
    """把两个 requirements 文件的 pin 同步改为 version，保留 extra 标记。"""
    changed: list[Path] = []
    for path in (_REQUIREMENTS, _LOCK):
        text = path.read_text(encoding="utf-8")
        updated, count = _PIN_RE.subn(
            lambda m: f"yt-dlp{m.group('extra') or ''}=={version}{m.group('tail')}", text, count=1,
        )
        if count != 1:
            raise RuntimeError(f"{path.name} 中未找到唯一 yt-dlp pin")
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed


def _version_key(version: str) -> tuple[int, ...]:
    """数值化版本段：GitHub tag 是 2026.08.19、PyPI 是 2026.8.19，同版异形。"""
    return tuple(int(part) for part in version.split("."))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 yt-dlp 是否有新版本")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    parser.add_argument("--apply", action="store_true", help="把 pins 同步更新到最新版本")
    args = parser.parse_args(argv)

    try:
        pinned = read_pinned_version()
        latest, source = fetch_latest_version()
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"[检查失败] {exc}", file=sys.stderr)
        return 2

    outcome = {
        "pinned": pinned,
        "latest": latest,
        "source": source,
        "update_available": _version_key(pinned) != _version_key(latest),
    }
    if args.json:
        print(json.dumps(outcome))
        return 1 if outcome["update_available"] else 0

    if _version_key(pinned) == _version_key(latest):
        print(f"[最新] yt-dlp pin 已是最新版本 {pinned}（来源 {source}）")
        return 0
    print(f"[可升级] yt-dlp pin={pinned} → 最新 {latest}（来源 {source}）")
    print("  升级步骤：")
    print(f"    1. python build/check_ytdlp_update.py --apply   # 写入 {latest}")
    print("    2. pip install -r requirements-base.txt -i https://mirrors.aliyun.com/pypi/simple/")
    print("    3. python -m pytest tests/ -k 'download or ytdlp'    # 本地回归")
    print("    4. SHENGJIAN_SMOKE=1 python -m pytest tests/test_ytdlp_smoke.py  # 平台冒烟（需网络）")
    if args.apply:
        changed = apply_pin(latest)
        print(f"[已写入] {', '.join(path.name for path in changed)} → {latest}")
        print("  记得执行 pip install 并跑冒烟测试后再提交。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
