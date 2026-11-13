// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const RUNNING_VERSION = '0.14.1';
const NEXT_VERSION = '0.14.2';
const TASK_ID = 'abc123def456';
const START_TIME = 1_000_000;
const sleepTurn = () => new Promise((resolve) => setImmediate(resolve));
async function settle() { for (let i = 0; i < 5; i += 1) await sleepTurn(); }
function deferred() {
  let resolve; let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function event() {
  const listeners = [];
  return { addListener(listener) { listeners.push(listener); }, emit(...args) { return listeners.map((listener) => listener(...args)); } };
}

// Run the real worker. No browser, local service, credentials, or live timers
// are involved; requests and persisted Chrome state are controlled fixtures.
async function harness(options = {}) {
  let now = options.now ?? START_TIME;
  let latest = options.latest ?? RUNNING_VERSION;
  let popupOpen = Boolean(options.popupOpen);
  let healthHandler = options.healthHandler;
  let writeHandler = null;
  const saved = structuredClone(options.saved || {});
  const healthCalls = []; const requests = []; const sockets = []; const reloads = [];
  const alarms = [];
  const timers = new Map();
  let nextTimer = 1;
  class FakeDate extends Date { static now() { return now; } }
  class FakeWebSocket {
    static CONNECTING = 0; static OPEN = 1;
    constructor() { this.readyState = FakeWebSocket.OPEN; this.sent = []; sockets.push(this); }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  const runtime = {
    getManifest: () => ({ version: options.mine || RUNNING_VERSION }),
    reload() { reloads.push(now); },
    lastError: null,
    onMessage: event(), onStartup: event(), onInstalled: event(),
    async getContexts() {
      if (options.popupQueryError) throw new Error('fixture view query unavailable');
      return popupOpen ? [{ contextType: 'POPUP' }] : [];
    },
  };
  const chrome = {
    runtime,
    extension: { getViews() {
      if (options.popupQueryError) throw new Error('fixture legacy view query unavailable');
      return popupOpen ? [{}] : [];
    } },
    storage: {
      local: {
        async get(keys) {
          await options.readHandler?.(keys);
          if (typeof keys === 'string') return { [keys]: structuredClone(saved[keys]) };
          if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, structuredClone(saved[key])]));
          if (keys && typeof keys === 'object') return Object.fromEntries(Object.entries(keys).map(([key, value]) => [key, structuredClone(saved[key] ?? value)]));
          return structuredClone(saved);
        },
        async set(data) { await writeHandler?.(data); Object.assign(saved, structuredClone(data)); },
      },
      onChanged: event(),
    },
    tabs: {
      query(query, callback) { callback?.([]); return Promise.resolve([]); },
      sendMessage(tabId, message, callback) { callback?.({}); return Promise.resolve({}); },
    },
    cookies: { async getAll() { throw new Error('update checks must not read credentials'); } },
    downloads: {
      onChanged: event(),
      search(query, callback) {
        const items = options.downloads?.[query.id] || [];
        callback?.(items); return Promise.resolve(items);
      },
      download() { throw new Error('update checks must not start a download'); },
    },
    alarms: { create(name, schedule) { alarms.push({ name, schedule }); }, onAlarm: event() },
  };
  if (options.legacyViews) delete runtime.getContexts;
  if (options.viewsUnavailable) { delete runtime.getContexts; delete chrome.extension; }
  const context = {
    chrome, URL, AbortController, Date: FakeDate, Math, Promise, Set, Map,
    console: { log() {}, warn() {}, error() {} },
    WebSocket: FakeWebSocket, navigator: { userAgent: 'update regression fixture' },
    getToken: async () => 'fixture-websocket-token',
    pickDouyinResolveCookies: () => [], browserFallbackMediaUrls: () => [],
    async fetch(url, init) {
      healthCalls.push({ url, at: now });
      if (healthHandler) return healthHandler(url, init);
      return { ok: true, async json() { return { ok: true, extension_latest: latest }; } };
    },
    async request(path, init) {
      requests.push({ path, init });
      if (options.requestHandler) return options.requestHandler(path, init);
      if (path === '/v1/downloads?limit=6') return { jobs: [] };
      if (path.startsWith('/v1/downloads/')) return { job: { id: TASK_ID, status: 'awaiting_browser', options: {} } };
      return { ok: true };
    },
    setTimeout(callback, delay) {
      const timer = { id: nextTimer++, callback, delay, unref() {} };
      timers.set(timer.id, timer); return timer;
    },
    clearTimeout(timer) { timers.delete(timer?.id ?? timer); },
  };
  let source = await readFile(new URL('../browser-extension/background.js', import.meta.url), 'utf8');
  source = source.replace(/^import .*;\r?\n/gm, '');
  vm.runInNewContext(source + '\nglobalThis.fixture = { selfUpdateCheck, withExtensionOperation, localTaskWatchers, inFlightBrowserDownloads, desktopRequestsInFlight, fallbacksInFlight };', context);
  await settle();
  return {
    ...context.fixture, saved, requests, healthCalls, reloads, alarms, timers,
    advance(ms) { now += ms; },
    setLatest(value) { latest = value; },
    setPopup(value) { popupOpen = value; },
    setHealthHandler(value) { healthHandler = value; },
    setWriteHandler(value) { writeHandler = value; },
    async heartbeat() {
      assert.equal(sockets.length, 1, 'desktop socket stays alive in the same worker');
      sockets[0].onmessage({ data: JSON.stringify({ type: 'heartbeat' }) });
      await settle();
    },
    async message(type = 'shengjian-update-check') {
      runtime.onMessage.emit({ type }, {}, () => {});
      await settle();
    },
    async alarm() { chrome.alarms.onAlarm.emit({ name: 'shengjianFailureReports' }); await settle(); },
  };
}

