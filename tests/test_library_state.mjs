// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={}){
  const page=await browser.newPage({viewport:{width:options.width||1280,height:800}});page.setDefaultTimeout(4000);page.errors=[];
  page.on('pageerror',e=>page.errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.addInitScript(installMock,options);
  await page.addInitScript(()=>{
    window.__docs=[{task_id:'a',title:'文本甲',status:'done',has_final:true,has_raw:true,source_path:'fixture.mp4',tags:['甲']},{task_id:'b',title:'文本乙',status:'done',has_final:true,has_raw:true,tags:['乙']}];
    window.__texts={a:'# 文本甲',b:'# 文本乙'};
    __MOCK__.get_documents=query=>structuredClone(__docs.filter(d=>!query||d.title.includes(query)));
    __MOCK__.get_reader=(id,tab)=>({ok:true,tab:tab==='source'?'source':tab,text:__texts[id],title:'原视频',path:'fixture.mp4',revision:'fixture-revision-'+id});
    __MOCK__.update_reader_text=(id,tab,text)=>{__texts[id]=text;return {ok:true};};
    __MOCK__.set_job_tags=(id,tags)=>{__docs.find(d=>d.task_id===id).tags=tags;return {ok:true};};
    __MOCK__.delete_texts=ids=>{window.__deletedTexts=ids;__docs=__docs.filter(d=>!ids.includes(d.task_id));return {ok:true,removed:ids};};
    __MOCK__.delete_tasks=()=>{throw new Error('Unexpected destructive task deletion');};
  });
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=library]').click();await page.waitForFunction(()=>document.querySelector('#readerBody h1')?.textContent==='文本甲');return page;
}
const doc=(p,id)=>p.locator('.doc').filter({has:p.locator('.doc-title',{hasText:id==='a'?'文本甲':'文本乙'})}).locator('.doc-open');
test('desktop save carries the loaded revision and preserves a draft after an Agent conflict',async()=>{
  const p=await open();try{
    await p.evaluate(()=>__MOCK__.update_reader_text=(id,tab,text,revision)=>{
      window.__conflictSave={id,tab,text,revision};
      return {ok:false,code:'revision_conflict',error:'文本已被更新，草稿已保留；请重新读取后合并修改'};
    });
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 我的未保存草稿');
    await p.locator('#saveReaderBtn').click();await p.locator('#toast.err').waitFor();
    assert.deepEqual(await p.evaluate(()=>__conflictSave),{id:'a',tab:'final',text:'# 我的未保存草稿',revision:'fixture-revision-a'});
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 我的未保存草稿');
    assert.equal(await p.locator('#saveReaderBtn').isDisabled(),false);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('reader loading and failed states cannot begin a blank edit',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__MOCK__.get_reader=()=>new Promise(resolve=>window.__finishReader=resolve);});await doc(p,'b').click();
    assert(await p.locator('#editReaderBtn').isDisabled());assert(await p.locator('#copyReaderBtn').isDisabled());
    await p.evaluate(()=>__finishReader({ok:false,error:'文件不可用'}));await p.getByText('文件不可用',{exact:true}).waitFor();
    assert(await p.locator('#editReaderBtn').isDisabled());assert.equal(await p.locator('.reader-editor').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('raw-only documents select and save their available text version',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__docs[1].has_final=false;Library.refreshLight();__MOCK__.get_reader=(id,tab)=>({ok:true,tab,text:'# 原稿',revision:'raw-revision'});__MOCK__.update_reader_text=(...args)=>{window.__saved=args;return {ok:true};};});
    await doc(p,'b').click();await p.getByText('原稿',{exact:true}).last().waitFor();
    assert.equal(await p.locator('#readerTabs button').filter({hasText:'原稿'}).getAttribute('aria-pressed'),'true');
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 修改原稿');await p.locator('#saveReaderBtn').click();
    await p.waitForFunction(()=>window.__saved);assert.deepEqual(await p.evaluate(()=>__saved),['b','raw','# 修改原稿','raw-revision']);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('out of order search responses never replace the latest filtered list',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__MOCK__.get_documents=query=>query==='甲'?new Promise(resolve=>window.__oldSearch=()=>resolve([__docs[0]])):[__docs[1]];});
    await p.locator('#libSearch').fill('甲');await p.waitForFunction(()=>window.__oldSearch);await p.locator('#libSearch').fill('乙');await doc(p,'b').waitFor();
    await p.evaluate(()=>__oldSearch());await p.waitForTimeout(30);assert.deepEqual(await p.locator('.doc-title').allTextContents(),['文本乙']);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('same document reselected after another document rejects an older reader response',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{let count=0;__MOCK__.get_reader=id=>id==='b'&&count++===0?new Promise(resolve=>window.__oldReader=()=>resolve({ok:true,tab:'final',text:'# 旧乙'})):{ok:true,tab:'final',text:id==='b'?'# 新乙':'# 文本甲'};});
    await doc(p,'b').click();await p.waitForFunction(()=>window.__oldReader);await doc(p,'a').click();await doc(p,'b').click();await p.getByText('新乙',{exact:true}).waitFor();
    await p.evaluate(()=>__oldReader());await p.waitForTimeout(30);assert.equal(await p.locator('#readerBody h1').textContent(),'新乙');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a pending save for another document never discards the current editor draft',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__MOCK__.update_reader_text=(id,tab,text)=>new Promise(resolve=>window.__finishSave=()=>{__texts[id]=text;resolve({ok:true});});});
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 甲已保存');await p.locator('#saveReaderBtn').click();await p.waitForFunction(()=>window.__finishSave);
    await doc(p,'b').click();await p.locator('#discardChanges').click();await p.getByText('文本乙',{exact:true}).last().waitFor();await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 乙未保存');
    await p.evaluate(()=>__finishSave());await p.waitForTimeout(30);assert.equal(await p.locator('.reader-editor').inputValue(),'# 乙未保存');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('reclicking the selected document preserves its current draft',async()=>{
  const p=await open();try{
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 正在修改');await doc(p,'a').click();
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 正在修改');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('keyboard document menu returns focus and is cleaned when leaving library',async()=>{
  const p=await open();try{
    await doc(p,'b').focus();await p.keyboard.press('Shift+F10');await p.waitForSelector('.ctx-menu');await p.keyboard.press('Escape');assert(await doc(p,'b').evaluate(n=>n===document.activeElement));
    await doc(p,'b').click({button:'right'});await p.locator('[data-page=videos]').click();assert.equal(await p.locator('.ctx-menu').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('empty search clears all obsolete reader actions and empty-state failures are explicit',async()=>{
  const p=await open();try{
    await p.locator('#libSearch').fill('没有匹配');await p.getByText('没有找到文本',{exact:true}).waitFor();
    for(const id of ['editReaderBtn','copyReaderBtn','deleteBtn','openBtn','subtitleBtn'])assert(await p.locator('#'+id).isDisabled(),id);
    assert.equal(await p.locator('#readerTabs button:not(:disabled)').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('navigation and filtering use page confirmation, cancel preserves draft, selected nav never resets it',async()=>{
  const p=await open();try{
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 未保存');
    await p.locator('[data-page=workbench]').click();await p.locator('#discardChanges').waitFor();await p.locator('#dlgCancel').click();
    assert.equal(await p.evaluate(()=>AppState.currentView),'library');assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');
    await p.locator('[data-page=library]').click();assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');assert.equal(await p.locator('.modal').count(),0);
    await p.locator('#libSearch').fill('乙');await p.locator('#dlgCancel').click();assert.equal(await p.locator('#libSearch').inputValue(),'');assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');
    await p.locator('[data-page=workbench]').click();await p.locator('#discardChanges').click();assert.equal(await p.evaluate(()=>AppState.currentView),'workbench');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

for(const confirmationOpen of [false,true])test(`revocation locks a dirty reader without discarding its draft (${confirmationOpen?'pending confirmation':'editing'})`,async()=>{
  const p=await open();try {
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 登录前的草稿');
    const editor=await p.locator('.reader-editor').elementHandle();
    if(confirmationOpen){await p.locator('[data-page=workbench]').click();await p.locator('#discardChanges').waitFor();}
    await p.evaluate(()=>__onState({account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:2}}));
    assert.equal(await p.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await p.locator('.modal').count(),0);
    assert.equal(await p.locator('.reader-editor').isVisible(),false);
    assert.equal(await editor.inputValue(),'# 登录前的草稿');
    await p.evaluate(()=>{AccountAccess.apply({required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:3});switchView('library');});
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 登录前的草稿');
    assert.equal(await p.locator('.reader-editor').evaluate((node,original)=>node===original,editor),true);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('saving errors preserve the draft and editing can retry with focus restored after success',async()=>{
  const p=await open();try{
    await p.evaluate(()=>__MOCK__.update_reader_text=()=>({ok:false,error:'暂时无法保存'}));
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 修改甲');await p.locator('#saveReaderBtn').click();await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 修改甲');assert.equal(await p.locator('#saveReaderBtn').isDisabled(),false);assert.equal(await p.locator('.reader-editor').evaluate(n=>n.readOnly),false);
    await p.evaluate(()=>__MOCK__.update_reader_text=(id,tab,text)=>{__texts[id]=text;return {ok:true};});await p.locator('#saveReaderBtn').click();await p.getByText('修改甲',{exact:true}).waitFor();
    assert(await p.locator('#editReaderBtn').evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('text-only toolbar and row deletion never call destructive task/file APIs, errors preserve selection',async()=>{
  const p=await open();try{
    await p.evaluate(()=>__MOCK__.delete_texts=()=>({ok:false,error:'删除被拒绝',removed:[]}));await p.locator('#deleteBtn').click();await p.locator('#dlgOk').click();await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('.doc.selected .doc-title').textContent(),'文本甲');assert.equal(await p.locator('#readerBody h1').textContent(),'文本甲');
    await p.evaluate(()=>__MOCK__.delete_texts=ids=>{__deletedTexts=ids;__docs=__docs.filter(d=>!ids.includes(d.task_id));return {ok:true,removed:ids};});
    await p.locator('.doc.selected .job-delete').click();assert.match(await p.locator('#dlgText').textContent(),/原视频保留/);await p.locator('#dlgOk').click();await p.waitForFunction(()=>document.querySelectorAll('.doc').length===1);
    assert.deepEqual(await p.evaluate(()=>__deletedTexts),['a']);assert.equal(await p.locator('.doc-title').textContent(),'文本乙');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('tag failure retains typed input and a successful retry preserves expanded keyboard focus',async()=>{
  const p=await open();try{
    await p.locator('.tag-details summary').click();await p.locator('#tagInput').fill('新标签');await p.evaluate(()=>__MOCK__.set_job_tags=()=>{throw new Error('保存标签失败');});await p.locator('#tagInput').press('Enter');await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('#tagInput').inputValue(),'新标签');assert(await p.locator('#tagInput').evaluate(n=>n===document.activeElement));
    await p.evaluate(()=>__MOCK__.set_job_tags=(id,tags)=>{__docs.find(d=>d.task_id===id).tags=tags;return {ok:true};});await p.locator('#tagInput').press('Enter');await p.getByRole('button',{name:'移除标签 新标签',exact:true}).waitFor();
    assert.equal(await p.locator('#tagInput').inputValue(),'');assert(await p.locator('.tag-details').evaluate(n=>n.open));assert(await p.locator('#tagInput').evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('background metadata refresh retains row and tag-editor focus and updates a renamed title',async()=>{
  const p=await open();try{
    await doc(p,'b').focus();await p.evaluate(async()=>{__docs[1].title='文本乙更名';await Library.refreshLight();});assert.equal(await p.locator('.doc-open').nth(1).evaluate(n=>n===document.activeElement),true);
    await p.locator('.tag-details summary').click();await p.locator('#tagInput').fill('未提交标签');await p.evaluate(async()=>{__docs[0].updated_at='new';await Library.refreshLight();});
    assert.equal(await p.locator('#tagInput').inputValue(),'未提交标签');assert(await p.locator('#tagInput').evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('subtitle save dialog retains the original document identity after another document is selected',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__MOCK__.pick_save_file=()=>new Promise(resolve=>window.__pickSubtitle=()=>resolve({ok:true,path:'fixture.srt'}));__MOCK__.export_subtitles=(...args)=>{window.__exportArgs=args;return {ok:true,count:1,path:'fixture.srt'};};});
    await p.locator('#subtitleBtn').click();await p.getByRole('menuitem',{name:'导出 SRT（通用字幕）',exact:true}).click();await p.waitForFunction(()=>window.__pickSubtitle);await doc(p,'b').click();await p.evaluate(()=>__pickSubtitle());await p.waitForFunction(()=>window.__exportArgs);
    assert.deepEqual(await p.evaluate(()=>__exportArgs),['a','srt','fixture.srt']);assert.equal(await p.locator('.modal').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('accepting a filter change discards the draft even if the same document remains visible',async()=>{
  const p=await open();try{
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 临时草稿');await p.locator('#libSearch').fill('文本');await p.locator('#discardChanges').click();
    await p.waitForSelector('#readerBody h1');assert.equal(await p.locator('#readerBody h1').textContent(),'文本甲');assert.equal(await p.locator('.reader-editor').count(),0);assert(await p.locator('#saveReaderBtn').isHidden());assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('canceling a keyboard menu deletion restores focus to its originating document',async()=>{
  const p=await open();try{
    // 2026-10-19 首项是「复制 Markdown 地址」；TASK-8.24（2026-10-25）在其后
    // 新增「重新转写…」「翻译…」，键盘路径需下移三次才到「删除文本」。
    await doc(p,'b').focus();await p.keyboard.press('Shift+F10');await p.keyboard.press('ArrowDown');await p.keyboard.press('ArrowDown');await p.keyboard.press('ArrowDown');await p.keyboard.press('Enter');await p.locator('#dlgOk').waitFor();await p.keyboard.press('Escape');assert(await doc(p,'b').evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('context menu copies the markdown address and opens the video when present',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{window.__menuToasts=[];const o=window.toast;window.toast=(m,k)=>{window.__menuToasts.push(m);return o(m,k);};});
    // 文本甲带源视频：两项都出现且可用
    await doc(p,'a').click({button:'right'});
    assert.equal(await p.locator('.ctx-menu [data-act="copy-md"]').count(),1);
    assert.equal(await p.locator('.ctx-menu [data-act="open-video"]').count(),1);
    await p.locator('.ctx-menu [data-act="copy-md"]').click();
    await p.waitForFunction(()=>window.__menuToasts.some(m=>/Markdown 地址已复制/.test(m)));
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu [data-act="open-video"]').click();
    await p.waitForFunction(()=>window.__fixtureCalls.includes('play_media'));
    assert.ok(await p.evaluate(()=>!window.__menuToasts.some(m=>/无法打开/.test(m))));
    // 文本乙无源视频：打开视频不出现，复制地址仍在；删除组原样保留
    await doc(p,'b').click({button:'right'});
    assert.equal(await p.locator('.ctx-menu [data-act="open-video"]').count(),0);
    assert.equal(await p.locator('.ctx-menu [data-act="copy-md"]').count(),1);
    assert.equal(await p.locator('.ctx-menu [data-act="delete-text"]').count(),1);
    await p.keyboard.press('Escape');
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('reader selection can be copied via right-click without entering edit mode',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{window.__menuToasts=[];const o=window.toast;window.toast=(m,k)=>{window.__menuToasts.push(m);return o(m,k);};window.__copied=null;navigator.clipboard.writeText=t=>{window.__copied=t;return Promise.resolve();};});
    // TASK-8.24（先生 2026-10-25）：无选中右键也弹菜单，但不提供「复制选中文字」
    await p.locator('#readerBody').click({button:'right'});
    assert.equal(await p.locator('.ctx-menu [data-act="copy-selection"]').count(),0);
    assert.ok((await p.locator('.ctx-menu .ctx-item').count())>=2);
    await p.keyboard.press('Escape');
    // 选中正文标题后在其上右键：菜单出现，「复制选中文字」直达成剪贴板
    await p.evaluate(()=>{
      const node=document.querySelector('#readerBody h1')||document.querySelector('#readerBody p');
      const range=document.createRange();range.selectNodeContents(node);
      const sel=getSelection();sel.removeAllRanges();sel.addRange(range);
    });
    await p.locator('#readerBody h1, #readerBody p').first().click({button:'right'});
    await p.locator('.ctx-menu [data-act="copy-selection"]').click();
    await p.waitForFunction(()=>window.__menuToasts.some(m=>/已复制选中文字/.test(m)));
    assert.ok(await p.evaluate(()=>String(window.__copied||'').length>0));
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

// ---------------------------------------------------------------- TASK-8.24
// 重新转写/翻译入口、右键复制视频地址、译文三种阅读方式与欢迎语（欢迎语在 setup 套件）。
test('doc context menu exposes retranscribe, translate and copy video path', async () => {
  const p = await open();try{
    await doc(p,'a').click({button:'right'});
    const labels = await p.locator('.ctx-menu .ctx-item').allTextContents();
    assert.ok(labels.some(t=>t.includes('复制视频地址')),'复制视频地址 item');
    assert.ok(labels.some(t=>t.includes('重新转写')),'重新转写 item');
    assert.ok(labels.some(t=>t.includes('翻译…')),'翻译 item');
    await p.keyboard.press('Escape');
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('reader body right-click copies the video path without a selection', async () => {
  const p = await open();try{
    await p.locator('#readerBody').click({button:'right'});
    const item = p.locator('.ctx-menu .ctx-item', {hasText:'复制视频地址'});
    await item.waitFor();
    await p.evaluate(()=>{window.__copied=null;navigator.clipboard.writeText=t=>{window.__copied=t;return Promise.resolve();};});
    await item.click();
    assert.equal(await p.evaluate(()=>window.__copied),'fixture.mp4');
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('retranscribe dialog submits the chosen mode', async () => {
  const p = await open();try{
    await p.evaluate(()=>{window.__retranscribe=null;__MOCK__.retranscribe_tasks=(ids,mode)=>{window.__retranscribe={ids,mode};return {ok:true,created:['newjob123']};};});
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu .ctx-item',{hasText:'重新转写'}).click();
    await p.locator('#retranscribeMode').selectOption('raw');
    await p.locator('#retranscribeStart').click();
    await p.locator('#toast:not(.err)').waitFor();
    assert.deepEqual(await p.evaluate(()=>window.__retranscribe),{ids:['a'],mode:'raw'});
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

test('remembered retranscribe choices take effect without sharing the bridge settings object', async () => {
  const p = await open(); try {
    await p.evaluate(() => {
      AppState.settings = structuredClone(AppState.settings);
      window.__retranscribes = [];
      __MOCK__.retranscribe_tasks = (ids,mode,level) => {
        __retranscribes.push({ids,mode,level}); return {ok:true,created:['newjob123']};
      };
    });
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu .ctx-item',{hasText:'重新转写'}).click();
    await p.locator('#retranscribeMode').selectOption('raw');
    await p.locator('#retranscribeLevel').selectOption('rewrite');
    await p.locator('#retranscribeNoRemind').check();
    await p.locator('#retranscribeStart').click();
    await p.waitForFunction(() => __retranscribes.length === 1);
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu .ctx-item',{hasText:'重新转写'}).click();
    await p.waitForFunction(() => __retranscribes.length === 2);
    assert.equal(await p.locator('#retranscribeStart').count(),0);
    assert.deepEqual(await p.evaluate(() => __retranscribes[1]),{ids:['a'],mode:'raw',level:'rewrite'});
    const translations = await p.evaluate(() => {
      I18n.setLanguage('en');
      return ['精炼改写（自由）','提示音','系统提示音','清脆（短促双音）'].map(x => I18n.t(x));
    });
    assert(translations.every(x => !/[\u4e00-\u9fff]/.test(x)));
    assert.deepEqual(p.errors,[]);
  } finally { await p.close(); }
});
test('translate dialog starts translation and renders the picked reading mode', async () => {
  const p = await open();try{
    await p.evaluate(()=>{
      window.__translateArgs=null;window.__poll=0;
      __MOCK__.translate_text=(id,direction)=>{window.__translateArgs={id,direction};return {ok:true,started:true};};
      __MOCK__.get_translation_status=()=>{
        const state={running:++window.__poll<1,status:'done',lang:'en',has_translation:true};
        if(!window.__poll1Flip&&window.__poll>=1){window.__poll1Flip=true;Object.assign(__docs.find(d=>d.task_id==='a'),{has_translation:true,translation_lang:'en'});}
        return state;
      };
      __MOCK__.get_reader=(id,tab)=>tab==='translation'
        ? {ok:true,tab:'translation',text:'# Translated',segments:[{src:'# 标题',dst:'# Translated'}],lang:'en',direction:'zh-en',path:'x.md'}
        : {ok:true,tab:tab==='source'?'source':tab,text:__texts[id],title:'原视频',path:'fixture.mp4',revision:'fixture-revision-'+id};
    });
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu .ctx-item',{hasText:'翻译'}).click();
    await p.locator('#translateDirection').selectOption('zh-en');
    await p.locator('#translateDisplay').selectOption('segments');
    await p.locator('#translateStart').click();
    await p.locator('.translation-segments').waitFor({timeout:6000});
    assert.equal(await p.locator('.seg-pair').count(),1);
    assert.equal(await p.locator('.seg-dst h1').textContent(),'# Translated'.replace('# ','').length?'Translated':'');
    assert.deepEqual(await p.evaluate(()=>window.__translateArgs),{id:'a',direction:'zh-en'});
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('translation view control switches between original and translated body', async () => {
  const p = await open();try{
    // 先切到文档 b，再为 a 补译文标记并重选，强制阅读器按新数据重载。
    await doc(p,'b').click();
    await p.waitForFunction(()=>document.querySelector('#readerBody h1')?.textContent==='文本乙');
    await p.evaluate(()=>{
      Object.assign(__docs.find(d=>d.task_id==='a'),{has_translation:true,translation_lang:'en'});
      __MOCK__.get_reader=(id,tab)=>tab==='translation'
        ? {ok:true,tab:'translation',text:'# 译文标题',segments:[{src:'# 标题',dst:'# 译文标题'}],lang:'en',direction:'zh-en',path:'x.md'}
        : {ok:true,tab:tab==='source'?'source':tab,text:__texts[id],title:'原视频',path:'fixture.mp4',revision:'fixture-revision-'+id};
    });
    await doc(p,'a').click();
    await p.waitForFunction(()=>document.querySelector('#readerBody h1')?.textContent==='文本甲');
    // mock 环境没有 push_state tick：手动驱动轻刷新（真实应用由 2s tick 承担）。
    await p.evaluate(()=>window.Library.refreshLight());
    await p.locator('#readerTransTabs .seg',{hasText:'逐段对照'}).click();
    await p.locator('.translation-segments').waitFor();
    assert.equal(await p.locator('.seg-src').count(),1);
    assert.ok(await p.locator('#editReaderBtn').isDisabled(),'编辑在译文视图下禁用');
    await p.locator('#readerTransTabs .seg',{hasText:'原文'}).first().click();
    await p.waitForFunction(()=>document.querySelector('#readerBody h1')?.textContent==='文本甲');
    assert.equal(await p.locator('.translation-segments').count(),0);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
