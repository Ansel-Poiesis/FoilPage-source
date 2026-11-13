// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
let browser;before(async()=>{browser=await chromium.launch({headless:true});});after(async()=>{await browser?.close();});
async function open(view='workbench'){
  const p=await browser.newPage({viewport:{width:1280,height:800}});p.setDefaultTimeout(4000);p.errors=[];p.on('pageerror',e=>p.errors.push(e.message));
  await p.addInitScript(installMock);await p.goto(pathToFileURL(path.resolve('app/webview_ui/index.html')).href);await p.waitForFunction(()=>window.__appReady);
  await p.locator('[data-page='+view+']').click();await p.waitForSelector(view==='videos'?'.video-card':'.job');return p;
}
test('Ctrl/Shift additive task selection and deselection keep rows and inspector in agreement',async()=>{
  const p=await open();try{
    const rows=p.locator('.job-open');const order=await p.locator('.job').evaluateAll(items=>items.map(n=>n.dataset.taskId));
    await rows.nth(0).click();await rows.nth(5).click({modifiers:['Control']});await rows.nth(3).click({modifiers:['Control','Shift']});
    assert.deepEqual(await p.evaluate(()=>[...AppState.selected].sort()),[order[0],...order.slice(3,6)].sort());
    await rows.nth(4).click({modifiers:['Control']});assert.equal(await p.locator('.job.selected').count(),3);assert.equal(await p.locator('.job-check-input:checked').count(),3);assert.equal(await p.locator('.job-open[aria-pressed=true]').count(),3);
    assert.match(await p.locator('#inspector .t').textContent(),/3/);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('task filter and inspector action focus survives rendering and pushed progress',async()=>{
  const p=await open();try{
    await p.getByRole('button',{name:/^已完成/}).first().click();assert(await p.locator('.filter.active').evaluate(n=>n===document.activeElement));
    await p.locator('.job-open').first().click();await p.locator('#inspector button').filter({hasText:'重新转写'}).focus();
    await p.evaluate(()=>{AppState.tasks[0].updated_at='now';Workbench.render();});assert.equal(await p.evaluate(()=>document.activeElement.textContent),'重新转写');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a late task refresh cannot overwrite a newer pushed task snapshot',async()=>{
  const p=await open();try{
    await p.evaluate(()=>{const old=structuredClone(AppState.tasks);__MOCK__.get_tasks=()=>new Promise(resolve=>window.__lateTasks=()=>resolve(old));Workbench.refreshAll();});await p.waitForFunction(()=>window.__lateTasks);
    await p.evaluate(()=>__onState({tasks:[{task_id:'new',kind:'transcription',display_title:'新任务',status:'queued'}]}));await p.evaluate(()=>__lateTasks());await p.waitForTimeout(30);
    assert.deepEqual(await p.locator('.job-title').allTextContents(),['新任务']);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('failed task deletion preserves selected task and avoids a false success message',async()=>{
  const p=await open();try{
    await p.locator('.job-open').first().click();await p.evaluate(()=>__MOCK__.delete_tasks=()=>({ok:false,error:'删除被拒绝',removed:[]}));await p.locator('.job-delete').first().click();await p.locator('#dlgOk').click();await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('.job.selected').count(),1);assert.equal(await p.locator('.job').count(),9);assert.equal(await p.locator('#toast').textContent(),'删除被拒绝');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('unchanged video refresh retains focused cover and a changed item restores focused menu trigger',async()=>{
  const p=await open('videos');try{
    const cover=p.locator('.video-cover').first();await cover.focus();await p.evaluate(async()=>{Videos.refreshLight();});await p.waitForTimeout(40);assert(await cover.evaluate(n=>n===document.activeElement));
    const trigger=p.locator('.video-menu-trigger').first();await trigger.focus();await p.evaluate(()=>{const old=__MOCK__.get_downloaded_videos;__MOCK__.get_downloaded_videos=async()=>{const result=await old();return {...result,items:result.items.map((item,index)=>index===0?{...item,title:'更新后标题'}:item)};};document.getElementById('refreshVideos').click();});
    await p.getByText('更新后标题',{exact:true}).waitFor();assert(await p.locator('.video-menu-trigger').first().evaluate(n=>n===document.activeElement));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('failed bulk deletion retains all selected tasks and exposes failure',async()=>{
  const p=await open();try{
    await p.locator('.job-open').first().click();await p.locator('.job-open').nth(3).click({modifiers:['Control']});await p.evaluate(()=>__MOCK__.delete_tasks=()=>({ok:false,error:'批量删除被拒绝',removed:[]}));
    await p.locator('#inspector button').filter({hasText:/^删除$/}).click();await p.locator('#dlgOk').click();await p.locator('#toast.err').waitFor();
    assert.equal(await p.locator('.job.selected').count(),2);assert.equal(await p.locator('#toast').textContent(),'批量删除被拒绝');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
