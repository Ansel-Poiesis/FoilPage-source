// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Launch routing uses isolated bootstrap fixtures; no process, model or provider calls.
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
async function open(options={}) {
  const page=await browser.newPage({viewport:{width:1280,height:800}});
  page.setDefaultTimeout(5000); page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,options);
  if(options.holdRestore)await page.addInitScript(installRestoreGates);
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[aria-current=page]');
  return page;
}

function installRestoreGates() {
  window.__restoreGates={template:structuredClone(__MOCK__.get_settings()),refresh:[],settings:[]};
  __MOCK__.refresh_subscription=()=>new Promise(resolve=>__restoreGates.refresh.push(resolve));
  __MOCK__.get_settings=()=>new Promise(resolve=>__restoreGates.settings.push(resolve));
}
async function releaseRestore(page,method,value) {
  await page.evaluate(({method,value})=>{
    const resolve=__restoreGates[method].shift();if(!resolve)throw new Error('Missing restore request: '+method);resolve(value);
  },{method,value});
}
async function restoredSettings(page,changes={}) {
  return page.evaluate(changes=>({...__restoreGates.template,...changes}),changes);
}

for (const launchMode of ['manual','background']) {
  test(`first entry cannot skip login through ${launchMode} launch or native navigation`,async()=>{
    const page=await open({accountAccess:false,launchMode,pendingNativeView:'workbench'});
    try {
      assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
      assert.equal(await page.locator('#setupPanel-login').isVisible(),true);
      assert.equal(await page.locator('#setupNextBtn').isDisabled(),true);
      assert.equal(await page.locator('#setupWorkbenchBtn').isHidden(),true);
      assert.equal(await page.locator('#setupTab-environment').isDisabled(),true);
      assert.equal(await page.locator('#setupTab-models').isDisabled(),true);
      for(const key of ['workbench','videos','library','browser','links','import']) {
        assert.equal(await page.locator(`[data-page=${key}]`).isDisabled(),true);
        await page.evaluate(key=>switchView(key),key);
        assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
      }
      assert.equal(await page.evaluate(()=>Setup.showStep('models')),false);
      await page.locator('#setupTab-login').focus();await page.keyboard.press('End');
      assert.equal(await page.locator('#setupTab-login').getAttribute('aria-selected'),'true');
      const calls=await page.evaluate(()=>__fixtureCalls);
      for(const action of ['get_setup_check','email_login_start','download_model','apply_setup_fixes'])assert(!calls.includes(action),action);
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  });
}

test('FAQ remains readable before login and after account revocation',async()=>{
  const page=await open({accountAccess:false});
  try {
    const faq=page.locator('[data-page=faq]');
    assert.equal(await faq.isDisabled(),false);
    await faq.click();
    assert.equal(await page.evaluate(()=>AppState.currentView),'faq');
    await page.locator('#page-faq summary').first().click();
    assert.equal(await page.locator('#page-faq details').first().getAttribute('open'),'');
    await page.evaluate(()=>AccountAccess.lock());
    assert.equal(await page.evaluate(()=>AppState.currentView),'faq');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('first login unlocks steps only after the bridge reports verified access',async()=>{
  const page=await open({accountAccess:false,paymentServer:'https://fixture.invalid'});
  try {
    await page.locator('#setupLoginBtn').click();await page.locator('#loginEmailInput').fill('entry@example.test');
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    await page.locator('#sendCodeBtn').click();await page.locator('#loginCodeInput').fill('123456');
    await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>AccountAccess.allowed());
    assert.equal(await page.locator('#setupNextBtn').isDisabled(),false);
    await page.locator('#setupNextBtn').click();assert.equal(await page.locator('#setupPanel-environment').isVisible(),true);
    await page.locator('[data-page=workbench]').click();assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='email_login_verify').length),1);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('a mere token or an expired cached access cannot unlock entry',async()=>{
  const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1});
  try {
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await page.locator('#setupLoginBtn').isVisible(),true);
    await page.evaluate(()=>switchView('library'));assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='email_login_start').length),0);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

