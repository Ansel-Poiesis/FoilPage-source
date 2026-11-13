# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from app.startup import background_command, disable_startup, enable_startup, startup_enabled


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_background_command_for_packaged_and_source_runs() -> None:
    packaged = background_command("C:/Program Files/ShengJian/app.exe", frozen=True)
    assert packaged == subprocess.list2cmdline([
        str(Path("C:/Program Files/ShengJian/app.exe").resolve()), "--background"
    ])
    source = background_command(
        "C:/Python/python.exe", frozen=False, project_root="C:/project",
    )
    assert source == subprocess.list2cmdline([
        str(Path("C:/Python/python.exe").resolve()),
        str(Path("C:/project/app/desktop_web.py").resolve()),
        "--background",
    ])


@mock.patch("app.startup.os.name", "nt")
def test_registry_startup_enable_query_and_disable() -> None:
    values = {}
    fake = mock.Mock()
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_SET_VALUE = 1
    fake.KEY_QUERY_VALUE = 2
    fake.REG_SZ = 1
    fake.CreateKeyEx.return_value = FakeKey()
    fake.OpenKey.return_value = FakeKey()
    fake.SetValueEx.side_effect = lambda _key, name, _zero, kind, value: values.update({name: (value, kind)})
    fake.QueryValueEx.side_effect = lambda _key, name: values[name]
    fake.DeleteValue.side_effect = lambda _key, name: values.pop(name)
    command = '"C:\\ShengJian.exe" --background'
    with mock.patch.dict("sys.modules", {"winreg": fake}):
        assert enable_startup(command) == command
        assert startup_enabled(command) is True
        disable_startup()
        assert values == {}
