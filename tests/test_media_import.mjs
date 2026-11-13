// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Chromium verifies presentation and the native import handoff; no real user media is read.
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
const shots=path.join(root,'_webview_ui_shots','media-import');
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}) {
  const page=await browser.newPage({viewport:size});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',e=>page.errors.push(e.message));
  await page.addInitScript(installMock,options);
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=import]').click();await page.waitForSelector('#mediaDropZone');
  await page.evaluate(()=>{const original=window.toast;window.__importToasts=[];window.toast=(message,kind)=>{__importToasts.push([message,kind??null]);return original(message,kind);};});
  return page;
}
async function calls(page,name){return page.evaluate(name=>__fixtureCalls.filter(call=>call===name).length,name);}
async function waitIdle(page){await page.waitForFunction(()=>document.getElementById('mediaDropZone').getAttribute('aria-busy')==='false');}
async function drag(page,type,target='#mediaDropZone',kind='Files') {
  return page.evaluate(({type,target,kind})=>{
    const transfer=new DataTransfer();
    // Chromium keeps a synthetic drag's data store read-only outside a trusted
    // OS drag session. Record the app's requested cursor, not an OS acceptance.
    let effect='none';
    Object.defineProperty(transfer,'dropEffect',{get:()=>effect,set:value=>{effect=value;}});
    if(kind==='Files') transfer.items.add(new File(['fixture only'],'first.mp4',{type:'video/mp4'}));
    else transfer.setData('text/plain','text only');
    const event=new DragEvent(type,{bubbles:true,cancelable:true,dataTransfer:transfer});
    document.querySelector(target).dispatchEvent(event);
    return {prevented:event.defaultPrevented,effect:transfer.dropEffect};
  },{type,target,kind});
}

