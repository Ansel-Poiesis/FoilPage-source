// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Actual MV3 injection in an isolated Chromium profile. All requests are fixtures.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
  const evidence = path.join(root, '.ui-audit', 'extension-main-browser');
  await fs.mkdir(evidence, {recursive: true});
  const profile = await fs.mkdtemp(path.join(evidence, 'profile-'));
  const extension = path.join(root, 'browser-extension');
  const manifest = JSON.parse(await fs.readFile(path.join(extension, 'manifest.json'), 'utf8'));
  const context = await chromium.launchPersistentContext(profile, {
    channel: 'chromium', headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
  });
  try {
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'www.douyin.com') return route.abort();
      if (url.pathname === '/aweme/v1/web/aweme/detail/') {
        return route.fulfill({contentType: 'application/json', body: JSON.stringify({aweme_detail: {
          aweme_id: '7691206606389808419', video: {play_addr: {url_list: ['https://v26.douyinvod.com/fixture.mp4']}}
        }})});
      }
      if (['/aweme/v1/web/aweme/favorite/', '/aweme/v1/web/aweme/listcollection/'].includes(url.pathname)) {
        const selectedIndex = url.pathname.includes('listcollection') ? 29 : 2;
        const revision = selectedIndex === 29 ? 'collection' : 'favorite';
        const aweme_list = Array.from({length: 30}, (_, index) => ({
          aweme_id: index === selectedIndex ? '7695284409359944995' : String(7695000000000000000n + BigInt(index)),
          video: {play_addr: {url_list: [index === selectedIndex
            ? `https://v26.douyinvod.com/selected.mp4?revision=${revision}`
            : `https://v26.douyinvod.com/neighbor-${index}.mp4`]}},
        }));
        return route.fulfill({contentType: 'application/json', body: JSON.stringify({aweme_list})});
      }
      if (url.pathname === '/video/7691206606389808419' || url.pathname === '/user/self') {
        return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><title>Isolated MV3 fixture</title></head><body>MAIN capture fixture</body></html>'});
      }
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('https://www.douyin.com/video/7691206606389808419');
    await page.waitForFunction(version => document.documentElement.dataset.shengjianMain === version, manifest.version);
    await page.evaluate(() => fetch('/aweme/v1/web/aweme/detail/').then(r => r.json()));
    await page.waitForFunction(() => document.documentElement.dataset.shengjianMainMediaItems === '1');
    const metrics = await page.evaluate(() => {
      const d = document.documentElement.dataset;
      return {version: d.shengjianMain, requests: d.shengjianMainApiRequests,
        responses: d.shengjianMainApiResponses, mediaItems: d.shengjianMainMediaItems};
    });
    assert.deepEqual(errors, []);
    assert.equal(metrics.requests, '1');
    assert.equal(metrics.responses, '1');
    await page.goto('https://www.douyin.com/user/self?modal_id=7695284409359944995&showTab=favorite_collection');
    await page.evaluate(() => Promise.all([
      fetch('/aweme/v1/web/aweme/favorite/').then(r => r.json()),
      fetch('/aweme/v1/web/aweme/listcollection/').then(r => r.json()),
    ]));
    await page.waitForFunction(() => document.documentElement.dataset.shengjianMainMediaItems === '48');
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const readMedia = (videoId) => worker.evaluate(async id => {
      const tabs = await chrome.tabs.query({url: 'https://www.douyin.com/user/self*'});
      if (tabs.length !== 1) throw new Error('Expected exactly one fixture tab');
      return chrome.tabs.sendMessage(tabs[0].id, {type:'shengjian-fresh-media', videoId:id});
    }, videoId);
    let selected;
    const deadline = Date.now() + 5000;
    do {
      selected = await readMedia('7695284409359944995');
      if (selected?.mediaUrl === 'https://v26.douyinvod.com/selected.mp4?revision=collection') break;
      if (Date.now() >= deadline) throw new Error('Selected favorite did not survive MAIN to ISOLATED');
      await new Promise(resolve => setTimeout(resolve, 30));
    } while (true);
    assert.equal(selected.videoId, '7695284409359944995');
    assert.deepEqual(selected.mediaCandidates, ['https://v26.douyinvod.com/selected.mp4?revision=collection']);
    const unrelated = await readMedia('7695999999999999999');
    assert.equal(unrelated.mediaUrl, '');
    assert.deepEqual(unrelated.mediaCandidates, []);
    assert.deepEqual(errors, []);
    const proof = {ok: true, fixture_only: true, actual_mv3_injection: true, ...metrics,
      favorite_entries_observed: true, selected_first_and_last_retained: true,
      coalesced_batches_received: true, unrelated_video_rejected: true, page_errors: errors};
    await fs.writeFile(path.join(profile, 'acceptance.json'), JSON.stringify(proof, null, 2));
    console.log(JSON.stringify(proof));
  } finally {
    await context.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
