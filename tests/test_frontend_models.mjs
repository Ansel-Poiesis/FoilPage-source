// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const contractSource = await readFile(new URL('../app/webview_ui/js/ui-contract.js', import.meta.url), 'utf8');

async function loadModel(name) {
  const window = {};
  const sandbox = {window};
  vm.runInNewContext(contractSource, sandbox);
  const source = await readFile(new URL(`../app/webview_ui/js/${name}.js`, import.meta.url), 'utf8');
  vm.runInNewContext(source, sandbox);
  return window;
}

const library = (await loadModel('library-model')).LibraryModel;
const settings = (await loadModel('settings-model')).SettingsModel;
const plain = value => JSON.parse(JSON.stringify(value));

test('library signatures and document views are deterministic', () => {
  assert.equal(library.textSignature('hello'), 0x4f9f2cab);
  assert.equal(library.textSignature('hello'), library.textSignature('hello'));
  assert.deepEqual(plain(library.documentView({has_final: true, has_raw: true}, {tab: 'raw', translationView: 'page'})), {
    tab: 'raw', translationView: 'off',
  });
  assert.deepEqual(plain(library.documentView({has_final: false, has_raw: true, has_translation: true}, {tab: 'final', translationView: 'segments'})), {
    tab: 'raw', translationView: 'segments',
  });
  assert.deepEqual(plain(library.documentView({has_final: true, has_translation: true}, {translationView: 'unknown'})), {
    tab: 'final', translationView: 'off',
  });
});

test('library sorting is stable for missing dates and never mutates the source list', () => {
  const docs = [
    {task_id: 'missing'},
    {task_id: 'new', updated_at: '2026-11-20T10:00:00'},
    {task_id: 'old', updated_at: '2026-11-19T10:00:00'},
  ];
  const original = [...docs];
  assert.deepEqual(plain(library.sortedDocs(docs, 'newest').map(doc => doc.task_id)), ['new', 'old', 'missing']);
  assert.deepEqual(plain(library.sortedDocs(docs, 'oldest').map(doc => doc.task_id)), ['old', 'new', 'missing']);
  assert.deepEqual(docs, original);
  assert.deepEqual(plain(library.applyTagFilter([
    {tags: ['甲', '乙']}, {tags: '甲'}, {tags: ['乙']}, {},
  ], '甲')), [{tags: ['甲', '乙']}]);
  const unfiltered = [{tags: ['甲']}];
  assert.equal(library.applyTagFilter(unfiltered), unfiltered);
});

test('settings profile options include only complete enabled custom profiles', () => {
  const options = settings.profileOptions({agent_custom_profiles: [
    {id: 'one', name: '我的策略', enabled: true},
    {id: 'two', name: '停用策略', enabled: false},
    {id: '', name: '缺少身份'},
    null,
  ]});
  assert.deepEqual(plain(options.map(option => option.value)), ['auto', 'emotional_copy', 'consulting', 'knowledge', 'custom:one']);
  assert.equal(settings.profileOptions({agent_custom_profiles: [{id: 'one', name: '我的策略'}]}).length, 5);
  assert.equal(settings.PROFILES.length, 4);
});

test('settings save revisions reject stale confirmations and reserve key changes', () => {
  assert.deepEqual(plain(settings.revisionKeys({agent_provider: 'deepseek'})), ['agent_provider', 'agent_api_key']);
  assert.deepEqual(plain(settings.revisionKeys({agent_base_url: 'https://example.invalid/v1'})), ['agent_base_url', 'agent_api_key']);
  assert.deepEqual(plain(settings.revisionKeys({agent_provider: 'custom', agent_api_key: 'new'})), ['agent_provider', 'agent_api_key']);
  const revisions = new Map([['theme', 2], ['font_scale', 3], ['agent_api_key', 2]]);
  assert.deepEqual(plain(settings.confirmedSettings(
    {theme: 'paper', font_scale: 1.2}, {theme: 'dark', font_scale: 1.2}, revisions, 2,
  )), {theme: 'dark'});
  assert.deepEqual(plain(settings.confirmedSettings(
    {agent_provider: 'custom'}, {agent_provider: 'custom', agent_api_key: 'stale'}, revisions, 2, true,
  )), {agent_api_key: ''});
});

test('external settings merge preserves local edits and excludes runtime account fields', () => {
  const before = new Map([['theme', 1], ['font_scale', 2]]);
  const current = new Map([['theme', 2], ['font_scale', 2]]);
  const patch = settings.externalSettingsPatch({
    theme: 'paper', font_scale: 1.2, agent_model: 'new-model', data_dir: '/private',
    account_login_token: 'secret', license_token: 'secret', agent_usage_total: 9,
    subscription_plan: 'advanced', agent_settings_revision: 'revision',
  }, current, before);
  assert.deepEqual(plain(patch), {font_scale: 1.2, agent_model: 'new-model'});
});

test('desktop entry loads pure models before their consumers', async () => {
  const html = await readFile(new URL('../app/webview_ui/index.html', import.meta.url), 'utf8');
  assert.ok(html.indexOf('js/ui-contract.js') < html.indexOf('js/api.js'));
  assert.ok(html.indexOf('js/ui-contract.js') < html.indexOf('js/settings-model.js'));
  assert.ok(html.indexOf('js/library-model.js') < html.indexOf('js/library.js'));
  assert.ok(html.indexOf('js/settings-model.js') < html.indexOf('js/settings.js'));
  assert.ok(html.indexOf('css/brand.css') > html.indexOf('css/settings-motion.css'));
});
