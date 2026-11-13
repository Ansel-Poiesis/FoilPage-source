# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Test-session hardening.

The uv-managed CPython on this machine ships a minimal Tcl/Tk that is
fragile when several ``Tk()`` roots are created and destroyed inside one
process: ``_tkinter.create`` intermittently fails with "Can't find a usable
tk.tcl" pointing at a stale ``$tk_library`` path.  Pinning ``TCL_LIBRARY`` /
``TK_LIBRARY`` to the real bundled trees makes every GUI test deterministic.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_tcl_tree():
    # A venv usually has no tcl/ tree of its own; the real, complete tree
    # lives under the base interpreter (the uv-managed CPython here).  Probe
    # both so the helper works with or without an active virtualenv.
    candidates = []
    for root in {sys.prefix, sys.base_prefix}:
        if root:
            candidates.append(Path(root) / "tcl")
    for tcl_root in candidates:
        tcl_library = tcl_root / "tcl8.6"
        tk_library = tcl_root / "tk8.6"
        if (tcl_library / "init.tcl").is_file() and (tk_library / "tk.tcl").is_file():
            return tcl_library, tk_library
    return None, None


def _pin_tcl_paths() -> None:
    if os.name != "nt":
        return
    tcl_library, tk_library = _find_tcl_tree()
    if tcl_library is None:
        return
    os.environ.setdefault("TCL_LIBRARY", str(tcl_library))
    os.environ.setdefault("TK_LIBRARY", str(tk_library))


_pin_tcl_paths()


import pytest
import tkinter as tk


@pytest.fixture(autouse=True)
def _reset_tkinter_default_root():
    """Tk roots must not leak across tests.

    The uv-managed CPython here ships a minimal Tcl/Tk; once a Tk() root is
    destroyed, ``tkinter._default_root`` can keep pointing at the dead
    interpreter and the next Tk() creation fails with a stale ``$tk_library``
    error.  Destroying any leftover root after every test makes the GUI
    smoke tests deterministic regardless of collection order.
    """
    yield
    root = getattr(tk, "_default_root", None)
    if root is not None:
        try:
            root.destroy()
        except Exception:
            pass
        tk._default_root = None


@pytest.fixture(autouse=True)
def _offline_client_config(monkeypatch):
    """单测不出网（TASK-8.16 起 LocalApiServer.start() 会异步拉取线上
    client-config）：真请求在本机全量跑时会与 TemporaryDirectory 拆卸竞态
    ——成功回包异步写 base_dir/client-config.json，收尾 rmdir 报
    WinError 145「目录不是空的」，反复落在配对族测试。按离线语义让
    ClientConfigStore 的拉取即刻失败；需要拉取行为的用例（test_client_config）
    自带 monkeypatch，后应用者覆盖本夹具，互不影响。"""
    def _refuse(*_args, **_kwargs):
        raise OSError("offline in unit tests")
    monkeypatch.setattr("app.client_config.fetch_config", _refuse)