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
async function open(options={}, runtime=browser){
  const page=await runtime.newPage({viewport:{width:options.width||1280,height:800}});page.setDefaultTimeout(4000);page.errors=[];
  page.on('pageerror',e=>page.errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.addInitScript(installMock,options);
  await page.addInitScript(()=>{
    window.__docs=[{task_id:'a',title:'文本甲',status:'done',has_final:true,has_raw:true,source_path:'fixture.mp4',tags:['甲']},{task_id:'b',title:'文本乙',status:'done',has_final:true,has_raw:true,tags:['乙']}];
    window.__texts={a:'# 文本甲',b:'# 文本乙'};
    __MOCK__.get_documents=query=>structuredClone(__docs.filter(d=>!query||d.title.includes(query)));
    __MOCK__.get_tag_index=()=>[{tag:'甲',count:1},{tag:'乙',count:1}];
    __MOCK__.get_reader=(id,tab)=>({ok:true,tab:tab==='source'?'source':tab,text:__texts[id],title:'原视频',path:'fixture.mp4',revision:'fixture-revision-'+id});
    __MOCK__.update_reader_text=(id,tab,text)=>{__texts[id]=text;return {ok:true};};
    __MOCK__.set_job_tags=(id,tags)=>{__docs.find(d=>d.task_id===id).tags=tags;return {ok:true};};
    __MOCK__.delete_texts=ids=>{window.__deletedTexts=ids;__docs=__docs.filter(d=>!ids.includes(d.task_id));return {ok:true,removed:ids};};
    __MOCK__.delete_tasks=()=>{throw new Error('Unexpected destructive task deletion');};
    __MOCK__.delete_library_items=()=>{throw new Error('Unexpected deletion of downloaded media');};
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
    assert(await p.locator('#saveReaderBtn').isDisabled());
    await p.evaluate(()=>{
      __MOCK__.get_reader=(id,tab)=>({ok:true,tab,text:'# Agent 新稿',revision:'new-revision'});
      __MOCK__.update_reader_text=(id,tab,text,revision)=>{window.__mergedSave={id,tab,text,revision};return {ok:true};};
    });
    await p.getByRole('button',{name:'查看最新稿',exact:true}).click();
    assert.equal(await p.locator('.reader-latest-draft').inputValue(),'# Agent 新稿');
    await p.locator('#mergeReaderDraft').click();
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 我的未保存草稿');
    await p.locator('.reader-editor').fill('# 合并后的稿件');
    await p.locator('#saveReaderBtn').click();
    await p.waitForFunction(()=>window.__mergedSave);
    assert.deepEqual(await p.evaluate(()=>__mergedSave),{id:'a',tab:'final',text:'# 合并后的稿件',revision:'new-revision'});
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

test('a partial document labels preview copies and locates the current file for its version', async () => {
  const p = await open({width: 710});
  try {
    const preview = '# 文本乙\n\n**留在可见范围内。**\n';
    const file = 'C:\\fixture\\整理稿乙.md';
    const reorganized = 'C:\\fixture\\重整稿乙.md';
    await p.evaluate(({preview, file}) => {
      const read = __MOCK__.get_reader;
      window.__copiedPreviews = [];
      window.__locatedFiles = [];
      window.__finalPath = file;
      navigator.clipboard.writeText = text => { __copiedPreviews.push(text); return Promise.resolve(); };
      __MOCK__.get_reader = (id, tab) => {
        if (id !== 'b' || tab === 'source') return read(id, tab);
        return {ok: true, tab, text: preview, path: tab === 'final' ? __finalPath : 'raw.md', truncated: tab === 'final'};
      };
      __MOCK__.open_path = path => {
        __locatedFiles.push(path);
        return __locatedFiles.length === 1 ? {ok: true} : {ok: false, error: '文件不存在'};
      };
    }, {preview, file});
    const notice = p.locator('#readerPreviewNotice');
    await doc(p, 'a').click();
    assert(await notice.isHidden());
    await p.locator('#copyReaderBtn').click();
    assert.deepEqual(await p.evaluate(() => __copiedPreviews), ['# 文本甲']);
    await p.locator('#readerBack').click();
    await doc(p, 'b').click();
    await notice.getByRole('status').waitFor();
    assert.equal(await notice.getByRole('status').textContent(), '当前显示部分文稿');
    assert(await p.locator('#editReaderBtn').isDisabled());
    assert(await p.locator('#copyReaderBtn').isHidden());
    await notice.getByRole('button', {name: '复制可见部分'}).click();
    await p.locator('#readerBody h1').click({button: 'right'});
    await p.locator('.ctx-menu').getByRole('menuitem', {name: '复制可见部分'}).click();
    assert.deepEqual(await p.evaluate(() => __copiedPreviews), ['# 文本甲', preview, preview]);
    const locate = notice.getByRole('button', {name: '定位完整文稿'});
    await p.evaluate(path => { window.__finalPath = path; }, reorganized);
    await locate.click();
    await p.waitForFunction(() => __locatedFiles.length === 1);
    await locate.click();
    await p.locator('#toast.err').waitFor();
    assert.deepEqual(await p.evaluate(() => __locatedFiles), [reorganized, reorganized]);
    assert.equal(await p.locator('#toast.err').textContent(), '文件不存在');
    assert.equal(await p.locator('#readerBody h1').textContent(), '文本乙');

    await p.evaluate(() => I18n.setLanguage('en'));
    await notice.getByRole('button', {name: 'Copy visible text'}).waitFor();
    assert.equal(await notice.evaluate(node => node.scrollWidth > node.clientWidth), false);
    await p.evaluate(() => I18n.setLanguage('zh-CN'));
    await p.locator('#compareReaderBtn').click();
    assert(await notice.isHidden());
    await p.locator('.reader-diff .diff-summary').waitFor();
    assert.equal(await p.locator('.reader-diff .diff-summary').textContent(), '已读取的部分文稿内容一致');
    assert(await p.locator('#copyReaderBtn').isDisabled());
    await p.locator('#compareReaderBtn').click();
    await notice.waitFor();
    await p.evaluate(() => {
      const read = __MOCK__.get_reader;
      __MOCK__.get_reader = (id, tab) => {
        __MOCK__.get_reader = read;
        return new Promise(resolve => { window.__finishFileLookup = () => resolve(read(id, tab)); });
      };
    });
    await locate.click();
    await p.waitForFunction(() => window.__finishFileLookup);
  await p.locator('#readerTabs').getByRole('button', {name: '原始转写', exact: true}).click();
    await p.waitForFunction(() => !document.getElementById('copyReaderBtn').disabled);
    await p.evaluate(async () => { __finishFileLookup(); await new Promise(requestAnimationFrame); });
    assert.deepEqual(await p.evaluate(() => __locatedFiles), [reorganized, reorganized]);
    assert(await notice.isHidden());
    await p.locator('#copyReaderBtn').click();
    assert.deepEqual(await p.evaluate(() => __copiedPreviews), ['# 文本甲', preview, preview, preview]);
    assert.deepEqual(p.errors, []);
  } finally { await p.close(); }
});

test('raw-only documents select and save their available text version',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{__docs[1].has_final=false;Library.refreshLight();__MOCK__.get_reader=(id,tab)=>({ok:true,tab,text:'# 原稿',revision:'raw-revision'});__MOCK__.update_reader_text=(...args)=>{window.__saved=args;return {ok:true};};});
    await doc(p,'b').click();await p.getByText('原稿',{exact:true}).last().waitFor();
    assert.equal(await p.locator('#readerTabs button').filter({hasText:'原始转写'}).getAttribute('aria-pressed'),'true');
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 修改原稿');await p.locator('#saveReaderBtn').click();
    await p.waitForFunction(()=>window.__saved);assert.deepEqual(await p.evaluate(()=>__saved),['b','raw','# 修改原稿','raw-revision']);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('out of order filter responses never replace the latest filtered list',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{
      let calls=0;
      __MOCK__.get_documents=()=>++calls===1
        ?new Promise(resolve=>window.__oldFilter=()=>resolve(structuredClone(__docs)))
        :structuredClone(__docs);
    });
    await p.locator('#libTagFilter').selectOption('甲');await p.waitForFunction(()=>window.__oldFilter);
    await p.locator('#libTagFilter').selectOption('乙');
    await p.getByText('当前文稿不在筛选结果中',{exact:true}).waitFor();
    await p.evaluate(async()=>{__oldFilter();await new Promise(resolve=>requestAnimationFrame(resolve));});
    assert.deepEqual(await p.locator('.doc-title').allTextContents(),['文本乙']);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('idle library hides noisy document count and menu trigger toggles cleanly on reclick',async()=>{
  const p=await open();try{
    assert.equal(await p.locator('#libResultsBar').isHidden(),true);
    assert.equal(await p.locator('#libSearch').count(),0);
    await p.locator('#readerMenuBtn').click();
    await p.waitForSelector('.doc-context-menu');
    assert.equal(await p.locator('#readerMenuBtn').getAttribute('aria-expanded'),'true');
    await p.locator('#readerMenuBtn').click();
    assert.equal(await p.locator('.doc-context-menu').count(),0);
    assert.equal(await p.locator('#readerMenuBtn').getAttribute('aria-expanded'),'false');
    assert(await p.locator('#readerMenuBtn').evaluate(node=>node===document.activeElement));
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('same-title documents show identifying metadata and sorting preserves the open document',async()=>{
  const p=await open();try{
    await p.evaluate(async()=>{
      Object.assign(__docs[0],{title:'同名文稿',updated_at:'2004-11-20 14:30:00',source_path:'C:\\样本\\第一段.mp4',tags:['生活','心理健康','访谈','口述']});
      Object.assign(__docs[1],{title:'同名文稿',updated_at:'2004-11-19 13:00:00',source_path:'C:\\样本\\第二段.mp4'});
      await Library.refreshLight();
    });
    const rows=p.locator('.doc');
    assert.deepEqual(await rows.locator('.doc-title').allTextContents(),['同名文稿','同名文稿']);
    assert.equal(await rows.locator('.doc-source').count(),0);
    assert.deepEqual(await rows.locator('.doc-date').allTextContents(),['2004/11/20 14:30','2004/11/19 13:00']);
    assert.deepEqual(await rows.first().locator('.doc-tag').allTextContents(),['生活','心理健康']);
    assert.equal(await rows.first().locator('.doc-tag-more').textContent(),'+2');
    assert.match(await rows.first().locator('.doc-tags').getAttribute('title'),/生活、心理健康、访谈、口述/);
    assert.deepEqual(await p.locator('.reader-tag-preview .reader-tag-pill').allTextContents(),['生活','心理健康','访谈','+1']);
    assert(await p.locator('.reader-tag-preview').isVisible());
    await p.locator('.tag-details summary').click();
    assert.deepEqual(await p.locator('.tag-pills .tag span').allTextContents(),['生活','心理健康','访谈','口述']);
    await p.locator('#libSort').selectOption('oldest');
    assert.deepEqual(await rows.locator('.doc-date').allTextContents(),['2004/11/19 13:00','2004/11/20 14:30']);
    assert.equal(await p.evaluate(()=>Library.currentTaskId()),'a');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a native catalog scrollbar fits inside the regular inset without resizing the rows',async()=>{
  const runtime=await chromium.launch({headless:true,ignoreDefaultArgs:['--hide-scrollbars']});
  try {
    const p=await open({},runtime);
    const layout=()=>p.locator('#docList').evaluate(list=>{
      const frame=list.getBoundingClientRect(), row=list.querySelector('.doc').getBoundingClientRect();
      return {
        width:row.width, left:row.left-frame.left, right:frame.right-row.right,
        gutter:list.offsetWidth-list.clientWidth, horizontal:list.scrollWidth>list.clientWidth,
      };
    });
    const short=await layout();
    await p.evaluate(async()=>{
      __docs.push(...Array.from({length:60},(_,index)=>({...__docs[0],task_id:'extra-'+index,title:'测试文稿 '+index})));
      await Library.refreshLight();
    });
    const long=await layout();
    assert.equal(short.gutter,0);
    assert(long.gutter>0 && long.gutter<=long.right);
    assert.deepEqual([long.width,long.left,long.right],[short.width,short.left,short.right]);
    assert.equal(long.horizontal,false);
    const list=await p.locator('#docList').boundingBox(), x=list.x+list.width-long.gutter/2;
    await p.mouse.move(x,list.y+18);
    await p.mouse.down();
    await p.mouse.move(x,list.y+132,{steps:8});
    await p.mouse.up();
    assert(await p.locator('#docList').evaluate(list=>list.scrollTop>0));
    assert.deepEqual(p.errors,[]);
  }finally{await runtime.close();}
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
test('empty document filter preserves the current view and offers to clear filters',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{document.getElementById('libTagFilter').add(new Option('无匹配标签','无匹配标签'));});
    await p.locator('#libTagFilter').selectOption('无匹配标签');await p.getByText('没有找到文本',{exact:true}).waitFor();
    assert.equal(await p.evaluate(()=>Library.currentTaskId()),'a');
    assert.equal(await p.locator('.reader-doc-title').textContent(),'文本甲');
    assert(await p.locator('#editReaderBtn').isEnabled());assert(await p.locator('#copyReaderBtn').isEnabled());
    await p.locator('#clearEmptyFilter').click();await p.waitForFunction(()=>document.querySelectorAll('.doc').length===2);
    assert.equal(await p.locator('#libTagFilter').inputValue(),'');
    assert.equal(await p.locator('#libResultsBar').isHidden(),true);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('failed document reload keeps the last results and can replay the active filter',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{
      const documents=__MOCK__.get_documents;
      window.__failSearch=true;
      __MOCK__.get_documents=query=>{if(__failSearch)throw new Error('mock query failure');return documents(query);};
    });
    await p.locator('#libTagFilter').selectOption('乙');await p.getByText('读取失败，显示上次结果',{exact:true}).waitFor();
    assert.deepEqual(await p.locator('.doc-title').allTextContents(),['文本甲','文本乙']);
    await p.evaluate(()=>{__failSearch=false;});await p.locator('#libRetry').click();
    await p.getByText('当前文稿不在筛选结果中',{exact:true}).waitFor();
    assert.deepEqual(await p.locator('.doc-title').allTextContents(),['文本乙']);
    assert.equal(await p.evaluate(()=>Library.currentTaskId()),'a');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('document filtering preserves the draft and opening a different result uses confirmation',async()=>{
  const p=await open();try{
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 未保存');
    await p.locator('[data-page=workbench]').click();await p.locator('#discardChanges').waitFor();await p.locator('#dlgCancel').click();
    assert.equal(await p.evaluate(()=>AppState.currentView),'library');assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');
    await p.locator('[data-page=library]').click();assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');assert.equal(await p.locator('.modal').count(),0);
    await p.locator('#libTagFilter').selectOption('乙');await p.getByText('当前文稿不在筛选结果中',{exact:true}).waitFor();
    assert.equal(await p.locator('.modal').count(),0);assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');
    await doc(p,'b').click();await p.locator('#dlgCancel').click();
    assert.equal(await p.locator('#libTagFilter').inputValue(),'乙');assert.equal(await p.locator('.reader-editor').inputValue(),'# 未保存');
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
    await p.evaluate(()=>__MOCK__.delete_texts=()=>({ok:false,error:'删除被拒绝',removed:[]}));await p.locator('#readerMenuBtn').click();await p.getByRole('menuitem',{name:'仅删除文稿',exact:true}).click();await p.locator('#dlgOk').click();await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('.doc.selected .doc-title').textContent(),'文本甲');assert.equal(await p.locator('#readerBody h1').textContent(),'文本甲');
    await p.evaluate(()=>__MOCK__.delete_texts=ids=>{__deletedTexts=ids;__docs=__docs.filter(d=>!ids.includes(d.task_id));return {ok:true,removed:ids};});
    await p.locator('.doc.selected .doc-menu-trigger').click();await p.getByRole('menuitem',{name:'仅删除文稿',exact:true}).click();
    assert.match(await p.locator('#dlgText').textContent(),/视频和任务记录保留/);await p.locator('#dlgOk').click();await p.waitForFunction(()=>document.querySelectorAll('.doc').length===1);
    assert.deepEqual(await p.evaluate(()=>__deletedTexts),['a']);assert.equal(await p.locator('.doc-title').textContent(),'文本乙');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('toolbar, row and right-click share file actions and send the chosen deletion scope',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{
      __MOCK__.open_path=path=>{window.__located=path;return {ok:true};};
      __MOCK__.delete_library_items=(ids,removeRecords)=>{
        (window.__deletionScopes ||= []).push({ids,removeRecords});
        return {ok:false,removed:[],error:'测试保留资料'};
      };
    });
    const top=p.locator('#readerMenuBtn'), row=p.locator('.doc.selected .doc-menu-trigger');
    const labels=()=>p.getByRole('menu',{name:'文本操作'}).getByRole('menuitem').allTextContents();
    await top.click();const expected=await labels();assert.equal(await top.getAttribute('aria-expanded'),'true');
    await p.keyboard.press('Escape');assert.equal(await top.getAttribute('aria-expanded'),'false');
    await row.click();assert.deepEqual(await labels(),expected);await p.keyboard.press('Escape');
    await doc(p,'a').click({button:'right'});assert.deepEqual(await labels(),expected);
    await p.getByRole('menuitem',{name:'定位文稿文件'}).click();assert.equal(await p.evaluate(()=>__located),'fixture.mp4');
    for(const [label,keepRecords] of [['删除文稿和下载视频',true],['删除文件并移除任务',false]]){
      await row.click();await p.getByRole('menuitem',{name:label,exact:true}).click();
      const effects=await p.locator('#dlgText').textContent();
      assert.match(effects,/手动导入的源文件保留/);
      assert.equal(effects.includes('任务记录和'),keepRecords);
      await p.locator('#dlgOk').click();await p.getByText('测试保留资料',{exact:true}).waitFor();
    }
    assert.deepEqual(await p.evaluate(()=>__deletionScopes),[{ids:['a'],removeRecords:false},{ids:['a'],removeRecords:true}]);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('externally removed media no longer offers playback, file location or retranscription',async()=>{
  const p=await open();try{
    await p.evaluate(async()=>{__docs[0].source_available=false;await Library.refreshLight();});
    await p.locator('#readerMenuBtn').click();
    assert.equal(await p.locator('.ctx-menu [data-act="open-video"], .ctx-menu [data-act="locate-source"], .ctx-menu [data-act="retranscribe"]').count(),0);
    assert.equal(await p.locator('.ctx-menu [data-act="locate-md"]').count(),1);
    assert.equal(await p.locator('.ctx-menu [data-act="view-video"]').count(),1);
    assert.equal(await p.locator('.reader-source').count(),0);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a deletion reporting partial failure refreshes disk state and displays the backend error',async()=>{
  const p=await open();try{
    await p.evaluate(()=>__MOCK__.delete_texts=ids=>{
      __docs=__docs.filter(d=>!ids.includes(d.task_id));
      return {ok:false,removed:ids,error:'文稿已移除，部分关联文件正在使用'};
    });
    await doc(p,'a').click({button:'right'});
    await p.locator('.ctx-menu [data-act="delete-text"]').click();
    await p.locator('#dlgOk').click();
    await p.getByText('文稿已移除，部分关联文件正在使用',{exact:true}).waitFor();
    assert.equal(await doc(p,'a').count(),0);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('deleting another document leaves the active comparison request intact',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{
      __MOCK__.get_reader=(id,tab)=>new Promise(resolve=>{
        (window.__comparisonReads ||= []).push(()=>resolve({ok:true,tab,text:__texts[id]}));
      });
    });
    await p.locator('#compareReaderBtn').click();
    await p.getByText('正在对照原稿与整理稿',{exact:true}).waitFor();
    await doc(p,'b').click({button:'right'});
    await p.getByRole('menuitem',{name:'仅删除文稿',exact:true}).click();
    await p.locator('#dlgOk').click();
    await p.getByText('文稿已删除',{exact:true}).waitFor();
    await p.evaluate(()=>__comparisonReads.forEach(finish=>finish()));
    await p.getByText('原稿与整理稿内容一致',{exact:true}).waitFor();
    assert.deepEqual(await p.evaluate(()=>__deletedTexts),['b']);
    assert.deepEqual(p.errors,[]);
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
    await p.locator('#readerMenuBtn').click();await p.getByRole('menuitem',{name:'导出转写字幕…',exact:true}).click();
    await p.getByRole('menuitem',{name:'导出 SRT（通用字幕）',exact:true}).click();await p.waitForFunction(()=>window.__pickSubtitle);await doc(p,'b').click();await p.evaluate(()=>__pickSubtitle());await p.waitForFunction(()=>window.__exportArgs);
    assert.deepEqual(await p.evaluate(()=>__exportArgs),['a','srt','fixture.srt']);assert.equal(await p.locator('.modal').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a legacy document without a subtitle timeline explains the issue before opening the save dialog',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{
      const get=__MOCK__.get_reader;
      __MOCK__.get_reader=(id,tab)=>({...get(id,tab),has_subtitle_timeline:false});
      __MOCK__.pick_save_file=()=>{window.__attemptedPick=true;};
    });
    await p.locator('#readerMenuBtn').click();await p.getByRole('menuitem',{name:'导出转写字幕…',exact:true}).click();
    await p.getByText('这篇文稿没有转写时间轴，重新转写后可导出字幕',{exact:true}).waitFor();
    assert(await p.locator('#readerMenuBtn').evaluate(button=>button===document.activeElement));
    assert.equal(await p.evaluate(()=>Boolean(window.__attemptedPick)),false);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('matching document filters never discard a draft and clearing them keeps the current row',async()=>{
  const p=await open();try{
    await p.locator('#editReaderBtn').click();await p.locator('.reader-editor').fill('# 临时草稿');await p.locator('#libTagFilter').selectOption('甲');
    await p.waitForFunction(()=>document.querySelectorAll('.doc').length===1);
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 临时草稿');assert.equal(await p.locator('.modal').count(),0);
    await p.locator('#libResetFilters').click();await p.waitForFunction(()=>document.querySelectorAll('.doc').length===2);
    assert.equal(await doc(p,'a').getAttribute('aria-current'),'true');
    assert.equal(await p.locator('.reader-editor').inputValue(),'# 临时草稿');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('canceling a keyboard menu deletion restores focus to its originating document',async()=>{
  const p=await open();try{
    await doc(p,'b').focus();await p.keyboard.press('Shift+F10');await p.keyboard.press('End');
    await p.keyboard.press('ArrowUp');await p.keyboard.press('ArrowUp');
    assert.equal(await p.evaluate(()=>document.activeElement.textContent),'仅删除文稿');
    await p.keyboard.press('Enter');await p.locator('#dlgOk').waitFor();await p.keyboard.press('Escape');assert(await doc(p,'b').evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
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
test('translation completion preserves another open document and offers the selected reading mode', async () => {
  const p = await open();try{
    await p.evaluate(()=>{
      window.__translateArgs=null;window.__poll=0;
      __MOCK__.translate_text=(id,direction)=>{window.__translateArgs={id,direction};return {ok:true,started:true};};
      __MOCK__.get_translation_status=id=>{
        if(id!=='a'||!window.__translateArgs)return {status:''};
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
    await doc(p,'b').click();
    await p.waitForFunction(()=>window.__poll1Flip,{timeout:6000});
    assert.equal(await p.locator('.reader-doc-title').textContent(),'文本乙');
    assert.equal(await p.locator('.translation-segments').count(),0);
    await doc(p,'a').click();
    await p.getByRole('button',{name:'查看译文',exact:true}).click();
    await p.locator('.translation-segments').waitFor();
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
    await p.locator('#readerTranslationBtn').click();
    await p.locator('#readerTransTabs .seg',{hasText:'逐段对照'}).click();
    await p.locator('.translation-segments').waitFor();
    assert.equal(await p.locator('.seg-src').count(),1);
    assert.ok(await p.locator('#editReaderBtn').isDisabled(),'编辑在译文视图下禁用');
    await p.locator('#readerTranslationBtn').click();
    await p.locator('#readerTransTabs .seg',{hasText:'原文'}).first().click();
    await p.waitForFunction(()=>document.querySelector('#readerBody h1')?.textContent==='文本甲');
    assert.equal(await p.locator('.translation-segments').count(),0);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

test('a failed retranslation leaves the earlier whole article readable without fake paragraph pairs', async () => {
  const p = await open();
  try {
    await doc(p, 'b').click();
    await p.evaluate(() => {
      Object.assign(__docs.find(doc => doc.task_id === 'a'), {has_translation: true, translation_lang: 'en', translation_source_tab: 'final'});
      __MOCK__.get_translation_status = id => id === 'a' ? {status: 'failed', running: false, has_translation: true} : {status: ''};
      const read = __MOCK__.get_reader;
      __MOCK__.get_reader = (id, tab) => tab === 'translation'
        ? {ok: true, text: 'Earlier translation', segments: [], lang: 'en', source_tab: 'final'} : read(id, tab);
    });
    await p.evaluate(() => Library.refreshLight());
    await doc(p,'a').click();
    await p.getByText('本次翻译失败，仍可查看上次译文', {exact: true}).waitFor();
    await p.locator('#readerTranslationBtn').click();
    await p.getByRole('button', {name: '逐段对照', exact: true}).click();
    await p.getByText('Earlier translation', {exact: true}).waitFor();
    assert.match(await p.locator('#readerTranslationStatus').textContent(), /译自整理稿.*缺少逐段对照数据/);
    assert.equal(await p.locator('.seg-pair').count(), 0);
    assert.deepEqual(p.errors, []);
  } finally { await p.close(); }
});
