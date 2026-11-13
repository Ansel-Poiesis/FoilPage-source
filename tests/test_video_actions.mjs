// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Local UI contracts: no real media files are opened or deleted by these fixtures.
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}){
  const page=await browser.newPage({viewport:size});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,options);
  await page.route('http://127.0.0.1:18765/v1/media/**',route=>route.fulfill({status:404,body:'fixture only'}));
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[data-page="videos"]');
  await page.evaluate(()=>switchView('videos'));await page.waitForSelector('.video-card');return page;
}
const calls=(page,name)=>page.evaluate(name=>__fixtureCalls.filter(call=>call===name).length,name);
const card=page=>page.locator('.video-card[data-video-id="download:one"]');
async function confirmDialog(page){
  await card(page).click({button:'right'});await page.getByRole('menuitem',{name:'删除视频',exact:true}).click();
  await page.locator('#videoDeleteConfirm').waitFor();
}
test('video right click opens an accessible menu and location action uses only the media id',async()=>{
  const page=await open();try{
    await card(page).click({button:'right'});assert(await page.getByRole('menu',{name:'视频操作',exact:true}).isVisible());
    assert.equal(await calls(page,'get_video_preview'),0);
    await page.keyboard.press('Enter');await page.waitForFunction(()=>__fixtureCalls.includes('open_video_location'));
    assert.equal(await page.locator('.video-context-menu').count(),0);
    assert.deepEqual(await page.evaluate(()=>__fixtureCallArgs.filter(call=>call[0]==='open_video_location')),[['open_video_location','download:one']]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('video menu supports keyboard navigation, escape, outside clicks and page departure',async()=>{
  const page=await open({}, {width:560,height:500});try{
    const anchor=card(page).locator('.video-menu-trigger');await anchor.focus();await page.keyboard.press('Shift+F10');
    await page.keyboard.press('ArrowDown');assert.equal(await page.evaluate(()=>document.activeElement.textContent),'删除视频');
    await page.keyboard.press('Home');assert.equal(await page.evaluate(()=>document.activeElement.textContent),'打开所在位置');
    await page.keyboard.press('End');await page.keyboard.press('Escape');assert.equal(await page.locator('.video-context-menu').count(),0);assert(await anchor.evaluate(node=>node===document.activeElement));
    await anchor.click();const box=await page.locator('.video-context-menu').boundingBox();assert(box.x>=0&&box.y>=0&&box.x+box.width<=560&&box.y+box.height<=500);
    await page.keyboard.press('Tab');assert.equal(await page.locator('.video-context-menu').count(),0);
    await anchor.click();await page.locator('#pageTitle').click();assert.equal(await page.locator('.video-context-menu').count(),0);
    await anchor.click();await page.evaluate(()=>switchView('workbench'));await page.waitForFunction(()=>!document.querySelector('.video-context-menu'));
    assert.equal(await calls(page,'delete_video'),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('video deletion always asks for confirmation and cancel preserves the card',async()=>{
  const page=await open({settings:{confirm_delete:false}});try{
    await confirmDialog(page);assert.equal(await calls(page,'delete_video'),0);
    assert.match(await page.locator('.video-delete-note').textContent(),/保留.*文本/);
    await page.locator('#videoDeleteCancel').click();assert.equal(await page.locator('.modal').count(),0);assert(await card(page).isVisible());assert.equal(await calls(page,'delete_video'),0);
    assert(await card(page).locator('.video-menu-trigger').evaluate(node=>node===document.activeElement));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('confirmed deletion is single flight and removes only the chosen card',async()=>{
  const page=await open({deleteVideoDelay:100});try{
    await confirmDialog(page);
    await page.evaluate(()=>{const original=__MOCK__.delete_video;__MOCK__.delete_video=id=>{window.__releasedBeforeDelete=!document.querySelector('video');return original(id);};});
    await page.locator('#videoDeleteConfirm').evaluate(node=>{node.click();node.click();});
    await page.waitForFunction(()=>!document.querySelector('.video-card[data-video-id="download:one"]'));
    assert.equal(await calls(page,'delete_video'),1);assert.equal(await page.locator('.video-card').count(),2);assert.equal(await page.locator('.modal').count(),0);
    assert(await page.evaluate(()=>__releasedBeforeDelete));assert.equal(await page.getByRole('status').filter({hasText:'视频已删除'}).count(),1);
    assert.deepEqual(await page.evaluate(()=>__fixtureCallArgs.filter(call=>call[0]==='delete_video')),[['delete_video','download:one']]);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('delete failures preserve the media and allow a later retry; open errors are concise',async()=>{
  const page=await open({deleteVideoFail:true,openVideoFail:true});try{
    await card(page).locator('.video-menu-trigger').click();await page.getByRole('menuitem',{name:'打开所在位置',exact:true}).click();await page.locator('#toast.err').waitFor();
    assert.notEqual(await page.locator('#toast').textContent(),'');await confirmDialog(page);await page.locator('#videoDeleteConfirm').click();
    await page.waitForFunction(()=>document.querySelector('.video-delete-error').textContent.length>0);
    assert(await card(page).isVisible());assert.equal(await page.locator('#videoDeleteConfirm').isDisabled(),false);assert.equal(await calls(page,'delete_video'),1);
    await page.keyboard.press('Escape');assert.equal(await page.locator('.modal').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('active video protection disables deletion while missing and imported media use explicit removal wording',async()=>{
  const items=[
    {id:'active',title:'转写中的视频',available:true,deletable:false,delete_label:'删除视频',deletion_note:'视频正在转写，任务结束后再删除。'},
    {id:'imported',title:'手动导入',available:true,deletable:true,delete_label:'从视频库移除',deletion_note:'仅从视频库移除记录，保留原文件、转写任务与文本结果。'},
    {id:'missing',title:'文件已清理',available:false,deletable:true,delete_label:'移除记录',deletion_note:'文件已不存在，仅移除记录，保留转写任务与文本结果。'},
  ];
  const page=await open({videoItems:items});try{
    await page.locator('.video-card').nth(0).locator('.video-menu-trigger').click();assert(await page.getByRole('menuitem',{name:'删除视频',exact:true}).isDisabled());assert(await page.getByText('视频正在转写，任务结束后再删除。',{exact:true}).isVisible());await page.keyboard.press('Escape');
    await page.locator('.video-card').nth(1).locator('.video-menu-trigger').click();await page.getByRole('menuitem',{name:'从视频库移除',exact:true}).click();assert.equal(await page.locator('#modalTitle').textContent(),'从视频库移除');assert.match(await page.locator('.video-delete-note').textContent(),/保留原文件/);await page.keyboard.press('Escape');
    await page.locator('.video-card').nth(2).locator('.video-menu-trigger').click();assert(await page.getByRole('menuitem',{name:'打开所在位置',exact:true}).isDisabled());await page.keyboard.press('Enter');assert.equal(await page.locator('#videoDeleteConfirm').textContent(),'移除记录');assert.match(await page.locator('.video-delete-note').textContent(),/仅移除记录/);
    assert.equal(await calls(page,'delete_video'),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('external media removal releases an open player and leaving the page releases playback',async()=>{
  const page=await open();try{
    await card(page).locator('.video-cover').click();await page.waitForSelector('video');await page.evaluate(()=>window.__heldVideo=document.querySelector('video'));
    await page.evaluate(async()=>{await __MOCK__.delete_video('download:one');Videos.refreshLight();});
    await page.waitForFunction(()=>!document.querySelector('video'));assert(await page.evaluate(()=>__heldVideo.paused&&__heldVideo.getAttribute('src')===null));assert.equal(await card(page).count(),0);
    await page.locator('.video-cover').first().click();await page.waitForSelector('video');await page.evaluate(()=>{window.__heldVideo=document.querySelector('video');switchView('library');});
    await page.waitForFunction(()=>!document.querySelector('video'));assert(await page.evaluate(()=>__heldVideo.paused&&__heldVideo.getAttribute('src')===null));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a stale refresh cannot resurrect a deleted card or open a late delete modal on another page',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{const original=__MOCK__.get_downloaded_videos;let first=true;__MOCK__.get_downloaded_videos=async()=>{const result=await original();if(first){first=false;const snapshot=structuredClone(result);await new Promise(resolve=>window.__resolveStaleRefresh=resolve);window.__staleRefreshDelivered=true;return snapshot;}return result;};});
    await page.locator('#refreshVideos').click();await page.waitForFunction(()=>typeof __resolveStaleRefresh==='function');await confirmDialog(page);await page.locator('#videoDeleteConfirm').click();
    await page.waitForFunction(()=>!document.querySelector('.video-card[data-video-id="download:one"]'));await page.evaluate(()=>__resolveStaleRefresh());await page.waitForFunction(()=>__staleRefreshDelivered);assert.equal(await card(page).count(),0);assert.equal(await page.locator('.modal').count(),0);
    await page.locator('.video-menu-trigger').first().click();await page.getByRole('menuitem',{name:'删除视频',exact:true}).click();
    await page.evaluate(()=>{const original=__MOCK__.delete_video;__MOCK__.delete_video=async id=>{await new Promise(resolve=>setTimeout(resolve,100));return original(id);};});
    await page.locator('#videoDeleteConfirm').click();await page.keyboard.press('Escape');await page.evaluate(()=>switchView('workbench'));await page.waitForTimeout(180);assert.equal(await page.locator('.modal').count(),0);assert.equal(await page.locator('.video-context-menu').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

// 先生 2026-10-23 视频页视觉整改：混合比例瀑布流＋悬停信息层＋播放器去遮挡。
test('mixed aspect covers render at natural ratios in a masonry flow',async()=>{
  const page=await open();try{
    const gridStyle=await page.locator('#videoGrid').evaluate(node=>getComputedStyle(node));
    assert.equal(gridStyle.display,'flex','视频墙为列容器');
    const cols=await page.locator('#videoGrid .video-col').count();
    assert.ok(cols>=2,'至少两列填充');
    const ratios=await page.evaluate(()=>{
      const read=selector=>{
        const img=document.querySelector(selector);
        if(!img||!img.naturalWidth) return null;
        return {natural:img.naturalWidth/img.naturalHeight, rendered:img.clientWidth/img.clientHeight};
      };
      return {vertical:read('.video-card[data-video-id="download:one"] img'), horizontal:read('.video-card[data-video-id="download:two"] img')};
    });
    assert.ok(ratios.vertical&&Math.abs(ratios.vertical.rendered-ratios.vertical.natural)<0.05,'竖屏封面保留自然比例');
    assert.ok(ratios.horizontal&&Math.abs(ratios.horizontal.rendered-ratios.horizontal.natural)<0.05,'横屏封面保留自然比例');
    const heights=await page.evaluate(()=>({
      vertical:document.querySelector('.video-card[data-video-id="download:one"] img').clientHeight,
      horizontal:document.querySelector('.video-card[data-video-id="download:two"] img').clientHeight,
    }));
    assert.ok(heights.vertical>heights.horizontal*1.4,'竖屏卡片显著高于横屏（瀑布流有落差）');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('masonry scrolls vertically and never horizontally when overflowing',async()=>{
  const vertical='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADwAAABpCAAAAABD1B/qAAAAf0lEQVR4nGNgYOHgERSVlFNS09Y3trBxcPX0DQqLik9OzykoqaxtbO3qmzR99vwlK9Zs3Lpz36Fjp89fvnHnwdOXbz99+/WfYVTzqOZRzaOaRzWPah7VPKp5VPOo5lHNo5pHNY9qHtU8qnlU86jmUc2jmkc1j2oe1TyqeZBqBgBmATaCT64IVgAAAABJRU5ErkJggg==';
  const horizontal='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAKAAAABaCAAAAAAarQKOAAABKElEQVR4nO3O4SpDAQCA0SvSRIsmTZOIRLSsSUSLSEQiWSSytEUiEk1rskS0iEQkIpGIRCSaRJZoWhOJSDSJSDStSbyGH995giMIXt4+viI//wBxYJAkOEQaKgsLj4iMio6JjYtPkCcqlEnJKalp6aqMzKzsnNy8/ILCouKSUnVZeUVlVbWmRqurratvaGxqbmlt07cbjB2dpq7unt4+c//A4NDwyOjY+MTk1PTM7Nz8wuLS8srq2vrG5tb2jmV3b//g0Hp0fGI7tTvOzi8ur65vbu/uH5yPT88vr2/vH5+uL7fn++dXIEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAn+n+AfV/rqCpmW7FgAAAAASUVORK5CYII=';
  const items=Array.from({length:40},(_,i)=>({id:'bulk:'+String(i).padStart(2,'0'),title:'批量视频 '+i,source:'抖音',downloaded_at:'2026-10-01',duration_seconds:60,available:true,deletable:true,delete_label:'删除视频',deletion_note:'删除下载的视频文件，保留转写文本',thumbnail_url:i%3===0?vertical:horizontal}));
  const page=await open({videoItems:items},{width:1000,height:640});try{
    const overflow=await page.waitForFunction(()=>{
      const grid=document.getElementById('videoGrid');
      return grid&&grid.scrollHeight>grid.clientHeight?{sw:grid.scrollWidth,cw:grid.clientWidth,sh:grid.scrollHeight,ch:grid.clientHeight}:null;
    }).then(handle=>handle.jsonValue());
    assert.ok(overflow.sh>overflow.ch,'内容纵向溢出（可向下滚动）');
    assert.ok(overflow.sw<=overflow.cw+1,'不产生横向溢出（先生 2026-10-25：多列横向滚动修复）');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('refresh button lives in the topbar beside the page title',async()=>{
  const page=await open();try{
    const button=page.locator('#refreshVideos');
    assert.equal(await button.count(),1);
    assert.ok(await button.evaluate(node=>!!node.closest('#viewActions')),'刷新按钮在顶栏动作区（视频字样旁）');
    assert.equal(await page.locator('.videos-toolbar #refreshVideos').count(),0,'工具栏不再放刷新按钮');
    const before=await calls(page,'get_downloaded_videos');
    await button.click();
    await page.waitForFunction(before=>__fixtureCalls.filter(call=>call==='get_downloaded_videos').length>=before+1,before);
    await page.evaluate(()=>switchView('workbench'));
    await page.waitForFunction(()=>!document.getElementById('refreshVideos'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('title and metadata reveal on hover and keyboard focus',async()=>{
  const page=await open();try{
    const info=card(page).locator('.video-info');
    assert.equal(await info.evaluate(node=>getComputedStyle(node).opacity),'0','默认收起');
    await card(page).hover();
    await page.waitForFunction(()=>{
      const node=document.querySelector('.video-card[data-video-id="download:one"] .video-info');
      return node&&getComputedStyle(node).opacity==='1';
    });
    assert.equal(await info.locator('.video-name').textContent(),'一次关于创作的谈话');
    assert.equal(await info.locator('.video-meta span').first().textContent(),'抖音');
    // 键盘路径：聚焦封面同样展开（focus-within）。
    await page.keyboard.press('Tab');await page.keyboard.press('Escape');
    await card(page).locator('.video-cover').focus();
    await page.waitForFunction(()=>getComputedStyle(document.querySelector('.video-card[data-video-id="download:one"] .video-info')).opacity==='1');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('player modal sizes to the video instead of a full-width black frame',async()=>{
  // 96x54 真实 mp4（testsrc2 一帧起）：loadedmetadata 后 JS 显式定尺寸，
  // 不放大到整宽——先生 2026-10-25「有声无画」修复的布局契约。
  const tinyMp4=Buffer.from('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','base64');
  const page=await open();try{
    await page.route('http://127.0.0.1:18765/v1/media/**',route=>route.fulfill({status:200,contentType:'video/mp4',body:tinyMp4}));
    await card(page).locator('.video-cover').click();
    await page.waitForSelector('.video-player');
    await page.waitForFunction(()=>{
      const v=document.querySelector('.video-player');
      return v&&v.readyState>=1&&v.videoWidth>0&&v.style.width!=='';
    });
    const box=await page.evaluate(()=>{
      const video=document.querySelector('.video-player');
      const rect=video.getBoundingClientRect();
      const body=video.closest('.modal-body');
      return {w:Math.round(rect.width),h:Math.round(rect.height),
              intrinsic:{w:video.videoWidth,h:video.videoHeight},
              bodyWidth:body.clientWidth};
    });
    assert.ok(box.w<=box.bodyWidth,'视频不超容器');
    assert.ok(box.w<=box.intrinsic.w+2,'小视频不被放大（去除整宽黑框）');
    assert.ok(Math.abs(box.h-box.w*box.intrinsic.h/box.intrinsic.w)<2,'高度按内在比例');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
