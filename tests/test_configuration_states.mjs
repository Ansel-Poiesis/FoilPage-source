// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Isolated configuration interactions: no native picker, model, network or user data.
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
const shots=path.join(root,'.ui-audit','round19','configuration-states');
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(view='settings',section='ui',options={},size={width:1280,height:800}) {
  const page=await browser.newPage({viewport:size,reducedMotion:'reduce'});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,options);
  // Wait for the local fixture DOM; font/file cold loads are not this test's
  // performance target. Control and business assertions still use 5 seconds.
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.evaluate(()=>{const original=window.toast;window.__configurationToasts=[];window.toast=(message,kind)=>{__configurationToasts.push({message,kind});return original(message,kind);};});
  await page.locator(`[data-page=${view}]`).click();
  if(view==='settings') await page.locator(`.sitem[data-section=${section}]`).click();
  return page;
}
async function shot(page,name){await page.screenshot({path:path.join(shots,name+'.png')});}
async function expandAgent(page){const summary=page.getByText('模型与连接设置',{exact:true});if(!await summary.evaluate(node=>node.parentElement.open)) await summary.click();}
// A pending request ends only after its assertions, independent of click/CI speed.
async function holdMock(page,...methods) {
  await page.evaluate(methods=>{
    window.__configurationRequests ||= {};
    for(const method of methods) {
      const state={calls:[],pending:[],original:window.__MOCK__[method]};
      window.__configurationRequests[method]=state;
      window.__MOCK__[method]=(...args)=>new Promise(resolve=>{
        state.calls.push(args);state.pending.push(resolve);
      });
    }
  },methods);
}
async function releaseMock(page,method,value) {
  await page.evaluate(({method,value})=>{
    const resolve=window.__configurationRequests[method].pending.shift();
    if(!resolve) throw new Error(`No pending fixture request: ${method}`);
    resolve(value);
  },{method,value});
}
async function waitForRequest(page,method,count) {
  await page.waitForFunction(({method,count})=>window.__configurationRequests[method].calls.length===count,{method,count});
}