for (const trigger of ['heartbeat', 'message', 'alarm']) {
  test(`a live worker detects a new version on ${trigger} after the cold check`, async () => {
    const fixture = await harness();
    assert.equal(fixture.healthCalls.length, 1);
    fixture.setLatest(NEXT_VERSION);
    fixture.advance(30_000);
    await fixture[trigger]();
    assert.equal(fixture.healthCalls.length, 2);
    assert.equal(fixture.reloads.length, 1);
    assert.deepEqual(fixture.saved.shengjianSelfReloadTarget, { from: RUNNING_VERSION, to: NEXT_VERSION });
  });
}

test('a failed cold health check releases the mutex and retries after 30 seconds', async () => {
  const fixture = await harness({ healthHandler: async () => { throw new Error('fixture app offline'); } });
  assert.equal(fixture.healthCalls.length, 1);
  fixture.setHealthHandler(null); fixture.setLatest(NEXT_VERSION);
  fixture.advance(29_999); await fixture.heartbeat();
  assert.equal(fixture.healthCalls.length, 1);
  fixture.advance(1); await fixture.heartbeat();
  assert.equal(fixture.healthCalls.length, 2);
  assert.equal(fixture.reloads.length, 1);
});

test('concurrent triggers use one health request and remain throttled after it resolves', async () => {
  const gate = deferred();
  const fixture = await harness({ healthHandler: async () => gate.promise });
  fixture.advance(30_000);
  await fixture.heartbeat(); await fixture.message(); await fixture.alarm();
  assert.equal(fixture.healthCalls.length, 1, 'in-flight cold request owns the mutex');
  gate.resolve({ ok: true, async json() { return { ok: true, extension_latest: RUNNING_VERSION }; } });
  await settle();
  await fixture.selfUpdateCheck();
  assert.equal(fixture.healthCalls.length, 2, 'the completed request releases its mutex');
  await Promise.all([fixture.selfUpdateCheck(), fixture.selfUpdateCheck()]);
  assert.equal(fixture.healthCalls.length, 2, 'checks at the same clock time are throttled');
});

