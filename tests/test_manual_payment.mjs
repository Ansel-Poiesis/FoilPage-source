// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Isolated account/UI fixtures; no browser profiles, remote payment or emails. */
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';

const root=path.resolve(import.meta.dirname,'..');
const shots=path.join(root,'_webview_ui_shots','manual-payment');
const requestId='0123456789abcdef0123456789abcdef';
const transactionId='2026100712345678901234567890';
const catalog=['standard','advanced'].map((id,index)=>({id,status:'ready',purchasable:true,offers:[
  {period:'month',months:1,amount:index?'79.80':'19.80',credits:index?3000:500,currency:'CNY',status:'ready',purchasable:true},
  {period:'year',months:12,amount:index?'766.08':'190.08',credits:index?36000:6000,currency:'CNY',status:'ready',purchasable:true},
]}));
const capability={configured:true,enabled:true,provider:'alipay_personal',catalog};
const configuration={ok:true,...capability,recipient_label:'签约收款方',instructions:'完成支付后填写账单中的交易号。'};
const payment=(status='pending_payment',revision=1)=>({request_id:requestId,association_code:'SJ-A1B2C3',status,revision,
  snapshot:{plan_id:'standard',period:'month',months:1,amount:'19.80',credits:500,currency:'CNY'},
  created_at:new Date().toISOString(),expires_at:new Date(Date.now()+86400000).toISOString()});
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}) {
  const page=await browser.newPage({viewport:size});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.invalid',loggedIn:true,
    manualPayments:capability,manualPaymentConfig:configuration,...options});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{timeout:15000});
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('.nav-row[data-page=subscription]').click();await page.waitForSelector('.plan-card');return page;
}
async function dialog(page){await page.locator('#manualPaymentOpen').click();await page.waitForSelector('#manualPaymentBody');}
async function instrument(page,{state='pending_payment',delay=0,submitFail=false,refreshFail=false}={}) {
  await page.evaluate(({request,state,delay,submitFail,refreshFail})=>{
    window.__manualCalls=[];
    let current=request;
    const wait=()=>delay?new Promise(resolve=>setTimeout(resolve,delay)):Promise.resolve();
    window.__MOCK__.create_manual_payment_request=async(...args)=>{
      window.__manualCalls.push(['create',...args]);await wait();return {ok:true,request:{...current,status:state}};
    };
    window.__MOCK__.submit_manual_payment_request=async(...args)=>{
      window.__manualCalls.push(['submit',...args]);await wait();if(submitFail)return{ok:false,error:'该交易号已提交其他申请'};
      current={...current,status:'pending_review',revision:2};return{ok:true,request:current};
    };
    window.__MOCK__.get_manual_payment_request=async(...args)=>{
      window.__manualCalls.push(['status',...args]);await wait();return{ok:true,request:{...current,status:window.__manualNextStatus||current.status,revision:3}};
    };
    window.__MOCK__.cancel_manual_payment_request=async(...args)=>{
      window.__manualCalls.push(['cancel',...args]);current={...current,status:'cancelled',revision:4};return{ok:true,request:current};
    };
    const refresh=window.__MOCK__.refresh_subscription;
    window.__MOCK__.refresh_subscription=async()=>{
      window.__manualCalls.push(['refresh']);return refreshFail?{ok:false,error:'许可证验签未通过'}:refresh();
    };
  },{request:payment(),state,delay,submitFail,refreshFail});
}

test('manual payment entry is absent unless the service explicitly configures this independent provider',async()=>{
  for(const capability of [undefined,{}, {configured:'true',enabled:true,provider:'alipay_personal'},
    {configured:true,enabled:true,provider:'afdian'}]) {
    const page=await open({manualPayments:capability});
    try {assert.equal(await page.locator('#manualPaymentOpen').count(),0);assert.deepEqual(page.errors,[]);}
    finally{await page.close();}
  }
});

test('configured manual collection requires a verified login while Afdian can remain closed',async()=>{
  const page=await open({loggedIn:false,accountAccess:false});
  try{assert.equal(await page.locator('#manualPaymentOpen').isDisabled(),true);assert.equal(await page.locator('#manualPaymentBody').count(),0);}
  finally{await page.close();}
  const valid=await open({serviceBlocked:true,buyerRequired:true,buyerConfigured:false,paymentsEnabled:false});
  try{assert.equal(await valid.locator('#manualPaymentOpen').isEnabled(),true);await dialog(valid);
    assert.equal(await valid.locator('#manualPaymentOffer').isVisible(),true);
    assert.deepEqual(await valid.locator('.plan-comparison-core th[scope="row"]').allTextContents(),[
      '月付积分','转写并发','本地转写与自带密钥整理','FoilPage 托管正文与标题','视觉校对与批量整理（自带密钥）',
    ]);
    assert.deepEqual(valid.errors,[]);}
  finally{await valid.close();}
});