test('rejected settings do not change the switch or emit success; retry emits one acknowledgement',async()=>{
  const page=await open('settings','control');try{
    await page.evaluate(()=>{window.__realSave=__MOCK__.save_settings;__MOCK__.save_settings=()=>({ok:false,error:'测试保存失败'});});
    const toggle=page.getByRole('switch',{name:'启用本地 API'});await toggle.click();
    await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await toggle.getAttribute('aria-checked'),'false');
    assert.equal(await page.evaluate(()=>AppState.settings.local_api_enabled),undefined);
    assert.deepEqual(await page.evaluate(()=>__configurationToasts.map(item=>item.kind)),['error']);
    await shot(page,'settings-control-save-rejected');
    await page.evaluate(()=>{__MOCK__.save_settings=__realSave;});await toggle.click();
    await page.waitForFunction(()=>document.querySelector('[aria-label="启用本地 API"]').getAttribute('aria-checked')==='true');
    assert.equal(await page.evaluate(()=>__configurationToasts.length),2,'one error then one successful acknowledgement');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('diagnostic export is single-flight, cancelable and leaves failure available for an explicit retry',async()=>{
  const page=await open('settings','data');try{
    await holdMock(page,'pick_save_file','export_diagnostics');
    const button=page.locator('.settings-data-actions button:last-child');assert.equal(await button.textContent(),'导出诊断包');
    await button.click();await waitForRequest(page,'pick_save_file',1);
    assert.equal(await button.isDisabled(),true);
    await button.evaluate(node=>node.click());assert.equal(await page.evaluate(()=>__configurationRequests.pick_save_file.calls.length),1);
    await releaseMock(page,'pick_save_file',{ok:true,cancelled:true});await page.waitForFunction(()=>!document.querySelector('.settings-data-actions button:last-child').disabled);
    assert.equal(await page.evaluate(()=>__configurationRequests.export_diagnostics.calls.length),0);assert.equal(await page.evaluate(()=>__configurationToasts.length),0);
    await button.click();await waitForRequest(page,'pick_save_file',2);
    await releaseMock(page,'pick_save_file',{ok:true,path:'fixture-diagnostics.zip'});await waitForRequest(page,'export_diagnostics',1);
    await releaseMock(page,'export_diagnostics',{ok:false,error:'测试导出失败'});await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await button.isDisabled(),false);assert.equal(await button.textContent(),'导出诊断包');
    assert.deepEqual(await page.evaluate(()=>__configurationToasts.map(item=>item.kind)),['error']);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('dirty text, caret, expanded details and scroll survive rebuilding the settings page',async()=>{
  const page=await open('settings','agent',{settings:{agent_model:'saved-model'}});try{
    await expandAgent(page);
    const model=page.getByLabel('模型',{exact:true});await model.fill('unsaved-model');
    await model.evaluate(node=>node.setSelectionRange(3,8));
    await page.evaluate(()=>Settings.render());
    assert.equal(await model.inputValue(),'unsaved-model');
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'saved-model');
    assert.deepEqual(await model.evaluate(node=>[node.selectionStart,node.selectionEnd]),[3,8]);
    assert.equal(await model.evaluate(node=>node===document.activeElement),true);
    assert.equal(await page.getByText('模型与连接设置',{exact:true}).evaluate(node=>node.parentElement.open),true);
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=agent]').click();
    assert.equal(await model.inputValue(),'unsaved-model');
    await shot(page,'settings-agent-dirty-retained');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('late save cannot replace a newer text draft or unrelated settings; Enter retry does not duplicate on blur',async()=>{
  const page=await open('settings','agent',{settings:{agent_model:'saved-model',confirm_delete:true}});try{
    await expandAgent(page);
    await holdMock(page,'save_settings');
    const model=page.getByLabel('模型',{exact:true});await model.fill('first-model');await page.keyboard.press('Enter');
    await waitForRequest(page,'save_settings',1);
    const firstSnapshot=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[0]);});
    assert.equal(firstSnapshot.agent_model,'first-model');assert.equal(firstSnapshot.confirm_delete,true,'the delayed full response deliberately contains an unrelated old value');
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'saved-model','pending acknowledgement has not updated local state');
    assert.equal(await model.getAttribute('aria-busy'),'true');assert.equal(await model.isDisabled(),false);
    await model.fill('newer-draft');
    await page.evaluate(()=>{AppState.settings.confirm_delete=false;Settings.render();});
    assert.equal(await model.inputValue(),'newer-draft');assert.equal(await model.isDisabled(),false);assert.equal(await model.getAttribute('aria-busy'),'true');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>__configurationRequests.save_settings.calls.length),1,'rebuilding an editable pending input does not submit a second request');
    await releaseMock(page,'save_settings',firstSnapshot);
    await page.waitForFunction(()=>AppState.settings.agent_model==='first-model' && !document.querySelector('[aria-label="模型"]').hasAttribute('aria-busy'));
    assert.equal(await model.inputValue(),'newer-draft');
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'first-model');
    assert.equal(await page.evaluate(()=>AppState.settings.confirm_delete),false);
    assert.equal(await model.getAttribute('aria-busy'),null);assert.equal(await model.evaluate(node=>node===document.activeElement),true);
    await model.focus();await page.keyboard.press('Enter');await waitForRequest(page,'save_settings',2);
    assert.equal(await model.getAttribute('aria-busy'),'true');
    const secondSnapshot=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[1]);});
    const baseUrl=page.getByLabel('接口地址',{exact:true});await baseUrl.fill('https://fixture.invalid/v1');await page.keyboard.press('Enter');
    assert.equal(await baseUrl.getAttribute('aria-busy'),'true');
    assert.equal(await page.evaluate(()=>__configurationRequests.save_settings.calls.length),2,'the second field waits in the existing serialized save queue');
    await releaseMock(page,'save_settings',secondSnapshot);
    await page.waitForFunction(()=>AppState.settings.agent_model==='newer-draft' && document.querySelector('[aria-label="模型"]').dataset.initialValue==='newer-draft' && !document.querySelector('[aria-label="模型"]').hasAttribute('aria-busy'));
    await waitForRequest(page,'save_settings',3);
    assert.equal(await baseUrl.getAttribute('aria-busy'),'true','finishing the model control does not clear another pending key');
    assert.equal(await baseUrl.inputValue(),'https://fixture.invalid/v1');assert.equal(await baseUrl.evaluate(node=>node===document.activeElement),true);
    const thirdSnapshot=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[2]);});
    await releaseMock(page,'save_settings',thirdSnapshot);
    await page.waitForFunction(()=>AppState.settings.agent_base_url==='https://fixture.invalid/v1' && !document.querySelector('[aria-label="接口地址"]').hasAttribute('aria-busy'));
    await model.focus();await baseUrl.focus();
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.save_settings.calls),[[{agent_model:'first-model'}],[{agent_model:'newer-draft'}],[{agent_base_url:'https://fixture.invalid/v1'}]],'each explicit save is submitted once; Enter followed by blur never repeats either text value');
    assert.equal(await page.evaluate(()=>AppState.settings.confirm_delete),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a rejected custom toggle is not retried by a name save; newer text drafts survive rejection',async()=>{
  const profiles=[
    {id:'alpha',name:'Alpha',enabled:true,system_prompt:'Preserve the original words.',model:'',temperature:null},
    {id:'beta',name:'Beta',enabled:false,system_prompt:'Preserve examples.',model:'',temperature:null},
  ];
  const page=await open('settings','agent',{settings:{agent_custom_profiles:profiles}});try{
    await holdMock(page,'save_settings');
    const alpha=page.locator('[data-profile-id=alpha]'),beta=page.locator('[data-profile-id=beta]');
    const toggle=alpha.getByRole('switch',{name:'启用自定义 Agent'}),name=alpha.getByLabel('自定义 Agent 名称');
    await toggle.click();await waitForRequest(page,'save_settings',1);
    assert.equal(await page.evaluate(()=>__configurationRequests.save_settings.calls[0][0].agent_custom_profiles.find(item=>item.id==='alpha').enabled),false);
    await page.evaluate(()=>Settings.render());
    await name.focus();await name.evaluate(node=>node.setSelectionRange(1,3));
    await releaseMock(page,'save_settings',{ok:false,error:'启用状态保存失败'});
    await page.waitForFunction(()=>!document.querySelector('[data-profile-id=alpha] [role=switch]').disabled);
    assert.equal(await toggle.getAttribute('aria-checked'),'true');assert.equal(await toggle.evaluate(node=>node.classList.contains('on')),true);
    assert.equal(await beta.getByRole('switch',{name:'启用自定义 Agent'}).getAttribute('aria-checked'),'false');
    assert.equal(await name.evaluate(node=>node===document.activeElement),true);assert.deepEqual(await name.evaluate(node=>[node.selectionStart,node.selectionEnd]),[1,3]);
    await name.fill('Renamed Alpha');await page.keyboard.press('Enter');await waitForRequest(page,'save_settings',2);
    const nameRequest=await page.evaluate(()=>__configurationRequests.save_settings.calls[1][0].agent_custom_profiles);
    assert.equal(nameRequest.find(item=>item.id==='alpha').name,'Renamed Alpha');assert.equal(nameRequest.find(item=>item.id==='alpha').enabled,true,'a name-only save cannot resurrect the previously rejected disable');
    assert.equal(nameRequest.find(item=>item.id==='beta').enabled,false);
    await name.fill('Newer Alpha draft');await name.evaluate(node=>node.setSelectionRange(2,7));await page.evaluate(()=>Settings.render());
    assert.equal(await name.getAttribute('aria-busy'),'true');assert.equal(await name.inputValue(),'Newer Alpha draft');
    await releaseMock(page,'save_settings',{ok:false,error:'名称保存失败'});
    await page.waitForFunction(()=>!document.querySelector('[data-profile-id=alpha] [aria-label="自定义 Agent 名称"]').hasAttribute('aria-busy'));
    assert.equal(await name.inputValue(),'Newer Alpha draft');assert.equal(await name.evaluate(node=>node===document.activeElement),true);assert.deepEqual(await name.evaluate(node=>[node.selectionStart,node.selectionEnd]),[2,7]);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_custom_profiles.find(item=>item.id==='alpha').name),'Alpha');
    await page.keyboard.press('Enter');await waitForRequest(page,'save_settings',3);
    const retried=await page.evaluate(()=>__configurationRequests.save_settings.calls[2][0].agent_custom_profiles.find(item=>item.id==='alpha'));
    assert.equal(retried.name,'Newer Alpha draft');assert.equal(retried.enabled,true);
    const saved=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[2]);});
    await releaseMock(page,'save_settings',saved);
    await page.waitForFunction(()=>AppState.settings.agent_custom_profiles.find(item=>item.id==='alpha').name==='Newer Alpha draft' && !document.querySelector('[data-profile-id=alpha] [aria-label="自定义 Agent 名称"]').hasAttribute('aria-busy'));
    assert.equal(await name.inputValue(),'Newer Alpha draft');assert.equal(await toggle.getAttribute('aria-checked'),'true');
    assert.equal(await page.evaluate(()=>__configurationRequests.save_settings.calls.length),3);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('select failure restores persisted choice; invalid numeric text remains editable and can be corrected',async()=>{
  const page=await open('settings','recognition',{settings:{device:'cuda',max_attempts:3}});try{
    await page.evaluate(()=>{window.__realSave=__MOCK__.save_settings;__MOCK__.save_settings=()=>({ok:false,error:'保存失败'});});
    await page.getByLabel('运行设备',{exact:true}).selectOption('cpu');
    await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await page.getByLabel('运行设备',{exact:true}).inputValue(),'cuda');
    assert.equal(await page.getByLabel('运行设备',{exact:true}).isDisabled(),false);
    await page.evaluate(()=>{__MOCK__.save_settings=__realSave;});
    const attempts=page.getByLabel('最大尝试次数',{exact:true});await attempts.fill('1.5');await page.keyboard.press('Enter');
    assert.equal(await attempts.inputValue(),'1.5');assert.equal(await attempts.getAttribute('aria-invalid'),'true');
    assert.equal(await page.evaluate(()=>AppState.settings.max_attempts),3,'fractional attempts must not be silently truncated');
    await attempts.fill('4');await page.keyboard.press('Enter');await page.waitForFunction(()=>AppState.settings.max_attempts===4);
    assert.equal(await attempts.getAttribute('aria-invalid'),null);
    await holdMock(page,'save_settings');
    await page.getByLabel('运行设备',{exact:true}).selectOption('cpu');await waitForRequest(page,'save_settings',1);
    await page.locator('.sitem[data-section=about]').click();await page.locator('.sitem[data-section=recognition]').click();
    const device=page.getByLabel('运行设备',{exact:true});assert.equal(await device.isDisabled(),true);
    await attempts.fill('7');await attempts.focus();
    const confirmed=await page.evaluate(()=>__configurationRequests.save_settings.original({device:'cuda'}));
    await releaseMock(page,'save_settings',confirmed);
    await page.waitForFunction(()=>!document.querySelector('[aria-label="运行设备"]').disabled);
    assert.equal(await device.inputValue(),'cuda','a rebuilt select uses the actual confirmed value, rather than the attempted choice');
    assert.equal(await device.getAttribute('aria-busy'),null);assert.equal(await device.evaluate(node=>node.dataset.initialValue),'cuda');
    assert.equal(await attempts.inputValue(),'7');assert.equal(await attempts.evaluate(node=>node===document.activeElement),true);
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.save_settings.calls),[[{device:'cpu'}]]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('accent selection updates all swatches and pending switches cannot be resubmitted after reentry',async()=>{
  const page=await open('settings','ui');try{
    await page.getByRole('button',{name:'霞红',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.accent-swatch[aria-label="霞红"]').getAttribute('aria-pressed')==='true');
    assert.equal(await page.locator('.accent-swatch[aria-pressed=true]').count(),1);
    assert.equal(await page.getByRole('button',{name:'霞红',exact:true}).evaluate(node=>node===document.activeElement),true);
    const acknowledgements=await page.evaluate(()=>__configurationToasts.length);
    await holdMock(page,'save_settings');
    await page.getByRole('switch',{name:'界面动效'}).click();
    await waitForRequest(page,'save_settings',1);
    await page.locator('.sitem[data-section=about]').click();await page.locator('.sitem[data-section=ui]').click();
    const toggle=page.getByRole('switch',{name:'界面动效'});
    assert.equal(await toggle.isDisabled(),true);assert.equal(await toggle.getAttribute('aria-busy'),'true');assert.equal(await toggle.getAttribute('aria-checked'),'false');
    await toggle.dispatchEvent('click');
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.save_settings.calls),[[{animations:true}]],'the reentered pending switch stays single flight');
    assert.equal(await page.evaluate(()=>__configurationToasts.length),acknowledgements,'pending changes do not acknowledge success');
    const confirm=page.getByRole('switch',{name:'删除前确认'});await confirm.click();
    await page.evaluate(()=>{Settings.render();window.__pendingSettingsPane=document.getElementById('settingsPane');});
    assert.equal(await confirm.isDisabled(),true);assert.equal(await confirm.getAttribute('aria-busy'),'true');
    const theme=page.getByLabel('主题',{exact:true});await theme.focus();
    const saved=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[0]);});
    await releaseMock(page,'save_settings',saved);
    await page.waitForFunction(()=>!document.querySelector('[aria-label="界面动效"]').disabled && AppState.settings.animations===true);
    assert.equal(await toggle.isDisabled(),false);assert.equal(await toggle.getAttribute('aria-busy'),null);
    assert.equal(await toggle.getAttribute('aria-checked'),'true');assert.equal(await toggle.evaluate(node=>node.classList.contains('on')),true,'the current switch reflects its confirmation without leaving the section');
    assert.equal(await confirm.isDisabled(),true);assert.equal(await confirm.getAttribute('aria-busy'),'true');assert.equal(await confirm.getAttribute('aria-checked'),'false');
    assert.equal(await theme.evaluate(node=>node===document.activeElement),true);assert.equal(await page.evaluate(()=>document.getElementById('settingsPane')===__pendingSettingsPane),true,'a late completion updates only its control, without rebuilding the page');
    assert.equal(await page.evaluate(()=>__configurationToasts.length),acknowledgements+1);
    await waitForRequest(page,'save_settings',2);
    const savedConfirm=await page.evaluate(()=>{const request=__configurationRequests.save_settings;return request.original(...request.calls[1]);});
    await releaseMock(page,'save_settings',savedConfirm);
    await page.waitForFunction(()=>!document.querySelector('[aria-label="删除前确认"]').disabled && AppState.settings.confirm_delete===true);
    assert.equal(await confirm.getAttribute('aria-checked'),'true');assert.equal(await confirm.evaluate(node=>node.classList.contains('on')),true);
    assert.equal(await theme.evaluate(node=>node===document.activeElement),true);assert.equal(await page.evaluate(()=>document.getElementById('settingsPane')===__pendingSettingsPane),true);
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.save_settings.calls),[[{animations:true}],[{confirm_delete:true}]]);
    assert.equal(await page.evaluate(()=>__configurationToasts.length),acknowledgements+2);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('startup registration errors keep persisted state and late successes do not notify another section',async()=>{
  const page=await open('settings','startup',{settings:{launch_at_startup:false}});try{
    await page.evaluate(()=>{window.__startupFailures=[];__MOCK__.set_launch_at_startup=enabled=>{__startupFailures.push(enabled);return {ok:false,error:'注册失败'};};});
    await page.getByRole('switch',{name:'开机静默启动'}).click();await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await page.getByRole('switch',{name:'开机静默启动'}).getAttribute('aria-checked'),'false');
    assert.equal(await page.getByRole('switch',{name:'开机静默启动'}).isDisabled(),false);assert.deepEqual(await page.evaluate(()=>__startupFailures),[true]);
    assert.equal(await page.evaluate(()=>__configurationToasts[0].kind),'error');
    await holdMock(page,'set_launch_at_startup');
    await page.getByRole('switch',{name:'开机静默启动'}).click();await waitForRequest(page,'set_launch_at_startup',1);
    assert.equal(await page.getByRole('switch',{name:'开机静默启动'}).isDisabled(),true);
    await page.locator('.sitem[data-section=ui]').click();
    await page.evaluate(()=>{window.__startupOutsideFocus=document.activeElement;window.__startupOutsidePane=document.getElementById('settingsPane');});
    assert.equal(await page.evaluate(()=>AppState.settings.launch_at_startup),false);assert.equal(await page.evaluate(()=>__configurationToasts.length),1);
    const saved=await page.evaluate(()=>({...__MOCK__.save_settings({launch_at_startup:true}),agent_enabled:false}));
    await releaseMock(page,'set_launch_at_startup',saved);
    await page.waitForFunction(()=>AppState.settings.launch_at_startup===true);
    assert.equal(await page.evaluate(()=>AppState.settings.launch_at_startup),true);assert.equal(await page.evaluate(()=>AppState.settings.agent_enabled),true);
    assert.equal(await page.evaluate(()=>document.activeElement===__startupOutsideFocus),true);assert.equal(await page.evaluate(()=>document.getElementById('settingsPane')===__startupOutsidePane),true,'another section is not replaced or focused by a late startup result');
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.set_launch_at_startup.calls),[[true]]);
    await page.locator('.sitem[data-section=startup]').click();
    assert.equal(await page.getByRole('switch',{name:'开机静默启动'}).getAttribute('aria-checked'),'true');assert.equal(await page.getByRole('switch',{name:'开机静默启动'}).isDisabled(),false);
    assert.equal(await page.evaluate(()=>__configurationToasts.length),1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('model read retries and pending downloads survive reentry without duplicate submission; delete modal cancels safely',async()=>{
  const page=await open('settings','model');try{
    await page.evaluate(()=>{window.__modelsActive=false;window.__modelDelete=0;__MOCK__.get_models=()=>({ok:false,error:'模型状态读取失败'});});
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=model]').click();
    await page.getByRole('button',{name:'重试读取模型'}).waitFor();await shot(page,'settings-model-read-error');
    await page.evaluate(()=>{__MOCK__.get_models=()=>({ok:true,free_bytes:1e10,download:{name:__modelsActive?'tiny':'',progress:.42},models:[{name:'tiny',size_label:'75 MB',description:'本地模型',verified:false},{name:'small',size_label:'488 MB',description:'本地模型',verified:true}]});__MOCK__.delete_model=()=>{++__modelDelete;return {ok:false,error:'fixture'};};});
    await holdMock(page,'download_model');
    await page.getByRole('button',{name:'重试读取模型'}).click();
    const tiny=page.locator('#settingsPane [data-model=tiny] button[data-state=idle]');
    await tiny.evaluate(node=>{window.__submitTinyAgain=node.onclick;});await tiny.click();await waitForRequest(page,'download_model',1);
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=model]').click();
    const active=page.locator('#settingsPane [data-model=tiny] button[data-state=active]');await active.waitFor();assert.equal(await active.isDisabled(),true);
    assert.equal(await page.evaluate(()=>__MOCK__.get_models().download.name),'','pending UI state is exercised before the backend reports a download');
    assert.equal(await page.locator('#settingsPane [data-model=tiny] .model-bar-fill').evaluate(node=>node.style.width),'0%');
    await page.evaluate(()=>__submitTinyAgain());
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.download_model.calls),[['tiny']],'even the original download handler cannot duplicate a pending request');
    await page.evaluate(()=>{__modelsActive=true;});await releaseMock(page,'download_model',{ok:true});
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=model]').click();
    await active.waitFor();assert.equal(await active.isDisabled(),true);
    assert.equal(await page.evaluate(()=>__MOCK__.get_models().download.name),'tiny');
    assert.equal(await page.locator('#settingsPane [data-model=tiny] .model-bar-fill').evaluate(node=>node.style.width),'42%','resolved submission now stays active from the actual backend download payload');
    assert.equal(await page.evaluate(()=>__configurationRequests.download_model.calls.length),1);
    assert.equal(await page.evaluate(()=>__configurationToasts.length),0,'the replaced panel does not notify a late submission success');
    await page.locator('#settingsPane [data-model=small] button.danger').click();await page.getByRole('dialog').waitFor();
    await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>__modelDelete),0);
    assert.equal(await page.locator('#settingsPane [data-model=small] button.danger').evaluate(node=>node===document.activeElement),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('link drafts and single pending submission persist across navigation; rejected submission stays retryable',async()=>{
  const page=await open('links');try{
    await holdMock(page,'download_from_url');
    await page.locator('#linkUrl').fill('https://www.youtube.com/watch?v=fixture123');await page.getByRole('button',{name:'开始下载'}).click();
    await waitForRequest(page,'download_from_url',1);
    await page.locator('[data-page=import]').click();await page.locator('[data-page=links]').click();
    assert.equal(await page.getByRole('button',{name:'开始下载'}).isDisabled(),true);
    assert.equal(await page.locator('#linkForm').getAttribute('aria-busy'),'true');
    assert.equal(await page.locator('#linkUrl').isDisabled(),true);
    assert.equal(await page.locator('#linkUrl').inputValue(),'https://www.youtube.com/watch?v=fixture123');
    await page.locator('#linkForm').dispatchEvent('submit');
    assert.equal(await page.evaluate(()=>__configurationRequests.download_from_url.calls.length),1,'pending reentry cannot submit the same request twice');
    await releaseMock(page,'download_from_url',{ok:false,error:'测试下载提交失败'});
    await page.waitForFunction(()=>document.getElementById('linkForm').getAttribute('aria-busy')==='false');
    assert.equal(await page.evaluate(()=>__configurationRequests.download_from_url.calls.length),1);assert.match(await page.locator('#linkState').textContent(),/测试下载提交失败/);
    assert.equal(await page.getByRole('button',{name:'开始下载'}).isDisabled(),false);
    assert.equal(await page.locator('#linkState').getAttribute('role'),'alert');await shot(page,'links-submit-failed');
    await page.getByRole('button',{name:'开始下载'}).click();await waitForRequest(page,'download_from_url',2);
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.download_from_url.calls),[
      ['https://www.youtube.com/watch?v=fixture123','',true],['https://www.youtube.com/watch?v=fixture123','',true],
    ],'retry preserves the original URL, optional title and transcription choice');
    await releaseMock(page,'download_from_url',{ok:true});await page.waitForFunction(()=>document.getElementById('linkState').textContent==='已加入任务');
    assert.equal(await page.evaluate(()=>__configurationRequests.download_from_url.calls.length),2);assert.equal(await page.evaluate(()=>__configurationToasts.length),0,'inline request state does not create duplicate notifications');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('failed data checks and rejected orphan cleanup never report empty or cleaned data, and allow retry',async()=>{
  const page=await open('settings','data');try{
    await page.evaluate(()=>{
      window.__cleanupCalls=0;window.__checkFails=true;
      __MOCK__.data_reconcile=()=>__checkFails?{ok:false,error:'自检读取失败'}:{ok:true,stale_fixed:[],orphan_outputs:[{name:'测试产物',path:'C:\\fixture\\orphan.md'}],orphan_downloads:[]};
      __MOCK__.cleanup_orphans=async()=>{++__cleanupCalls;await new Promise(resolve=>setTimeout(resolve,120));return __cleanupCalls===1?{ok:false,error:'清理失败，请重试'}:{ok:true,removed_outputs:[],removed_downloads:[]};};
    });
    await page.getByRole('button',{name:'运行数据自检'}).click();await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await page.locator('.reconcile-box').textContent(),'自检读取失败');assert.equal(await page.getByText('未发现孤儿产物。',{exact:true}).count(),0);
    await page.evaluate(()=>{__checkFails=false;});await page.getByRole('button',{name:'运行数据自检'}).click();
    await page.locator('.reconcile-box .btn').waitFor();await page.locator('.reconcile-box .btn').click();await page.locator('#dlgOk').click();
    await page.waitForFunction(()=>__configurationToasts.length===3);assert.equal(await page.evaluate(()=>__configurationToasts.at(-1).kind),'error');
    assert.match(await page.locator('.reconcile-box').textContent(),/测试产物/);assert.equal(await page.locator('.reconcile-box .btn').isDisabled(),false);
    await page.locator('.reconcile-box .btn').click();await page.locator('#dlgOk').click();await page.waitForFunction(()=>__configurationToasts.length===4);
    assert.match(await page.evaluate(()=>__configurationToasts.at(-1).message),/未被清理/);assert.equal(await page.evaluate(()=>__configurationToasts.some(item=>item.message==='已清理')),false);
    await shot(page,'settings-data-cleanup-rejected');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('browser pairing and missing paths are safely disabled while loading; pairing remains single flight',async()=>{
  const page=await open('browser');try{
    await holdMock(page,'browser_status','begin_browser_pairing');
    const status={ok:true,running:true,configured:false,extension_dir:'C:\\fixture\\extension'};
    await page.locator('[data-page=links]').click();await page.locator('[data-page=browser]').click();
    await waitForRequest(page,'browser_status',1);
    assert.equal(await page.locator('#pairBrowser').isDisabled(),true);assert.equal(await page.locator('#extensionDir').isDisabled(),true);
    await releaseMock(page,'browser_status',status);
    await page.locator('.browser-install-steps button').nth(2).click();
    assert.equal(await page.locator('#pairBrowser').isVisible(),true,'connection is available in its dedicated installation step');
    await page.waitForFunction(()=>!document.getElementById('pairBrowser').disabled);await page.locator('#pairBrowser').click();
    await waitForRequest(page,'begin_browser_pairing',1);
    await page.locator('[data-page=links]').click();await page.locator('[data-page=browser]').click();
    await waitForRequest(page,'browser_status',2);await releaseMock(page,'browser_status',status);
    await page.locator('.browser-install-steps button').nth(2).click();
    await page.waitForFunction(()=>document.getElementById('pairBrowser').dataset.serviceReady==='true');
    assert.equal(await page.locator('#pairBrowser').isDisabled(),true);assert.equal(await page.locator('#extensionDir').isDisabled(),false);
    await page.locator('#pairBrowser').dispatchEvent('click');
    assert.equal(await page.evaluate(()=>__configurationRequests.begin_browser_pairing.calls.length),1,'pending pairing cannot be resubmitted after reentry');
    await releaseMock(page,'begin_browser_pairing',{ok:false,error:'配对失败，请重试'});
    await page.waitForFunction(()=>!document.getElementById('pairBrowser').disabled);
    assert.equal(await page.locator('#pairBrowser').isDisabled(),false);assert.equal(await page.evaluate(()=>__configurationRequests.begin_browser_pairing.calls.length),1);
    assert.equal(await page.evaluate(()=>__configurationToasts.length),0,'old-panel pairing error stays quiet after replacement');
    await page.locator('#pairBrowser').click();await waitForRequest(page,'begin_browser_pairing',2);
    await releaseMock(page,'begin_browser_pairing',{ok:false,error:'配对失败，请重试'});
    await page.waitForFunction(()=>__configurationToasts.length===1);assert.equal(await page.evaluate(()=>__configurationToasts[0].kind),'error');
    assert.equal(await page.evaluate(()=>__configurationRequests.begin_browser_pairing.calls.length),2);
    assert.equal(await page.locator('#pairBrowser').isDisabled(),false);
    await shot(page,'browser-pair-failed');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('setup rejects an unconfirmed default model and keeps focused selection during asynchronous checks',async()=>{
  const page=await open('setup','ui',{modelItems:[
    {name:'tiny',installed:true,verified:true,ready:true,reason:''},
    {name:'base',installed:true,verified:true,ready:true,reason:''},
  ]});try{
    await page.locator('#setupTab-models').click();
    await page.locator('[data-tour=setup-default-model]').waitFor();
    await page.evaluate(()=>{__MOCK__.save_settings=()=>({ok:false,error:'模型配置保存失败'});});
    const model=page.locator('[data-tour=setup-default-model]');const original=await model.inputValue();
    await model.selectOption('base');await page.waitForFunction(()=>__configurationToasts.length===1);
    assert.equal(await model.inputValue(),original);assert.equal(await page.evaluate(()=>__configurationToasts[0].kind),'error');
    await page.waitForFunction(()=>!document.querySelector('[data-tour=setup-default-model]').disabled);
    const checkedData=await page.evaluate(()=>__MOCK__.get_setup_check());
    checkedData.rows[0].value='受控复查完成';
    await holdMock(page,'get_setup_check');
    await page.locator('#setupTab-environment').click();
    await page.getByRole('button',{name:'重新检查',exact:true}).click();
    await waitForRequest(page,'get_setup_check',1);
    await page.locator('#setupTab-models').click();await model.focus();
    await releaseMock(page,'get_setup_check',checkedData);
    await page.waitForFunction(()=>document.getElementById('setupEnvRows').textContent.includes('受控复查完成'));
    assert.equal(await model.inputValue(),original);
    assert.equal(await model.evaluate(node=>node===document.activeElement),true);
    await shot(page,'setup-model-save-rejected');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('onboarding reports a rejected progress acknowledgement and close never executes a background action',async()=>{
  const page=await open('setup');try{
    await page.evaluate(()=>{__MOCK__.save_settings=()=>({ok:false,error:'进度保存失败'});Onboarding.show({chapter:'browser'});});
    await page.waitForSelector('.ob-card');await page.waitForFunction(()=>document.querySelector('.ob-save-notice')?.textContent.includes('保存失败'));
    await shot(page,'guide-progress-save-rejected');
    const before=await page.evaluate(()=>__fixtureCalls.filter(call=>['begin_browser_pairing','download_model','pick_and_import'].includes(call)).length);
    await page.getByRole('button',{name:'跳过本轮全部引导'}).click();assert.equal(await page.locator('.ob-card').count(),0);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(call=>['begin_browser_pairing','download_model','pick_and_import'].includes(call)).length),before);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('all configuration pages and the guide modal remain contained and usable at the minimum window',async()=>{
  for(const theme of ['dark','paper']){
    const page=await open('settings','ui',{theme,scale:1.25,nativeChrome:true},{width:560,height:500});try{
      for(const section of ['ui','recognition','agent','control','model','data','startup','about']){
        await page.locator(`.sitem[data-section=${section}]`).click();
        assert.equal(await page.locator('.sitem[aria-pressed=true]').count(),1);
        assert.equal(await page.locator('.sitem.active').getAttribute('data-section'),section);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
        await shot(page,`${theme}-settings-${section}`);
      }
      await page.locator('.sitem[data-section=agent]').click();await page.getByRole('button',{name:'阅读配置指南'}).click();await page.getByRole('dialog').waitFor();
      await shot(page,`${theme}-agent-guide-modal`);await page.mouse.click(2,2);assert.equal(await page.locator('.modal').count(),0);assert.equal(await page.locator('.sitem.active').getAttribute('data-section'),'agent');
      for(const view of ['setup','browser','links','import']){await page.locator(`[data-page=${view}]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await shot(page,`${theme}-${view}`);}
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  }
});

test('the default model and library use one confirmed inventory and reject unavailable choices',async()=>{
  const page=await open('settings','ui',{settings:{whisper_model:'tiny'}});try{
    await holdMock(page,'get_models');await page.locator('.sitem[data-section=model]').click();await waitForRequest(page,'get_models',1);
    const model=page.getByLabel('默认模型',{exact:true});
    assert.equal(await model.isDisabled(),true);
    assert.equal(await model.locator('option:not(:disabled)').count(),0);
    const inventory={ok:true,free_bytes:1e10,download:{name:'medium',progress:.42},models:[
      {name:'tiny',installed:false,verified:false,ready:false,reason:'未下载'},
      {name:'base',installed:true,verified:true,ready:false,reason:'模型权重不完整，请重新下载'},
      {name:'small',installed:true,verified:true,ready:true,reason:''},
      {name:'medium',installed:true,verified:true,ready:true,reason:''},
    ]};
    await releaseMock(page,'get_models',inventory);await page.waitForFunction(()=>document.querySelector('[data-model-select]').dataset.modelAvailable==='true');
    assert.equal(await model.inputValue(),'tiny','checking inventory never silently saves a replacement');
    assert.deepEqual(await model.locator('option').evaluateAll(options=>options.filter(option=>!option.disabled).map(option=>option.value)),['small']);
    assert.match(await model.locator('option[value=base]').textContent(),/模型权重不完整/);
    assert.match(await model.locator('option[value=medium]').textContent(),/下载中/);
    assert.match(await page.locator('#settingsPane .model-item[data-model=small] .set-label').textContent(),/已就绪/);
    assert.match(await page.locator('#settingsPane .model-item[data-model=base] .model-desc').textContent(),/模型权重不完整/);
    assert.equal(await page.locator('#settingsPane .model-item[data-model=medium] [data-state=active]').isDisabled(),true);
    const saves=await page.evaluate(()=>__fixtureCalls.filter(name=>name==='save_settings').length);
    await model.evaluate(node=>{node.value='base';node.dispatchEvent(new Event('change',{bubbles:true}));});
    assert.equal(await model.inputValue(),'tiny');assert.equal(await page.evaluate(()=>__fixtureCalls.filter(name=>name==='save_settings').length),saves);
    assert.equal(await page.evaluate(()=>AppState.settings.whisper_model),'tiny');
    assert.equal(await page.evaluate(()=>__configurationRequests.get_models.calls.length),1,'the selector does not issue a second inventory request');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('ready models save once, a rejected acknowledgement restores the confirmed choice, and legacy readiness is strict',async()=>{
  const page=await open('settings','model',{settings:{whisper_model:'tiny'},modelItems:[
    {name:'tiny',installed:true,verified:true,ready:true,reason:''},
    {name:'small',installed:true,verified:true,ready:true,reason:''},
  ]});try{
    const model=page.getByLabel('默认模型',{exact:true});await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    await holdMock(page,'save_settings');await model.selectOption('small');await waitForRequest(page,'save_settings',1);
    assert.equal(await model.isDisabled(),true);assert.equal(await page.evaluate(()=>AppState.settings.whisper_model),'tiny');
    const saved=await page.evaluate(()=>__configurationRequests.save_settings.original({whisper_model:'small'}));await releaseMock(page,'save_settings',saved);
    await page.waitForFunction(()=>AppState.settings.whisper_model==='small'&&!document.querySelector('[data-model-select]').disabled);
    assert.equal(await model.inputValue(),'small');
    await model.selectOption('tiny');await waitForRequest(page,'save_settings',2);await releaseMock(page,'save_settings',{ok:false,code:'model_not_ready',error:'模型缓存已变化，请重新下载'});
    await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    assert.equal(await model.inputValue(),'small');assert.equal(await page.evaluate(()=>AppState.settings.whisper_model),'small');
    assert.deepEqual(await page.evaluate(()=>__configurationRequests.save_settings.calls),[[{whisper_model:'small'}],[{whisper_model:'tiny'}]]);
    assert.deepEqual(await page.evaluate(()=>__configurationToasts.map(item=>item.kind)),[undefined,'error']);
    await page.evaluate(()=>{__MOCK__.get_models=()=>({ok:true,models:[{name:'tiny',verified:'true'},{name:'small',verified:true},{name:'base',verified:true,ready:false,reason:'未下载'}],download:{name:'tiny'}});});
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=model]').click();await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    assert.deepEqual(await model.locator('option').evaluateAll(options=>options.filter(option=>!option.disabled).map(option=>option.value)),['small']);
    assert.equal(await model.inputValue(),'small');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('an old inventory cannot unlock a replacement selector and a read failure keeps a saved unknown model unavailable',async()=>{
  const page=await open('settings','ui',{settings:{whisper_model:'retired-model'}});try{
    await holdMock(page,'get_models');await page.locator('.sitem[data-section=model]').click();await waitForRequest(page,'get_models',1);
    await page.locator('.sitem[data-section=ui]').click();await page.locator('.sitem[data-section=model]').click();await waitForRequest(page,'get_models',2);
    const model=page.getByLabel('默认模型',{exact:true});
    await releaseMock(page,'get_models',{ok:true,models:[{name:'small',ready:true,verified:true}]});
    await page.evaluate(()=>new Promise(requestAnimationFrame));
    assert.equal(await model.isDisabled(),true);assert.equal(await model.inputValue(),'retired-model');
    await releaseMock(page,'get_models',{ok:false,error:'缓存目录暂时无法读取'});await page.getByRole('button',{name:'重试读取模型'}).waitFor();
    assert.equal(await model.isDisabled(),true);assert.equal(await model.locator('option[value=retired-model]').isDisabled(),true);
    assert.match(await model.locator('option[value=small]').textContent(),/检查失败/);
    await page.getByRole('button',{name:'重试读取模型'}).click();await waitForRequest(page,'get_models',3);
    await releaseMock(page,'get_models',{ok:true,models:[{name:'small',ready:true,verified:true}]});await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    assert.equal(await model.inputValue(),'retired-model');assert.equal(await model.locator('option[value=retired-model]').isDisabled(),true);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(name=>name==='save_settings').length),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('model progress keeps an in-flight model unavailable, closes choices on transport failure, and restores verified completion',async()=>{
  const page=await open('settings','ui',{settings:{whisper_model:'tiny'}});try{
    await page.evaluate(()=>{window.__inventoryStage=0;__MOCK__.get_models=()=>{if(__inventoryStage===1)throw new Error('模型状态暂时无法读取');return {ok:true,models:[{name:'tiny',installed:true,verified:true,ready:true},{name:'small',installed:true,verified:true,ready:true}],download:{name:__inventoryStage===0?'tiny':'',progress:.42}};};});
    await page.locator('.sitem[data-section=model]').click();await page.waitForFunction(()=>document.querySelector('[data-model-select]').dataset.modelAvailable==='true');
    const model=page.getByLabel('默认模型',{exact:true});
    assert.equal(await model.locator('option[value=tiny]').isDisabled(),true);assert.equal(await model.locator('option[value=small]').isDisabled(),false);
    assert.equal(await page.locator('.model-item[data-model=tiny] [data-state=active]').isDisabled(),true);
    await page.evaluate(()=>{__inventoryStage=1;});await page.waitForFunction(()=>document.querySelector('[data-model-select]').dataset.modelAvailable==='false');
    assert.equal(await model.isDisabled(),true);assert.match(await model.locator('option[value=small]').textContent(),/检查失败/);
    await page.evaluate(()=>{__inventoryStage=2;});await page.waitForFunction(()=>document.querySelector('.model-item[data-model=tiny] .set-label')?.textContent.includes('已就绪'));
    assert.equal(await model.isDisabled(),false);assert.equal(await model.locator('option[value=tiny]').isDisabled(),false);assert.equal(await model.inputValue(),'tiny');
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(name=>name==='save_settings').length),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('inventory polling stays single flight and cannot unlock a newer read failure in the same model library',async()=>{
  const page=await open('settings','ui',{settings:{whisper_model:'small'}});try{
    await page.clock.install();
    const inventory={ok:true,models:[{name:'tiny',installed:false,verified:false,ready:false},{name:'base',installed:false,verified:false,ready:false},{name:'small',installed:true,verified:true,ready:true}],download:{name:'tiny',progress:.42}};
    await page.evaluate(inventory=>{__MOCK__.get_models=()=>inventory;},inventory);
    await page.locator('.sitem[data-section=model]').click();await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    await holdMock(page,'get_models','download_model');await page.clock.fastForward(1200);await waitForRequest(page,'get_models',1);
    await page.clock.fastForward(2400);assert.equal(await page.evaluate(()=>__configurationRequests.get_models.calls.length),1,'slow polling never overlaps another polling request');
    await page.locator('#settingsPane [data-model=base] [data-state=idle]').click();await waitForRequest(page,'get_models',2);await waitForRequest(page,'download_model',1);
    await page.evaluate(()=>{const resolve=__configurationRequests.get_models.pending.splice(1,1)[0];resolve({ok:false,error:'当前库存读取失败'});});
    await page.getByRole('button',{name:'重试读取模型'}).waitFor();
    await releaseMock(page,'get_models',inventory);await page.clock.runFor(17);
    assert.equal(await page.getByLabel('默认模型',{exact:true}).isDisabled(),true,'a late previous poll cannot reopen choices after the newer read failed');
    assert.equal(await page.getByRole('button',{name:'重试读取模型'}).count(),1);
    await releaseMock(page,'download_model',{ok:false,error:'fixture 不下载'});await waitForRequest(page,'get_models',3);
    await releaseMock(page,'get_models',{...inventory,download:{name:''}});await page.waitForFunction(()=>!document.querySelector('[data-model-select]').disabled);
    assert.equal(await page.getByLabel('默认模型',{exact:true}).inputValue(),'small');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('the native themed dropdown keeps keyboard confirmation, Escape cancellation and editable text selection',async()=>{
  const page=await open('settings','ui');try{
    const theme=page.getByLabel('主题',{exact:true});
    await theme.focus();await page.keyboard.press('Space');await page.waitForFunction(()=>document.querySelector('select[aria-label="主题"]').matches(':open'));
    assert.equal(await theme.evaluate(node=>getComputedStyle(node).appearance),'base-select');
    assert.equal(await theme.evaluate(node=>getComputedStyle(node,'::picker-icon').rotate),'225deg','the picker arrow also respects reduced motion');
    await page.keyboard.press('ArrowDown');await page.keyboard.press('Escape');
    assert.equal(await theme.inputValue(),'dark');assert.equal(await page.evaluate(()=>AppState.settings.theme),'dark');
    assert.equal(await theme.evaluate(node=>node===document.activeElement),true);
    await page.keyboard.press('Space');await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
    await page.waitForFunction(()=>AppState.settings.theme==='paper'&&!document.querySelector('select[aria-label="主题"]').disabled);
    assert.equal(await theme.inputValue(),'paper');assert.equal(await theme.evaluate(node=>node===document.activeElement),true);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(name=>name==='save_settings').length),1);
    await page.locator('.sitem[data-section=agent]').click();await expandAgent(page);
    const input=page.getByLabel('模型',{exact:true});await input.fill('selected-text');await input.evaluate(node=>node.setSelectionRange(0,8));
    assert.deepEqual(await input.evaluate(node=>[node.selectionStart,node.selectionEnd]),[0,8]);
    const selection=await input.evaluate(node=>({color:getComputedStyle(node,'::selection').color,background:getComputedStyle(node,'::selection').backgroundColor,text:getComputedStyle(node).color}));
    assert.equal(selection.color,selection.text);assert.notEqual(selection.background,'rgba(0, 0, 0, 0)');
    await page.keyboard.insertText('edited');assert.equal(await input.inputValue(),'edited-text');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('native selected content contains long labels without extra tab stops or duplicate adaptation',async()=>{
  const page=await open('settings','ui',{theme:'mocha',scale:1.25},{width:560,height:500});try{
    const theme=page.getByLabel('主题',{exact:true});
    await page.waitForFunction(()=>document.querySelector('select[aria-label="主题"] selectedcontent')?.textContent.includes('Catppuccin Mocha'));
    for(const value of ['mocha','latte','rose-pine','tokyo-night']){
      if(await theme.inputValue()!==value){await theme.selectOption(value);await page.waitForFunction(value=>AppState.settings.theme===value&&!document.querySelector('select[aria-label="主题"]').disabled,value);}
      const dimensions=await theme.evaluate(node=>{const selected=node.querySelector('selectedcontent');return {width:node.clientWidth,scroll:node.scrollWidth,text:selected.textContent,option:node.selectedOptions[0].textContent,overflow:getComputedStyle(selected).textOverflow,triggerCount:node.querySelectorAll(':scope > button').length};});
      assert.equal(dimensions.scroll<=dimensions.width,true,`${value} stays inside its control`);
      assert.equal(dimensions.text,dimensions.option);assert.equal(dimensions.overflow,'ellipsis');assert.equal(dimensions.triggerCount,1);
      await theme.evaluate(node=>{NativeSelect.enhance(node);NativeSelect.enhance(node);});assert.equal(await theme.locator(':scope > button').count(),1);
    }
    await theme.focus();await page.keyboard.press('Tab');assert.equal(await theme.evaluate(node=>node.contains(document.activeElement)),false,'the native trigger adds no separate keyboard stop');
    await page.evaluate(()=>{const list=document.createElement('select'),sized=document.createElement('select');list.multiple=true;sized.size=1;list.id='multipleFixture';sized.id='sizeFixture';document.body.append(list,sized);});
    assert.equal(await page.locator('#multipleFixture > button,#sizeFixture > button').count(),0,'a supported browser still keeps explicit listbox controls untouched');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('native select adaptation leaves unsupported and listbox controls untouched',async()=>{
  const page=await browser.newPage();page.setDefaultTimeout(5000);try{
    await page.setContent('<select aria-label="fallback"><option value="one">One</option><option value="two" selected>Two</option></select><select multiple><option>Multiple</option></select><select size="1"><option>Size</option></select>');
    await page.evaluate(()=>{window.__supports=CSS.supports.bind(CSS);CSS.supports=(...args)=>args[0]==='appearance'&&args[1]==='base-select'?false:__supports(...args);});
    await page.addScriptTag({path:path.join(root,'app/webview_ui/js/native-select.js')});
    assert.equal(await page.locator('selectedcontent').count(),0);assert.equal(await page.getByLabel('fallback').inputValue(),'two');
    await page.getByLabel('fallback').selectOption('one');assert.equal(await page.getByLabel('fallback').inputValue(),'one');
  }finally{await page.close();}
});

test('a settings acknowledgement cannot restore an old account gate and changing connection trust locks access',async()=>{
  const page=await open('settings','ui',{settings:{account_login_token:'account-a',subscription_server_url:'https://fixture-a.invalid',subscription_license_public_key:'key-a'}});try{
    await holdMock(page,'save_settings');
    await page.evaluate(()=>{window.__saveFinished=false;Settings.save({theme:'paper'},false).finally(()=>{__saveFinished=true;});});await waitForRequest(page,'save_settings',1);
    const old=await page.evaluate(()=>__configurationRequests.save_settings.original({theme:'paper'}));
    await page.evaluate(()=>{AppState.settings.account_login_token='account-b';AppState.settings.account_access={required:true,allowed:false,expires_at:null,reason:'login_required'};AccountAccess.sync();});
    await releaseMock(page,'save_settings',old);await page.waitForFunction(()=>__saveFinished);
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'account-b');
    await page.evaluate(()=>{AppState.settings.account_access={required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified'};AccountAccess.sync();window.__saveFinished=false;Settings.save({subscription_server_url:'https://fixture-b.invalid'},false).finally(()=>{__saveFinished=true;});});await waitForRequest(page,'save_settings',2);
    const next=await page.evaluate(()=>__configurationRequests.save_settings.original({subscription_server_url:'https://fixture-b.invalid'}));
    next.account_access={required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified'};
    await releaseMock(page,'save_settings',next);await page.waitForFunction(()=>__saveFinished);
    assert.equal(await page.evaluate(()=>AppState.settings.subscription_server_url),'https://fixture-b.invalid');assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);
    const locked=page.locator('[data-page=workbench]');
    assert.equal(await locked.evaluate(node=>node.disabled),true,'content navigation is disabled after its account trust is invalidated');
    assert.equal(await locked.getAttribute('aria-disabled'),'true');
    assert.match(await locked.getAttribute('aria-description'),/请先登录/);
    assert.equal(await page.evaluate(()=>AppState.currentView),'settings','the current repair page remains accessible');
    await page.locator('[data-page=setup]').click();
    await page.waitForFunction(()=>AppState.currentView==='setup');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('startup group saves the ci runner wakeup toggle',async()=>{
  const page=await open('settings','startup',{settings:{ci_runner_wakeup:false}});try{
    const sw=page.getByRole('switch',{name:'启动时唤起工程 CI 服务'});
    assert.equal(await sw.getAttribute('aria-checked'),'false');
    const acknowledgements=await page.evaluate(()=>__configurationToasts.length);
    await sw.click();await page.waitForFunction((n)=>__configurationToasts.length===n+1,acknowledgements);
    await page.waitForFunction(()=>AppState.settings.ci_runner_wakeup===true);
    assert.equal(await sw.getAttribute('aria-checked'),'true');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('faq lives above subscription in the sidebar and renders its own page',async()=>{
  const page=await open('settings','ui');try{
    const labels=await page.locator('.nav-row .nav-label').allTextContents();
    assert.ok(!labels.includes('新手引导'),'guide hidden from the sidebar');
    const faqIdx=labels.indexOf('常见问题'),subIdx=labels.indexOf('订阅'),setIdx=labels.indexOf('设置');
    assert.ok(faqIdx>-1&&subIdx>-1&&setIdx>-1,'faq/subscription/settings present');
    assert.ok(faqIdx<subIdx&&subIdx<setIdx,'faq sits above subscription');
    await page.locator(".nav-row[data-page='faq']").click();
    const groups=await page.locator('#page-faq .set-group h3').allTextContents();
    assert.ok(groups.includes('下载与任务')&&groups.includes('付费与权益'),`faq page groups: ${groups.join(',')}`);
    const count=await page.locator('#page-faq .faq-item').count();
    assert.ok(count>=20,`expected >=20 faq items, got ${count}`);
    await page.locator('#page-faq .faq-item summary',{hasText:'下载失败了，需要马上做什么吗？'}).first().click();
    assert.match(await page.locator('#page-faq .faq-item[open] .faq-answer').first().textContent(),/不必惊慌/);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
