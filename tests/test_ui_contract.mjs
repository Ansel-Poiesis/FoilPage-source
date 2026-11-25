// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const read = name => readFile(new URL(`../${name}`, import.meta.url), 'utf8');
const source = await read('browser-extension/ui-contract.js');
const sandbox = {};
vm.runInNewContext(source, sandbox);
const contract = sandbox.FoilPageContract;
const plain = value => JSON.parse(JSON.stringify(value));

test('shared contract is pure and bounds task progress consistently', () => {
  assert.doesNotMatch(source, /\b(?:document|window|chrome|fetch|localStorage|WebSocket)\b/);
  assert.deepEqual(plain(contract.TERMINAL), ['done', 'failed', 'cancelled']);
  assert.equal(contract.isTerminalTask('done'), true);
  assert.equal(contract.isTerminalTask('retryable'), false);
  assert.equal(contract.progressFor({status: 'done'}), 1);
  assert.equal(contract.progressFor({status: 'downloading', progress: -2}), 0);
  assert.equal(contract.progressFor({status: 'downloading', progress: 4}), 1);
  assert.equal(contract.progressFor({status: 'queued', progress: 'broken'}), 0);
  assert.equal(contract.percent({status: 'downloading', progress: .426}), 43);
});

test('shared task labels preserve desktop and popup outcomes', () => {
  assert.equal(contract.rowStatusText({status: 'downloading', is_gallery: true}), '正在下载图集');
  assert.equal(contract.rowStatusText({status: 'done', kind: 'download'}), '仅完成下载');
  assert.equal(contract.rowStatusText({status: 'done', error_details: {diagnostic: {transcript_preserved: true}}}), '原稿已保留，AI 处理未完成');
  assert.equal(contract.progressLabelFor({status: 'downloading', progress: .42}), '正在下载 42%');
  assert.equal(contract.progressLabelFor({status: 'done', job_id: 'job', transcription_label: '正文转写成功'}), '正文转写成功');
  assert.equal(contract.progressLabelFor({status: 'queued', waiting_connection: true}), '转写排队中（连接恢复后继续）');
});

test('shared error categories sanitize codes and preserve explicit retry decisions', () => {
  assert.deepEqual(plain(contract.errorInfo('network_timeout')), {
    code: 'network_timeout', category: 'connection', retryable: true,
  });
  assert.deepEqual(plain(contract.errorInfo('origin_denied', 403)), {
    code: 'origin_denied', category: 'pairing', retryable: false,
  });
  assert.deepEqual(plain(contract.errorInfo('network_timeout', 0, false)), {
    code: 'network_timeout', category: 'connection', retryable: false,
  });
  assert.deepEqual(plain(contract.errorInfo('private token value', 503)), {
    code: 'local_api_error', category: 'http', retryable: true,
  });
  assert.equal(contract.needsDesktopConnection('pairing_closed'), true);
  assert.equal(contract.needsDesktopConnection('network_timeout'), false);
  assert.equal(contract.isTemporaryOffline({code: 'media_forbidden', retryable: false}), false);
});

test('shared pill parsing and hold times keep both video sites aligned', () => {
  assert.equal(contract.pillStatusFor('正在下载 137%', false).percent, 100);
  assert.equal(contract.pillStatusFor('任务完成', false).mode, 'done');
  assert.equal(contract.pillStatusFor('本地服务未运行', true).text, '请先打开 FoilPage');
  assert.equal(contract.pillStatusFor('已在处理中', false).text, '已在处理中');
  assert.equal(contract.pillHoldFor('完成', 'done'), 4500);
  assert.ok(contract.pillHoldFor('下载失败', 'error') >= 4000);
  assert.ok(contract.pillHoldFor('下载失败', 'error') <= 8000);
  assert.ok(contract.pillHoldFor('已提交', 'text') >= 2000);
  assert.ok(contract.pillHoldFor('已提交', 'text') <= 4000);
});

test('generated desktop and extension assets stay synchronized with the brand source', async () => {
  const [extensionContract, desktopContract, extensionBrand, desktopBrand] = await Promise.all([
    read('browser-extension/ui-contract.js'), read('app/webview_ui/js/ui-contract.js'),
    read('browser-extension/brand.css'), read('app/webview_ui/css/brand.css'),
  ]);
  assert.equal(extensionContract, desktopContract);
  assert.match(extensionBrand, /^\/\* Generated /);
  assert.match(desktopBrand, /^\/\* Generated /);
  assert.match(extensionBrand, /SPDX-License-Identifier: MPL-2.0/);
  assert.match(desktopBrand, /SPDX-License-Identifier: MPL-2.0/);
  assert.doesNotMatch(extensionBrand + desktopBrand, /^\/\//m);
  for (const token of ['--fp-pill-bg', '--fp-pill-done-bg', '--fp-content-bg']) {
    assert.ok(extensionBrand.includes(token), `extension brand must define ${token}`);
    assert.ok(desktopBrand.includes(token), `desktop brand must define ${token}`);
  }
  assert.ok(extensionBrand.includes('--fp-popup-paper'));
  assert.ok(desktopBrand.includes('body[data-theme="dark"][data-accent="green"]'));
  assert.equal(contract.ACCENTS[0].color, contract.BRAND.accents[0].color);
});

test('all script surfaces load the contract and brand tokens in dependency order', async () => {
  const [html, popup, manifest, biliCss, youtubeCss, main] = await Promise.all([
    read('app/webview_ui/index.html'), read('browser-extension/popup.html'),
    read('browser-extension/manifest.json'), read('browser-extension/content-bili.css'),
    read('browser-extension/content-youtube.css'), read('browser-extension/content-main.js'),
  ]);
  assert.ok(html.indexOf('js/ui-contract.js') < html.indexOf('js/api.js'));
  assert.ok(html.indexOf('css/brand.css') > html.indexOf('css/product.css'));
  assert.ok(popup.indexOf('brand.css') < popup.indexOf('popup.css'));
  const parsed = JSON.parse(manifest);
  for (const entry of parsed.content_scripts) {
    assert.equal(entry.css?.[0], entry.world === 'MAIN' ? undefined : 'brand.css');
    if (entry.js?.includes('content-bili.js') || entry.js?.includes('content-youtube.js')) {
      assert.equal(entry.js[0], 'ui-contract.js');
    }
  }
  assert.doesNotMatch(main, /FoilPageContract/);
  for (const css of [biliCss, youtubeCss]) {
    assert.match(css, /var\(--fp-pill-bg\)/);
    assert.match(css, /var\(--fp-pill-done-bg\)/);
    assert.match(css, /var\(--fp-pill-error-bg\)/);
  }
});
