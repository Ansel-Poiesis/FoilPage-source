// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* No real accounts, browser profiles, emails, cashier or payments. */
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
const shots=path.join(root,'_webview_ui_shots','alipay-payment');
const id='0123456789abcdef0123456789abcdef';
const catalog=['standard','advanced'].map((id,i)=>({id,status:'ready',purchasable:true,offers:[
  {period:'month',months:1,amount:i?'79.80':'19.80',credits:i?3000:500,currency:'CNY',status:'ready',purchasable:true},
  {period:'year',months:12,amount:i?'766.08':'190.08',credits:i?36000:6000,currency:'CNY',status:'ready',purchasable:true},
]}));
const capability={configured:true,enabled:true,processing_enabled:true,provider:'alipay_page_pay',max_order_amount_minor:10000000000,catalog};
const order=(status='pending_payment',revision=1)=>({order_id:id,status,revision,
  created_at:new Date().toISOString(),updated_at:new Date().toISOString(),
  snapshot:{plan_id:'standard',period:'month',months:1,amount:'19.80',amount_minor:1980,credits:500,currency:'CNY'}});
let browser;
before(async()=>{fs.mkdirSync(shots,{recursive:true});browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},size={width:1280,height:800}) {
  const p=await browser.newPage({viewport:size});p.setDefaultTimeout(5000);p.errors=[];
  p.on('pageerror',e=>p.errors.push(e.message));
  await p.addInitScript(installMock,{paymentServer:'https://pay.example.invalid',loggedIn:true,alipayPayments:capability,...options});
  await p.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded',timeout:30000});
  await p.waitForSelector('.nav-row[aria-current=page]');await p.locator('.nav-row[data-page=subscription]').click();
  await p.waitForSelector('.plan-card');return p;
}
async function instrument(p,{requests=[],config=capability,delay=0,refreshFail=false,openFail=false,fastPoll=false}={}) {
  await p.evaluate(({requests,config,delay,refreshFail,openFail,fastPoll,id})=>{
    window.__alipayCalls=[];window.__alipayNext=null;window.__alipayNextRevision=2;
    const wait=()=>delay?new Promise(r=>setTimeout(r,delay)):Promise.resolve();
    let current=requests[0]||null;
    window.__MOCK__.get_alipay_payment_config=()=>({ok:true,...config});
    window.__MOCK__.list_alipay_orders=()=>({ok:true,orders:requests});
    window.__MOCK__.create_alipay_order=async(...args)=>{
      window.__alipayCalls.push(['create',...args]);await wait();
      const offer=config.catalog.find(q=>q.id===args[0]).offers.find(q=>q.period===args[1]);
      current={order_id:id,status:'pending_payment',revision:1,snapshot:{...offer,plan_id:args[0],amount_minor:Math.round(Number(offer.amount)*100)}};
      return{ok:true,order:current,cashier_available:true,cashier_expires_at:new Date(Date.now()+600000).toISOString()};
    };
    window.__MOCK__.get_alipay_order=async(...args)=>{
      window.__alipayCalls.push(['status',...args]);await wait();
      current={...current,status:window.__alipayNext||current.status,revision:window.__alipayNextRevision};
      return{ok:true,order:current,cashier_available:args[1]===true,cashier_expires_at:new Date(Date.now()+600000).toISOString()};
    };
    window.__MOCK__.query_alipay_order=async(...args)=>{
      window.__alipayCalls.push(['query',...args]);await wait();
      return{ok:true,order:{...current,status:window.__alipayNext||current.status,revision:window.__alipayNextRevision}};
    };
    window.__MOCK__.open_alipay_cashier=async(...args)=>{window.__alipayCalls.push(['open',...args]);await wait();return openFail?{ok:false,error:'无法打开浏览器'}:{ok:true};};
    const refresh=window.__MOCK__.refresh_subscription;
    window.__MOCK__.refresh_subscription=async()=>{window.__alipayCalls.push(['refresh']);await wait();return refreshFail?{ok:false,error:'许可证验签未通过'}:refresh();};
    window.open=()=>{throw new Error('UI must not directly open arbitrary cashier URLs');};
    if(fastPoll){const timer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===10000?80:ms,...args);}
  },{requests,config,delay,refreshFail,openFail,fastPoll,id});
}
async function dialog(p){await p.locator('#alipayPaymentOpen').click();await p.waitForSelector('#alipayPaymentBody');}
async function create(p){await p.locator('#alipayPaymentCreate').click();await p.waitForSelector('#alipayPaymentQuery');
  await p.waitForFunction(()=>!document.querySelector('#alipayPaymentQuery').disabled);}