for(const missing of ['address','key','both']) {
  test(`first login has a reachable service configuration entry when ${missing} is absent`,async()=>{
    const page=await open({accountAccess:false,settings:{subscription_server_url:missing==='key'?'https://fixture.invalid':'',subscription_license_public_key:missing==='address'?'public-fixture':''}});
    try {
      assert.equal(await page.locator('#setupAccountManageBtn').textContent(),'配置登录服务');
      assert.equal(await page.locator('#setupNextBtn').isDisabled(),true);
      assert.match(await page.locator('#setupAccountStatus').textContent(),/登录服务尚未配置/);
      await page.locator('#setupAccountManageBtn').click();
      assert.equal(await page.evaluate(()=>AppState.currentView),'subscription');
      assert.equal(await page.locator('#subscriptionTools').evaluate(node=>node.open),true);
      assert.equal(await page.locator('#subscriptionConnectionDetails').evaluate(node=>node.open),true);
      assert.equal(await page.getByLabel('服务地址',{exact:true}).isVisible(),true);
      assert.equal(await page.getByLabel('服务地址',{exact:true}).evaluate(node=>node===document.activeElement),true);
      assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}

test('an expired cached session renders centered recovery before the held refresh and only confirmed access unlocks it',async()=>{
  const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1,holdRestore:true,paymentServer:'https://fixture.invalid',settings:{subscription_license_public_key:'public-fixture'}});
  try {
    assert.equal(await page.locator('#setupAccountTitle').textContent(),'正在恢复登录');
    assert.equal(await page.locator('#setupAccountCard').getAttribute('aria-busy'),'true');assert.equal(await page.locator('#setupLoginBtn').isHidden(),true);
    assert.equal(await page.locator('#setupNextBtn').isDisabled(),true);assert.equal(await page.evaluate(()=>__restoreGates.refresh.length),1);
    const centered=await page.locator('#setupAccountTitle').evaluate(node=>{const title=node.getBoundingClientRect(),stage=document.getElementById('setupStage').getBoundingClientRect();return Math.abs(title.x+title.width/2-stage.x-stage.width/2)<10;});assert.equal(centered,true);
    assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));
    await releaseRestore(page,'refresh',{ok:true});await page.waitForFunction(()=>__restoreGates.settings.length===1);
    assert.equal(await page.locator('#setupAccountTitle').textContent(),'正在恢复登录','an accepted refresh without its signed settings has not completed restoration');
    const confirmed=await restoredSettings(page,{account_access:{required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:1},account_display_name:'恢复的账户'});
    await releaseRestore(page,'settings',confirmed);await page.waitForFunction(()=>!AppState.accountRestorePending&&AccountAccess.allowed());
    assert.equal(await page.locator('#setupAccountTitle').textContent(),'欢迎回来，恢复的账户'); // TASK-8.24 欢迎语带用户名assert.equal(await page.locator('#setupAccountStatus').textContent(),'恢复的账户');
    assert.equal(await page.locator('#setupAccountCard').getAttribute('aria-busy'),'false');assert.equal(await page.locator('#setupNextBtn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a revoked restore synchronizes the cleared session and returns to editable login without issuing a code',async()=>{
  const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1,holdRestore:true,paymentServer:'https://fixture.invalid',settings:{subscription_license_public_key:'public-fixture'}});
  try {
    await releaseRestore(page,'refresh',{ok:false,error:'登录已失效，请重新登录'});await page.waitForFunction(()=>__restoreGates.settings.length===1);
    const revoked=await restoredSettings(page,{account_login_token:'',account_user_id:'',account_display_name:'',account_avatar:'',account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:1}});
    await releaseRestore(page,'settings',revoked);await page.waitForFunction(()=>!AppState.accountRestorePending);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'');assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);
    assert.equal(await page.locator('#setupAccountTitle').textContent(),'登录 FoilPage');assert.equal(await page.locator('#setupLoginBtn').isVisible(),true);
    assert.match(await page.locator('#setupAccountStatus').textContent(),/登录已失效/);assert.equal(await page.locator('#setupNextBtn').isDisabled(),true);
    await page.locator('#setupLoginBtn').click();assert.equal(await page.locator('#loginEmailInput').isEditable(),true);
    assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a temporary restore failure retains cached login and its retry can resume without email verification',async()=>{
 const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1,holdRestore:true,paymentServer:'https://fixture.invalid',settings:{subscription_license_public_key:'public-fixture'}});
 try{
  await releaseRestore(page,'refresh',{ok:false,error:'暂时无法连接账户服务'});await page.waitForFunction(()=>__restoreGates.settings.length===1);
  const cached=await restoredSettings(page);await releaseRestore(page,'settings',cached);await page.waitForFunction(()=>!AppState.accountRestorePending);
  assert.equal(await page.locator('#setupRestoreBtn').isVisible(),true);assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');
  await page.locator('#setupRestoreBtn').click();await page.waitForFunction(()=>__restoreGates.refresh.length===1);await releaseRestore(page,'refresh',{ok:true});await page.waitForFunction(()=>__restoreGates.settings.length===1);
  await releaseRestore(page,'settings',{...cached,account_access:{required:true,allowed:true,expires_at:Date.now()/1000+86400,reason:'verified',revision:2}});
  await page.waitForFunction(()=>AccountAccess.allowed()&&!AppState.accountRestorePending);assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));assert.deepEqual(page.errors,[]);
 }finally{await page.close();}
});

