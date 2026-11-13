// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// 准备页：环境自检、一键配置与模型档位下载索引（Playwright + mock 桥）。
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import installMock from "./ui_fixture.cjs";

const root = path.resolve(import.meta.dirname, "..");
const shots = path.join(root, "_webview_ui_shots", "setup");
let browser;
before(async () => { fs.mkdirSync(shots, {recursive:true}); browser = await chromium.launch({headless:true}); });
after(async () => { await browser?.close(); });

async function open(options = {}, size = {width:1280,height:800}) {
  const page = await browser.newPage({viewport:size});
  page.setDefaultTimeout(5000);
  page.errors = [];
  page.on("pageerror", e => page.errors.push(e.message));
  await page.addInitScript(installMock, options);
  if (options.holdSetupCheck) await page.addInitScript(installHeldRequest, {method:'get_setup_check',captureSnapshot:true});
  await page.goto(pathToFileURL(path.join(root, "app/webview_ui/index.html")).href, { timeout: 15000 });
  await page.waitForSelector(".nav-row[aria-current=page]");
  if (!options.firstRun && !options.wizard) await page.evaluate(()=>Setup.showStep('environment'));
  return page;
}

// Keep a real fixture response pending until the navigation assertions finish.
// Setup snapshots are captured at request time, so releasing an old one remains stale.
function installHeldRequest({method,captureSnapshot=false}) {
  const original=window.__MOCK__[method], state={requests:[]};
  window.__setupHeldRequests ||= {};
  window.__setupHeldRequests[method]=state;
  window.__MOCK__[method]=(...args)=>new Promise(resolve=>{
    const snapshot=captureSnapshot ? original(...args) : null;
    const request={args,pending:true,release:async()=>{
      if(!request.pending) throw new Error(`Fixture request already released: ${method}`);
      const result=await (captureSnapshot ? snapshot : original(...args));
      request.pending=false;resolve(result);
    }};
    state.requests.push(request);
  });
}
async function waitForHeldRequest(page,method,count) {
  await page.waitForFunction(({method,count})=>window.__setupHeldRequests[method].requests.length===count,{method,count});
}
async function releaseHeldRequest(page,method,index=0) {
  await page.evaluate(async({method,index})=>{
    const request=window.__setupHeldRequests[method].requests[index];
    if(!request) throw new Error(`No pending fixture request: ${method}`);
    await request.release();
    // Drain the released response's async handlers before checking ignored snapshots.
    await new Promise(resolve=>requestAnimationFrame(resolve));
  },{method,index});
}

