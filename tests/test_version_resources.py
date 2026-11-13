# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Windows version strings must be reachable through the declared translation.

Only read the build files and optional final binaries. No desktop/worker is
launched or modified. Roundtrip the source through PyInstaller's resource
compiler so language/codepage and packed versions are checked semantically.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = (("desktop", "FoilPage-Desktop.exe", "FoilPage"),
            ("worker", "worker/FoilPage-Worker.exe", "FoilPage Worker"))


def expected_version():
    source = (ROOT / "app" / "bridge.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', source)
    assert match is not None
    return match.group(1) + ".0"


@pytest.mark.skipif(sys.platform != "win32", reason="PyInstaller Windows version resource compiler")
@pytest.mark.parametrize("role,relative,description", PRODUCTS)
def test_compiled_version_table_matches_translation_and_application_version(role, relative, description):
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils.win32 import versioninfo

    source = versioninfo.load_version_info_from_text_file(ROOT / "build" / f"version_info_{role}.txt")
    raw = source.toRaw()
    compiled = versioninfo.VSVersionInfo()
    assert compiled.fromRaw(raw) == len(raw)
    tables = {table.name.upper(): {item.name: item.val for item in table.kids}
              for block in compiled.kids if isinstance(block, versioninfo.StringFileInfo)
              for table in block.kids}
    translations = [item.kids for block in compiled.kids if isinstance(block, versioninfo.VarFileInfo)
                    for item in block.kids if item.name == "Translation"]
    assert translations
    for entries in translations:
        assert entries and len(entries) % 2 == 0
        for language, codepage in zip(entries[::2], entries[1::2]):
            key = f"{language:04X}{codepage:04X}"
            assert key in tables, f"Declared Windows translation {key} has no version string table"
            values = tables[key]
            assert values["FileVersion"] == values["ProductVersion"] == expected_version()
            assert values["FileDescription"] == description
            assert values["ProductName"] == "FoilPage"
            assert values["OriginalFilename"] == Path(relative).name
    fixed = compiled.ffi
    packed = (fixed.fileVersionMS >> 16, fixed.fileVersionMS & 0xFFFF,
              fixed.fileVersionLS >> 16, fixed.fileVersionLS & 0xFFFF)
    assert ".".join(map(str, packed)) == expected_version()
    assert (fixed.productVersionMS, fixed.productVersionLS) == (fixed.fileVersionMS, fixed.fileVersionLS)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native resource reader")
@pytest.mark.parametrize("role,relative,description", PRODUCTS)
def test_optional_final_exe_has_native_readable_version_strings(role, relative, description):
    directory = os.environ.get("SHENGJIAN_VERSION_RESOURCE_EXE_ROOT")
    if not directory:
        pytest.skip("set SHENGJIAN_VERSION_RESOURCE_EXE_ROOT to verify the final packaged directory")
    import win32api

    executable = Path(directory) / relative
    assert executable.is_absolute() and executable.is_file()
    translations = win32api.GetFileVersionInfo(str(executable), r"\VarFileInfo\Translation")
    assert translations
    for language, codepage in translations:
        prefix = f"\\StringFileInfo\\{language:04X}{codepage:04X}\\"
        assert win32api.GetFileVersionInfo(str(executable), prefix + "FileVersion") == expected_version()
        assert win32api.GetFileVersionInfo(str(executable), prefix + "ProductVersion") == expected_version()
        assert win32api.GetFileVersionInfo(str(executable), prefix + "FileDescription") == description
        assert win32api.GetFileVersionInfo(str(executable), prefix + "ProductName") == "FoilPage"
