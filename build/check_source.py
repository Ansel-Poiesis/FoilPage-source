# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Check the tracked public source distribution without reading user settings."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.css', '.html', '.ps1', '.spec', '.iss', '.svg', '.yml'}
PRIVATE_FILES = {'settings.json', 'private.env', '.env', 'library.db', 'queue.db'}
SECRET_PATTERNS = [
    re.compile(r'sk-[a-fA-F0-9]{32,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{36,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{40,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
]


def main():
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    files = [Path(name) for name in files if name]
    failures = []
    if not files:
        failures.append('Source files must be tracked before verification')
    for relative in files:
        path = ROOT / relative
        if (relative.parts[0] in {'payment_service', '.venv', '.local-data', 'node_modules', 'dist', '.ui-audit'}
                or path.name in PRIVATE_FILES or path.suffix in {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.log'}):
            failures.append(f'Private or generated file: {relative.as_posix()}')
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            failures.append(f'Credential pattern requires review: {relative.as_posix()}')
        if path.suffix in CODE_SUFFIXES and 'vendor' not in path.parts:
            if 'SPDX-License-Identifier: MPL-2.0' not in text[:1600]:
                failures.append(f'Missing source license notice: {relative.as_posix()}')
    license_text = (ROOT / 'LICENSE').read_text(encoding='utf-8')
    if not license_text.startswith('Mozilla Public License Version 2.0') or 'Exhibit A' not in license_text:
        failures.append('MPL-2.0 license text is incomplete')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Public source checks passed for {len(files)} tracked files')


if __name__ == '__main__':
    main()
