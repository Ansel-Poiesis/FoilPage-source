# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Interface language persists independently of recognition language."""
import pytest
from engine.settings import SettingsStore, SettingsValidationError


def test_ui_language_round_trip_does_not_change_asr_language(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    assert store.load()['ui_language'] == 'zh-CN'
    store.update(ui_language='en', language='ja')
    assert store.load()['ui_language'] == 'en'
    assert store.load()['language'] == 'ja'
    store.update(ui_language='zh-CN')
    assert store.load()['language'] == 'ja'
    with pytest.raises(SettingsValidationError):
        store.update(ui_language='invalid')


def test_existing_profile_gets_chinese_interface_default(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    settings = store.defaults()
    settings.pop('ui_language')
    import json
    store.path.write_text(json.dumps(settings), encoding='utf-8')
    assert store.load()['ui_language'] == 'zh-CN'
