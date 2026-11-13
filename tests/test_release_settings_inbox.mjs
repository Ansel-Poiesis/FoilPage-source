// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Settings and inbox acceptance using synthetic responses only: no native or provider operations.
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..'),shots=path.join(root,'.ui-audit','release-1','frontend');
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(section='data',options={},size={width:1280,height:800}){
  const page=await browser.newPage({viewport:size,reducedMotion:'reduce'});page.errors=[];page.setDefaultTimeout(6000);page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{loggedIn:true,...options});
  await page.addInitScript(()=>{
    window.__settingsPickerReplies=[];window.__settingsPickerCalls=0;window.__inboxReply={ok:true,events:[],updates:[]};window.__openedUrls=[];
    __MOCK__.pick_video_scan_directory=()=>{__settingsPickerCalls++;return __settingsPickerReplies.shift()||{ok:false,cancelled:true};};
    __MOCK__.get_product_notifications=()=>__inboxReply;
    __MOCK__.open_url=url=>{__openedUrls.push(url);return {ok:true};};
  });
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded'});await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=settings]').click();await page.locator(`.sitem[data-section=${section}]`).click();return page;
}
const expand=(page,label)=>page.locator('details > summary').filter({hasText:new RegExp('^'+label+'$')}).click();
test('failed retention defaults off, rejects invalid bounds and persists confirmed integer settings',async()=>{
  const page=await open();try{
    await expand(page,'失败记录清理');const days=page.getByLabel('保留天数',{exact:true}),hours=page.getByLabel('检查间隔（小时）',{exact:true});
    assert.equal(await days.inputValue(),'0');assert.equal(await hours.inputValue(),'24');assert.match(await page.locator('#settingsPane').textContent(),/无正文、无文件的空失败记录/);
    const before=await page.evaluate(()=>__fixtureCalls.filter(x=>x==='save_settings').length);
    for(const value of ['','-1','3651','1.5']){await days.fill(value);await days.press('Enter');assert.equal(await days.getAttribute('aria-invalid'),'true');}
    for(const value of ['0','721','2.5']){await hours.fill(value);await hours.press('Enter');assert.equal(await hours.getAttribute('aria-invalid'),'true');}
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='save_settings').length),before);
    await days.fill('30');await days.press('Enter');await page.waitForFunction(()=>AppState.settings.failed_task_retention_days===30);
    await hours.fill('72');await hours.press('Enter');await page.waitForFunction(()=>AppState.settings.failed_task_cleanup_interval_hours===72);
    await days.fill('0');await days.press('Enter');await page.waitForFunction(()=>AppState.settings.failed_task_retention_days===0);
    assert.equal(await page.evaluate(()=>__fixtureCalls.includes('cleanup_orphans')||__fixtureCalls.includes('delete_tasks')),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('video directories use a native picker, reject relative and network paths, deduplicate and remove configuration only',async()=>{
  const page=await open('data',{settings:{download_dir:'C:\\Videos\\Downloads',video_scan_directories:[],video_scan_recursive:false}});try{
    await expand(page,'视频读取');const add=page.locator('#addVideoScanDirectory');
    await page.evaluate(()=>__settingsPickerReplies.push({ok:false,cancelled:true},{ok:true,path:'relative/videos'},{ok:true,path:'\\\\server\\share'},{ok:true,path:'c:\\videos\\downloads\\'},{ok:true,path:'D:\\Films\\精选'}));
    for(let index=1;index<=5;index++){await add.click();await page.waitForFunction(index=>__settingsPickerCalls===index&&!document.querySelector('#addVideoScanDirectory').disabled,index);}
    await page.waitForFunction(()=>AppState.settings.video_scan_directories?.length===1);
    assert.deepEqual(await page.evaluate(()=>AppState.settings.video_scan_directories),['D:\\Films\\精选']);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='save_settings').length),1);
    await page.getByRole('switch',{name:'读取子目录',exact:true}).click();await page.waitForFunction(()=>AppState.settings.video_scan_recursive===true);
    await page.getByRole('button',{name:'移除目录 D:\\Films\\精选',exact:true}).click();await page.waitForFunction(()=>AppState.settings.video_scan_directories.length===0);
    assert.match(await page.locator('#videoScanDirectories').textContent(),/仅读取默认下载目录/);
    const saves=await page.evaluate(()=>__fixtureCalls.filter(x=>x==='save_settings').length);
    await page.evaluate(()=>{AppState.settings.video_scan_directories=Array.from({length:20},(_,index)=>'D:\\Folder'+index);Settings.render();__settingsPickerReplies.push({ok:true,path:'D:\\Folder21'});});
    await add.click();await page.waitForFunction(()=>__settingsPickerCalls===6&&!document.querySelector('#addVideoScanDirectory').disabled);
    assert.equal(await page.evaluate(()=>AppState.settings.video_scan_directories.length),20);assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='save_settings').length),saves);
    assert.equal(await page.evaluate(()=>__fixtureCalls.some(x=>['delete_files','delete_video','cleanup_orphans'].includes(x))),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('late directory selection cannot write after leaving its configuration page',async()=>{
  const page=await open();try{
    await expand(page,'视频读取');await page.evaluate(()=>__MOCK__.pick_video_scan_directory=()=>new Promise(resolve=>window.__finishDirectory=resolve));
    await page.locator('#addVideoScanDirectory').click();await page.waitForFunction(()=>typeof __finishDirectory==='function');
    await page.locator('.sitem[data-section=about]').click();await page.evaluate(()=>__finishDirectory({ok:true,path:'D:\\Late'}));
    await page.waitForTimeout(30);assert.equal(await page.evaluate(()=>__fixtureCalls.includes('save_settings')),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
const snapshot=()=>({ok:true,events:[
  {event_id:'credit-one',kind:'order_credited',title:'充值到账',message:'已到账 500 积分',created_at:'2026-10-09T01:00:00Z',expires_at:'2099-01-01T00:00:00Z'},
  {event_id:'credit-one',kind:'order_credited',title:'重复',message:'不应重复',created_at:'2026-10-09T01:00:00Z'},
  {event_id:'expired',kind:'notice',title:'过期',expires_at:'2000-01-01T00:00:00Z'},
  {event_id:'plain',kind:'notice',title:'<img src=x onerror=alert(1)>',message:'按原样显示',created_at:'2026-10-09T02:00:00Z'},
],updates:[
  {event_id:'update-formal',kind:'software_update',channel:'stable',version:'1.0.1',url:'https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v1.0.1',title:'FoilPage 1.0.1',message:'新的更新已发布',created_at:'2026-10-09T03:00:00Z'},
  {event_id:'update-test',kind:'software_update',channel:'test',version:'1.1.0',url:'https://github.com/Ansel-Poiesis/FoilPage-source/releases',title:'测试更新',created_at:'2026-10-09T03:00:00Z'},
  {event_id:'update-unsafe',kind:'software_update',channel:'stable',version:'1.0.2',url:'javascript:alert(1)',title:'无有效下载链接',created_at:'2026-10-09T02:00:00Z'},
]});
test('notification snapshot updates the folded inbox without toasts, duplicate, expired or opposite-channel entries',async()=>{
  const page=await open('about');try{
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:value})),snapshot());
    const inbox=page.locator('#productNotificationInbox');assert.equal(await inbox.evaluate(node=>node.open),false);
    await expand(page,'通知与更新4');const list=page.locator('#productNotificationsList');
    assert.equal(await list.locator(':scope > li').count(),4);assert.doesNotMatch(await list.textContent(),/不应重复|过期|测试更新/);
    assert.equal(await list.locator('img').count(),0);assert.match(await list.textContent(),/<img src=x onerror=alert\(1\)>/);
    await list.locator('li:first-child').evaluate(node=>node.dataset.retained='true');
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:value})),snapshot());
    assert.equal(await list.locator('li:first-child').getAttribute('data-retained'),'true','unchanged periodic snapshots keep reading and text selection stable');
    assert.equal(await list.getByRole('button',{name:'查看更新'}).count(),1);
    await list.getByRole('button',{name:'查看更新'}).click();assert.deepEqual(await page.evaluate(()=>__openedUrls),['https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v1.0.1']);
    await page.evaluate(()=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:{ok:false}})));assert.match(await inbox.textContent(),/暂时无法读取/);
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:value})),snapshot());assert.equal(await list.locator(':scope > li').count(),4,'recovery restores the previous valid snapshot');
    assert.equal(await page.locator('.toast').isVisible(),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a live snapshot wins over a stale read and is not reused across account contexts',async()=>{
  const page=await open('about');try{
    await page.evaluate(()=>__MOCK__.get_product_notifications=()=>new Promise(resolve=>window.__finishInbox=resolve));
    await expand(page,'通知与更新');await page.getByRole('button',{name:'刷新通知',exact:true}).click();await page.waitForFunction(()=>typeof __finishInbox==='function');
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:value})),snapshot());
    await page.evaluate(()=>__finishInbox({ok:true,events:[{event_id:'stale',title:'过时读取'}],updates:[]}));
    assert.doesNotMatch(await page.locator('#productNotificationsList').textContent(),/过时读取/);
    await page.evaluate(()=>{AppState.settings.account_login_token='another-synthetic-account';__MOCK__.get_product_notifications=()=>({ok:true,events:[],updates:[]});Settings.render();});
    await page.waitForFunction(()=>document.querySelector('#productNotificationsList').textContent==='暂无新通知');
    assert.doesNotMatch(await page.locator('#productNotificationsList').textContent(),/500|1\.0\.1/);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
for(const theme of ['dark','paper','nord-light'])test(`scan settings and inbox fit ${theme} at 560px`,async()=>{
  const page=await open('data',{theme,scale:1.3,settings:{download_dir:'D:\\Media\\Downloads',video_scan_directories:['D:\\Media\\长目录名称\\第二层\\收藏视频']}},{width:560,height:700});try{
    await expand(page,'视频读取');await page.screenshot({path:path.join(shots,theme+'-560-scan-settings.png')});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.locator('#settingsPane').evaluate(node=>node.scrollWidth>node.clientWidth+1),false);
    await expand(page,'失败记录清理');await page.getByLabel('保留天数',{exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(shots,theme+'-560-retention-settings.png')});
    await page.locator('.sitem[data-section=about]').click();await page.evaluate(value=>window.dispatchEvent(new CustomEvent('shengjian:product-notifications',{detail:value})),snapshot());
    await expand(page,'通知与更新4');await page.locator('#productNotificationsList li:first-child').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(shots,theme+'-560-product-inbox.png')});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.locator('#settingsPane').evaluate(node=>node.scrollWidth>node.clientWidth+1),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
