// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Rendered 1.0 UI contracts. Synthetic accounts; no providers or real files. */
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..'),shots=path.join(root,'.ui-audit','release-1','frontend');
const unlocked={ok:true,source:'signed_license',status:'unlocked',unlocked:true,allowed:true,days_left:15};
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},viewport={width:1280,height:800}){
  const page=await browser.newPage({viewport});page.errors=[];page.setDefaultTimeout(6000);page.on('pageerror',e=>page.errors.push(e.message));
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.invalid',loggedIn:true,...options});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.nav-row[aria-current=page]');return page;
}
test('preparation shows no trial advertising and subscription shows 500 credits unchanged',async()=>{
  const page=await open({firstRun:true,softwareAccess:unlocked});
  try{
    await page.locator('[data-page=setup]').click();await page.evaluate(()=>Setup.showStep('login'));
    assert.equal(await page.locator('#setupTrialOffer').count(),0,'TASK-8.29：试用横幅已移除');
    await page.screenshot({path:path.join(shots,'permanent-preparation.png')});
    await page.evaluate(()=>{const original=__MOCK__.get_subscription_status;__MOCK__.get_subscription_status=()=>({...original(),active:true,plan_id:'standard',credit_balance:500});});
    await page.locator('[data-page=subscription]').click();await page.waitForFunction(()=>document.querySelector('#accountSummary').textContent.includes('500'));
    assert.equal(await page.locator('#softwareAccessCard').count(),0,'TASK-8.29：使用权卡已移除');
    assert.equal((await page.evaluate(()=>__fixtureCalls)).includes('create_software_unlock_order'),false);
    await page.screenshot({path:path.join(shots,'permanent-and-500-credits.png')});assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('logged-out content navigation is truly disabled, while email OTP remains editable and consistent',async()=>{
  const page=await open({firstRun:true,loggedIn:false,accountAccess:false});
  try{
    for(const item of await page.locator('.nav-row').all())assert.equal(await item.isDisabled(),!['setup','faq','subscription','settings'].includes(await item.getAttribute('data-page')));
    await page.locator('#setupLoginBtn').click();await page.locator('#loginEmailInput').fill('fixture@example.com');
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'fixture@example.com');await page.screenshot({path:path.join(shots,'login-email.png')});
    await page.locator('#sendCodeBtn').click();await page.waitForSelector('#loginCodeInput:visible');
    await page.locator('#loginCodeInput').fill('654321');assert.equal(await page.locator('#loginCodeInput').inputValue(),'654321');
    assert.equal(await page.locator('.auth-progress').getAttribute('data-step'),'2');await page.screenshot({path:path.join(shots,'login-otp.png')});
    const width=await page.locator('#loginCodeInput').evaluate(el=>el.getBoundingClientRect().width);assert(width>=300,'all six digits have a full-width input');
    await page.locator('#authBackBtn').click();assert.equal(await page.locator('#loginEmailInput').inputValue(),'fixture@example.com');
    await page.locator('#accountAuthClose').click();assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('finishing preparation requires a confirmed settings save before navigating to tasks',async()=>{
  const page=await open({firstRun:true});
  try{
    await page.locator('[data-page=setup]').click();assert(await page.locator('#setupPanel-environment').isVisible());await page.evaluate(()=>Setup.showStep('models'));
    await page.evaluate(()=>{const original=__MOCK__.save_settings;window.__finishSave=original;__MOCK__.save_settings=()=>({ok:false});});
    await page.locator('#setupNextBtn').click();await page.waitForFunction(()=>!document.querySelector('#setupNextBtn').disabled);
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');assert.equal(await page.evaluate(()=>AppState.settings.setup_completed),false);
    await page.evaluate(()=>__MOCK__.save_settings=__finishSave);await page.locator('#setupNextBtn').click();
    await page.waitForFunction(()=>AppState.currentView==='workbench');assert.equal(await page.evaluate(()=>AppState.settings.setup_completed),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('background configuration persists and safe Agent activity does not render parameters or execute controls',async()=>{
  const page=await open({agentActivity:{ok:true,enabled:true,last_saved_at:'2026-10-09T06:00:00Z',last_revision:'a'.repeat(64),records:[{operation:'settings.update',at:'2026-10-09T06:00:00Z',ok:true,category:'settings',params:'secret-synthetic'}]}});
  try{
    await page.locator('[data-page=settings]').click();await page.getByLabel('背景',{exact:true}).selectOption('texture');
    await page.waitForFunction(()=>AppState.settings.background_style==='texture' && document.body.dataset.backgroundStyle==='texture');
    await page.locator('.sitem[data-section=control]').click();await page.waitForFunction(()=>document.querySelector('#agentActivityState').textContent==='Agent 模型已启用');
    assert(await page.locator('#agentLastSaved').textContent().then(v=>v!=='尚无保存记录'));
    await page.locator('#agentActivity summary').click();assert.match(await page.locator('#agentActivityRecords').textContent(),/settings.update/);
    assert.doesNotMatch(await page.locator('#settingsPane').textContent(),/secret-synthetic/);
    assert.equal((await page.evaluate(()=>__fixtureCalls)).includes('get_agent_control_config'),false);
    await page.screenshot({path:path.join(shots,'agent-activity.png')});
    await page.locator('.sitem[data-section=about]').click();assert(await page.getByText('@玩家川行出品',{exact:true}).isVisible());assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
for(const theme of ['dark','paper','nord-light'])test(`settings, model and account controls fit 560px in ${theme}`,async()=>{
  const page=await open({theme,scale:1.3,softwareAccess:unlocked},{width:560,height:700});
  try{
    await page.locator('[data-page=settings]').click();
    for(const section of ['ui','agent','control','model','data','about']){
      await page.locator(`.sitem[data-section=${section}]`).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,section+' has no horizontal viewport overflow');
      assert.equal(await page.locator('#settingsPane').evaluate(el=>el.scrollWidth>el.clientWidth+1),false,section+' controls fit the reading surface');
      if(section==='model')assert.equal(await page.locator('.path-text.is-empty').evaluate(el=>el.getBoundingClientRect().height<=52),true,'unset cache path cannot inherit the 240px full-page empty state');
      await page.screenshot({path:path.join(shots,theme+'-560-'+section+'.png')});
    }
    await page.locator('[data-page=subscription]').click();
    assert.equal(await page.locator('.plan-period').count(),0);assert.equal(await page.locator('.plan-annual').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(shots,theme+'-560-subscription.png')});assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
