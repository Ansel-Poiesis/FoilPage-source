// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1480,height:940}});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(require('./ui_fixture.cjs'));
    await page.goto(pathToFileURL(path.resolve('app/webview_ui/index.html')).href);
    await page.waitForFunction(()=>window.__appReady);
    await page.evaluate(()=>switchView('settings'));
    // Controls carry their persisted field in data-persisted-value rather than a
    // selector contract, so locate the actual select by its option values.
    const picker=page.locator('select').filter({has:page.locator('option[value="zh-CN"]')});
    await picker.selectOption('en');
    await page.waitForFunction(()=>document.documentElement.lang==='en');
    await page.getByRole('button',{name:'Library',exact:true}).click();
    await page.waitForSelector('.reader-body h1');
    assert.equal(await page.getByPlaceholder('Search documents').count(),1);
    const original=await page.locator('.reader-body').innerText();
    assert(original.includes('先留下原话'));
    assert((await page.locator('.doc-title').first().innerText()).includes('完整标题不应被截断'));
    await page.locator('.doc-title').first().evaluate(el=>{el.textContent='设置';el.title='设置';});
    await page.evaluate(()=>I18n.translate());
    assert.equal(await page.locator('.doc-title').first().innerText(),'设置');
    assert.equal(await page.locator('.doc-title').first().getAttribute('title'),'设置');
    await page.getByRole('button',{name:'Settings',exact:true}).click();
    assert.equal(await page.locator('option[value="paper"]').innerText(),'Light (White Rock)');
    await page.locator('select').filter({has:page.locator('option[value="zh-CN"]')}).selectOption('zh-CN');
    await page.waitForFunction(()=>document.documentElement.lang==='zh-CN');
    await page.getByRole('button',{name:'文本库',exact:true}).click();
    await page.waitForSelector('.reader-body h1');
    assert.equal(await page.locator('.reader-body').innerText(),original);
    assert.equal(await page.getByPlaceholder('搜索文本').count(),1);
    const patches=await page.evaluate(()=>window.__fixtureCallArgs.filter(x=>x[0]==='save_settings').map(x=>x[1]));
    assert.deepEqual(patches.filter(x=>'ui_language' in x),[{ui_language:'en'},{ui_language:'zh-CN'}]);
    assert(!patches.some(x=>'language' in x));
    assert.deepEqual(errors,[]);
    console.log('UI language: saved switch, theme names, dynamic navigation, content isolation, Chinese restore passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