test('file picker reports one queued media and folder picker aggregates queued, duplicate and failed files once',async()=>{
  const page=await open({importReplies:[
    {value:{ok:true,imported:1,enqueued:['one'],skipped:0,failed:0}},
    {value:{ok:true,imported:3,enqueued:['two','three'],skipped:1,failed:1}},
  ]});try{
    await page.getByRole('button',{name:'选择文件',exact:true}).click();await waitIdle(page);
    assert.equal(await page.locator('#importState').textContent(),'已加入 1 项转写任务');
    assert.equal(await calls(page,'pick_and_import'),1);
    await page.getByRole('button',{name:'选择文件夹',exact:true}).click();await waitIdle(page);
    assert.equal(await page.locator('#importState').textContent(),'已加入 2 项转写任务，1 项已存在，1 项未导入');
    assert.equal(await calls(page,'pick_folder_and_import'),1);
    assert.equal(await page.locator('#importState').getAttribute('role'),'status');
    assert.equal(await page.evaluate(()=>__importToasts.length),2,'one aggregate completion notification per request');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('duplicate-only import distinguishes existing tasks from newly queued jobs',async()=>{
  const page=await open({importResult:{ok:true,imported:2,enqueued:[],skipped:2,failed:0}});try{
    await page.locator('#pickFiles').click();await waitIdle(page);
    assert.equal(await page.locator('#importState').textContent(),'已读取 2 项媒体，已有转写任务，2 项已存在');
    assert.equal(await calls(page,'pick_and_import'),1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('nested file drag highlights the drop area, text drag does not; browser preview prevents file navigation',async()=>{
  const page=await open();try{
    assert.equal((await drag(page,'dragenter','#mediaDropZone','text')).prevented,false);
    assert.equal(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')),false);
    assert((await drag(page,'dragenter')).prevented);
    assert(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')));
    await drag(page,'dragenter','.import-drop-title');await drag(page,'dragleave','.import-drop-title');
    assert(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')),'moving between drop-zone children retains highlight');
    assert.equal((await drag(page,'dragover')).effect,'copy');
    await page.screenshot({path:path.join(shots,'dragging-dark.png')});
    const original=page.url();
    assert((await drag(page,'drop')).prevented);
    assert.equal(page.url(),original,'dropping a media file never navigates away from the app');
    assert.equal(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')),false);
    assert.match(await page.locator('#importState').textContent(),/桌面应用/);
    assert.equal(await calls(page,'import_files'),0,'ordinary browser preview does not invent native file paths');
    await drag(page,'dragenter');await drag(page,'dragleave');
    assert.equal(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('native drop handshake is single flight, keeps file paths at the bridge, and ignores duplicate completion',async()=>{
  const paths=['C:\\fixture\\first.mp4','C:\\fixture\\second.wav'];
  const result={ok:true,imported:2,enqueued:['one','two'],skipped:0,failed:0};
  const page=await open({importDelay:300,importResult:result});try{
    await page.locator('[data-page=workbench]').click();
    assert.equal(await page.evaluate(()=>Tools.beginNativeDrop()),false,'native drops only start on the import page');
    await page.evaluate(()=>Tools.finishNativeDrop({ok:true,imported:99,enqueued:[]}));
    await page.locator('[data-page=import]').click();
    assert.equal(await page.evaluate(paths=>{
      const accepted=Tools.beginNativeDrop();
      if(accepted) window.__pendingNativeImport=AppApi.importFiles(paths).then(value=>Tools.finishNativeDrop(value));
      return accepted;
    },paths),true);
    assert.equal(await page.evaluate(()=>Tools.beginNativeDrop()),false);
    assert.equal(await page.locator('#mediaDropZone').getAttribute('aria-busy'),'true');
    assert(await page.locator('#pickFiles').isDisabled());assert(await page.locator('#pickFolder').isDisabled());
    await page.locator('#pickFiles').evaluate(button=>button.click());
    assert.equal(await calls(page,'pick_and_import'),0,'picker cannot start while native drop is reading');
    await drag(page,'dragenter');assert.equal((await drag(page,'dragover')).effect,'none');
    assert.equal(await page.locator('#mediaDropZone').evaluate(node=>node.classList.contains('is-dragging')),false);
    await page.evaluate(()=>__pendingNativeImport);await waitIdle(page);
    const args=await page.evaluate(()=>__fixtureCallArgs.filter(call=>call[0]==='import_files'));
    assert.deepEqual(args,[['import_files',paths]]);
    assert.equal(await page.locator('#importState').textContent(),'已加入 2 项转写任务');
    await page.evaluate(value=>Tools.finishNativeDrop(value),result);
    assert.equal(await page.evaluate(()=>__importToasts.length),1,'repeat native callback cannot double-notify');
    assert(await page.locator('#pickFiles').isEnabled());assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('leaving and returning while reading preserves pending state and does not resubmit',async()=>{
  const page=await open({importDelay:650});try{
    await page.locator('#pickFiles').click();
    assert.equal(await page.locator('#mediaDropZone').getAttribute('aria-busy'),'true');
    await page.locator('[data-page=workbench]').click();
    await page.locator('[data-page=import]').click();
    assert.equal(await page.locator('#mediaDropZone').getAttribute('aria-busy'),'true');
    assert.match(await page.locator('#importState').textContent(),/正在读取媒体/);
    assert(await page.locator('#pickFolder').isDisabled());
    await waitIdle(page);
    assert.equal(await calls(page,'pick_and_import'),1);
    assert.equal(await page.locator('#importState').textContent(),'已加入 1 项转写任务');
    assert.equal(await page.evaluate(()=>__importToasts.length),1);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('late import results leave another page and its notifications untouched, then appear upon return',async()=>{
  const page=await open({importDelay:350});try{
    await page.locator('#pickFolder').click();await page.locator('[data-page=library]').click();
    await page.waitForSelector('.doc');
    const title=await page.locator('#pageTitle').textContent();
    await page.waitForFunction(()=>__fixtureCalls.includes('pick_folder_and_import'));
    await page.waitForTimeout(400);
    assert.equal(await page.locator('#pageTitle').textContent(),title);
    assert.equal(await page.evaluate(()=>AppState.currentView),'library');
    assert.equal(await page.evaluate(()=>__importToasts.length),0,'late completion does not interrupt an unrelated page');
    assert.equal(await page.locator('.modal').count(),0);
    await page.locator('[data-page=import]').click();
    assert.equal(await page.locator('#mediaDropZone').getAttribute('aria-busy'),'false');
    assert.equal(await page.locator('#importState').textContent(),'已加入 1 项转写任务');
    assert.equal(await calls(page,'pick_folder_and_import'),1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('cancelled, rejected and unavailable imports release controls and allow a successful retry',async()=>{
  const page=await open({importReplies:[
    {value:{ok:false,cancelled:true}},
    {value:{ok:false,error:'文件不可用，请重试'}},
    {throws:true},
    {value:{ok:true,imported:1,enqueued:['retry'],skipped:0,failed:0}},
  ]});try{
    await page.locator('#pickFiles').click();await waitIdle(page);
    assert.equal(await page.locator('#importState').textContent(),'');
    assert.equal(await page.evaluate(()=>__importToasts.length),0,'cancel is quiet');
    assert(await page.locator('#pickFiles').isEnabled());
    await page.locator('#pickFiles').click();await waitIdle(page);
    assert.equal(await page.locator('#importState').getAttribute('role'),'alert');
    assert.equal(await page.locator('#importState').textContent(),'文件不可用，请重试');
    await page.locator('#pickFolder').click();await waitIdle(page);
    assert.equal(await page.locator('#importState').textContent(),'导入未完成，请检查文件后重试');
    assert(!((await page.locator('#importState').textContent()).includes('fixture')),'internal exception text stays out of product feedback');
    await page.locator('#pickFiles').click();await waitIdle(page);
    assert.equal(await page.locator('#importState').getAttribute('role'),'status');
    assert.equal(await page.locator('#importState').textContent(),'已加入 1 项转写任务');
    assert.equal(await page.evaluate(()=>__importToasts.length),3);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const theme of ['dark','paper']) {
  test('small import page stays readable and keyboard usable: '+theme,async()=>{
    const page=await open({nativeChrome:true,theme,scale:1.25},{width:560,height:500});try{
      const geometry=await page.locator('#mediaDropZone').evaluate(node=>{
        const box=node.getBoundingClientRect();const title=node.querySelector('h3').getBoundingClientRect();
        return {inside:box.left>=0&&box.right<=innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,titleSize:parseFloat(getComputedStyle(node.querySelector('h3')).fontSize),clear:title.width>0,titleText:node.querySelector('h3').textContent};
      });
      assert(geometry.inside);assert(!geometry.overflow);assert(geometry.clear&&geometry.titleSize>=18);
      await page.locator('#pickFiles').focus();assert(await page.locator('#pickFiles').evaluate(node=>node===document.activeElement));
      await page.keyboard.press('Tab');assert(await page.locator('#pickFolder').evaluate(node=>node===document.activeElement));
      await page.keyboard.press('Enter');await waitIdle(page);assert.equal(await calls(page,'pick_folder_and_import'),1);
      assert.equal(await page.locator('#importState').getAttribute('aria-live'),'polite');
      await page.screenshot({path:path.join(shots,'small-'+theme+'.png')});assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}