for (const legacyViews of [false, true]) {
  test(`an open popup defers reload and closing it permits a later check (${legacyViews ? 'legacy Chrome' : 'runtime contexts'})`, async () => {
    const fixture = await harness({ latest: NEXT_VERSION, popupOpen: true, legacyViews });
    assert.equal(fixture.reloads.length, 0);
    assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined, 'a deferred check must not mark the version as tried');
    fixture.setPopup(false); fixture.advance(30_000);
    await fixture.message();
    assert.equal(fixture.reloads.length, 1);
  });
}

for (const options of [{ popupQueryError: true }, { viewsUnavailable: true }]) {
  test(`unknown popup visibility conservatively defers reload (${Object.keys(options)[0]})`, async () => {
    const fixture = await harness({ ...options, latest: NEXT_VERSION });
    assert.equal(fixture.reloads.length, 0);
    assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined);
  });
}

for (const [kind, mapKey, entry] of [
  ['local download watcher', 'localTaskWatchers', { status: 'downloading' }],
  ['transcription watcher', 'localTaskWatchers', { status: 'transcribing' }],
  ['Chrome transfer', 'inFlightBrowserDownloads', { taskId: TASK_ID }],
  ['desktop dispatch', 'desktopRequestsInFlight', null],
  ['fallback handoff', 'fallbacksInFlight', null],
]) {
  test(`a new version waits for an active ${kind}`, async () => {
    const fixture = await harness();
    const activity = fixture[mapKey];
    if (activity instanceof Set) activity.add(TASK_ID); else activity.set(TASK_ID, entry);
    fixture.setLatest(NEXT_VERSION); fixture.advance(30_000);
    await fixture.selfUpdateCheck();
    assert.equal(fixture.reloads.length, 0);
    activity.clear();
    await fixture.selfUpdateCheck();
    assert.equal(fixture.reloads.length, 1, 'retries once the activity becomes idle');
  });
}

for (const [kind, saved] of [
  ['persisted Chrome transfer', { shengjianBrowserDownloadJobs: { 7: { taskId: TASK_ID } } }],
  ['persisted watcher', { watchedDownloadTasks: [{ id: TASK_ID, downloadNotified: true }] }],
  ['pending browser import', { shengjianPendingImports: [{ downloadId: 7, taskId: TASK_ID, queuedAt: START_TIME - 5_000 }] }],
]) {
  test(`the cold update check cannot interrupt a ${kind}`, async () => {
    const fixture = await harness({ latest: NEXT_VERSION, saved });
    assert.equal(fixture.reloads.length, 0);
    assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined);
    fixture.localTaskWatchers.clear(); fixture.inFlightBrowserDownloads.clear();
    fixture.saved.shengjianBrowserDownloadJobs = {};
    fixture.saved.watchedDownloadTasks = [];
    fixture.saved.shengjianPendingImports = [];
    fixture.advance(30_000); await fixture.selfUpdateCheck();
    assert.equal(fixture.reloads.length, 1);
  });
}

test('the update check waits until an asynchronous extension operation completes', async () => {
  const fixture = await harness();
  const gate = deferred();
  const operation = fixture.withExtensionOperation(() => gate.promise);
  fixture.setLatest(NEXT_VERSION); fixture.advance(30_000);
  await fixture.selfUpdateCheck();
  assert.equal(fixture.reloads.length, 0);
  gate.resolve(); await operation; await settle();
  assert.equal(fixture.reloads.length, 1, 'finishing an operation performs the deferred check immediately');
});

test('new work during the reload-marker write prevents reload even if it finishes quickly', async () => {
  const fixture = await harness();
  let raced = false;
  fixture.setWriteHandler(async (data) => {
    if (Object.hasOwn(data, 'shengjianSelfReloadTarget') && !raced) {
      raced = true;
      await fixture.withExtensionOperation(async () => {});
    }
  });
  fixture.setLatest(NEXT_VERSION); fixture.advance(30_000);
  await fixture.selfUpdateCheck();
  assert.equal(raced, true, 'exercise the asynchronous storage-write window');
  assert.equal(fixture.reloads.length, 0);
  fixture.setWriteHandler(null); fixture.advance(60_001);
  await fixture.selfUpdateCheck();
  assert.equal(fixture.reloads.length, 1, 'an aborted reload does not poison the retry guard');
});