test('create binds the selected period and a stable opaque receipt; submission shows pending review without granting credits',async()=>{
  const page=await open();
  try{await instrument(page);await dialog(page);await page.locator('#manualPaymentOffer').selectOption('0');
    await page.locator('#manualPaymentCreate').click();await page.waitForSelector('#manualPaymentTransaction');
    assert.equal(await page.locator('#manualPaymentState').textContent(),'待支付');
    assert.equal(await page.locator('#manualPaymentCode').textContent(),'SJ-A1B2C3');
    await page.locator('#manualPaymentTransaction').fill(transactionId);await page.locator('#manualPaymentSubmit').click();
    await page.waitForFunction(()=>document.querySelector('#manualPaymentState')?.textContent==='待核验');
    const calls=await page.evaluate(()=>window.__manualCalls);
    assert.equal(calls[0][0],'create');assert.equal(calls[0][1],'standard');assert.equal(calls[0][2],'month');assert.match(calls[0][3],/^[A-Za-z0-9_-]{12,64}$/);
    assert.deepEqual(calls[1],['submit',requestId,transactionId]);assert.equal(calls.some(call=>call[0]==='refresh'),false);
    assert.equal(await page.locator('#manualPaymentTransaction').count(),0);assert.equal(await page.evaluate(()=>AppState.settings.credit_balance||0),0);
    assert.equal((await page.locator('#manualPaymentBody').textContent()).includes('已到账'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('changing the formal offer sends the real selected tier and year without inventing a local price',async()=>{
  const page=await open();
  try{await page.evaluate(request=>{window.__manualCreate=[];window.__MOCK__.create_manual_payment_request=(...args)=>{
    window.__manualCreate.push(args);return{ok:true,request:{...request,snapshot:{plan_id:'advanced',period:'year',months:12,amount:'766.08',credits:36000,currency:'CNY'}}};};},payment());
    await dialog(page);await page.locator('#manualPaymentOffer').selectOption('3');assert.match(await page.locator('#manualPaymentQuote').textContent(),/36,000/);
    await page.locator('#manualPaymentCreate').click();await page.waitForSelector('#manualPaymentTransaction');
    assert.deepEqual((await page.evaluate(()=>window.__manualCreate))[0].slice(0,2),['advanced','year']);
    assert.equal(await page.locator('.manual-payment-amount').textContent(),'¥766.08');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('lost create response retries the same idempotency id and a failed receipt submission preserves input',async()=>{
  const page=await open();
  try{await instrument(page,{submitFail:true});await page.evaluate(request=>{
    const real=window.__MOCK__.create_manual_payment_request;let count=0;
    window.__MOCK__.create_manual_payment_request=async(...args)=>{const result=await real(...args);if(++count===1)throw new Error('fixture lost response');return result;};
  },payment());
    await dialog(page);await page.locator('#manualPaymentCreate').click();await page.waitForFunction(()=>document.querySelector('#manualPaymentNotice')?.textContent.includes('重试'));
    await page.locator('#manualPaymentCreate').click();await page.waitForSelector('#manualPaymentTransaction');
    const creates=await page.evaluate(()=>window.__manualCalls.filter(call=>call[0]==='create'));assert.equal(creates.length,2);assert.equal(creates[0][3],creates[1][3]);
    await page.locator('#manualPaymentTransaction').fill(transactionId);await page.locator('#manualPaymentSubmit').click();
    await page.waitForFunction(()=>document.querySelector('#manualPaymentNotice')?.textContent.includes('其他申请'));
    assert.equal(await page.locator('#manualPaymentTransaction').inputValue(),transactionId);assert.equal(await page.locator('#manualPaymentState').textContent(),'待支付');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('invalid transaction identifiers stay local and never trigger receipt submission',async()=>{
  const page=await open({manualRequests:[payment()]});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentTransaction');
    for(const value of ['123','user@example.com','<script>alert(1)</script>','123456789012345a']) {
      await page.locator('#manualPaymentTransaction').fill(value);await page.locator('#manualPaymentSubmit').click();
      await page.waitForFunction(()=>!document.querySelector('#manualPaymentSubmit').disabled);
      assert.equal(await page.locator('#manualPaymentTransaction').inputValue(),value);
    }
    assert.equal((await page.evaluate(()=>window.__manualCalls)).some(call=>call[0]==='submit'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const refreshFail of [false,true])test(`only confirmed ledger plus verified refresh reports credits received: refresh failure ${refreshFail}`,async()=>{
  const page=await open({manualRequests:[payment('pending_review',2)]});
  try{await instrument(page,{state:'pending_review',refreshFail});await dialog(page);await page.waitForSelector('#manualPaymentRefresh');
    await page.evaluate(()=>{window.__manualNextStatus='credited';});await page.locator('#manualPaymentRefresh').click();
    await page.waitForFunction(()=>window.__manualCalls.some(call=>call[0]==='refresh'));
    await page.waitForFunction(()=>!document.querySelector('#manualPaymentRefresh').disabled);
    if(refreshFail){assert.equal(await page.locator('#manualPaymentState').textContent(),'已核验');assert.match(await page.locator('#manualPaymentNotice').textContent(),/验签/);}
    else{assert.equal(await page.locator('#manualPaymentState').textContent(),'已到账');assert.equal(await page.locator('#manualPaymentNotice').textContent(),'积分已到账');}
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('restoring a pending request does not recreate it; disabled new payments can still show account history',async()=>{
  const page=await open({manualPayments:{...capability,enabled:false},manualPaymentConfig:{...configuration,enabled:false},manualRequests:[payment('pending_review',2)]});
  try{await instrument(page);assert.equal(await page.locator('#manualPaymentOpen').textContent(),'查看申请');await dialog(page);
    await page.waitForSelector('#manualPaymentState');assert.equal(await page.locator('#manualPaymentState').textContent(),'待核验');
    assert.equal(await page.locator('#manualPaymentCreate').count(),0);assert.equal(await page.evaluate(()=>window.__manualCalls.length),0);
    assert.equal(await page.locator('#manualPaymentCancel').count(),0,'已提交核验后不能撤销锁定的交易号');
    assert.equal(await page.locator('#manualPaymentTransaction').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('unsubmitted requests can be explicitly cancelled without touching credits',async()=>{
  const page=await open({manualRequests:[payment()]});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentCancel');
    await page.locator('#manualPaymentCancel').click();await page.waitForFunction(()=>document.querySelector('#manualPaymentState')?.textContent==='已撤销');
    assert.equal(await page.locator('#manualPaymentTransaction').count(),0);assert.equal(await page.evaluate(()=>AppState.settings.credit_balance||0),0);
    assert.equal((await page.evaluate(()=>window.__manualCalls)).some(call=>call[0]==='refresh'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('expired payment windows allow only earlier-payment declaration and never show a payable QR',async()=>{
  const page=await open({manualRequests:[payment('expired',2)],manualPaymentConfig:{...configuration,qr_data_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8qkAAAAASUVORK5CYII='}});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentTransaction');
    assert.equal(await page.locator('#manualPaymentState').textContent(),'已过期');assert.equal(await page.locator('#manualPaymentQr').count(),0);
    assert.equal(await page.locator('#manualPaymentCancel').count(),0);assert.match(await page.locator('#manualPaymentBody').textContent(),/付款时限已过；此前已付款可提交核验/);
    await page.locator('#manualPaymentTransaction').fill(transactionId);await page.locator('#manualPaymentSubmit').click();
    await page.waitForFunction(()=>document.querySelector('#manualPaymentState')?.textContent==='待核验');
    assert.equal((await page.evaluate(()=>window.__manualCalls)).some(call=>call[0]==='refresh'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('older status revisions cannot confirm a newer pending receipt or change its locked quote',async()=>{
  const page=await open({manualRequests:[payment('pending_review',4)]});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentRefresh');
    await page.evaluate(()=>{window.__manualNextStatus='credited';});await page.locator('#manualPaymentRefresh').click();
    await page.waitForFunction(()=>!document.querySelector('#manualPaymentRefresh').disabled);
    assert.equal(await page.locator('#manualPaymentState').textContent(),'待核验');assert.equal((await page.evaluate(()=>window.__manualCalls)).some(call=>call[0]==='refresh'),false);
    await page.evaluate(request=>{window.__MOCK__.get_manual_payment_request=()=>({ok:true,request:{...request,status:'credited',revision:5,snapshot:{...request.snapshot,amount:'1.00'}}});},payment());
    await page.locator('#manualPaymentRefresh').click();await page.waitForFunction(()=>document.querySelector('#manualPaymentNotice')?.textContent.includes('报价已变更'));
    assert.equal(await page.locator('#manualPaymentState').textContent(),'待核验');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('automatic account expiry closes private payment details and disables reopening',async()=>{
  const page=await open({manualRequests:[payment()],accountExpiry:Date.now()/1000+7});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentTransaction');
    await page.waitForSelector('#manualPaymentBody',{state:'detached',timeout:9000});assert.equal(await page.locator('#manualPaymentOpen').isDisabled(),true);
    assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);assert.equal(await page.evaluate(()=>AppState.settings.credit_balance||0),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('the original payment deadline removes a live QR without granting rights and keeps late declaration available',async()=>{
  const request={...payment(),expires_at:new Date(Date.now()+5000).toISOString()};
  const page=await open({manualRequests:[request],manualPaymentConfig:{...configuration,qr_data_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8qkAAAAASUVORK5CYII='}});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentQr');assert.match(await page.locator('#manualPaymentDeadline').textContent(),/付款截止/);
    await page.waitForSelector('#manualPaymentQr',{state:'detached',timeout:7000});assert.equal(await page.locator('#manualPaymentState').textContent(),'已过期');
    assert.equal(await page.locator('#manualPaymentTransaction').isVisible(),true);assert.equal(await page.locator('#manualPaymentCancel').count(),0);
    assert.equal(await page.evaluate(()=>window.__manualCalls.length),0,'Local clock only hides stale payment UI, not ledger actions');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const change of ['close','logout','account','service'])test(`late create result cannot affect closed or switched account: ${change}`,async()=>{
  const page=await open();
  try{await instrument(page,{delay:150});await dialog(page);await page.locator('#manualPaymentCreate').click();
    if(change==='close')await page.locator('#dlgCancel').click();
    else await page.evaluate(change=>{
      if(change==='logout'){AppState.settings.account_login_token='';AccountAccess.lock();}
      else if(change==='account'){AppState.settings.account_login_token='other-session';AppState.settings.account_user_id='other-user';}
      else AppState.settings.subscription_server_url='https://other.example.invalid';
      Subscription.render();
    },change);
    await page.waitForTimeout(250);assert.equal(await page.locator('#manualPaymentBody').count(),0);
    assert.equal(await page.evaluate(()=>AppState.settings.credit_balance||0),0);assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('double submit uses one receipt call and closing during review does not show success later',async()=>{
  const page=await open({manualRequests:[payment()]});
  try{await instrument(page,{delay:150});await dialog(page);await page.waitForSelector('#manualPaymentTransaction');
    await page.locator('#manualPaymentTransaction').fill(transactionId);
    await page.evaluate(()=>{const button=document.querySelector('#manualPaymentSubmit');button.click();button.click();});
    await page.locator('#dlgCancel').click();await page.waitForTimeout(250);
    assert.equal((await page.evaluate(()=>window.__manualCalls.filter(call=>call[0]==='submit'))).length,1);
    assert.equal(await page.locator('#manualPaymentBody').count(),0);assert.equal(await page.locator('#toast').isVisible(),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const theme of ['dark','paper'])test(`payment dialog keeps keyboard access, inert images and narrow layout: ${theme}`,async()=>{
  const page=await open({theme,manualRequests:[payment()],manualPaymentConfig:{...configuration,qr_data_url:'https://external.example.invalid/private-qr.svg'}},{width:560,height:620});
  try{await instrument(page);await dialog(page);await page.waitForSelector('#manualPaymentTransaction');
    assert.equal(await page.locator('#manualPaymentQr').count(),0);assert.equal(await page.locator('.app').evaluate(node=>node.inert),true);
    const overflow=await page.locator('.manual-payment-dialog').evaluate(node=>node.scrollWidth>node.clientWidth);assert.equal(overflow,false);
    await page.screenshot({path:path.join(shots,theme+'-560.png')});
    await page.keyboard.press('Escape');assert.equal(await page.locator('#manualPaymentBody').count(),0);
    assert.equal(await page.locator('#manualPaymentOpen').evaluate(node=>node===document.activeElement),true);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
