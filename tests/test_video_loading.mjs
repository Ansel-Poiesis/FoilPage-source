// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}) {
  const page=await browser.newPage({viewport:size});
  page.errors=[];page.on('pageerror',e=>page.errors.push(e.message));
  await page.addInitScript(installMock,options);
  await page.goto(pathToFileURL(path.resolve('app/webview_ui/index.html')).href);
  await page.waitForFunction(()=>window.__appReady);
  return page;
}
test('video reading state is centered in the content, appropriately sized and accessible',async()=>{
  for (const size of [{width:1280,height:800},{width:560,height:500}]) {
    const page=await open({videoDelay:900},size);
    try {
      await page.evaluate(()=>switchView('videos'));
      await page.waitForSelector('#videosReadState:not([hidden])');
      const rect=await page.evaluate(()=>{
        const region=document.querySelector('.videos-content').getBoundingClientRect();
        const heading=document.querySelector('#videosReadState h2').getBoundingClientRect();
        return {cx:region.x+region.width/2,hx:heading.x+heading.width/2,
          ry:region.y,rh:region.height,hy:heading.y+heading.height/2,
          font:parseFloat(getComputedStyle(document.querySelector('#videosReadState h2')).fontSize),
          busy:document.querySelector('#videoGrid').getAttribute('aria-busy')};
      });
      assert(Math.abs(rect.cx-rect.hx)<2);assert(rect.hy>rect.ry+rect.rh*.35 && rect.hy<rect.ry+rect.rh*.7);
      assert(rect.font>=18);assert.equal(rect.busy,'true');
      await page.waitForSelector('.video-card');
      assert.equal(await page.locator('#videosReadState').isVisible(),false);
      assert.equal(await page.locator('#videoGrid').getAttribute('aria-busy'),'false');
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  }
});
test('empty, failure retry and a late reply cannot replace a newer page',async()=>{
  const page=await open({videosFail:true});
  try {
    await page.evaluate(()=>switchView('videos'));
    await page.waitForSelector('#videosReadState.is-error');
    assert.equal(await page.locator('#videosReadState').getAttribute('role'),'alert');
    await page.evaluate(()=>__MOCK__.get_downloaded_videos=async()=>({ok:true,items:[]}));
    await page.locator('#videosReadState button').click();
    await page.waitForSelector('#videoGrid .empty');
    assert.equal(await page.locator('#videosReadState').isVisible(),false);
    await page.evaluate(()=>{__MOCK__.get_downloaded_videos=()=>new Promise(resolve=>window.resolveVideos=resolve);});
    await page.locator('#refreshVideos').click();
    await page.waitForFunction(()=>typeof resolveVideos==='function');
    await page.evaluate(()=>switchView('import'));
    await page.evaluate(()=>resolveVideos({ok:true,items:[{id:'late',title:'迟到的视频',available:true}]}));
    await page.waitForTimeout(80);
    assert.equal(await page.locator('.video-card[data-video-id=late]').count(),0);
    assert(await page.locator('#mediaDropZone').isVisible());assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});