test('the persisted 60-second reload debounce is respected and later releases', async () => {
  const fixture = await harness({ latest: NEXT_VERSION, saved: { shengjianSelfReloadAt: START_TIME - 30_000 } });
  assert.equal(fixture.reloads.length, 0);
  assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined);
  fixture.advance(30_001); await fixture.selfUpdateCheck();
  assert.equal(fixture.reloads.length, 1);
});

for (const latest of [RUNNING_VERSION, '0.13.99', '0.14.0', '', 'v0.14.2', 'not-a-version', '0.14.2-beta']) {
  test(`equal, older, or invalid health version ${JSON.stringify(latest)} cannot reload the worker`, async () => {
    const fixture = await harness({ latest });
    fixture.advance(60_001); await fixture.heartbeat();
    assert.equal(fixture.reloads.length, 0);
    assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined);
  });
}

test('health ok=false cannot announce an update', async () => {
  const fixture = await harness({ healthHandler: async () => ({ ok: true, async json() { return { ok: false, extension_latest: NEXT_VERSION }; } }) });
  assert.equal(fixture.reloads.length, 0);
});

test('an unchanged registered path cannot cause a reload loop, while a later target can retry', async () => {
  const first = await harness();
  first.setLatest(NEXT_VERSION); first.advance(30_000); await first.heartbeat();
  assert.equal(first.reloads.length, 1);
  // A reload from an old directory starts the same manifest version again.
  const restarted = await harness({ latest: NEXT_VERSION, saved: first.saved, now: START_TIME + 120_000 });
  assert.equal(restarted.reloads.length, 0);
  restarted.advance(120_000); await restarted.heartbeat();
  assert.equal(restarted.reloads.length, 0, 'the same from/to pair remains suppressed beyond the debounce');
  restarted.setLatest('0.14.3'); restarted.advance(30_000);
  await restarted.message();
  assert.equal(restarted.reloads.length, 1, 'a genuinely new target gets one further attempt');
  assert.deepEqual(restarted.saved.shengjianSelfReloadTarget, { from: RUNNING_VERSION, to: '0.14.3' });
});
test('a popup opened during the reload-marker write defers reload without poisoning a retry', async () => {
  const fixture = await harness();
  fixture.setWriteHandler(async (data) => {
    if (data.shengjianSelfReloadTarget?.to === NEXT_VERSION) fixture.setPopup(true);
  });
  fixture.setLatest(NEXT_VERSION); fixture.advance(30_000);
  await fixture.selfUpdateCheck();
  assert.equal(fixture.reloads.length, 0);
  assert.equal(fixture.saved.shengjianSelfReloadTarget, null);
  fixture.setWriteHandler(null); fixture.setPopup(false); fixture.advance(30_000);
  await fixture.selfUpdateCheck();
  assert.equal(fixture.reloads.length, 1);
});

test('an HTTP health failure cannot announce an update and a later healthy response can retry', async () => {
  const fixture = await harness({ healthHandler: async () => ({ ok: false, async json() { return { ok: true, extension_latest: NEXT_VERSION }; } }) });
  assert.equal(fixture.reloads.length, 0);
  fixture.setHealthHandler(null); fixture.setLatest(NEXT_VERSION); fixture.advance(30_000);
  await fixture.heartbeat();
  assert.equal(fixture.reloads.length, 1);
});

