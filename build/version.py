# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Synchronize release markers from the bridge version; never edit historical tags."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"


def synchronize(root: Path, version: str | None = None, channel: str | None = None,
                *, write: bool = False) -> list[str]:
    bridge = (root / "app/bridge.py").read_text(encoding="utf-8")
    current = re.search(r'^APP_VERSION = "([^"]+)"$', bridge, re.M)
    current_channel = re.search(r'^RELEASE_CHANNEL = "([^"]+)"$', bridge, re.M)
    if not current or not current_channel:
        raise ValueError("Missing authoritative version/channel")
    version = version or current[1]
    channel = channel or current_channel[1]
    if not re.fullmatch(VERSION_PATTERN, version) or any(int(n) > 65535 for n in version.split(".")):
        raise ValueError("Version must be three Windows-compatible integers")
    if channel not in {"formal", "test"}:
        raise ValueError("Channel must be formal or test")
    dotted = version + ".0"
    native = ", ".join(version.split(".")) + ", 0"
    rules = {
        "app/bridge.py": [(r'^APP_VERSION = "[^"]+"$', f'APP_VERSION = "{version}"'),
                          (r'^RELEASE_CHANNEL = "[^"]+"$', f'RELEASE_CHANNEL = "{channel}"')],
        "app/webview_ui/js/app.js": [(r'window.APP_VERSION = "[^"]+";', f'window.APP_VERSION = "{version}";')],
        "app/webview_ui/js/settings.js": [(r'window.APP_VERSION \|\| "[^"]+"', f'window.APP_VERSION || "{version}"')],
        "app/webview_ui/index.html": [(r'(id="brandVer">)v[^<]+', rf'\g<1>v{version}')],
        "browser-extension/manifest.json": [(r'"version": "[^"]+"', f'"version": "{version}"')],
        "browser-extension/content.js": [(r'EXTENSION_VERSION = "[^"]+"', f'EXTENSION_VERSION = "{version}"')],
        "build/installer.iss": [(r'^#define AppVersion "[^"]+"$', f'#define AppVersion "{version}"'),
                                (r'^VersionInfoVersion=.+$', f'VersionInfoVersion={dotted}')],
    }
    for name in ("content-main-hook.js", "content-main.js"):
        rules[f"browser-extension/{name}"] = [(r'MAIN_MARK_VERSION = "[^"]+"', f'MAIN_MARK_VERSION = "{version}"')]
    for role in ("desktop", "worker"):
        rules[f"build/version_info_{role}.txt"] = [
            (r'filevers=\([\d, ]+\)', f'filevers=({native})'),
            (r'prodvers=\([\d, ]+\)', f'prodvers=({native})'),
            (r"'FileVersion', '[^']+'", f"'FileVersion', '{dotted}'"),
            (r"'ProductVersion', '[^']+'", f"'ProductVersion', '{dotted}'"),
        ]
    pending = {}
    for relative, replacements in rules.items():
        source = (root / relative).read_text(encoding="utf-8")
        updated = source
        for pattern, replacement in replacements:
            updated, count = re.subn(pattern, replacement, updated, flags=re.M)
            if count != 1:
                raise ValueError(f"Expected exactly one marker in {relative}: {pattern}")
        if updated != source:
            pending[relative] = updated
    if pending and not write:
        raise ValueError("Version markers drifted: " + ", ".join(pending))
    for relative, updated in pending.items():
        (root / relative).write_text(updated, encoding="utf-8", newline="\n")
    return list(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--set", dest="version")
    parser.add_argument("--channel", choices=("formal", "test"))
    args = parser.parse_args()
    if args.check and (args.version or args.channel):
        parser.error("--check cannot modify markers")
    try:
        changed = synchronize(ROOT, args.version, args.channel, write=bool(args.version or args.channel))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print("Version markers consistent" + (f"; updated {len(changed)} files" if changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
