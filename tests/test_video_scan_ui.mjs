// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });
async function open() {
  const page = await browser.newPage({viewport: {width: 1280, height: 800}});
  page.errors = []; page.on('pageerror', e => page.errors.push(e.message));
  await page.addInitScript(installMock, {});
  await page.goto(pathToFileURL(path.resolve('app/webview_ui/index.html')).href);
  await page.waitForFunction(() => window.__appReady);
  // Each test controls the catalog snapshot, independently from task polling.
  await page.evaluate(() => { Videos.refreshLight = () => {}; window.scanCatalogCalls = 0; });
  return page;
}
test('background discovery keeps current videos usable and fetches new catalog after completion', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      const old = {id:'old',title:'已有视频',available:true};
      __MOCK__.get_downloaded_videos = async () => {
        scanCatalogCalls++;
        return {ok:true,items:scanCatalogCalls === 1 ? [old] : [old,{id:'new',title:'新发现视频',available:true}],
          scan:{running:scanCatalogCalls === 1,visited:4}};
      };
      __MOCK__.get_video_scan_status = async () => ({ok:true,scan:{running:false,state:'done',visited:5}});
      switchView('videos');
    });
    await page.waitForSelector('.video-card[data-video-id=old]');
    assert.equal(await page.locator('#videosReadState').isVisible(), false);
    assert.equal(await page.locator('.video-card[data-video-id=old] .video-cover').isEnabled(), true);
    assert.match(await page.locator('#videosStatus').textContent(), /已读取 4/);
    await page.locator('.video-card[data-video-id=old] .video-menu-trigger').focus();
    await page.waitForSelector('.video-card[data-video-id=new]');
    assert.equal(await page.locator('.video-card').count(), 2);
    assert.equal(await page.evaluate(() => scanCatalogCalls), 2);
    assert.equal(await page.locator('#videoGrid').getAttribute('aria-busy'), 'false');
    assert(await page.locator('.video-card[data-video-id=old] .video-menu-trigger').evaluate(node => node === document.activeElement));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
test('late scan response after page departure cannot fetch or change media', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __MOCK__.get_downloaded_videos = async () => { scanCatalogCalls++; return {ok:true,items:[],scan:{running:true,visited:0}}; };
      __MOCK__.get_video_scan_status = () => new Promise(resolve => window.finishScan = resolve);
      switchView('videos');
    });
    await page.waitForFunction(() => typeof finishScan === 'function');
    await page.evaluate(() => switchView('workbench'));
    await page.evaluate(() => finishScan({ok:true,scan:{running:false,state:'done'}}));
    await page.waitForTimeout(80);
    assert.equal(await page.evaluate(() => scanCatalogCalls), 1);
    assert.equal(await page.evaluate(() => AppState.currentView), 'workbench');
    assert.equal(await page.locator('.video-card').count(), 0);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
test('refresh failure keeps existing catalog and reports concise inline error', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __MOCK__.get_downloaded_videos = async () => ({ok:true,items:[{id:'keep',title:'保留视频',available:true}],scan:{running:false}});
      switchView('videos');
    });
    await page.waitForSelector('.video-card[data-video-id=keep]');
    await page.evaluate(() => { __MOCK__.get_downloaded_videos = async () => ({ok:false,error:'目录暂时不可读'}); });
    await page.locator('#refreshVideos').click();
    await page.waitForFunction(() => document.querySelector('#videosStatus').textContent === '目录暂时不可读');
    assert(await page.locator('.video-card[data-video-id=keep]').isVisible());
    assert.equal(await page.locator('#videosReadState').isVisible(), false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