test('version ordering compares numeric components rather than text', async () => {
  const fixture = await harness({ mine: '0.14.9', latest: '0.14.9' });
  fixture.setLatest('0.14.10'); fixture.advance(30_000); await fixture.heartbeat();
  assert.equal(fixture.reloads.length, 1);
});
test('the cold check waits for startup recovery and then detects a newer version while idle', async () => {
  const fixture = await harness({ latest: NEXT_VERSION });
  assert.equal(fixture.healthCalls.length, 1, 'startup operations do not consume a health-check interval');
  assert.equal(fixture.reloads.length, 1);
});

// 2026-10-20 实机回归：一条滞留的 pending import（桌面端任务已 404、Chrome
// 下载记录已被清理）曾把扩展自更新冻死在 1.0.23——按钮兜底修复送不到浏览器。
test('day-old stuck imports and submissions cannot freeze the self-update', async () => {
  const staleAt = START_TIME - 25 * 60 * 60 * 1000;
  const fixture = await harness({
    latest: NEXT_VERSION,
    saved: {
      shengjianPendingImports: [{ downloadId: 271, taskId: TASK_ID, queuedAt: staleAt }],
      shengjianSubmissionQueue: [{ createdAt: staleAt, status: 'downloading', lastJob: { status: 'downloading' } }],
    },
    requestHandler(path) {
      if (path === `/v1/downloads/${TASK_ID}`) throw Object.assign(new Error('下载任务不存在'), { code: 'not_found', status: 404 });
      return undefined;
    },
  });
  assert.equal(fixture.reloads.length, 1, 'stale persisted work must not block the reload');
  assert.equal((fixture.saved.shengjianPendingImports || []).length, 0, 'the stuck import is drained, not merely ignored');
});

test('recent persisted work still defers the self-update', async () => {
  const fixture = await harness({
    latest: NEXT_VERSION,
    saved: { shengjianPendingImports: [{ downloadId: 271, taskId: TASK_ID, queuedAt: START_TIME - 60_000 }] },
  });
  assert.equal(fixture.reloads.length, 0);
  assert.equal(fixture.saved.shengjianSelfReloadTarget, undefined);
});
// TASK-8.16 热切换配置：/health 透传入库与门控（绝不参与/阻塞自更新判定）。
const CLIENT_CONFIG_V1 = { version: 3, config: { flags: { covers_enabled: true, douyin_gallery_enabled: false } } };
const configHealth = (entry, latestVersion = RUNNING_VERSION) => async () => ({
  ok: true,
  json: async () => ({ ok: true, extension_latest: latestVersion, client_config: entry }),
});

test('health client_config lands in storage without touching reload decisions', async () => {
  const fixture = await harness({ healthHandler: configHealth(CLIENT_CONFIG_V1) });
  assert.deepEqual(fixture.saved.shengjianClientConfig, CLIENT_CONFIG_V1);
  assert.equal(fixture.reloads.length, 0);
});

test('a newer extension version still reloads while the config is stored first', async () => {
  const fixture = await harness({ healthHandler: configHealth(CLIENT_CONFIG_V1, NEXT_VERSION) });
  assert.equal(fixture.reloads.length, 1);
  assert.deepEqual(fixture.saved.shengjianClientConfig, CLIENT_CONFIG_V1);
});

test('an older client_config version never overwrites a newer stored one', async () => {
  const newer = { version: 5, config: { flags: { covers_enabled: false, douyin_gallery_enabled: true } } };
  const fixture = await harness({
    healthHandler: configHealth(CLIENT_CONFIG_V1),
    saved: { shengjianClientConfig: newer },
  });
  assert.deepEqual(fixture.saved.shengjianClientConfig, newer);
});

test('malformed or oversized client_config payloads are ignored without side effects', async () => {
  const huge = { version: 4, config: { filler: 'x'.repeat(20000) } };
  for (const entry of [null, { version: 'x', config: {} }, { version: 2 }, { version: 2, config: 'flags' }, huge]) {
    const fixture = await harness({ healthHandler: configHealth(entry) });
    assert.equal(fixture.saved.shengjianClientConfig, undefined, JSON.stringify(entry)?.slice(0, 40));
    assert.equal(fixture.reloads.length, 0);
  }
});
