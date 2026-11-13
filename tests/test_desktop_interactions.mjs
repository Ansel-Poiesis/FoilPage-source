// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Real DOM interactions; fixture bridges never download, pay or call a model.
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
let browser;
before(async()=>{fs.mkdirSync(path.join(root,'.ui-audit','release-1','frontend'),{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},width=1280){
  const page=await browser.newPage({viewport:{width,height:800}});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',e=>page.errors.push(e.message));
  await page.addInitScript(installMock,options);
  await page.route('http://127.0.0.1:18765/v1/media/**',r=>r.fulfill({status:404,body:'fixture only'}));
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);await page.waitForSelector('.nav-row[aria-current=page]');await page.locator('[data-page=workbench]').click();await page.waitForSelector('.job-open');return page;
}
async function selected(page){return page.evaluate(()=>[...AppState.selected].sort());}
async function ids(page){return page.locator('.job').evaluateAll(rows=>rows.map(row=>row.dataset.taskId));}
async function step(page){return page.evaluate(()=>Onboarding.__shengjianOnboardingInternals.state().index);}
async function calls(page,name){return page.evaluate(name=>window.__fixtureCalls.filter(call=>call===name).length,name);}

test('clipboard fallback reports a refusal and restores dialog focus without leaving temporary fields',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{
      window.__copyCompletions=0;
      document.execCommand=()=>false;
      openModal('复制文本','<p>复制当前文本</p>','<button class="btn" id="copyFixture">复制</button>');
      document.getElementById('copyFixture').onclick=()=>fallbackCopy('声音记录',()=>{window.__copyCompletions++;toast('已复制');});
    });
    await page.locator('#copyFixture').click();
    assert.equal(await page.locator('#toast').textContent(),'复制失败');
    assert.equal(await page.evaluate(()=>window.__copyCompletions),0);
    assert.equal(await page.locator('body > textarea').count(),0);
    assert(await page.locator('#copyFixture').evaluate(e=>e===document.activeElement));
    await page.evaluate(()=>{document.execCommand=()=>true;});
    await page.locator('#copyFixture').click();
    assert.equal(await page.locator('#toast').textContent(),'已复制');
    assert.equal(await page.evaluate(()=>window.__copyCompletions),1);
    assert.equal(await page.locator('body > textarea').count(),0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('delete confirmation setting failures remain enabled and successful saves merge only their own field',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{window.__confirmed=0;__MOCK__.save_settings=()=>({ok:false,error:'fixture refusal'});ConfirmDelete.run({onConfirm:()=>window.__confirmed++});});
    await page.locator('#dlgNoAsk').check();await page.locator('#dlgOk').click();
    await page.waitForFunction(()=>document.getElementById('toast').textContent==='未能保存删除确认设置');
    assert.notEqual(await page.evaluate(()=>AppState.settings.confirm_delete),false);
    assert.equal(await page.evaluate(()=>window.__confirmed),1);
    await page.evaluate(()=>{AppState.settings.theme='paper';__MOCK__.save_settings=()=>({confirm_delete:false,theme:'dark'});ConfirmDelete.run({onConfirm:()=>window.__confirmed++});});
    await page.locator('#dlgNoAsk').check();await page.locator('#dlgOk').click();
    await page.waitForFunction(()=>AppState.settings.confirm_delete===false);
    assert.equal(await page.evaluate(()=>AppState.settings.theme),'paper');
    assert.equal(await page.evaluate(()=>window.__confirmed),2);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('discard dialog defaults to keeping edits and runs the continuation only after confirmation',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{window.__discards=0;ConfirmDiscard.run({onDiscard:()=>window.__discards++});});
    assert(await page.locator('#dlgCancel').evaluate(e=>e===document.activeElement));
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>window.__discards),0);
    await page.evaluate(()=>ConfirmDiscard.run({onDiscard:()=>window.__discards++}));
    await page.locator('#discardChanges').click();
    assert.equal(await page.locator('.modal').count(),0);
    assert.equal(await page.evaluate(()=>window.__discards),1);
    assert.equal(await page.locator('.app').evaluate(e=>e.inert),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('Shift range keeps its anchor when extending, shrinking and reversing',async()=>{
  const page=await open();try{
    const order=await ids(page);await page.locator('.job-open').nth(1).click();
    await page.locator('.job-open').nth(6).click({modifiers:['Shift']});assert.deepEqual(await selected(page),order.slice(1,7).sort());
    await page.locator('.job-open').nth(3).click({modifiers:['Shift']});assert.deepEqual(await selected(page),order.slice(1,4).sort());
    await page.locator('.job-open').nth(0).click({modifiers:['Shift']});assert.deepEqual(await selected(page),order.slice(0,2).sort());
    assert.equal(await page.evaluate(()=>AppState.anchorId),order[1]);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('checkbox Shift ranges preserve outside selections and support shrinking and clearing',async()=>{
  const page=await open();try{
    const order=await ids(page);const checks=page.locator('.job-check-input');await checks.nth(0).click();await checks.nth(7).click();
    await checks.nth(3).click({modifiers:['Shift']});assert.deepEqual(await selected(page),[order[0],...order.slice(3,8)].sort());
    await checks.nth(5).click({modifiers:['Shift']});assert.deepEqual(await selected(page),[order[0],...order.slice(5,8)].sort());
    await checks.nth(7).click();await checks.nth(2).click({modifiers:['Shift']});assert.deepEqual(await selected(page),[order[0]]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('Shift key alone never changes selection and checkbox Space changes one item',async()=>{
  const page=await open();try{
    await page.locator('.job-open').nth(2).click();const before=await selected(page);
    await page.keyboard.down('Shift');await page.mouse.move(350,480);await page.keyboard.up('Shift');assert.deepEqual(await selected(page),before);
    await page.locator('.job-check-input').nth(4).focus();await page.keyboard.press('Space');assert.equal((await selected(page)).length,2);
    await page.locator('.job-check').nth(6).evaluate(label=>label.click());assert.equal((await selected(page)).length,3,'native label activation changes only its checkbox');
  }finally{await page.close();}
});
test('filter and pushed task removal invalidate hidden selections and anchors',async()=>{
  const page=await open();try{
    await page.locator('.job-open').nth(1).click();await page.getByRole('button',{name:'已完成',exact:false}).first().click();
    assert.deepEqual(await selected(page),[]);assert.equal(await page.evaluate(()=>AppState.anchorId),null);
    await page.locator('.job-open').first().click({modifiers:['Shift']});assert.equal((await selected(page)).length,1);
    await page.evaluate(()=>{const id=[...AppState.selected][0];__onState({tasks:AppState.tasks.filter(task=>task.task_id!==id)});});
    assert.deepEqual(await selected(page),[]);assert.equal(await page.evaluate(()=>AppState.anchorId),null);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a sort change rebuilds visible rows and subsequent range uses current order',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{AppState.tasks.forEach((task,i)=>task.updated_at='2026-10-04T00:00:'+String(i).padStart(2,'0'));Workbench.render();});
    await page.locator('.job-open').nth(2).click();const anchor=await page.evaluate(()=>AppState.anchorId);
    await page.evaluate(()=>{AppState.settings.task_sort_order='oldest';Workbench.render();});const order=await ids(page),at=order.indexOf(anchor);
    await page.locator('.job-open').first().click({modifiers:['Shift']});assert.deepEqual(await selected(page),order.slice(0,at+1).sort());
  }finally{await page.close();}
});
test('guide sheet empty space advances while text and real controls retain their own action',async()=>{
  const page=await open();try{
    await page.evaluate(() => switchView('guide'));
    await page.locator('.ob-chapter-choice[data-chapter="environment"] .ob-start').click();
    await page.locator('.ob-lead').click();assert.equal(await step(page),0);
    await page.locator('.ob-body').click({position:{x:2,y:2}});assert.equal(await step(page),1);
    await page.locator('#setupDownloadDirBtn').click();assert.equal(await calls(page,'pick_setting_dir'),1);assert.equal(await step(page),1);
    await page.getByRole('button',{name:'跳过本轮全部引导'}).click();assert.equal(await page.locator('.ob-card').count(),0);
    assert.equal(await page.evaluate(()=>AppState.settings.onboarding_chapter_progress.chapters.environment.completed),false);
    await page.evaluate(()=>Onboarding.maybeShow());assert.equal(await page.locator('.ob-card').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('browser fallback hides custom window controls and every page omits pageContext',async()=>{
  const page=await open();try{
    assert.equal(await page.locator('#windowChrome').isVisible(),false);assert.equal(await page.locator('#pageContext').count(),0);
    const nav=await page.locator('.nav-row').evaluateAll(rows=>rows.map(row=>row.dataset.page));assert.deepEqual(nav.slice(0,4),['setup','workbench','videos','library']);
    assert.equal(await calls(page,'minimize_window'),0);assert.equal(await calls(page,'hide_window'),0);
  }finally{await page.close();}
});
test('native controls share the page title row inside the main panel at every width and page',async()=>{
  const page=await open({nativeChrome:true});try{
    for(const size of [{width:1440,height:900},{width:1280,height:800},{width:960,height:800},{width:640,height:800},{width:560,height:500}]){
      await page.setViewportSize(size);
      for(const view of ['workbench','videos','library','setup','settings','links','import','browser']) {
        await page.evaluate(view=>switchView(view),view);
        const layout=await page.evaluate(()=>{
          const box=selector=>document.querySelector(selector).getBoundingClientRect();
          const main=box('.main'),side=box('.sidebar'),header=box('.topbar'),title=box('#pageTitle'),chrome=box('#windowChrome'),drag=box('.topbar.window-drag-region'),edge=box('.window-drag-edge');
          return {main:{left:main.left,right:main.right,top:main.top},sideTop:side.top,header:{top:header.top,bottom:header.bottom},titleCenter:(title.top+title.bottom)/2,chrome:{left:chrome.left,right:chrome.right,top:chrome.top,bottom:chrome.bottom},dragWidth:drag.width,titleWidth:title.width,edge:{top:edge.top,width:edge.width,height:edge.height},appHeight:box('.app').height,viewportHeight:innerHeight,inside:!!document.querySelector('.main > .topbar > #windowChrome'),overflow:document.documentElement.scrollWidth>innerWidth,buttons:[...document.querySelectorAll('.window-control')].map(button=>{const r=button.getBoundingClientRect(),s=button.querySelector('svg').getBoundingClientRect();return {width:r.width,height:r.height,center:(r.top+r.bottom)/2,dx:Math.abs((r.left+r.right-s.left-s.right)/2),dy:Math.abs((r.top+r.bottom-s.top-s.bottom)/2),drag:getComputedStyle(button).getPropertyValue('-webkit-app-region')};})};
        });
        assert(layout.inside&&layout.chrome.left>layout.main.left&&layout.chrome.right<layout.main.right,view+' controls belong to the main panel');
        assert(layout.chrome.top>=layout.header.top&&layout.chrome.bottom<=layout.header.bottom);
        assert(layout.buttons.every(g=>g.width===44&&g.height===44&&g.dx<1&&g.dy<1&&Math.abs(g.center-layout.titleCenter)<1&&g.drag==='no-drag'));
        assert.equal(layout.appHeight,layout.viewportHeight);assert.equal(layout.sideTop,layout.main.top);assert(layout.main.top<20,'there is no separate window strip');
        assert(layout.dragWidth>layout.titleWidth+32,'the title row retains blank native drag space');assert.equal(layout.overflow,false);
        assert(layout.edge.top===0&&layout.edge.height===12&&layout.edge.width===size.width-16,'the top edge remains a separate full-width drag affordance');
      }
    }assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('native chrome APIs are exact, keyboard usable and maximize state follows snapshots',async()=>{
  const page=await open({nativeChrome:true});try{
    await page.getByRole('button',{name:'最小化',exact:true}).click();assert.equal(await calls(page,'minimize_window'),1);
    await page.getByRole('button',{name:'最大化',exact:true}).focus();await page.keyboard.press('Enter');
    await page.getByRole('button',{name:'还原窗口',exact:true}).waitFor();assert.equal(await calls(page,'toggle_maximize_window'),1);
    // An external native resize updates backend state before publishing its snapshot.
    await page.evaluate(()=>{window.__fixtureWindowChrome.maximized=false;__onState({window_chrome:{...window.__fixtureWindowChrome}});});await page.getByRole('button',{name:'最大化',exact:true}).waitFor();
    await page.locator('.topbar.window-drag-region').dispatchEvent('dblclick');await page.getByRole('button',{name:'还原窗口',exact:true}).waitFor();assert.equal(await calls(page,'toggle_maximize_window'),2);
    await page.locator('#pageTitle').dblclick();await page.getByRole('button',{name:'最大化',exact:true}).waitFor();assert.equal(await calls(page,'toggle_maximize_window'),3,'the title is also draggable, and nested regions invoke maximize once');
    await page.locator('.brand.window-drag-region').dispatchEvent('dblclick');await page.getByRole('button',{name:'还原窗口',exact:true}).waitFor();assert.equal(await calls(page,'toggle_maximize_window'),4);
    await page.locator('.window-drag-edge').dispatchEvent('dblclick');await page.getByRole('button',{name:'最大化',exact:true}).waitFor();assert.equal(await calls(page,'toggle_maximize_window'),5);
    await page.getByRole('button',{name:'关闭窗口',exact:true}).click();assert.equal(await calls(page,'hide_window'),1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('integrated window controls stay beneath modal and guide layers with usable dismissal',async()=>{
  const page=await open({nativeChrome:true});try{
    await page.setViewportSize({width:560,height:500});
    await page.evaluate(()=>openModal('确认操作','<p>对话框覆盖底层窗口操作</p>','<button class="btn" id="chromeModalCancel">返回</button>'));
    assert.equal(await page.locator('.app').evaluate(node=>node.inert),true);
    assert(await page.locator('#windowHide').evaluate(node=>{const r=node.getBoundingClientRect();return !!document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('.modal-mask');}));
    await page.keyboard.press('Escape');assert.equal(await page.locator('.modal').count(),0);assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);
    await page.evaluate(() => switchView('guide'));
    const guide=await page.locator('.ob-card').boundingBox(),header=await page.locator('.topbar').boundingBox();
    assert(guide.y>=header.y+header.height&&guide.y+guide.height<=500,'short-window guide fits below the integrated title row');
    assert(await page.locator('#windowHide').evaluate(node=>{const r=node.getBoundingClientRect();return !!document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('.ob-shade');}));
    await page.getByRole('button',{name:'关闭新手引导',exact:true}).click();assert.equal(await page.locator('.ob-card').count(),0);
    await page.evaluate(()=>toast('已完成'));
    assert((await page.locator('#toast').boundingBox()).y>=header.y+header.height,'notifications leave the title controls unobstructed');
    assert.equal(await calls(page,'hide_window'),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('native caption disables the mousemove fallback and avoids a second maximize call',async()=>{
  const page=await open({nativeChrome:true});try{
    await page.evaluate(()=>__onState({window_chrome:{enabled:true,native_drag:true,maximized:false}}));
    const regions=page.locator('.window-drag-region');
    assert.equal(await regions.count(),4,'brand, topbar, title copy and top edge each support native dragging');
    assert.deepEqual(await regions.evaluateAll(nodes=>nodes.map(node=>[node.classList.contains('window-drag-fallback'),node.style.getPropertyValue('-webkit-app-region')])),[[false,'drag'],[false,'drag'],[false,'drag'],[false,'drag']]);
    assert.equal(await page.locator('#pageTitle').evaluate(node=>getComputedStyle(node).getPropertyValue('-webkit-app-region')),'drag');
    for(const selector of ['.topbar.window-drag-region','.brand.window-drag-region','.window-drag-edge','#pageTitle'])await page.locator(selector).dispatchEvent('dblclick');
    assert.equal(await calls(page,'toggle_maximize_window'),0,'native caption consumes double click without invoking the JS fallback');
    for(const selector of ['#windowChrome','#windowMinimize','#windowMaximize','#windowHide'])assert.equal(await page.locator(selector).evaluate(node=>getComputedStyle(node).getPropertyValue('-webkit-app-region')),'no-drag');
    await page.evaluate(()=>openModal('检查遮罩','<p>底层不应进入原生拖动</p>'));
    await page.waitForFunction(()=>[...document.querySelectorAll('.window-drag-region')].every(node=>node.style.getPropertyValue('-webkit-app-region')==='no-drag'));
    assert.equal(await page.locator('.window-drag-edge').evaluate(node=>getComputedStyle(node).pointerEvents),'none');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>[...document.querySelectorAll('.window-drag-region')].every(node=>node.style.getPropertyValue('-webkit-app-region')==='drag'));
    await page.screenshot({path:path.join(root,'.ui-audit','release-1','frontend','native-caption-regions.png')});
    await page.evaluate(()=>__onState({window_chrome:{enabled:true,native_drag:false,maximized:false}}));
    assert.deepEqual(await regions.evaluateAll(nodes=>nodes.map(node=>node.classList.contains('window-drag-fallback'))),[true,true,true,true]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('video cards show media metadata, inaccessible files and a safe keyboard player',async()=>{
  const page=await open();try{
    await page.locator('[data-page="videos"]').click();await page.waitForSelector('.video-card');assert.equal(await page.locator('.video-card').count(),3);
    assert.deepEqual(await page.locator('.video-duration').allTextContents(),['3:04','1:01:01']);assert(await page.locator('.video-cover').nth(2).isDisabled());
    await page.locator('.video-cover').first().focus();await page.keyboard.press('Enter');await page.waitForSelector('video.video-player');
    const video=page.locator('video');assert(await video.evaluate(node=>node.controls&&node.playsInline));assert.match(await video.getAttribute('src'),/^http:\/\/127\.0\.0\.1:/);
    await page.evaluate(()=>window.__testVideo=document.querySelector('video'));await page.keyboard.press('Escape');assert.equal(await page.locator('video').count(),0);
    assert(await page.evaluate(()=>__testVideo.paused&&__testVideo.getAttribute('src')===null));assert(await page.locator('.video-cover').first().evaluate(node=>node===document.activeElement));
    const args=await page.evaluate(()=>window.__fixtureCallArgs);assert(args.some(call=>call[0]==='get_video_preview'&&call[1]==='download:one'));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('empty and failed video lists keep an actionable state',async()=>{
  for(const options of [{videoItems:[]},{videosFail:true}]){
    const page=await open(options);try{
      await page.locator('[data-page="videos"]').click();
      if(options.videosFail)await page.getByText('读取视频失败',{exact:true}).waitFor();
      else{await page.getByRole('button',{name:'下载视频',exact:true}).click();assert.equal(await page.evaluate(()=>AppState.currentView),'links');}
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  }
});
test('preview errors and non-loopback URLs do not open an untrusted player',async()=>{
  for(const options of [{previewFail:true},{previewUrl:'https://example.com/video.mp4'},{previewUrl:'javascript:alert(1)'}]){
    const page=await open(options);try{
      await page.locator('[data-page="videos"]').click();await page.locator('.video-cover').first().click();
      await page.waitForFunction(()=>document.querySelector('#videosStatus').getAttribute('role')==='alert');assert.equal(await page.locator('video').count(),0);
      assert.equal(await page.locator('.video-cover').first().isDisabled(),false);assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  }
});
test('leaving video page during preview request never opens a late modal',async()=>{
  const page=await open({previewDelay:150});try{
    await page.locator('[data-page="videos"]').click();await page.locator('.video-cover').first().click();await page.locator('[data-page="library"]').click();
    await page.waitForTimeout(220);assert.equal(await page.locator('video').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('body and title AI controls save independently and BYOK is not presented as credit billing',async()=>{
  const page=await open({settings:{agent_enabled:false,title_optimization_enabled:true}});try{
    await page.locator('[data-page="settings"]').click();await page.locator('[data-section="agent"]').click();
    const body=page.getByRole('switch',{name:'AI 整理正文',exact:true}),title=page.getByRole('switch',{name:'AI 优化标题',exact:true});
    assert.equal(await body.getAttribute('aria-checked'),'false');assert.equal(await title.getAttribute('aria-checked'),'true');
    await body.click();await page.waitForFunction(()=>AppState.settings.agent_enabled===true);assert.equal(await title.getAttribute('aria-checked'),'true');assert(await body.evaluate(node=>node===document.activeElement),'saved body switch keeps keyboard focus');
    await title.click();await page.waitForFunction(()=>AppState.settings.title_optimization_enabled===false);assert.equal(await body.getAttribute('aria-checked'),'true');
    assert.equal(await page.getByLabel('AI 调用方式',{exact:true}).inputValue(),'byok');
    assert.match(await page.getByLabel('AI 调用方式',{exact:true}).locator('option:checked').textContent(),/自带密钥/);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('hosted AI enabling follows real capability changes and disabling stays available',async()=>{
  const page=await open({settings:{agent_billing_mode:'hosted',agent_enabled:false,title_optimization_enabled:true}});try{
    await page.locator('[data-page="settings"]').click();await page.locator('[data-section="agent"]').click();
    await page.getByText('托管模型尚未配置',{exact:true}).waitFor();
    const body=page.getByRole('switch',{name:'AI 整理正文',exact:true}),title=page.getByRole('switch',{name:'AI 优化标题',exact:true});
    assert(await body.isDisabled());assert.equal(await title.isDisabled(),false);
    for(const label of ['服务商','接口地址','模型','API Key','标题模型','启用字幕视觉校对']) assert(await page.getByLabel(label,{exact:true}).isDisabled(),label+' is BYOK-only');
    await page.getByRole('button',{name:'＋ 添加自定义 Agent',exact:true}).click();
    assert(await page.getByLabel('自定义 Agent 模型',{exact:true}).isDisabled());assert.equal(await page.getByLabel('自定义 Agent 创造性',{exact:true}).isDisabled(),false);
    const prompt=page.getByLabel('自定义 Agent 系统提示词',{exact:true});assert.equal(await prompt.isEnabled(),true);
    await prompt.fill('保留原文顺序，只修复明确错字。');await prompt.press('Tab');
    await page.waitForFunction(()=>AppState.settings.agent_custom_profiles?.some(profile=>profile.system_prompt==='保留原文顺序，只修复明确错字。'));
    await title.click();await page.waitForFunction(()=>AppState.settings.title_optimization_enabled===false);assert(await title.isDisabled());
    await page.evaluate(()=>window.__fixtureHostedReady=true);await page.getByRole('button',{name:'检查托管服务',exact:true}).click();await page.getByText('托管已就绪',{exact:true}).waitFor();
    assert.equal(await body.isDisabled(),false);assert.equal(await title.isDisabled(),false);assert.equal(await body.getAttribute('aria-checked'),'false');
    await body.click();await page.waitForFunction(()=>AppState.settings.agent_enabled===true);assert.equal(await title.getAttribute('aria-checked'),'false');
    assert.equal(await page.getByLabel('AI 调用方式',{exact:true}).inputValue(),'hosted');assert.equal(await page.locator('#hostedAgentState').textContent(),'托管已就绪');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

// TASK-8.17 封面展示：任务卡缩略与检查器大图懒取，无封面行不渲染占位。
test('workbench task cards and inspector show platform covers lazily',async()=>{
  const page=await open({taskCover:true});try{
    await page.waitForFunction(()=>{
      const cover=document.querySelector('.job[data-task-id="job:1"] .job-cover');
      return cover && /taskcover\/fixturecover/.test(cover.style.backgroundImage);
    });
    const covers=await page.locator('.job .job-cover').count();
    assert.equal(covers,1,'仅带 cover_path 的任务渲染封面元素');
    await page.locator('.job[data-task-id="job:1"] .job-open').click();
    await page.waitForFunction(()=>{
      const big=document.querySelector('.inspector-cover');
      return big && big.dataset.coverTask==='job:1' && /taskcover\/fixturecover/.test(big.style.backgroundImage);
    });
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('cover fetch failure degrades silently without page errors',async()=>{
  const page=await open({taskCover:true,taskCoverFail:true});try{
    await page.locator('.job[data-task-id="job:1"] .job-open').click();
    await page.waitForSelector('.inspector-cover');
    // 失败的封面查询只留空占位，绝不弹错或崩页。
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.inspector-cover').evaluate(el=>el.style.backgroundImage),'');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('settings data section carries the download-covers toggle',async()=>{
  const page=await open();try{
    await page.locator('[data-page=settings]').click();
    await page.locator('.sitem[data-section=data]').click();
    const row=page.locator('.set-row',{has:page.locator('.set-label',{hasText:'下载封面'})}).first();
    await row.waitFor();
    const toggle=row.locator('[role="switch"]').first();
    assert.equal(await toggle.getAttribute('aria-checked'),'true','默认开');
    await toggle.click();
    await page.waitForFunction(()=>AppState.settings.download_covers===false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

// TASK-8.18 图集：任务卡图文徽标＋检查器缩略网格（懒取 token URL）。
test('gallery task shows the badge and a lazy inspector grid',async()=>{
  const page=await open({taskGallery:true});try{
    const badge=page.locator('.job[data-task-id="job:1"] .job-kind-badge');
    await badge.waitFor();
    assert.equal(await badge.textContent(),'图文');
    assert.equal(await page.locator('.job[data-task-id="job:1"]').evaluate(row=>row.dataset.status),'done');
    await page.locator('.job[data-task-id="job:1"] .job-open').click();
    await page.waitForFunction(()=>document.querySelectorAll('.inspector-grid-cell').length===2);
    const cells=await page.locator('.inspector-grid-cell').evaluateAll(nodes=>nodes.map(node=>node.style.backgroundImage.slice(0,60)));
    assert.ok(cells.every(url=>url.includes('taskcover/fixture.jpg')));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