test('a late restored settings snapshot cannot replace newer account pushes or unrelated edits',async()=>{
  const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1,holdRestore:true,launchMode:'background',paymentServer:'https://fixture.invalid'});
  try {
    await releaseRestore(page,'refresh',{ok:true});await page.waitForFunction(()=>__restoreGates.settings.length===1);
    const stale=await restoredSettings(page,{account_login_token:'old-snapshot',account_display_name:'旧账户',theme:'dark',account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:1}});
    await page.evaluate(()=>{AppState.settings.theme='paper';AppState.settings.account_display_name='较新的昵称';__onState({account_access:{required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:3}});});
    await releaseRestore(page,'settings',stale);await page.waitForFunction(()=>!AppState.accountRestorePending);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'较新的昵称');
    assert.equal(await page.evaluate(()=>AppState.settings.theme),'paper');assert.equal(await page.evaluate(()=>AppState.settings.account_access.revision),3);
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),true);assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('restore completion keeps a deliberate settings route and connection changes invalidate a late account response',async()=>{
  const page=await open({loggedIn:true,accountAccess:false,accountExpiry:1,holdRestore:true,paymentServer:'https://fixture.invalid',settings:{subscription_license_public_key:'public-fixture'}});
  try {
    await page.locator('[data-page=settings]').click();
    await releaseRestore(page,'refresh',{ok:true});await page.waitForFunction(()=>__restoreGates.settings.length===1);
    const stale=await restoredSettings(page,{account_access:{required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:4}});
    await page.evaluate(()=>{AppState.settings.subscription_server_url='https://new-fixture.invalid';AppState.settings.theme='paper';AccountAccess.lock();});
    await releaseRestore(page,'settings',stale);await page.waitForFunction(()=>!AppState.accountRestorePending);
    assert.equal(await page.evaluate(()=>AppState.currentView),'settings');assert.equal(await page.evaluate(()=>AppState.settings.subscription_server_url),'https://new-fixture.invalid');
    assert.equal(await page.evaluate(()=>AppState.settings.theme),'paper');assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('remote revocation locks the current page and stale pushes cannot undo a newer verified login',async()=>{
  const page=await open({loggedIn:true,launchMode:'background'});
  try {
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    await page.evaluate(()=>__onState({account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:2}}));
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');assert.equal(await page.locator('#setupNextBtn').isDisabled(),true);
    await page.evaluate(()=>__onState({account_access:{required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:1}}));
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);
    await page.evaluate(()=>__onState({account_access:{required:true,allowed:true,expires_at:Date.now()/1000+3600,reason:'verified',revision:3}}));
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),true);
    await page.locator('[data-page=workbench]').click();
    await page.evaluate(()=>__onState({account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:2}}));
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    await page.evaluate(()=>{AppState.settings.account_access.expires_at=Date.now()/1000-1;AccountAccess.sync();});
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('valid cached entry performs neither automatic email nor another license refresh',async()=>{
  const page=await open({loggedIn:true,launchMode:'background'});
  try {
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    const calls=await page.evaluate(()=>__fixtureCalls);
    assert(!calls.includes('email_login_start'));assert(!calls.includes('refresh_subscription'));
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

for (const firstRun of [false,true]) {
  test(`manual launch starts preparation with firstRun=${firstRun}`,async()=>{
    const page=await open({firstRun,loggedIn:true,launchMode:'manual'});
    try {
      assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
      assert.equal(await page.locator('#pageTitle').textContent(),'准备');
      await page.waitForSelector('#setupPanel-environment');
      assert.equal(await page.locator('#setupTab-environment').getAttribute('aria-selected'),'true');
      assert.equal(await page.locator('#setupPanel-login').isHidden(),true);
      assert.equal(await page.locator('#setupPanel-models').isHidden(),true);
      assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
      assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('download_model'),false);
      assert.equal(await page.locator('.ob-card').count(),0);
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  });
  test(`silent background launch skips preparation with firstRun=${firstRun}`,async()=>{
    const page=await open({firstRun,launchMode:'background'});
    try {
      assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
      await page.waitForSelector('.job');
      assert.equal(await page.locator('.ob-card').count(),0);
      assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('get_setup_check'),false);
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  });
}

test('preparation comes before tasks and the faq sits above subscription and settings',async()=>{
  const page=await open();
  try {
    const nav=await page.locator('.nav-row').evaluateAll(nodes=>nodes.map(node=>node.dataset.page));
    assert.deepEqual(nav.slice(0,4),['setup','workbench','videos','library']);
    // 先生 2026-10-19：新手引导移出导航；底部组＝常见问题→订阅→设置。
    assert.equal(nav.includes('guide'),false);
    assert.deepEqual(nav.slice(-3),['faq','subscription','settings']);
    assert.equal(await page.locator('.nav-row.pinned').getAttribute('data-page'),'faq');
    await page.locator('[data-page=subscription]').click();
    await page.waitForSelector('#page-subscription.active .plan-card');
    assert.equal(await page.locator('#pageTitle').textContent(),'使用与订阅');
    await page.locator('[data-page=settings]').click();
    assert.equal(await page.locator('.sitem[data-section=subscription]').count(),0);
    assert.equal(await page.locator('#page-settings .plan-card').count(),0);
    // Existing direct callers keep working without a duplicated settings section.
    await page.evaluate(()=>Settings.show('subscription'));
    assert.equal(await page.evaluate(()=>AppState.currentView),'subscription');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('state polling records launch sequence without resetting the current page',async()=>{
  const page=await open({launchMode:'background'});
  try {
    await page.locator('[data-page=library]').click();
    await page.evaluate(()=>__onState({launch_context:{mode:'manual',sequence:2},tasks:AppState.tasks}));
    assert.equal(await page.evaluate(()=>AppState.currentView),'library');
    assert.equal(await page.evaluate(()=>AppState.launchContext.sequence),2);
    // This is the explicit route used by the native manual-reactivation event.
    await page.evaluate(()=>switchView('setup'));
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    await page.locator('[data-page=subscription]').click();
    await page.evaluate(()=>__onState({launch_context:{mode:'manual',sequence:2}}));
    assert.equal(await page.evaluate(()=>AppState.currentView),'subscription');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

for(const view of ['setup','browser','unrecognized','guide']) {
  test(`queued native view ${view} is consumed safely after initial bootstrap`,async()=>{
    const page=await open({launchMode:'background',pendingNativeView:view});
    try {
      assert.equal(await page.evaluate(()=>window.__appReady),true);
      assert.equal(await page.evaluate(()=>window.__pendingNativeView),undefined);
      assert.equal(await page.evaluate(()=>AppState.currentView),['setup','browser'].includes(view)?view:'workbench');
      assert.equal(await page.locator('.ob-card').count(),0);
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}


test('configured verified account opens tasks on manual relaunch without discarding the local library',async()=>{
  const page=await open({loggedIn:true,settings:{setup_completed:true}});
  try {
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    assert.equal(await page.locator('.job').count(),9);
    assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));
    await page.locator('[data-page=setup]').click();
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});
