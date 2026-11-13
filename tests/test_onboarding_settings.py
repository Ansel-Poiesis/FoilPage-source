# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bounded chapter progress persists independently from release versions."""
import json
import pytest
from engine.settings import SettingsStore, SettingsValidationError


def test_legacy_settings_get_empty_chapters_without_resetting_other_fields(tmp_path):
    store = SettingsStore(base_dir=str(tmp_path))
    settings = store.defaults()
    settings.pop('onboarding_chapter_progress')
    settings['onboarding_completed_version'] = '0.16.1'
    settings['download_dir'] = str(tmp_path / 'my-downloads')
    store.path.write_text(json.dumps(settings), encoding='utf-8')
    loaded = store.load()
    assert loaded['onboarding_chapter_progress'] == {'schema_version': 1, 'chapters': {}}
    assert loaded['onboarding_completed_version'] == '0.16.1'
    assert loaded['download_dir'] == str(tmp_path / 'my-downloads')


def test_chapters_roundtrip_and_remain_after_app_patch_update(tmp_path):
    store = SettingsStore(base_dir=str(tmp_path))
    value = {'schema_version': 1, 'chapters': {
        'environment': {'step': 4, 'completed': True},
        'browser': {'step': 2, 'completed': False},
        'application': {'step': 6, 'completed': False},
    }}
    store.update(onboarding_chapter_progress=value)
    store.update(onboarding_completed_version='0.17.1')
    assert store.load()['onboarding_chapter_progress'] == value


@pytest.mark.parametrize('value', [None, {}, {'schema_version': True, 'chapters': {}},
    {'schema_version': 2, 'chapters': {}}, {'schema_version': 1, 'chapters': []},
    {'schema_version': 1, 'chapters': {'unknown': {'step': 0, 'completed': False}}},
    {'schema_version': 1, 'chapters': {'browser': {'step': 4, 'completed': False}}},
    {'schema_version': 1, 'chapters': {'environment': {'step': -1, 'completed': False}}},
    {'schema_version': 1, 'chapters': {'application': {'step': True, 'completed': False}}},
    {'schema_version': 1, 'chapters': {'application': {'step': 0, 'completed': 1}}},
])
def test_invalid_progress_is_rejected_without_clobbering_saved_settings(tmp_path, value):
    store = SettingsStore(base_dir=str(tmp_path))
    store.save(store.defaults())
    before = store.path.read_bytes()
    with pytest.raises(SettingsValidationError):
        store.update(onboarding_chapter_progress=value)
    assert store.path.read_bytes() == before