const planCard = (p,index=1) => p.locator('.plan-card').nth(index);
const planAction = (p,index=1) => planCard(p,index).locator('button.btn');
async function cardDialog(p,index=1){await planAction(p,index).click();await p.waitForSelector('#alipayPaymentBody');}

test('payment entry stays hidden without explicit capabilities and a valid full login',async()=>{
  for(const options of [
    {alipayPayments:{}},{alipayPayments:{...capability,configured:'true',processing_enabled:false}},
    {alipayPayments:{...capability,provider:'alipay_personal'}},
    {alipayPayments:{...capability,enabled:false,processing_enabled:false}},
    {loggedIn:false,accountAccess:false},
  ]){const p=await open(options);try{assert.equal(await p.locator('#alipayPaymentOpen').count(),0);assert.deepEqual(p.errors,[]);}finally{await p.close();}}
});
test('authenticated account config enables a capped test purchase while public health stays closed',async()=>{
  const privateConfig={...capability,max_order_amount_minor:5000};
  const publicConfig={...privateConfig,enabled:false,catalog:[]};
  const p=await open({alipayPayments:publicConfig,alipayPaymentConfig:{ok:true,...privateConfig}});
  try{await p.waitForSelector('#alipayPaymentOpen');assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'支付宝支付');
    await instrument(p,{config:privateConfig});await dialog(p);
    assert.equal(await p.locator('#alipayPaymentOffer option').count(),1);
    assert.match(await p.locator('#alipayPaymentOffer').textContent(),/标准版.*月付.*19.80/);
    assert.equal((await p.locator('#alipayPaymentBody').textContent()).includes('u-fixture'),false);
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='create'||c[0]==='open'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('main cards share private Alipay quotes and never offer over-ceiling tiers or periods',async()=>{
  const privateConfig={...capability,max_order_amount_minor:5000};
  const p=await open({alipayPayments:{...privateConfig,enabled:false,catalog:[]},alipayPaymentConfig:{ok:true,...privateConfig},publicCatalog:[],serviceBlocked:true});
  try{await p.waitForSelector('#alipayPaymentOpen');
    assert.equal(await planAction(p).textContent(),'支付宝支付');
    assert.equal(await planAction(p).isEnabled(),true);
    assert.equal(await planCard(p).locator('.plan-price').textContent(),'¥19.80/月');
    assert.equal(await p.locator('#planPeriod-standard').count(),0);
    assert.equal(await planAction(p,2).isEnabled(),false);
    await instrument(p,{config:privateConfig});await cardDialog(p);
    await p.waitForSelector('#alipayPaymentCreate');
    assert.match(await p.locator('#alipayPaymentOffer option:checked').textContent(),/标准版.*月付.*19.80/);
    assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('merchant-blocked prices remain visible while only the in-limit standard quote is payable',async()=>{
  const limited={...capability,max_order_amount_minor:5000,catalog:catalog.map(p=>({...p,
    status:p.id==='standard'?'ready':'unavailable',purchasable:p.id==='standard',
    offers:p.offers.map(q=>({...q,status:Number(q.amount)>50?'merchant_limit':'ready',purchasable:Number(q.amount)<=50}))}))};
  const p=await open({alipayPaymentConfig:{ok:true,...limited},publicCatalog:[],serviceBlocked:true});
  try{
    await p.waitForFunction(()=>document.querySelectorAll('.plan-card')[2]?.textContent.includes('当前支付宝单笔收款上限 ¥50.00'));
    assert.equal(await planCard(p).locator('.plan-price').textContent(),'¥19.80/月');
    assert.equal(await planCard(p).locator('.plan-annual').count(),0);
    assert.equal(await p.locator('#planPeriod-standard').count(),0);
    assert.equal(await p.locator('.plan-period').count(),0);
    assert.equal(await planAction(p).isEnabled(),true,'Afdian checkout_ready=false cannot block the private Alipay quote');
    assert.equal(await planCard(p,2).locator('.plan-price').textContent(),'¥79.80/月');
    assert.equal(await planCard(p,2).locator('.plan-annual').count(),0);
    assert.equal(await planAction(p,2).isEnabled(),false);
    assert.match(await planAction(p,2).getAttribute('title'),/单笔收款上限 ¥50.00/);
    assert.equal(await p.getByRole('rowheader',{name:'年付积分',exact:true}).count(),0);
    await instrument(p,{config:limited});await cardDialog(p);assert.equal(await p.locator('#alipayPaymentOffer option').count(),1);
    assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

test('display uses a validated blocked server amount rather than a stale client plan price',async()=>{
  const limited={...capability,max_order_amount_minor:5000,catalog:[{...catalog[1],status:'unavailable',purchasable:false,
    offers:[{...catalog[1].offers[0],amount:'99.80',credits:4200,status:'merchant_limit',purchasable:false}]}]};
  const p=await open({alipayPaymentConfig:{ok:true,...limited},publicCatalog:[],serviceBlocked:true});
  try{
    await p.waitForFunction(()=>document.querySelectorAll('.plan-price')[2]?.textContent==='¥99.80/月');
    assert.match(await planCard(p,2).textContent(),/4,200 积分/);assert.equal(await planAction(p,2).isEnabled(),false);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

test('display-only merchant quotes reject invalid or inconsistent fields and never become payment offers',async()=>{
  const p=await open();try{
    const bad=[{amount:'NaN'},{amount:'79.801'},{amount:'0.00'},{currency:'USD'},{credits:0},{credits:'3000'},
      {credits:true},{months:true},{months:12},{purchasable:true},{status:'missing_price'},{amount:'49.80'},
      {amount:'100000000.01'}];
    const results=await p.evaluate(({bad,capability,catalog})=>bad.map(patch=>{
      const q={...catalog[1].offers[0],status:'merchant_limit',purchasable:false,...patch};
      const c={...capability,max_order_amount_minor:5000,catalog:[{...catalog[1],status:'unavailable',purchasable:false,offers:[q]}]};
      return {display:AlipayPayments.displayOffer(c,'advanced','month'),payment:AlipayPayments.offer(c,'advanced','month')};
    }),{bad,capability,catalog});
    assert.deepEqual(results,bad.map(()=>({display:null,payment:null})));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});

test('monthly professional Alipay takes precedence over independently-ready Afdian',async()=>{
  const p=await open({serviceReady:true,publicCatalog:catalog});try{await p.waitForSelector('#alipayPaymentOpen');await instrument(p);
    await p.evaluate(()=>{window.__afdianStarts=0;__MOCK__.begin_subscription=()=>{__afdianStarts++;return {ok:false};};});
    assert.equal(await p.locator('#planPeriod-advanced').count(),0);await cardDialog(p,2);
    await p.waitForSelector('#alipayPaymentCreate');
    assert.match(await p.locator('#alipayPaymentOffer option:checked').textContent(),/专业版.*月付.*79.80/);
    assert.equal(await p.evaluate(()=>__afdianStarts),0);assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('logout immediately disables the Alipay card before a held logout reply resolves',async()=>{
  const p=await open();try{await p.waitForSelector('#alipayPaymentOpen');
    await p.evaluate(()=>{__MOCK__.account_logout=()=>new Promise(resolve=>{window.__releaseCardLogout=()=>resolve({ok:false,error:'fixture retry'});});});
    await p.locator('#accountLogoutBtn').click();await p.waitForFunction(()=>typeof __releaseCardLogout==='function');
    assert.equal(await planAction(p).isEnabled(),false);
    await p.evaluate(()=>__releaseCardLogout());assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const state of ['waiting','credited','unrelated'])test(`card intention finds the correct quote without creating an order: ${state}`,async()=>{
  const p=await open();try{
    const existing=state==='unrelated'?{...order(),snapshot:{...order().snapshot,plan_id:'advanced',amount:'79.80',amount_minor:7980,credits:3000}}:order(state);
    await instrument(p,{requests:[existing]});await cardDialog(p);await p.waitForFunction(()=>!document.querySelector('#alipayPaymentNotice')?.textContent.includes('正在读取'));
    if(state==='waiting'){
      assert.equal(await p.locator('#alipayPaymentHistory').inputValue(),id);
      assert.equal(await p.locator('#alipayPaymentResume').count(),1);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    }else{
      assert.equal(await p.locator('#alipayPaymentCreate').count(),1);assert.equal(await p.locator('#alipayPaymentState').count(),0);
      assert.match(await p.locator('#alipayPaymentOffer option:checked').textContent(),/标准版.*月付.*19.80/);
    }
    assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const change of ['unavailable','price','credits','denied','pending_price'])test(`fresh card quote ${change} never redirects intention or creates a payment`,async()=>{
  const p=await open();try{
    let config={...capability},requests=[];
    if(change==='unavailable')config={...config,catalog:[catalog[1]]};
    if(change==='denied')config={...config,enabled:false,catalog:[]};
    if(change==='price'||change==='credits')config={...config,catalog:[{...catalog[0],offers:catalog[0].offers.map(o=>o.period==='month'?{...o,...(change==='price'?{amount:'21.80'}:{credits:501})}:o)},catalog[1]]};
    if(change==='pending_price')requests=[{...order(),snapshot:{...order().snapshot,amount:'18.80',amount_minor:1880}}];
    await instrument(p,{config,requests});await cardDialog(p);
    await p.waitForFunction(()=>document.querySelector('#alipayPaymentNotice')?.classList.contains('is-error'));
    assert.equal(await p.locator('#alipayPaymentCreate').count(),0);assert.equal(await p.locator('#alipayPaymentResume').count(),0);
    assert.equal(await p.locator('#alipayPaymentOffer').count(),0);assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);
    if(change==='unavailable'||change==='denied')assert.equal(await planAction(p).isEnabled(),false);
    assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('fresh denied private reply disables main card and does not inherit the public payment capability',async()=>{
  const p=await open();try{await instrument(p);await p.evaluate(()=>{__MOCK__.get_alipay_payment_config=()=>({ok:false,error:'账户支付暂不可用'});});
    await cardDialog(p);await p.waitForFunction(()=>document.querySelector('#alipayPaymentNotice')?.classList.contains('is-error'));
    assert.equal(await planAction(p).isEnabled(),false);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('a superseded card config/list pair cannot reopen purchase cards after a newer history-only reply',async()=>{
  const p=await open();try{await instrument(p);await p.waitForSelector('#alipayPaymentOpen');
    await p.evaluate(config=>{
      let configCalls=0,listCalls=0;
      __MOCK__.get_alipay_payment_config=()=>++configCalls===1?new Promise(resolve=>{window.__releaseOldCardConfig=()=>resolve({ok:true,...config});}):({ok:true,...config,enabled:false,catalog:[]});
      __MOCK__.list_alipay_orders=()=>++listCalls===1?new Promise(resolve=>{window.__releaseOldCardOrders=()=>resolve({ok:true,orders:[]});}):({ok:true,orders:[]});
    },capability);
    await planAction(p).click();await p.waitForFunction(()=>typeof __releaseOldCardOrders==='function');
    await p.keyboard.press('Escape');await p.locator('#alipayPaymentOpen').click();await p.waitForFunction(()=>document.querySelector('#alipayPaymentNotice')?.textContent==='购买暂未开放');
    await p.evaluate(async()=>{__releaseOldCardConfig();await new Promise(requestAnimationFrame);__releaseOldCardOrders();await new Promise(requestAnimationFrame);});
    assert.equal(await planAction(p).isEnabled(),false);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    assert.deepEqual(await p.evaluate(()=>__alipayCalls),[]);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('public purchase capability cannot replace a failed or denied private account config',async()=>{
  for(const config of [{ok:false,error:'fixture denial'},{ok:true,...capability,enabled:false,processing_enabled:false}]){
    const p=await open({alipayPaymentConfig:config});try{
      await p.evaluate(()=>new Promise(requestAnimationFrame));
      assert.equal(await p.locator('#alipayPaymentOpen').count(),0);assert.deepEqual(p.errors,[]);
    }finally{await p.close();}
  }
});
test('private enabled flag without a unique valid in-limit quote only shows historical orders',async()=>{
  for(const config of [{...capability,catalog:[]},{...capability,max_order_amount_minor:1000},
    {...capability,catalog:[{...catalog[0],offers:[catalog[0].offers[0],catalog[0].offers[0]]}]}]){
    const p=await open({alipayPaymentConfig:{ok:true,...config}});try{
      await p.waitForSelector('#alipayPaymentOpen');assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'查看订单');
      assert.deepEqual(p.errors,[]);
    }finally{await p.close();}
  }
});
for(const change of ['close','logout','session','user','service','newer','session_revision'])test(`private purchase config late reply is ignored after ${change}`,async()=>{
  const p=await open();try{await p.waitForSelector('#alipayPaymentOpen');
    await p.evaluate(config=>{
      __MOCK__.get_alipay_payment_config=()=>new Promise(resolve=>{window.__releaseAlipayConfig=()=>resolve({ok:true,...config});});
      Subscription.render();
    },capability);
    await p.waitForFunction(()=>typeof __releaseAlipayConfig==='function');
    assert.equal(await p.locator('#alipayPaymentOpen').count(),1,'same-account confirmed capability stays visible during refresh');
    if(change==='close')await p.locator('.nav-row[data-page=workbench]').click();
    else await p.evaluate(change=>{
      __MOCK__.get_alipay_payment_config=()=>({ok:true,configured:false,enabled:false,processing_enabled:false,provider:'alipay_page_pay',catalog:[]});
      if(change==='logout'){AppState.settings.account_login_token='';AppState.settings.account_user_id='';AccountAccess.lock();}
      if(change==='session')AppState.settings.account_login_token='different-session';
      if(change==='user')AppState.settings.account_user_id='different-account';
      if(change==='service')AppState.settings.subscription_server_url='https://different.example.invalid';
      if(change==='session_revision')AccountAccess.apply({...AppState.settings.account_access,revision:AppState.settings.account_access.revision+1});
      else Subscription.render();
    },change);
    await p.evaluate(async()=>{__releaseAlipayConfig();await new Promise(requestAnimationFrame);});
    assert.equal(await p.locator('#alipayPaymentOpen').count(),0);
    assert.equal(await p.locator('#alipayPaymentBody').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('remount immediately closes old private order controls and preserves processing-only history entry',async()=>{
  const p=await open();try{await instrument(p,{requests:[order()]});await dialog(p);await p.waitForSelector('#alipayPaymentResume');
    await p.evaluate(config=>{
      const host=document.querySelector('#alipayPaymentOpen').parentElement;
      AlipayPayments.mount(host,{...config,enabled:false},{isCurrent:()=>true});
    },capability);
    assert.equal(await p.locator('#alipayPaymentBody').count(),0);
    assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'查看订单');
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='open'||c[0]==='create'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('actual server monthly pricing binds create; annual quotes cannot enter the new-order picker',async()=>{
  const p=await open({serviceBlocked:true,buyerRequired:true,buyerConfigured:false,paymentsEnabled:false});
  try{await instrument(p);await dialog(p);assert.equal(await p.locator('#alipayPaymentOffer option').count(),2);await p.locator('#alipayPaymentOffer').selectOption('1');await create(p);
    assert.equal(await p.locator('.manual-payment-amount').textContent(),'¥79.80');
    assert.equal(await p.locator('#alipayPaymentState').textContent(),'待支付');
    const calls=await p.evaluate(()=>window.__alipayCalls);assert.deepEqual(calls[0].slice(0,3),['create','advanced','month']);
    assert.match(calls[0][3],/^[A-Za-z0-9_-]{12,64}$/);assert.deepEqual(calls[1],['open',id]);
    assert.equal(calls.some(c=>c[0]==='refresh'),false);assert.equal(await p.evaluate(()=>AppState.settings.credit_balance||0),0);
    assert.equal(await p.getByRole('rowheader').count(),5);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('lost create replies retry same idempotency key, while browser failures retain the recoverable order',async()=>{
  const p=await open();try{await instrument(p,{openFail:true});await p.evaluate(()=>{const fn=__MOCK__.create_alipay_order;let n=0;
    __MOCK__.create_alipay_order=async(...args)=>{const value=await fn(...args);if(++n===1)throw new Error('fixture lost reply');return value;};});
    await dialog(p);await p.locator('#alipayPaymentCreate').click();await p.waitForFunction(()=>document.querySelector('#alipayPaymentNotice')?.textContent.includes('重试'));
    await create(p);const calls=await p.evaluate(()=>__alipayCalls.filter(c=>c[0]==='create'));assert.equal(calls[0][3],calls[1][3]);
    assert.equal(await p.locator('#alipayPaymentState').textContent(),'待支付');assert.match(await p.locator('#alipayPaymentNotice').textContent(),/无法打开/);
    assert.equal(await p.locator('#alipayPaymentResume').count(),1);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('historical unpaid order resumes the same order explicitly and never creates another',async()=>{
  const p=await open();try{await instrument(p,{requests:[order('waiting')]});await dialog(p);
    await p.locator('#alipayPaymentResume').click();await p.waitForFunction(()=>__alipayCalls.some(c=>c[0]==='open'));
    const calls=await p.evaluate(()=>__alipayCalls);assert.deepEqual(calls[0],['status',id,true]);assert.deepEqual(calls[1],['open',id]);
    assert.equal(calls.some(c=>c[0]==='create'||c[0]==='refresh'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const refreshFail of [false,true])test(`credited status requires signed refresh; failure=${refreshFail}`,async()=>{
  const p=await open();try{await instrument(p,{requests:[order()],refreshFail});await dialog(p);await p.evaluate(()=>{__alipayNext='credited';});
    await p.locator('#alipayPaymentQuery').click();await p.waitForFunction(()=>!document.querySelector('#alipayPaymentQuery').disabled);
    assert.equal(await p.locator('#alipayPaymentState').textContent(),refreshFail?'已支付':'已到账');
    assert.match(await p.locator('#alipayPaymentNotice').textContent(),refreshFail?/验签/:/积分已到账/);
    assert.equal(await p.locator('#alipayPaymentResume').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('verified refresh may advance the same session revision without losing the payment completion UI',async()=>{
  const p=await open();try{await instrument(p,{requests:[order()]});await dialog(p);
    await p.evaluate(()=>{
      const getSettings=__MOCK__.get_settings;
      __MOCK__.get_settings=()=>{const settings=getSettings();return {...settings,credit_balance:500,
        account_access:{...settings.account_access,revision:settings.account_access.revision+1}};};
      __alipayNext='credited';
    });
    await p.locator('#alipayPaymentQuery').click();
    await p.waitForFunction(()=>document.querySelector('#alipayPaymentState')?.textContent==='已到账');
    assert.match(await p.locator('#alipayPaymentNotice').textContent(),/积分已到账/);
    assert.equal(await p.evaluate(()=>AppState.settings.account_access.revision),1);
    assert.equal(await p.evaluate(()=>AppState.settings.credit_balance),500);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('automatic status checking uses status only and stops when modal closes',async()=>{
  const p=await open();try{await instrument(p,{requests:[order()],fastPoll:true});await dialog(p);
    await p.waitForFunction(()=>__alipayCalls.some(c=>c[0]==='status'));
    const before=await p.evaluate(()=>__alipayCalls);assert.equal(before.some(c=>c[0]==='create'||c[0]==='open'||c[0]==='query'),false);
    assert.deepEqual(before.find(c=>c[0]==='status'),['status',id,false]);
    await p.keyboard.press('Escape');await p.waitForTimeout(100);const count=await p.evaluate(()=>__alipayCalls.length);
    await p.waitForTimeout(160);assert.equal(await p.evaluate(()=>__alipayCalls.length),count);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('refund review and closed orders cannot open cashier or report newly received credits',async()=>{
  for(const state of ['refund_review','closed']){const p=await open();try{await instrument(p,{requests:[order(state)]});await dialog(p);
    assert.equal(await p.locator('#alipayPaymentResume').count(),0);assert.equal(await p.locator('#alipayPaymentState').textContent(),state==='closed'?'已关闭':'订单复核中');
    assert.equal((await p.locator('#alipayPaymentBody').textContent()).includes('已到账'),false);
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='open'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}}
});
test('processing-only mode provides order recovery and no payment controls',async()=>{
  const disabled={...capability,enabled:false};const p=await open({alipayPayments:disabled});
  try{await instrument(p,{requests:[order()],config:disabled});assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'查看订单');await dialog(p);
    assert.equal(await p.locator('#alipayPaymentResume').count(),0);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    await p.locator('#alipayPaymentQuery').click();await p.waitForFunction(()=>__alipayCalls.some(c=>c[0]==='query'));assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('account sales denial preserves historical orders even when public health advertises purchases',async()=>{
  const historyOnly={...capability,enabled:false,catalog:[]};
  const p=await open({alipayPaymentConfig:{ok:true,...historyOnly}});
  try{await p.waitForSelector('#alipayPaymentOpen');assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'查看订单');
    await instrument(p,{config:historyOnly,requests:[order()]});await dialog(p);
    assert.equal(await p.locator('#alipayPaymentResume').count(),0);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    await p.locator('#alipayPaymentQuery').click();await p.waitForFunction(()=>__alipayCalls.some(c=>c[0]==='query'));
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='create'||c[0]==='open'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('50 yuan merchant ceiling filters every over-limit offer without splitting payment',async()=>{
  const limited={...capability,max_order_amount_minor:5000};const p=await open({alipayPayments:limited});
  try{await instrument(p,{config:limited});await dialog(p);
    assert.equal(await p.locator('#alipayPaymentOffer option').count(),1);
    assert.match(await p.locator('#alipayPaymentOffer').textContent(),/标准版.*月付.*19.80/);
    await create(p);assert.deepEqual((await p.evaluate(()=>__alipayCalls))[0].slice(0,3),['create','standard','month']);
    assert.equal((await p.evaluate(()=>__alipayCalls)).filter(c=>c[0]==='create').length,1);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('unverified purchase ceiling does not block already-paid order verification',async()=>{
  const closed={...capability,configured:false,enabled:false,max_order_amount_minor:null,catalog:[]};
  const p=await open({alipayPayments:closed});try{await instrument(p,{config:closed,requests:[order()]});
    assert.equal(await p.locator('#alipayPaymentOpen').textContent(),'查看订单');await dialog(p);
    assert.equal(await p.locator('#alipayPaymentResume').count(),0);assert.equal(await p.locator('#alipayPaymentCreate').count(),0);
    await p.evaluate(()=>{__alipayNext='credited';});await p.locator('#alipayPaymentQuery').click();
    await p.waitForFunction(()=>document.querySelector('#alipayPaymentState')?.textContent==='已到账');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const refreshFail of [false,true])test(`refund review syncs signed paused ledger without claiming arrival; failure=${refreshFail}`,async()=>{
  const p=await open();try{await instrument(p,{requests:[order()],refreshFail});await dialog(p);
    await p.evaluate(()=>{__alipayNext='refund_review';});await p.locator('#alipayPaymentQuery').click();
    await p.waitForFunction(()=>!document.querySelector('#alipayPaymentQuery').disabled);
    assert.equal(await p.locator('#alipayPaymentState').textContent(),'订单复核中');assert.equal(await p.locator('#alipayPaymentResume').count(),0);
    assert.match(await p.locator('#alipayPaymentNotice').textContent(),refreshFail?/验签/:/托管已暂停，积分记录保留/);
    assert.equal((await p.locator('#alipayPaymentBody').textContent()).includes('已到账'),false);
    assert.equal((await p.evaluate(()=>__alipayCalls)).filter(c=>c[0]==='refresh').length,1);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('rollback revisions, changed quotes and unrelated responses cannot confirm paid state',async()=>{
  const p=await open();try{await instrument(p,{requests:[order('waiting',5)]});await dialog(p);
    await p.evaluate(()=>{__alipayNext='credited';__alipayNextRevision=1;});await p.locator('#alipayPaymentQuery').click();
    await p.waitForFunction(()=>!document.querySelector('#alipayPaymentQuery').disabled);assert.equal(await p.locator('#alipayPaymentState').textContent(),'待支付');
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='refresh'),false);
    await p.evaluate(value=>{__MOCK__.query_alipay_order=()=>({ok:true,order:{...value,status:'credited',revision:6,snapshot:{...value.snapshot,amount:'1.00',amount_minor:100}}});},order());
    await p.locator('#alipayPaymentQuery').click();await p.waitForFunction(()=>document.querySelector('#alipayPaymentNotice')?.textContent.includes('报价已变更'));
    assert.equal(await p.locator('#alipayPaymentState').textContent(),'待支付');assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const phase of ['create','query','refresh'])for(const change of ['close','logout','service'])test(`${phase} late reply is ignored after ${change}`,async()=>{
  const p=await open();try{await instrument(p,{requests:phase==='create'?[]:[order()],delay:130});await dialog(p);
    if(phase==='create'){await p.evaluate(()=>document.querySelector('#alipayPaymentCreate').click());}
    else{await p.evaluate(phase=>{if(phase==='refresh')__alipayNext='credited';document.querySelector('#alipayPaymentQuery').click();},phase);
      if(phase==='refresh')await p.waitForFunction(()=>__alipayCalls.some(c=>c[0]==='refresh'));}
    if(change==='close')await p.keyboard.press('Escape');
    else await p.evaluate(change=>{if(change==='logout'){AppState.settings.account_login_token='';AppState.settings.account_user_id='';}
      else AppState.settings.subscription_server_url='https://different.example.invalid';Subscription.render();},change);
    await p.waitForTimeout(330);assert.equal(await p.locator('#alipayPaymentBody').count(),0);
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='open'),false);
    assert.equal(await p.locator('#toast').isVisible(),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('double create clicks have one in-flight order request',async()=>{
  const p=await open();try{await instrument(p,{delay:100});await dialog(p);await p.evaluate(()=>{const b=document.querySelector('#alipayPaymentCreate');b.click();b.click();});
    await p.waitForFunction(()=>document.querySelector('#alipayPaymentQuery') && !document.querySelector('#alipayPaymentQuery').disabled);
    assert.equal((await p.evaluate(()=>__alipayCalls)).filter(c=>c[0]==='create').length,1);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
test('signed account expiry closes private order and hides the payment entry',async()=>{
  const p=await open();try{await instrument(p,{requests:[order()]});
    await p.evaluate(()=>AccountAccess.apply({...AppState.settings.account_access,expires_at:Math.floor(Date.now()/1000)+2}));
    await dialog(p);await p.waitForSelector('#alipayPaymentQuery');
    await p.waitForSelector('#alipayPaymentBody',{state:'detached',timeout:4000});
    assert.equal(await p.locator('#alipayPaymentOpen').isVisible(),false);
    assert.equal((await p.evaluate(()=>__alipayCalls)).some(c=>c[0]==='open'||c[0]==='create'),false);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
for(const theme of ['dark','paper'])test(`${theme} compact payment supports narrow viewport and keyboard`,async()=>{
  const p=await open({theme},{width:560,height:620});try{await instrument(p,{requests:[order()]});await dialog(p);
    await p.waitForSelector('#alipayPaymentQuery');assert.equal(await p.locator('.app').evaluate(n=>n.inert),true);
    assert.equal(await p.locator('.modal').evaluate(n=>n.scrollWidth>n.clientWidth+1),false);
    assert.ok(await p.locator('#alipayPaymentResume').evaluate(n=>n.getBoundingClientRect().height)>=44);
    await p.screenshot({path:path.join(shots,`${theme}-560.png`),fullPage:true});await p.keyboard.press('Escape');
    assert.equal(await p.locator('#alipayPaymentBody').count(),0);assert.deepEqual(p.errors,[]);
  }finally{await p.close();}
});
