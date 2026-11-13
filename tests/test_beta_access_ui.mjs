// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Production DOM/CSS with synthetic Bridge responses; no native user windows,
   real cards, emails, payments, model calls or external requests. */
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..'), shots=path.join(root,'.ui-audit','beta-access','frontend');
const beta={version:1,active:true,unlimited_credits:true,expires_at:null,revoked:false};
const catalog=['standard','advanced'].map((id,i)=>({id,status:'ready',purchasable:true,offers:[
  {period:'month',months:1,amount:i?'49.80':'19.80',credits:i?2000:500,currency:'CNY',status:'ready',purchasable:true},
]}));
const capability={ok:true,configured:true,enabled:true,processing_enabled:true,provider:'alipay_page_pay',max_order_amount_minor:5000,catalog};
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}){
  const page=await browser.newPage({viewport:size,reducedMotion:'reduce'});page.setDefaultTimeout(6000);
  page.errors=[];page.on('pageerror',e=>page.errors.push(e.message));
  await page.route('https://**/*',route=>route.abort());
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.invalid',loggedIn:true,alipayPayments:capability,...options});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.nav-row[aria-current=page]');return page;
}
const subscription=page=>page.locator('[data-page=subscription]').click();
const standardButton=page=>page.locator('.plan-comparison thead [data-plan-id=standard] button');
async function hold(page,method){
  await page.evaluate(method=>{
    const original=__MOCK__[method];window.__held=[];
    __MOCK__[method]=(...args)=>new Promise(resolve=>__held.push(async()=>resolve(await original(...args))));
  },method);
}
async function release(page,index=0){await page.evaluate(async index=>{await __held[index]();await new Promise(requestAnimationFrame);},index);}
async function setStatus(page,value){await page.evaluate(value=>{
  const original=__MOCK__.get_subscription_status;__MOCK__.get_subscription_status=()=>({...original(),credit_balance:498,...value});
  const devices=__MOCK__.get_account_devices;__MOCK__.get_account_devices=async(...args)=>({...await devices(...args),credit_balance:498});
},value);}

