# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Windows CUDA runtime directory registration tests."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from engine import dll


def test_nvrtc_directory_is_registered_even_when_already_on_path() -> None:
    with tempfile.TemporaryDirectory(prefix="cuda_nvrtc_") as temp:
        root = Path(temp)
        (root / "nvrtc64_120_0.dll").write_bytes(b"dll")
        normalized = str(root).lower()
        dll._REGISTERED_DLL_DIRS.discard(normalized)
        with (
            mock.patch.object(dll, "_candidate_dirs", return_value=[root]),
            mock.patch.object(dll.os, "add_dll_directory") as register,
            mock.patch.dict(os.environ, {"PATH": str(root)}, clear=False),
        ):
            added = dll.add_runtime_dll_dirs()
        register.assert_called_once_with(str(root))
        assert added == [str(root)]
        dll._REGISTERED_DLL_DIRS.discard(normalized)