test("first run lands on the setup page: env rows, tier index, guide entry", async () => {
  const page = await open({firstRun:true});
  try {
    await page.waitForSelector("#page-setup.active");
    await page.waitForSelector("#setupModelList .model-item",{state:"attached"});
    assert.equal(await page.locator('#setupPanel-login').isVisible(),true);
    assert.equal(await page.locator('#setupPanel-environment').isVisible(),false);
    await page.locator('#setupNextBtn').click();
    for (const label of ["GPU / CUDA", "显存", "磁盘剩余", "下载镜像", "缓存目录", "默认模型"]) {
      assert(await page.locator("#setupEnvRows .set-label").filter({hasText: label}).count() >= 1, "缺少自检行：" + label);
    }
    assert.equal(await page.locator("#setupModelList .model-item").count(), 2, "前置下载索引按档位列出模型");
    assert.equal(await page.locator(".tier-badge").filter({hasText: "均衡档"}).count(), 1);
    assert.equal(await page.locator(".verdict-badge.v-rec").filter({hasText: "本机推荐"}).count(), 1);
    assert.equal(await page.locator(".verdict-badge.v-ok").filter({hasText: "配置满足"}).count(), 2);
    assert.equal(await page.locator("#setupDownloadRec").count(), 1, "未安装推荐档位时提供一键下载");
    assert.equal(await page.locator("[data-tour='setup-default-model']").count(), 1);
    await page.evaluate(()=>Setup.showStep('models'));
    await page.screenshot({path:path.join(shots, "first-run.png")});
    await page.locator("#setupGuideBtn").click();
    await page.waitForSelector(".ob-card");
    await page.keyboard.press("Escape");
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("one-click setup applies bridge fixes; per-model download and default model save", async () => {
  const page = await open({pendingFixes:true,modelItems:[{name:'base',ready:true,verified:true,installed:true}]});
  try {
    await page.locator(".nav-row[data-page='setup']").click();
    await page.waitForSelector("#setupModelList .model-item",{state:"attached"});
    await page.locator("#setupApplyBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("apply_setup_fixes"));
    await page.evaluate(()=>Setup.showStep('models'));
    await page.locator("[data-tour='model-small'] button[data-state='idle']").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("download_model"));
    await page.locator("[data-tour='setup-default-model']").selectOption("base");
    await page.waitForFunction(() => window.__fixtureCalls.includes("save_settings"));
    const saved = await page.evaluate(() => {
      const calls = window.__fixtureCalls;
      return calls.filter(c => c === "save_settings").length;
    });
    assert(saved >= 1, "默认模型切换会保存设置");
    await page.waitForFunction(()=>AppState.settings.whisper_model === 'base');
    await page.waitForFunction(()=>document.querySelector('[data-tour=setup-default-model]').value === 'base');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('download directory picker saves the chosen path and current client state',async()=>{
  const page=await open({pickerDelay:250,pickDirectory:'C:\\fixture\\chosen'});
  try {
    assert.equal(await page.locator('#setupDownloadPath').textContent(),'C:\\fixture\\downloads');
    await page.locator('#setupDownloadDirBtn').click();
    await page.evaluate(()=>{AppState.settings.agent_enabled=false;});
    await page.waitForFunction(()=>document.querySelector('#setupDownloadPath').textContent==='C:\\fixture\\chosen');
    assert.equal(await page.evaluate(()=>AppState.settings.download_dir),'C:\\fixture\\chosen');
    assert.equal(await page.evaluate(()=>AppState.settings.agent_enabled),false,'picker response must not overwrite unrelated later edits');
    assert.equal(await page.locator('#setupDownloadDirBtn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const failure of ['cancel','fail']) {
  test(`download directory picker ${failure} keeps the current path`,async()=>{
    const page=await open(failure==='cancel'?{pickerCancel:true}:{pickerFail:true});
    try {
      await page.locator('#setupDownloadDirBtn').click();
      await page.waitForFunction(()=>window.__fixtureCalls.includes('pick_setting_dir'));
      await page.waitForFunction(()=>!document.querySelector('#setupDownloadDirBtn').disabled);
      assert.equal(await page.locator('#setupDownloadPath').textContent(),'C:\\fixture\\downloads');
      assert.equal(await page.evaluate(()=>AppState.settings.download_dir),'C:\\fixture\\downloads');
      if(failure==='cancel') assert.equal(await page.locator('#toast').isHidden(),true);
      else assert.equal(await page.locator('#toast').textContent(),'目录保存失败');
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}

test('directory picker is single-flight across departure and reentry and updates the current setup',async()=>{
  const page=await open();
  try {
    await page.evaluate(installHeldRequest,{method:'pick_setting_dir'});
    await page.locator('#setupDownloadDirBtn').click();
    await waitForHeldRequest(page,'pick_setting_dir',1);
    await page.locator('[data-page=subscription]').click();
    await page.locator('[data-page=setup]').click();
    assert.equal(await page.locator('#setupDownloadDirBtn').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('#setupDownloadDirBtn').onclick());
    assert.equal(await page.evaluate(()=>__setupHeldRequests.pick_setting_dir.requests.length),1);
    assert.deepEqual(await page.evaluate(()=>__setupHeldRequests.pick_setting_dir.requests[0].args),['download_dir']);
    assert.equal(await page.evaluate(()=>__setupHeldRequests.pick_setting_dir.requests[0].pending),true);
    await releaseHeldRequest(page,'pick_setting_dir');
    await page.waitForFunction(()=>document.querySelector('#setupDownloadPath').textContent==='C:\\fixture\\downloads-new');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='pick_setting_dir').length,1);
    assert.equal(await page.locator('#setupDownloadDirBtn').isDisabled(),false);
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('old setup refresh cannot block reentry or overwrite a newer response',async()=>{
  const page=await open({holdSetupCheck:true,setupReplies:[
    {value:{settings:{whisper_model:'tiny',download_dir:'C:\\fixture\\stale'}}},
    {value:{settings:{whisper_model:'small',download_dir:'C:\\fixture\\fresh'}}},
  ]});
  try {
    await waitForHeldRequest(page,'get_setup_check',1);
    await page.locator('[data-page=subscription]').click();
    await page.locator('[data-page=setup]').click();
    await waitForHeldRequest(page,'get_setup_check',2);
    await releaseHeldRequest(page,'get_setup_check',1);
    await page.waitForFunction(()=>document.querySelector('#setupDownloadPath').textContent==='C:\\fixture\\fresh');
    assert.equal(await page.evaluate(()=>__setupHeldRequests.get_setup_check.requests[0].pending),true);
    await releaseHeldRequest(page,'get_setup_check');
    assert.equal(await page.locator('#setupDownloadPath').textContent(),'C:\\fixture\\fresh');
    assert.equal(await page.locator('[data-tour=setup-default-model]').inputValue(),'small');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a directory change invalidates older environment snapshots in the same page',async()=>{
  const page=await open({holdSetupCheck:true,pickDirectory:'C:\\fixture\\current'});
  try {
    await waitForHeldRequest(page,'get_setup_check',1);
    await page.locator('#setupDownloadDirBtn').click();
    await page.waitForFunction(()=>document.querySelector('#setupDownloadPath').textContent==='C:\\fixture\\current');
    await waitForHeldRequest(page,'get_setup_check',2);
    await releaseHeldRequest(page,'get_setup_check',1);
    assert.equal(await page.evaluate(()=>__setupHeldRequests.get_setup_check.requests[0].pending),true);
    await releaseHeldRequest(page,'get_setup_check');
    assert.equal(await page.locator('#setupDownloadPath').textContent(),'C:\\fixture\\current');
    assert.equal(await page.evaluate(()=>AppState.settings.download_dir),'C:\\fixture\\current');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('pending model submission remains single-flight after setup reentry',async()=>{
  const page=await open();
  try {
    await page.evaluate(installHeldRequest,{method:'download_model'});
    await page.evaluate(()=>Setup.showStep('models'));
    await page.waitForSelector('#setupDownloadRec');
    await page.locator('#setupDownloadRec').click();
    await waitForHeldRequest(page,'download_model',1);
    await page.locator('[data-page=subscription]').click();
    await page.locator('[data-page=setup]').click();
    await page.waitForSelector('[data-tour=model-small] button[data-state=active]');
    assert.equal(await page.locator('#setupDownloadRec').count(),0);
    assert.equal(await page.evaluate(()=>__setupHeldRequests.download_model.requests.length),1);
    assert.deepEqual(await page.evaluate(()=>__setupHeldRequests.download_model.requests[0].args),['small']);
    assert.equal(await page.evaluate(()=>__setupHeldRequests.download_model.requests[0].pending),true);
    await releaseHeldRequest(page,'download_model');
    await page.waitForFunction(()=>document.querySelector('[data-tour=model-small] button').dataset.state==='idle');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='download_model').length,1);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test("installed recommendation shows ready state without duplicate download entry", async () => {
  const page = await open({installed:true});
  try {
    await page.locator(".nav-row[data-page='setup']").click();
    await page.evaluate(()=>Setup.showStep('models'));
    await page.waitForSelector(".model-item.is-recommended");
    assert.equal(await page.locator("#setupDownloadRec").count(), 0, "推荐档位已安装时不再显示一键下载");
    const small = page.locator(".model-item[data-model='small']");
    assert.equal(await small.locator("button.danger").count(), 1, "已安装档位提供删除入口");
    assert.equal(await small.locator(".set-hint").filter({hasText: "已安装"}).count(), 1);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('a previously verified cached login can continue without another account request',async()=>{
  const page=await open({wizard:true,loggedIn:true});try{
    assert.equal(await page.locator('#setupPanel-login').isVisible(),false);
    assert.equal(await page.locator('#setupPanel-environment').isVisible(),true);
    assert.equal(await page.locator('#setupPreviousBtn').isDisabled(),false);
    assert.equal(await page.locator('#setupPanel-environment').isVisible(),true);
    await page.locator('#setupWorkbenchBtn').click();
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    const calls=await page.evaluate(()=>__fixtureCalls);
    for(const action of ['check_subscription_service','email_login_start','email_login_verify','download_model','apply_setup_fixes'])assert(!calls.includes(action),action);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('steps support arrow/Home/End keyboard navigation and preserve the current page on reentry',async()=>{
  const page=await open({wizard:true});try{
    assert.equal(await page.locator('#setupWorkbenchBtn').isVisible(),true);
    await page.locator('#setupTab-login').focus();await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#setupTab-environment').getAttribute('aria-selected'),'true');
    assert.equal(await page.locator('#setupTab-environment').evaluate(node=>node===document.activeElement),true);
    assert.equal(await page.locator('#setupWorkbenchBtn').isVisible(),true);
    await page.keyboard.press('End');assert.equal(await page.locator('#setupPanel-models').isVisible(),true);
    assert.equal(await page.locator('#setupWorkbenchBtn').isHidden(),true);
    await page.locator('[data-page=workbench]').click();await page.locator('[data-page=setup]').click();
    assert.equal(await page.locator('#setupPanel-models').isVisible(),true);
    assert.equal(await page.locator('#setupWorkbenchBtn').isHidden(),true);
    await page.locator('#setupTab-models').focus();await page.keyboard.press('Home');
    assert.equal(await page.locator('#setupTab-login').getAttribute('aria-selected'),'true');
    assert.equal(await page.locator('.setup-step[aria-selected=true]').count(),1);
    await page.locator('#setupNextBtn').click();assert.equal(await page.locator('#setupPanel-environment').evaluate(node=>node===document.activeElement),true);
    assert.equal(await page.locator('#setupWorkbenchBtn').isVisible(),true);
    await page.locator('#setupNextBtn').click();assert.equal(await page.locator('#setupPanel-models').isVisible(),true);
    assert.equal(await page.locator('#setupWorkbenchBtn').isHidden(),true);
    assert.equal(await page.locator('#setupNextBtn').textContent(),'进入任务 →');
    await page.locator('#setupNextBtn').click();assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('cache directory writes only its acknowledged field and survives flipping and reentry while pending',async()=>{
  const page=await open();try{
    await page.waitForFunction(()=>!document.querySelector('#setupCacheDirBtn').disabled);
    await page.evaluate(()=>{
      window.__cachePickCount=0;
      __MOCK__.pick_setting_dir=key=>{__cachePickCount++;window.__cachePickKey=key;return new Promise(resolve=>window.__releaseCachePick=resolve);};
    });
    await page.locator('#setupCacheDirBtn').click();
    await page.locator('#setupTab-models').click();await page.locator('[data-page=workbench]').click();await page.locator('[data-page=setup]').click();
    assert.equal(await page.locator('#setupPanel-models').isVisible(),true);
    await page.locator('#setupTab-environment').click();assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),true);
    await page.evaluate(()=>{AppState.settings.agent_enabled=false;document.querySelector('#setupCacheDirBtn').onclick();});
    assert.equal(await page.evaluate(()=>__cachePickCount),1);assert.equal(await page.evaluate(()=>__cachePickKey),'model_dir');
    await page.evaluate(()=>{
      const check=__MOCK__.get_setup_check;
      __MOCK__.get_setup_check=async()=>{const data=await check();return {...data,cache_root:'C:\\fixture\\new-cache',settings:{...data.settings,model_dir:'C:\\fixture\\new-cache'}};};
      __releaseCachePick({ok:true,settings:{model_dir:'C:\\fixture\\new-cache',agent_enabled:true,download_dir:'C:\\stale'}});
    });
    await page.waitForFunction(()=>!document.querySelector('#setupCacheDirBtn').disabled);
    assert.equal(await page.locator('#setupCachePath').textContent(),'C:\\fixture\\new-cache');
    assert.equal(await page.evaluate(()=>AppState.settings.agent_enabled),false);
    assert.equal(await page.evaluate(()=>AppState.settings.download_dir),'C:\\fixture\\downloads');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const result of [{ok:false,cancelled:true},{ok:false,error:'缓存目录保存失败'}]){
  test(`cache picker ${result.cancelled?'cancel':'failure'} keeps the existing path and can retry`,async()=>{
    const page=await open();try{
      await page.waitForFunction(()=>!document.querySelector('#setupCacheDirBtn').disabled);
      const before=await page.locator('#setupCachePath').textContent();
      await page.evaluate(result=>{__MOCK__.pick_setting_dir=()=>result;},result);
      await page.locator('#setupCacheDirBtn').click();await page.waitForFunction(()=>!document.querySelector('#setupCacheDirBtn').disabled);
      assert.equal(await page.locator('#setupCachePath').textContent(),before);
      if(result.cancelled)assert.equal(await page.locator('#toast').isHidden(),true);
      else assert.equal(await page.locator('#toast').textContent(),result.error);
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}

test('model submission protects all downloads, installed deletions and cache changes across steps',async()=>{
  const page=await open({installed:true});try{
    await page.evaluate(()=>{const original=__MOCK__.download_model;__MOCK__.download_model=(...args)=>new Promise(resolve=>window.__releaseModel=()=>resolve(original(...args)));});
    await page.locator('#setupTab-models').click();await page.waitForSelector('[data-tour=model-tiny] button');
    await page.locator('[data-tour=model-tiny] button').click();
    assert.equal(await page.locator('[data-model=small] button.danger').isDisabled(),true);
    await page.locator('#setupTab-environment').click();assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),true);
    await page.locator('[data-page=workbench]').click();await page.locator('[data-page=setup]').click();
    assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),true);
    await page.locator('#setupTab-models').click();await page.evaluate(()=>document.querySelector('[data-tour=model-tiny] button').onclick?.());
    await page.evaluate(()=>__releaseModel());
    await page.waitForFunction(()=>!document.querySelector('[data-model=small] button.danger').disabled);
    assert.equal((await page.evaluate(()=>__fixtureCalls)).filter(call=>call==='download_model').length,1);
    assert.equal(await page.locator('[data-tour=model-tiny] button').getAttribute('data-state'),'idle');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('an unavailable environment exposes retry on environment and model pages without hiding the failure',async()=>{
  const page=await open({wizard:true,setupReplies:[{value:{ok:false,error:'环境暂时不可读取'}}]});try{
    await page.waitForFunction(()=>document.querySelector('#setupEnvState').textContent==='环境暂时不可读取');
    await page.locator('#setupTab-environment').click();assert.equal(await page.locator('#setupEnvRetryBtn').isVisible(),true);
    assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),true);
    await page.locator('#setupTab-models').click();assert.equal(await page.locator('#setupModelState').textContent(),'环境暂时不可读取');
    await page.locator('#setupModelRetryBtn').click();await page.waitForSelector('#setupModelList .model-item');
    assert.equal(await page.locator('#setupModelRetryBtn').isHidden(),true);
    assert.equal(await page.locator('#setupModelState').textContent(),'');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a download task using local ASR protects cache and model deletion; downloading alone does not',async()=>{
  const page=await open({installed:true});try{
    await page.waitForFunction(()=>!document.querySelector('#setupCacheDirBtn').disabled);
    await page.locator('#setupTab-models').click();await page.locator('[data-tour=setup-default-model]').focus();
    const before=await page.evaluate(()=>__fixtureCalls.filter(call=>['pick_setting_dir','delete_model','get_setup_check'].includes(call)).length);
    await page.evaluate(()=>__onState({tasks:[{kind:'download',status:'transcribing',task_id:'dl:test'}]}));
    assert.equal(await page.locator('[data-model=small] button.danger').isDisabled(),true);
    assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),true);
    await page.evaluate(()=>{document.querySelector('#setupCacheDirBtn').onclick();document.querySelector('[data-model=small] button.danger').onclick();});
    assert.equal(await page.locator('#modalRoot .modal').count(),0);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(call=>['pick_setting_dir','delete_model','get_setup_check'].includes(call)).length),before);
    assert.equal(await page.locator('#setupPanel-models').isVisible(),true);
    assert.equal(await page.locator('[data-tour=setup-default-model]').evaluate(node=>node===document.activeElement),true);
    await page.evaluate(()=>__onState({tasks:[{kind:'download',status:'downloading',task_id:'dl:test'}]}));
    assert.equal(await page.locator('#setupCacheDirBtn').isDisabled(),false);
    assert.equal(await page.locator('[data-model=small] button.danger').isDisabled(),false);
    assert.equal(await page.locator('[data-tour=setup-default-model]').isDisabled(),false);
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(call=>call==='get_setup_check').length),1,'push sync never performs another check');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('login delegates to the shared account flow and its callback never redirects a departed setup',async()=>{
  const page=await open({wizard:true});try{
    await page.evaluate(()=>{Subscription.openLogin=options=>{window.__setupLoginOptions=options;};});
    await page.locator('#setupLoginBtn').click();assert.equal(await page.evaluate(()=>__setupLoginOptions.trigger.id),'setupLoginBtn');
    assert.equal(await page.evaluate(()=>__setupLoginOptions.isCurrent()),true);
    await page.locator('#setupNextBtn').click();await page.locator('[data-page=workbench]').click();
    await page.evaluate(()=>{AppState.settings.account_login_token='fixture-account';AppState.settings.account_display_name='测试账户';__setupLoginOptions.onSuccess();});
    assert.equal(await page.evaluate(()=>__setupLoginOptions.isCurrent()),false);
    assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    await page.locator('[data-page=setup]').click();await page.locator('#setupTab-login').click();
    assert.match(await page.locator('#setupAccountTitle').textContent(),/测试账户/);
    assert.equal(await page.locator('#setupLoginBtn').isHidden(),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('preparation reuses the real shared modal flow and login success stays on the login step',async()=>{
  const page=await open({wizard:true,paymentServer:'https://fixture.invalid'});try{
    await page.locator('#setupLoginBtn').click();await page.locator('#loginEmailInput').fill('setup@example.test');
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    await page.locator('#sendCodeBtn').click();await page.locator('#loginCodeInput').waitFor({state:'visible'});
    await page.locator('#loginCodeInput').fill('123456');await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#setupAccountTitle').textContent.includes('fixture用户'));
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await page.locator('#setupPanel-login').isVisible(),true);
    assert.equal(await page.locator('#setupLoginBtn').isHidden(),true);
    assert.equal(await page.locator('#accountAuthModal').count(),0);
    assert.equal(await page.evaluate(()=>document.querySelector('.app').inert),false);
    const calls=await page.evaluate(()=>__fixtureCalls);
    assert.equal(calls.filter(call=>call==='email_login_start').length,1);
    assert.equal(calls.filter(call=>call==='email_login_verify').length,1);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a blocked shared login gives inline feedback and an account management route',async()=>{
  const page=await open({wizard:true});try{
    await page.evaluate(()=>{Subscription.openLogin=()=>false;});
    await page.locator('#setupLoginBtn').click();
    await page.evaluate(()=>__onState({tasks:AppState.tasks}));
    assert.match(await page.locator('#setupAccountStatus').textContent(),/暂不可用/);
    assert.equal(await page.locator('#setupAccountManageBtn').isVisible(),true);
    assert.equal(await page.locator('#accountAuthModal').count(),0);
    await page.locator('#setupAccountManageBtn').click();assert.equal(await page.evaluate(()=>AppState.currentView),'subscription');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('leaving preparation closes the login modal, clears the code and preserves the editable email',async()=>{
  const page=await open({wizard:true,paymentServer:'https://fixture.invalid'});try{
    await page.locator('#setupLoginBtn').click();await page.locator('#loginEmailInput').fill('setup@example.test');
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);await page.locator('#sendCodeBtn').click();
    await page.locator('#loginCodeInput').waitFor({state:'visible'});await page.locator('#loginCodeInput').fill('654321');
    // Programmatic native route changes can happen while a dialog has made the app inert.
    await page.evaluate(()=>Setup.showStep('environment'));
    assert.equal(await page.locator('#accountAuthModal').count(),0);assert.equal(await page.evaluate(()=>document.querySelector('.app').inert),false);
    await page.locator('#setupTab-login').click();await page.locator('#setupLoginBtn').click();
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'');
    assert.equal(await page.locator('#authEmailDestination').textContent(),'setup@example.test');
    await page.keyboard.press('Escape');assert.equal(await page.locator('#accountAuthModal').count(),0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('all three preparation pages retain visible navigation and fit the minimum scaled window',async()=>{
  const evidence=path.join(root,'.ui-audit','setup-wizard-review');fs.mkdirSync(evidence,{recursive:true});
  const metrics=[];
  for(const theme of ['dark','paper']){
    const page=await open({wizard:true,theme,scale:1.25,nativeChrome:true},{width:560,height:500});try{
      await page.emulateMedia({reducedMotion:'reduce'});
      for(const step of ['login','environment','models']){
        await page.locator('#setupTab-'+step).click();
        if(step==='models')await page.waitForSelector('#setupModelList .model-item');
        const metric=await page.evaluate(()=>{
          const rect=node=>{const r=node.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};};
          return {step:document.querySelector('.setup-step[aria-selected=true]').dataset.step,width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,footer:rect(document.querySelector('.setup-footer')),next:rect(document.querySelector('#setupNextBtn')),animation:getComputedStyle(document.querySelector('.setup-slide:not([hidden])')).animationName};
        });
        assert(!metric.overflow);assert(metric.footer.bottom<=500&&metric.next.right<=560&&metric.next.top>=0);assert.equal(metric.animation,'none');
        if(step==='login')assert.equal(await page.evaluate(()=>{const card=document.querySelector('#setupAccountCard').getBoundingClientRect(),stage=document.querySelector('#setupStage').getBoundingClientRect();return card.top>=stage.top&&card.bottom<=stage.bottom;}),true,'the complete login card fits above navigation');
        metrics.push({theme,...metric});await page.screenshot({path:path.join(evidence,`${theme}-${step}-560x500.png`)});
      }
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  }
  fs.writeFileSync(path.join(evidence,'metrics.json'),JSON.stringify(metrics,null,2)+'\n');
});

// TASK-8.24（先生 2026-10-25）：欢迎回来后接逗号与用户名；无名回落原文案。
test('welcome-back title greets the signed-in user by name', async () => {
  const p = await open({ loggedIn: true, displayName: '阿明' });
  try {
    await p.waitForFunction(() => document.getElementById('setupAccountTitle')?.textContent === '欢迎回来，阿明');
    const bare = await p.evaluate(() => { const s = window.AppState.settings; s.account_display_name = ''; window.Setup?.refreshAccount?.(); return document.getElementById('setupAccountTitle').textContent; });
    assert.equal(bare, '欢迎回来');
    assert.deepEqual(p.errors, []);
  } finally { await p.close(); }
});
test('web sign-in waits for the local browser without displaying a confirmation code',async()=>{
 const page=await open({loggedIn:false,accountAccess:false,wizard:true,paymentServer:'https://fixture.invalid',loginMethods:{email:true,web:true}});
 try{
  await page.locator('#setupWebLoginBtn').waitFor({state:'visible'});await page.locator('#setupWebLoginBtn').click();
  assert.equal(await page.locator('.desktop-login-confirm h2').textContent(),'正在通过浏览器登录…');
  assert.equal(await page.locator('.desktop-login-confirm strong').count(),0);
  await page.waitForFunction(()=>AccountAccess.allowed());assert.equal(await page.locator('.desktop-login-confirm').count(),0);
  assert(!await page.evaluate(()=>__fixtureCalls.includes('email_login_start')));assert.deepEqual(page.errors,[]);
 }finally{await page.close();}
});
test('closing desktop web authorization cancels the pending request and keeps the application locked',async()=>{
 const page=await open({loggedIn:false,accountAccess:false,wizard:true,paymentServer:'https://fixture.invalid',loginMethods:{email:true,web:true},webLoginPending:true});
 try{
  await page.locator('#setupWebLoginBtn').waitFor({state:'visible'});await page.locator('#setupWebLoginBtn').click();await page.locator('#cancelWebLogin').click();
  await page.waitForFunction(()=>__fixtureCalls.includes('web_login_cancel'));assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);assert.deepEqual(page.errors,[]);
 }finally{await page.close();}
});