test('startup programmatic focus cannot reveal skip navigation; trusted Tab can, then pointer and routing hide it',async()=>{
  const page=await open();try{
    await page.locator('.skip-link').focus();
    const top=()=>page.locator('.skip-link').evaluate(n=>n.getBoundingClientRect().bottom);
    assert(await top()<0,'autofocus stays offscreen even when browser calls it focus-visible');
    await page.evaluate(()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true})));
    assert(await top()<0,'untrusted synthetic event is not keyboard navigation');
    await page.keyboard.press('Tab');assert(await top()>0,'first real Tab reveals an auto-focused skip link');
    assert.equal(await page.locator('.skip-link').evaluate(n=>n===document.activeElement),true);
    await page.keyboard.press('Enter');assert(await top()<0,'link leaves focus after reaching content');
    await page.locator('.skip-link').focus();await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');assert(await top()>0);
    await page.locator('[data-page=settings]').click();await page.locator('.skip-link').focus();assert(await top()<0);
    await page.keyboard.press('Tab');assert(await top()>0);
    await page.evaluate(()=>switchView('subscription'));await page.locator('.skip-link').focus();assert(await top()<0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('unknown health and private account quote render connecting, never unavailable or purchasable',async()=>{
  const page=await open();try{
    await hold(page,'check_subscription_service');await subscription(page);
    await page.waitForFunction(()=>__held.length===1);
    assert.equal(await standardButton(page).textContent(),'正在连接…');assert(await standardButton(page).isDisabled());
    assert.match(await page.locator('#subscriptionServiceStatus').textContent(),/正在连接/);
    await release(page);await page.waitForFunction(()=>document.querySelector('.plan-comparison thead [data-plan-id=standard] button').textContent==='支付宝支付');
    assert(!await standardButton(page).isDisabled());
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='create_alipay_order').length),0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('confirmed same-service offer survives a held health and private quote refresh; a new trust key is loading',async()=>{
  const page=await open();try{
    await subscription(page);await page.getByRole('button',{name:'支付宝支付',exact:true}).first().waitFor();
    await page.locator('[data-page=workbench]').click();await hold(page,'check_subscription_service');await subscription(page);
    await page.waitForFunction(()=>__held.length===1);
    assert.equal(await standardButton(page).textContent(),'支付宝支付');assert(!await standardButton(page).isDisabled());
    assert.match(await page.locator('#subscriptionServiceStatus').textContent(),/正在更新/);
    await page.evaluate(()=>{const original=__MOCK__.get_alipay_payment_config;__MOCK__.get_alipay_payment_config=()=>new Promise(resolve=>{window.__quoteRelease=()=>resolve(original());});});
    await release(page);await page.waitForFunction(()=>typeof __quoteRelease==='function');
    assert.equal(await standardButton(page).textContent(),'支付宝支付');assert(!await standardButton(page).isDisabled());
    await page.evaluate(()=>__quoteRelease());
    await page.evaluate(()=>{AppState.settings.subscription_license_public_key='different-public-key';Subscription.render();});
    await page.waitForFunction(()=>__held.length===2);
    assert.equal(await standardButton(page).textContent(),'正在连接…');assert(await standardButton(page).isDisabled());
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('connection failure is a retryable connection state, and explicit sales downgrade closes a cached offer',async()=>{
  const page=await open();try{
    await subscription(page);await page.getByRole('button',{name:'支付宝支付',exact:true}).first().waitFor();
    await page.evaluate(()=>{__MOCK__.check_subscription_service=()=>Promise.reject(new Error('offline'));Subscription.render();});
    await page.waitForFunction(()=>document.querySelector('#subscriptionServiceStatus').dataset.state==='error');
    assert.equal(await standardButton(page).textContent(),'连接暂不可用');assert(await standardButton(page).isDisabled());
    assert.match(await page.locator('.plan-comparison thead [data-plan-id=standard] .plan-price').textContent(),/19.80/);
    await page.evaluate(()=>{
      __MOCK__.check_subscription_service=()=>({login_methods:{email:true},public_catalog:[],checkout_ready:false,available_plans:[]});
      __MOCK__.get_alipay_payment_config=()=>({ok:true,provider:'alipay_page_pay',configured:false,enabled:false,processing_enabled:false,catalog:[]});Subscription.render();
    });
    await page.waitForFunction(()=>document.querySelector('.plan-comparison thead [data-plan-id=standard] button').textContent==='暂未开放');
    assert(await standardButton(page).isDisabled());assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('late health for a replaced service cannot authorize the new connection',async()=>{
  const page=await open();try{
    await hold(page,'check_subscription_service');await subscription(page);await page.waitForFunction(()=>__held.length===1);
    await page.evaluate(()=>{AppState.settings.subscription_server_url='https://other.example.invalid';Subscription.render();});
    await page.waitForFunction(()=>__held.length===2);await release(page,0);
    assert.equal(await standardButton(page).textContent(),'正在连接…');assert(await standardButton(page).isDisabled());
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const [name,value,allowed] of [
  ['signed active',{beta_access:beta},true],
  ['top-level local flag',{unlimited_credits:true},false],
  ['revoked',{beta_access:{...beta,revoked:true}},false],
  ['expired',{beta_access:{...beta,expires_at:'2020-01-01T00:00:00Z'}},false],
  ['non-boolean claim',{beta_access:{...beta,active:'true'}},false],
  ['unversioned',{beta_access:{...beta,version:null}},false],
  ['paused order',{beta_access:beta,order_review:{paused:true}},false],
])test(`${name} beta state ${allowed?'shows':'cannot show'} infinity and retains numeric credits`,async()=>{
  const page=await open();try{
    await setStatus(page,value);await subscription(page);await page.waitForFunction(()=>document.querySelector('#accountSummary')?.textContent.includes('498') || document.querySelector('#accountSummary')?.textContent.includes('∞'));
    const text=await page.locator('#accountSummary').textContent();assert.equal(text.includes('∞'),allowed);
    if(allowed){if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();assert.equal(await page.locator('#betaRecordedBalance').textContent(),'原有余额 498 积分');}
    else assert.match(text,/498/);
    assert.equal(await page.evaluate(()=>AppState.settings.credit_balance ?? 498),498);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('card submission is single-flight, trims paste, clears secret, and confirms signed status without changing software or numeric balance',async()=>{
  const page=await open();try{
    await setStatus(page,{beta_access:{...beta,active:false,unlimited_credits:false}});await subscription(page);
    if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();
    await page.evaluate(beta=>{window.__cardCalls=[];__MOCK__.redeem_beta_card=code=>{__cardCalls.push(code);return new Promise(resolve=>{window.__cardReply=()=>{
      const original=__MOCK__.get_subscription_status;__MOCK__.get_subscription_status=()=>({...original(),beta_access:beta});resolve({ok:true});
    };});};},beta);
    await page.locator('#betaCardCode').fill('  sjbeta_'+'a'.repeat(64)+'  ');await page.locator('#redeemBetaCardBtn').click();
    await page.locator('#betaCardCode').press('Enter');assert.equal(await page.evaluate(()=>__cardCalls.length),1);
    assert(await page.locator('#redeemBetaCardBtn').isDisabled());assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));
    await page.evaluate(()=>__cardReply());await page.waitForFunction(()=>document.querySelector('#betaCardMessage').textContent==='内测权益已启用');
    assert.match(await page.locator('#accountSummary').textContent(),/∞/);assert.equal(await page.locator('#betaCardCode').inputValue(),'');
    assert.equal(await page.evaluate(()=>__cardCalls[0]),'sjbeta_'+'a'.repeat(64));
    assert.equal(await page.evaluate(()=>Object.values(AppState.settings).some(v=>typeof v==='string'&&v.startsWith('sjbeta_'))),false);
    assert.equal(await page.evaluate(()=>__fixtureCalls.includes('create_alipay_order')),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('signed beta expiry and a later verified revocation return to the numeric balance without replacing an input draft',async()=>{
  const page=await open();try{
    const now=Date.now();await page.clock.install({time:new Date(now)});
    await setStatus(page,{beta_access:{...beta,expires_at:new Date(now+2000).toISOString()}});await subscription(page);
    await page.waitForFunction(()=>document.querySelector('#accountSummary').textContent.includes('∞'));
    if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();await page.locator('#betaCardCode').fill('unfinished local input');
    await page.clock.runFor(2500);assert.match(await page.locator('#accountSummary').textContent(),/498/);
    assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));
    assert.equal(await page.locator('#betaCardCode').inputValue(),'unfinished local input');
    await setStatus(page,{beta_access:{...beta,active:false,unlimited_credits:false,revoked:true}});
    await page.evaluate(()=>__onState({account_access:{...AppState.settings.account_access,revision:AppState.settings.account_access.revision+1}}));
    await page.waitForFunction(()=>document.querySelector('#betaRecordedBalance').hidden);
    assert.match(await page.locator('#accountSummary').textContent(),/498/);assert.equal(await page.locator('#betaCardCode').inputValue(),'unfinished local input');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a failed or malformed card remains unredeemed and clears submitted secrets without echoing them',async()=>{
  const page=await open();try{
    await setStatus(page,{});await subscription(page);if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();
    await page.locator('#betaCardCode').fill('not a valid card');await page.locator('#redeemBetaCardBtn').click();
    assert.equal(await page.locator('#betaCardCode').getAttribute('aria-invalid'),'true');
    const card='sjbeta_'+'d'.repeat(64);await page.evaluate(card=>{__MOCK__.redeem_beta_card=()=>({ok:false,error:'卡密无效：'+card});},card);
    await page.locator('#betaCardCode').fill(card);await page.locator('#redeemBetaCardBtn').click();
    await page.waitForFunction(()=>document.querySelector('#betaCardMessage').textContent.includes('[卡密]'));
    assert.equal(await page.locator('#betaCardCode').inputValue(),'');assert(!await page.locator('#betaCardMessage').textContent().then(t=>t.includes(card)));
    assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('an old signed status response cannot restore infinity after a newer verified revocation',async()=>{
  const page=await open();try{
    await setStatus(page,{beta_access:beta});
    await page.evaluate(()=>{
      const original=__MOCK__.get_subscription_status;let first=true;window.__newBeta=null;
      __MOCK__.get_subscription_status=()=>{
        const snapshot={...original(),...(window.__newBeta?{beta_access:window.__newBeta}:{})};
        if(!first)return snapshot;first=false;return new Promise(resolve=>{window.__oldSignedStatus=()=>resolve(snapshot);});
      };
    });
    await subscription(page);await page.waitForFunction(()=>typeof __oldSignedStatus==='function');
    if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();await page.locator('#betaCardCode').fill('a local draft');
    await page.evaluate(beta=>{window.__newBeta={...beta,active:false,unlimited_credits:false,revoked:true};
      AccountAccess.apply({...AppState.settings.account_access,revision:AppState.settings.account_access.revision+1});
    },beta);
    await page.waitForFunction(()=>document.querySelector('#accountSummary').textContent.includes('498'));
    await page.evaluate(async()=>{__oldSignedStatus();await new Promise(requestAnimationFrame);});
    assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));assert.equal(await page.locator('#betaCardCode').inputValue(),'a local draft');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('raw card success cannot fabricate infinity, and a departed account cannot receive a late card result',async()=>{
  const page=await open();try{
    await setStatus(page,{});await subscription(page);if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();
    await page.evaluate(()=>{__MOCK__.redeem_beta_card=()=>({ok:true});});
    await page.locator('#betaCardCode').fill('sjbeta_'+'b'.repeat(64));await page.locator('#redeemBetaCardBtn').click();
    await page.waitForFunction(()=>document.querySelector('#betaCardMessage').textContent.includes('尚未同步'));
    assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));
    await page.evaluate(()=>{__MOCK__.redeem_beta_card=()=>new Promise(resolve=>{window.__lateCard=()=>resolve({ok:true});});});
    await page.locator('#betaCardCode').fill('sjbeta_'+'c'.repeat(64));await page.locator('#redeemBetaCardBtn').click();
    await page.locator('[data-page=workbench]').click();await page.evaluate(()=>{AppState.settings.account_login_token='another-account';});
    await subscription(page);if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();
    assert(await page.locator('#redeemBetaCardBtn').isDisabled(),'shared flight stays blocked until old request settles');
    await page.evaluate(()=>__lateCard());await page.waitForFunction(()=>!document.querySelector('#redeemBetaCardBtn').disabled);
    assert.equal(await page.locator('#betaCardCode').inputValue(),'');assert(!await page.locator('#accountSummary').textContent().then(t=>t.includes('∞')));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('browser wizard has a full-size illustration, stable explicit step selection, native actions only on their step, and accessible zoom',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{__MOCK__.browser_status=()=>({ok:true,running:true,configured:false,extension_dir:'C:\\Programs\\ShengJian\\browser-extension'});});
    await page.locator('[data-page=browser]').click();await page.waitForFunction(()=>document.querySelector('#browserStatus').textContent==='等待浏览器配对');
    assert.equal(await page.locator('#browserStepTitle').textContent(),'加载扩展');
    assert(await page.locator('#browserStepImage').evaluate(n=>n.getBoundingClientRect().width>=440));assert(!await page.locator('#pairBrowser').isVisible());
    await page.locator('#browserNext').click();assert.equal(await page.locator('#browserStepTitle').textContent(),'打开扩展');
    await page.locator('#browserGuideZoom').click();assert(await page.getByRole('dialog').isVisible());assert(await page.locator('.browser-pairing-large').evaluate(n=>n.getBoundingClientRect().width>=440));
    await page.keyboard.press('Escape');assert.equal(await page.locator('#browserGuideZoom').evaluate(n=>n===document.activeElement),true);
    await page.locator('#browserNext').click();assert(await page.locator('#pairBrowser').isVisible());assert(!await page.locator('#extensionDir').isVisible());
    await page.evaluate(()=>{__MOCK__.browser_status=()=>({ok:true,running:true,configured:true,extension_dir:'C:\\Programs\\ShengJian\\browser-extension'});});
    await page.locator('#checkBrowser').click();await page.waitForFunction(()=>document.querySelector('#browserStatus').textContent==='浏览器已配对');
    assert.equal(await page.locator('#pairBrowser').textContent(),'重新配对');
    await page.locator('.browser-install-steps button').nth(0).click();await page.locator('#checkBrowser').click();
    assert.equal(await page.locator('#browserStepTitle').textContent(),'加载扩展','status updates respect the chosen step');
    assert.equal(await page.evaluate(()=>__fixtureCalls.includes('begin_browser_pairing')),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('all ten themes keep installation, zoom and beta UI readable and bounded at a narrow 560px window',async()=>{
  for(const theme of ['dark','paper','dracula','tokyo-night','nord','mocha','gruvbox','rose-pine','latte','nord-light']){
    const page=await open({theme,scale:1.25,nativeChrome:true},{width:560,height:620});try{
      await setStatus(page,{beta_access:beta});await subscription(page);if (!await page.locator('#betaCardDetails').evaluate(n=>n.open)) await page.locator('#betaCardDetails summary').click();
      await page.locator('#betaCardCode').scrollIntoViewIfNeeded();
      assert(await page.locator('#betaCardCode').evaluate(n=>n.getBoundingClientRect().width>=180));
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(shots,`subscription-${theme}-560.png`)});
      await page.locator('[data-page=browser]').click();await page.locator('.browser-install-steps button').nth(0).click();
      await page.locator('#browserStepImage').waitFor({state:'visible'});await page.locator('#browserStepImage').evaluate(n=>n.decode());
      assert(await page.locator('#browserStepImage').evaluate(n=>n.getBoundingClientRect().width>=300));
      await page.screenshot({path:path.join(shots,`browser-${theme}-560.png`)});
      await page.locator('#browserGuideZoom').click();
      assert(await page.locator('.modal').evaluate(n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}));
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.keyboard.press('Escape');
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  }
});
