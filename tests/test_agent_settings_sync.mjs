// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Changed revisions use a real page and mocked local bridge, never a live profile. */
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..'),initial='a'.repeat(64),next='b'.repeat(64),latest='c'.repeat(64);
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});after(async()=>{await browser?.close();});
async function open(){const page=await browser.newPage({viewport:{width:1280,height:800}});page.setDefaultTimeout(5000);page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{loggedIn:true,paymentServer:'https://pay.example.invalid',settings:{agent_settings_revision:initial,agent_model:'old-model',title_skill_prompt:'old rules'}});
  await page.addInitScript(()=>{const original=__MOCK__.get_settings;window.__externalReads=[];window.__externalSnapshot=null;__MOCK__.get_settings=()=>{__externalReads.push('read');return __externalSnapshot?{...original(),...__externalSnapshot}:original();};});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);await page.waitForSelector('.nav-row[aria-current=page]');await page.locator('[data-page=settings]').click();await page.locator('.sitem[data-section=agent]').click();return page;}
async function push(page,revision,changes={}){await page.evaluate(({revision,changes})=>{__externalSnapshot={agent_settings_revision:revision,...changes};window.__onState({agent_settings_revision:revision});},{revision,changes});await page.waitForFunction(revision=>AppState.settings.agent_settings_revision===revision,revision);}
test('same or malformed revision does not read settings, while a changed revision updates clean controls once',async()=>{
  const page=await open();try{const baseline=await page.evaluate(()=>__externalReads.length);await page.evaluate(initial=>{__onState({agent_settings_revision:initial});__onState({agent_settings_revision:'x'});},initial);assert.equal(await page.evaluate(()=>__externalReads.length),baseline);
    await push(page,next,{agent_model:'agent-new-model'});assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'agent-new-model');
    await page.evaluate(next=>__onState({agent_settings_revision:next}),next);assert.equal(await page.evaluate(()=>__externalReads.length),baseline+1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('external prompt change preserves the focused local draft, cursor and DOM until explicit refresh',async()=>{
  const page=await open();try{await page.locator('textarea[aria-label="标题附加规则"]').evaluate(node=>node.closest('details').open=true);const field=page.getByRole('textbox',{name:'标题附加规则',exact:true});await field.fill('my unsaved title rules');await field.evaluate(node=>{node.setSelectionRange(3,7);window.__draftNode=node;});
    await push(page,next,{title_skill_prompt:'agent title rules',agent_model:'agent model'});
    assert.equal(await field.inputValue(),'my unsaved title rules');assert.deepEqual(await field.evaluate(node=>[node.selectionStart,node.selectionEnd]),[3,7]);assert(await field.evaluate(node=>node===document.activeElement&&node===__draftNode));
    assert.equal(await page.evaluate(()=>AppState.settings.title_skill_prompt),'agent title rules');assert(await page.locator('#externalSettingsNotice').isVisible());
    await page.getByRole('button',{name:'更新显示',exact:true}).click();assert.equal(await field.inputValue(),'my unsaved title rules');assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'agent model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a newer push rejects the earlier pending snapshot',async()=>{
  const page=await open();try{await page.evaluate(()=>{window.__heldSettings=[];__MOCK__.get_settings=()=>new Promise(resolve=>__heldSettings.push(resolve));});
    await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>__heldSettings.length===1);
    await page.evaluate(latest=>__onState({agent_settings_revision:latest}),latest);await page.waitForFunction(()=>__heldSettings.length===2);
    await page.evaluate(latest=>__heldSettings[1]({...AppState.settings,agent_settings_revision:latest,agent_model:'newest'}),latest);await page.waitForFunction(latest=>AppState.settings.agent_settings_revision===latest,latest);
    await page.evaluate(next=>__heldSettings[0]({...AppState.settings,agent_settings_revision:next,agent_model:'stale'}),next);await page.waitForTimeout(20);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'newest');assert.equal(await page.evaluate(()=>AppState.settings.agent_settings_revision),latest);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a settings response from a departed account cannot replace current settings',async()=>{
  const page=await open();try{await page.evaluate(()=>{__MOCK__.get_settings=()=>new Promise(resolve=>{window.__releaseOldAccountSettings=resolve;});});await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>typeof __releaseOldAccountSettings==='function');
    await page.evaluate(next=>{const old={...AppState.settings,agent_settings_revision:next,agent_model:'stale-account-model'};AppState.settings.account_login_token='new-account';__releaseOldAccountSettings(old);},next);await page.waitForTimeout(20);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'old-model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('local confirmed save begun after an external read keeps its newer value',async()=>{
  const page=await open();try{await page.evaluate(()=>{__MOCK__.get_settings=()=>new Promise(resolve=>{window.__releaseSettings=resolve;});});await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>typeof __releaseSettings==='function');
    await page.evaluate(async()=>{await Settings.save({agent_model:'local confirmed'},false);});
    await page.evaluate(next=>__releaseSettings({...AppState.settings,agent_settings_revision:next,agent_model:'older agent value',title_model:'agent title model'}),next);await page.waitForFunction(next=>AppState.settings.agent_settings_revision===next,next);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'local confirmed');assert.equal(await page.evaluate(()=>AppState.settings.title_model),'agent title model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('external settings cannot replace balances, signed licenses or account sessions',async()=>{
  const page=await open();try{await push(page,next,{agent_model:'new model',credit_balance:9999,subscription_active:true,license_token:'invented license',account_login_token:'invented account'});
    assert.notEqual(await page.evaluate(()=>AppState.settings.credit_balance),9999);assert.notEqual(await page.evaluate(()=>AppState.settings.license_token),'invented license');assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('an external connection change locks the former account instead of carrying old entitlement across services',async()=>{
  const page=await open();try{await push(page,next,{subscription_server_url:'https://new.example.invalid',account_login_token:'',account_user_id:'',license_token:''});
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('an off-page confirmed change is visible on entering the settings section without redirecting navigation',async()=>{
  const page=await open();try{await page.locator('[data-page=workbench]').click();await push(page,next,{agent_model:'background updated'});assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    await page.locator('[data-page=settings]').click();await page.locator('.sitem[data-section=agent]').click();assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'background updated');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a snapshot with a different revision is ignored and retried on the next push',async()=>{
  const page=await open();try{await page.evaluate(({next,latest})=>{__externalSnapshot={agent_settings_revision:latest,agent_model:'unmatched'};__onState({agent_settings_revision:next});},{next,latest});await page.waitForFunction(()=>__externalReads.length===1);await page.waitForTimeout(20);assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'old-model');
    await push(page,next,{agent_model:'matched'});assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'matched');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
