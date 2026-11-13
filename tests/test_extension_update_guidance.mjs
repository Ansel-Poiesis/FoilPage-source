// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Fresh Chromium fixtures exercise the product UI without using browser profiles
// or calling the real local service. The updater behavior itself has separate tests.
import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import {chromium} from 'playwright';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';

const root = path.resolve(import.meta.dirname, '..');
const extensionRoot = path.join(root, 'browser-extension');
const shots = path.join(root, '_webview_ui_shots', 'extension-update-guidance');
const fixedPath = 'C:\\Users\\fixture\\AppData\\Local\\Programs\\VideoToArticle\\browser-extension';
const paired = {ok:true, running:true, configured:true, extension_dir:fixedPath};
let browser, server, popupUrl;

before(async () => {
  fs.mkdirSync(shots, {recursive:true});
  server = http.createServer((request, response) => {
    const name = new URL(request.url, 'http://localhost').pathname.replace(/^\//, '') || 'popup.html';
    const file = path.resolve(extensionRoot, name);
    if (!file.startsWith(extensionRoot + path.sep)) {response.writeHead(403).end(); return;}
    try {
      const mime = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.png') ? 'image/png' : 'text/html';
      response.setHeader('Content-Type', mime + (mime === 'image/png' ? '' : '; charset=utf-8'));
      response.end(fs.readFileSync(file));
    } catch {response.writeHead(404).end();}
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  popupUrl = `http://127.0.0.1:${server.address().port}/popup.html`;
  browser = await chromium.launch({headless:true});
});
after(async () => {
  await browser?.close();
  await new Promise(resolve => server?.close(resolve));
});

async function openDesktop(replies = [{value:paired}], options = {}, size = {width:1280,height:800}) {
  const page = await browser.newPage({viewport:size, reducedMotion:'reduce'});
  page.setDefaultTimeout(5000); page.errors = [];
  page.on('pageerror', error => page.errors.push(error.message));
  await page.addInitScript(installMock, options);
  await page.goto(pathToFileURL(path.join(root, 'app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.evaluate(replies => {
    let request = 0;
    __MOCK__.open_directory = directory => {
      __fixtureCalls.push('open_directory'); __fixtureCallArgs.push(['open_directory',directory]);
      return {ok:true};
    };
    __MOCK__.browser_status = async () => {
      __fixtureCalls.push('browser_status');
      const index=request++, reply = replies[index] || replies.at(-1);
      if(reply.hold) return new Promise(resolve=>{
        window.__guidanceStatusReplies ||= [];
        window.__guidanceStatusReplies[index]=()=>resolve(reply.value);
      });
      if (reply.delay) await new Promise(resolve => setTimeout(resolve, reply.delay));
      if (reply.throws) throw new Error('fixture status unavailable');
      return reply.value;
    };
  }, replies);
  await page.locator('[data-page=browser]').click();
  await page.waitForSelector('#page-browser.active #browserStatus');
  return page;
}

async function openPopup(options = {}, theme = 'light') {
  const page = await browser.newPage({viewport:{width:360,height:600}, colorScheme:theme, reducedMotion:'reduce'});
  page.setDefaultTimeout(5000); page.errors = [];
  page.on('pageerror', error => page.errors.push(error.message));
  await page.addInitScript(options => {
    window.__extensionActions = [];
    globalThis.chrome = {
      runtime:{getManifest:() => ({version:options.mine || '0.17.0'}), sendMessage:async message => {
        __extensionActions.push(['message', message.type]); return {ok:true,tasks:[]};
      }, reload:() => __extensionActions.push(['reload'])},
      storage:{local:{get:async key => {
        if (key === 'localApiToken') return {localApiToken:'fixture-only'};
        if (options.storageThrows) throw new Error('fixture storage unavailable');
        return {shengjianSelfReloadTarget:options.attempt};
      }, set:async value => __extensionActions.push(['write',value])}},
      tabs:{query:async () => [{id:1,url:'https://www.youtube.com/watch?v=fixture123',title:'读取视频 - YouTube'}]},
    };
  }, options);
  await page.route('http://127.0.0.1:18765/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname !== '/health') throw new Error('Unexpected local API request: ' + pathname);
    return route.fulfill({contentType:'application/json', headers:{'access-control-allow-origin':'*'}, body:JSON.stringify({ok:true,service:'VideoToArticle',api_version:'1',extension_latest:options.latest ?? '0.17.0'})});
  });
  await page.goto(popupUrl);
  await page.waitForFunction(() => document.getElementById('connectionText').textContent === '已连接');
  return page;
}

test('desktop shows durable pairing only and uses the installed fixed directory for first installation', async () => {
  const page = await openDesktop();
  try {
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
    assert.equal(await page.locator('#extensionPath').textContent(), fixedPath);
    assert.match(await page.locator('#browserHint').textContent(), /升级后刷新已打开的视频页/);
    assert.doesNotMatch(await page.locator('#browserStatus').textContent(), /在线|已连接|已更新|最新/);
    await page.getByText('首次安装与后续更新', {exact:true}).click();
    const guidance = await page.locator('#page-browser details').textContent();
    assert.match(guidance, /加载下方固定目录/);
    assert.match(guidance, /任务完成、弹窗关闭后，插件空闲重载/);
    assert.match(guidance, /版本仍不一致.*重新加载本页目录/s);
    assert.equal(await page.evaluate(() => __fixtureCalls.includes('begin_browser_pairing')), false, 'reading the guidance does not pair or update');
    await page.locator('.browser-install-steps button').nth(0).click();
    await page.locator('#extensionDir').click();
    assert.deepEqual(await page.evaluate(() => __fixtureCallArgs.find(call => call[0] === 'open_directory')), ['open_directory',fixedPath]);
    assert.deepEqual(page.errors, []);
  } finally {await page.close();}
});

test('stopped local service and failed status cannot imply the paired browser is online; check can recover', async () => {
  const page = await openDesktop([
    {value:{...paired,running:false}}, {value:{ok:false}}, {value:paired},
  ]);
  try {
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '本地服务未运行');
    assert.equal(await page.locator('#browserDot').evaluate(node => node.classList.contains('on')), false);
    await page.getByRole('button', {name:'检查配对'}).click();
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '无法读取配对状态');
    assert.equal(await page.locator('#browserDot').evaluate(node => node.classList.contains('on')), false);
    await page.getByRole('button', {name:'检查配对'}).click();
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
    assert.equal(await page.locator('#browserDot').evaluate(node => node.classList.contains('on')), true);
    assert.deepEqual(page.errors, []);
  } finally {await page.close();}
});

test('the latest desktop status check wins and a departed browser panel cannot repaint its replacement', async () => {
  const stale = {ok:true,running:false,configured:false,extension_dir:'C:\\old-copy'};
  const page = await openDesktop([{hold:true,value:stale}, {value:paired}, {hold:true,value:stale}, {value:paired}]);
  try {
    await page.waitForFunction(()=>typeof window.__guidanceStatusReplies?.[0]==='function');
    await page.locator('#checkBrowser').click();
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
    await page.evaluate(async()=>{window.__guidanceStatusReplies[0]();await new Promise(requestAnimationFrame);});
    assert.equal(await page.locator('#extensionPath').textContent(), fixedPath, 'late first check cannot overwrite the newer check');
    await page.locator('#checkBrowser').click();
    await page.waitForFunction(()=>typeof window.__guidanceStatusReplies?.[2]==='function');
    await page.locator('[data-page=workbench]').click();
    await page.locator('[data-page=browser]').click();
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
    await page.evaluate(async()=>{window.__guidanceStatusReplies[2]();await new Promise(requestAnimationFrame);});
    assert.equal(await page.locator('#extensionPath').textContent(), fixedPath, 'late response from the previous panel cannot alter the next render');
    assert.deepEqual(page.errors, []);
  } finally {await page.close();}
});

test('pairing replies stay on their browser panel after navigation', async () => {
  const page = await openDesktop();
  try {
    await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
    await page.evaluate(() => {
      __MOCK__.begin_browser_pairing = () => new Promise(resolve=>{window.__releaseGuidancePairing=()=>resolve({ok:true});});
      window.__guidanceToasts = []; window.toast = message => __guidanceToasts.push(message);
    });
    await page.locator('#pairBrowser').click();
    await page.waitForFunction(()=>typeof window.__releaseGuidancePairing==='function');
    await page.locator('[data-page=workbench]').click();
    await page.evaluate(async()=>{window.__releaseGuidancePairing();await new Promise(requestAnimationFrame);});
    assert.deepEqual(await page.evaluate(() => __guidanceToasts), []);
    assert.equal(await page.locator('#browserHint').textContent(), '在视频页提交下载与转写；升级后刷新已打开的视频页。');
    assert.deepEqual(page.errors, []);
  } finally {await page.close();}
});

test('fixed-directory instructions remain accessible at 560 by 500 with long paths in both materials', async () => {
  for (const theme of ['dark','paper']) {
    const longPath = fixedPath.replace('fixture', 'fixture-long-windows-profile-name');
    const page = await openDesktop([{value:{...paired,extension_dir:longPath}}], {theme,scale:1.25,nativeChrome:true}, {width:560,height:500});
    try {
      await page.waitForFunction(() => document.getElementById('browserStatus').textContent === '浏览器已配对');
      await page.locator('.browser-install-steps button').nth(0).click();
      await page.locator('#page-browser summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#page-browser details').getAttribute('open'), '');
      await page.locator('#extensionPath').scrollIntoViewIfNeeded();
      assert.equal(await page.locator('#extensionPath').isVisible(), true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const bounds = await page.locator('#extensionPath').evaluate(node => ({scroll:node.scrollWidth,width:node.clientWidth}));
      assert.ok(bounds.scroll <= bounds.width + 1, 'the fixed path wraps instead of adding horizontal scroll');
      await page.screenshot({path:path.join(shots, `desktop-${theme}-fixed-directory.png`)});
      assert.deepEqual(page.errors, []);
    } finally {await page.close();}
  }
});

test('matching popup versions explain desktop updates and refresh without claiming an update just completed', async () => {
  for (const latest of ['0.17.0', '0.17.0.0']) {
    const page = await openPopup({latest});
    try {
      const note = await page.locator('#versionNote').textContent();
      assert.equal(note, '扩展 0.17.0：固定目录随桌面更新，升级后刷新视频页。');
      assert.equal(await page.locator('#versionNote').isVisible(), true);
      assert.doesNotMatch(note, /已更新|更新成功|最新版本/);
      assert.deepEqual(page.errors, []);
    } finally {await page.close();}
  }
});

test('newer bundled popup version distinguishes pending reload from an old registered copy', async () => {
  const cases = [
    [{latest:'0.17.1'}, /关闭弹窗后，空闲时尝试重载；随后刷新视频页/],
    [{latest:'0.17.1',attempt:{from:'0.17.0',to:'0.17.1'}}, /尚未切换到新版，请从 FoilPage 重新加载固定扩展目录/],
    [{latest:'0.17.1',attempt:{from:'0.16.0',to:'0.17.1'}}, /空闲时尝试重载/],
    [{latest:'0.17.1',storageThrows:true}, /空闲时尝试重载/],
    [{mine:'0.9.0',latest:'0.10.0'}, /空闲时尝试重载/],
  ];
  for (const [options, expected] of cases) {
    const page = await openPopup(options);
    try {
      const note = await page.locator('#versionNote').textContent();
      assert.match(note, expected);
      assert.doesNotMatch(note, /已更新|更新成功|重载后/);
      assert.equal(await page.evaluate(() => __extensionActions.some(([name]) => name === 'write' || name === 'reload')), false, 'popup advice does not itself replace or reload files');
      assert.deepEqual(page.errors, []);
    } finally {await page.close();}
  }
});

test('popup running ahead of desktop directs users to the installed bundle; invalid health versions are omitted', async () => {
  const ahead = await openPopup({mine:'0.18.0',latest:'0.17.1'});
  try {
    assert.match(await ahead.locator('#versionNote').textContent(), /版本不一致，请加载随当前 FoilPage 安装的扩展目录/);
    assert.doesNotMatch(await ahead.locator('#versionNote').textContent(), /尝试重载|已更新/);
    assert.deepEqual(ahead.errors, []);
  } finally {await ahead.close();}
  for (const latest of ['', '0.17.1-dev', '0.17.1.0.2']) {
    const page = await openPopup({latest});
    try {assert.equal(await page.locator('#versionNote').isVisible(), false); assert.deepEqual(page.errors, []);}
    finally {await page.close();}
  }
});

test('popup update advice fits the 360 pixel popup in both schemes and keeps actions reachable', async () => {
  for (const theme of ['light','dark']) {
    const page = await openPopup({latest:'0.17.1',attempt:{from:'0.17.0',to:'0.17.1'}}, theme);
    try {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.locator('#pageDownload').isVisible(), true);
      assert.equal(await page.locator('#pageTranscribe').isVisible(), true);
      const bottom = await page.locator('#jobsPanel').evaluate(node => node.getBoundingClientRect().bottom);
      assert.ok(bottom <= 600, 'version guidance leaves current-page and task sections in the initial popup viewport');
      await page.screenshot({path:path.join(shots, `popup-${theme}-old-copy.png`)});
      assert.deepEqual(page.errors, []);
    } finally {await page.close();}
  }
});
