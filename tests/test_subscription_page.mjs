// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// 订阅页：套餐档位卡片（免费/标准/专业）与未开放支付时的预览态（Playwright + mock 桥）。
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import installMock from "./ui_fixture.cjs";

const root = path.resolve(import.meta.dirname, "..");
const shots = path.join(root, "_webview_ui_shots", "subscription");
let browser;
before(async () => { fs.mkdirSync(shots, {recursive:true}); browser = await chromium.launch({headless:true}); });
after(async () => { await browser?.close(); });

async function open(options = {}, size = {width:1280,height:800}) {
  const page = await browser.newPage({viewport:size});
  page.setDefaultTimeout(5000);
  page.errors = [];
  page.on("pageerror", e => page.errors.push(e.message));
  await page.addInitScript(installMock, options);
  await page.goto(pathToFileURL(path.join(root, "app/webview_ui/index.html")).href, { timeout: 15000 });
  await page.waitForSelector(".nav-row[aria-current=page]");
  return page;
}

async function openSubscription(page, fullFeatures = true, fullTools = true) {
  await page.locator(".nav-row[data-page='subscription']").click();
  await page.waitForSelector(".plan-card");
  if(fullTools) await page.evaluate(()=>{document.querySelector('#subscriptionTools').open=true;document.querySelectorAll('#subscriptionTools details').forEach(node=>node.open=true);});
  if(fullFeatures && await page.locator('#subscriptionFeatureToggle').getAttribute('aria-expanded')==='false') await page.locator('#subscriptionFeatureToggle').click();
}

async function openEmailAuth(page, intent = 'login') {
  if (intent==='bind') await page.evaluate(()=>{document.querySelector('#subscriptionTools').open=true;document.querySelector('#accountToolsGroup').parentElement.open=true;});
  await page.locator(intent === 'bind' ? '#accountBindEmailBtn' : '#accountEmailLoginBtn').click();
  await page.waitForSelector('#accountAuthModal[aria-hidden="false"]',{state:'attached'});
  await page.getByRole('dialog').waitFor({state:'visible'});
}

async function closeEmailAuth(page) {
  await page.locator('#accountAuthClose').click();
  await page.waitForFunction(() => !document.querySelector('#accountAuthModal') && !document.querySelector('.app').inert);
}

async function sendEmailCode(page, email = 'user@example.com') {
  await page.locator('#loginEmailInput').fill(email);
  await page.locator('#sendCodeBtn').click();
  await page.waitForSelector('#loginCodeInput:visible');
}

// Consume the real fixture response, but deliver one chosen reply explicitly.
async function holdFirstReply(page, method) {
  await page.evaluate(method => {
    const original=window.__MOCK__[method]; let calls=0;
    window.__MOCK__[method]=(...args)=>{
      const response=original(...args);
      if(++calls!==1) return response;
      return new Promise(resolve=>{window.__releaseHeldReply=async()=>resolve(await response);});
    };
  },method);
}
async function releaseHeldReply(page) {
  await page.evaluate(async()=>{
    await window.__releaseHeldReply();
    await new Promise(requestAnimationFrame);
  });
}

function comparisonCell(page, feature, plan) {
  return page.locator('.plan-comparison tbody tr').filter({has:page.getByRole('rowheader', {name:feature, exact:true})})
    .locator(`td[data-plan-id="${plan}"]`);
}

test("subscription page shows the free local tier and two formal tier cards", async () => {
  const page = await open({loggedIn:true,paymentServer:'https://pay.example.invalid'});
  try {
    await openSubscription(page);
    assert.equal(await page.locator(".plan-card").count(), 3, "免费 + 两个付费档位");
    const names = await page.locator(".plan-name").allTextContents();
    assert.deepEqual(names, ["自带服务", "标准版", "专业版"]);
    assert.equal(await page.locator(".plan-card.is-current .plan-name").textContent(), "自带服务", "本地与自带密钥能力不需购买云端积分");
    const prices = await page.locator(".plan-price").allTextContents();
    assert.deepEqual(prices, ["按服务商计费", "¥19.80/月", "¥49.80/月"]);
    const annuals = await page.locator(".plan-annual").allTextContents();
    assert.deepEqual(annuals, []);
    assert.equal(await page.locator('.plan-draft-label').count(),0);
    assert.equal(await comparisonCell(page,'月付积分','standard').textContent(),'500 积分/次');
    assert.equal(await page.getByRole('rowheader',{name:'年付积分',exact:true}).count(),0);
    assert.equal(await comparisonCell(page,'字幕视觉校对（自带密钥）','standard').textContent(),'包含');
    assert.equal(await comparisonCell(page,'转写并发','standard').textContent(),'2 路并发转写');
    assert.equal(await comparisonCell(page,'转写并发','advanced').textContent(),'4 路并发转写');
    assert.equal(await page.locator(".plan-quota-note").count(), 1, "积分换算说明统一收在帮助中，避免重复占据方案表头");
    assert(await page.locator(".plan-quota-note").first().textContent().then(text => text.includes("1 积分")));
    assert.equal(await page.getByRole('heading',{name:'当前使用',exact:true}).count(),0);
    assert.equal(await page.locator('#softwareAccessCard').count(),0,'TASK-8.29：使用权卡已随解锁门槛移除');
    const examples = await page.locator("#quotaExamples").textContent();
    for (const part of ["一分钟口播", "2 积分", "8 积分"]) {
      assert(examples.includes(part), "换算表缺少：" + part);
    }
    await page.screenshot({path:path.join(shots, "tiers.png"), fullPage:true});
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('compact comparison keeps local and BYOK capabilities out of paid features',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',loggedIn:true});
  try {
    await openSubscription(page,false,false);
    assert.equal(await page.locator('#accountName').textContent(),'微信用户');
    assert.equal(await page.locator('#accountConnectionBtn').count(),0);
    assert.equal(await page.locator('#subscriptionAccountGroup h3').count(),0);
    assert.equal(await page.locator('#subscriptionAccountGroup').textContent().then(text=>text.includes('FoilPage 账户')),false);
    assert.equal(await page.locator('#accountLogoutBtn').isVisible(),true);
    assert.equal(await page.locator('#subscriptionTools').evaluate(node=>node.open),false);
    assert.equal(await page.getByRole('rowheader').count(),5,'首屏只展开五项核心差别');
    assert.equal(await page.locator('#subscriptionFeatureRows').isVisible(),false);
    assert.equal(await page.locator('.plan-card').first().locator('.plan-name').textContent(),'自带服务');
    assert.equal(await comparisonCell(page,'转写并发','free').textContent(),'1 路并发转写');
    for(const plan of ['free','standard','advanced']) {
      for(const feature of ['本地转写与自带密钥整理','视觉校对与批量整理（自带密钥）']) assert.equal(await comparisonCell(page,feature,plan).textContent(),'包含');
    }
    for(const feature of ['月付积分','FoilPage 托管正文与标题']) assert.equal(await comparisonCell(page,feature,'free').textContent(),'不包含','试用不赠送托管积分或权限');
    await page.locator('#subscriptionFeatureToggle').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('#subscriptionFeatureToggle').getAttribute('aria-expanded'),'true');
    assert.equal(await comparisonCell(page,'视频封面、播放与文件位置','free').textContent(),'包含');
    await page.evaluate(()=>Subscription.render());
    assert.equal(await page.locator('#subscriptionFeatureToggle').getAttribute('aria-expanded'),'true','刷新账户保留已展开的功能明细');
    await page.locator('#subscriptionFeatureToggle').click();
    assert.equal(await page.getByRole('rowheader').count(),5);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('begin_subscription'),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});


test('public login works before visiting subscription and synchronizes the persisted account without changing the page',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      window.__sharedLoginSuccess=0;
      const trigger=document.createElement('button');trigger.id='sharedLoginTrigger';trigger.textContent='登录';document.querySelector('#page-setup').appendChild(trigger);
      trigger.onclick=()=>Subscription.openLogin({trigger,onSuccess:()=>window.__sharedLoginSuccess++});
    });
    await page.locator('#sharedLoginTrigger').click();await page.waitForSelector('#loginEmailInput:visible');
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='check_subscription_service').length,1);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    await sendEmailCode(page,'prepare@example.com');await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>window.__sharedLoginSuccess===1);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-email-fixture');
    assert.equal(await page.evaluate(()=>AppState.settings.account_user_id),'u-email');
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await page.evaluate(()=>AppState.subscription.plan_id),'free','摘要仅来自真实Bridge读取的权益');
    assert.equal(await page.locator('#subscriptionAccountGroup').count(),0,'登录不需要先渲染订阅页');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('public login does not accept checkout readiness as an email login capability',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      window.__MOCK__.check_subscription_service=()=>({configuration_ready:true,checkout_ready:true,login_methods:{email:'true'}});
      Subscription.openLogin();
    });
    await page.waitForSelector('#loginEmailInput:visible');await page.locator('#loginEmailInput').fill('prepare@example.com');
    assert.equal(await page.locator('#loginEmailInput').isDisabled(),false);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('public login finishing after dismissal synchronizes Bridge state but does not call a stale page callback',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      window.__sharedLoginSuccess=0;window.__sharedLoginCurrent=true;
      const original=window.__MOCK__.email_login_verify;
      window.__MOCK__.email_login_verify=(...args)=>new Promise(resolve=>{window.__releaseSharedVerify=()=>resolve(original(...args));});
      Subscription.openLogin({isCurrent:()=>window.__sharedLoginCurrent,onSuccess:()=>window.__sharedLoginSuccess++});
    });
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn')&&!document.querySelector('#sendCodeBtn').disabled);
    await sendEmailCode(page);await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>typeof window.__releaseSharedVerify==='function');
    await closeEmailAuth(page);await page.evaluate(()=>{window.__sharedLoginCurrent=false;});
    await page.locator('[data-page=settings]').click();await page.evaluate(()=>window.__releaseSharedVerify());
    await page.waitForFunction(()=>AppState.settings.account_login_token==='login-email-fixture');
    assert.equal(await page.evaluate(()=>AppState.currentView),'settings');assert.equal(await page.evaluate(()=>window.__sharedLoginSuccess),0);
    assert.equal(await page.locator('#accountAuthModal').count(),0);
    assert.equal(await page.locator('#toast').textContent().then(text=>text.includes('登录成功')),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('public login sends device-limit results to the existing device-management flow',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',deviceLimit:true});
  try {
    await page.evaluate(()=>Subscription.openLogin());
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn')&&!document.querySelector('#sendCodeBtn').disabled);
    await sendEmailCode(page);await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>AppState.currentView==='subscription');
    await page.locator('#completeAccountLoginBtn').waitFor({state:'visible'});
    assert.equal(await page.locator('#subscriptionTools').evaluate(node=>node.open),true);
    assert.equal(await page.locator('#accountDevicesGroup').evaluate(node=>node.parentElement.open),true);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token || ''),'');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

for(const change of ['service','account'])test(`public login rejects a late response after a ${change} change`,async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_verify;window.__sharedLoginSuccess=0;
      window.__MOCK__.email_login_verify=(...args)=>new Promise(resolve=>{window.__releaseChangedVerify=async()=>{resolve(await original(...args));window.__changedVerifyDelivered=true;};});
      Subscription.openLogin({onSuccess:()=>window.__sharedLoginSuccess++});
    });
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn')&&!document.querySelector('#sendCodeBtn').disabled);
    await sendEmailCode(page);await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>typeof window.__releaseChangedVerify==='function');
    await closeEmailAuth(page);
    await page.evaluate(change=>{
      if(change==='service') AppState.settings.subscription_server_url='https://pay-next.example.com';
      else AppState.settings.account_login_token='another-actual-session';
      window.__releaseChangedVerify();
    },change);
    await page.waitForFunction(()=>window.__changedVerifyDelivered===true);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token || ''),change==='account'?'another-actual-session':'');
    assert.equal(await page.evaluate(()=>window.__sharedLoginSuccess),0);
    assert.equal(await page.locator('#toast').textContent().then(text=>text.includes('登录成功')),false);
    assert.equal(await page.locator('#accountAuthModal').count(),0);assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('public and subscription login share an in-flight request, email draft and resend cooldown',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_start;
      window.__MOCK__.email_login_start=(...args)=>new Promise(resolve=>{window.__releaseSharedSend=()=>resolve(original(...args));});
      Subscription.openLogin();
    });
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn')&&!document.querySelector('#sendCodeBtn').disabled);
    await page.locator('#loginEmailInput').fill('shared@example.com');await page.locator('#sendCodeBtn').click();
    await page.waitForFunction(()=>typeof window.__releaseSharedSend==='function');await closeEmailAuth(page);
    await openSubscription(page);await openEmailAuth(page);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('#sendCodeBtn').onclick());await page.evaluate(()=>window.__releaseSharedSend());
    await page.waitForSelector('#loginCodeInput:visible');
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'shared@example.com');
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='email_login_start').length,1);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('raw login success without persisted Bridge state never creates a client-only account',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      window.__sharedLoginSuccess=0;
      window.__MOCK__.email_login_verify=()=>({ok:true,login_token:'unpersisted-response',account:{user_id:'unpersisted-user',display_name:'未经持久化'}});
      Subscription.openLogin({onSuccess:()=>window.__sharedLoginSuccess++});
    });
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn')&&!document.querySelector('#sendCodeBtn').disabled);
    await sendEmailCode(page);await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountAuthError')?.textContent.includes('登录状态尚未同步'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token || ''),'');
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name || ''),'');
    assert.equal(await page.evaluate(()=>window.__sharedLoginSuccess),0);
    assert.equal(await page.getByRole('dialog',{name:'登录 FoilPage',exact:true}).isVisible(),true,'未持久化的验证失败仍可在原弹窗明确重试');
    assert.equal(await page.locator('#toast').textContent().then(text=>text.includes('登录成功')),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test("paid tier buttons stay preview-only until checkout is configured", async () => {
  const page = await open();
  try {
    await openSubscription(page);
    const paidButtons = page.locator(".plan-card:not(.is-current) .btn");
    assert.equal(await paidButtons.count(), 2);
    for (const label of ["暂未开放"]) {
      const texts = await paidButtons.allTextContents();
      assert(texts.every(t => t === label), "未配置收款时按钮应为预览态，实际：" + JSON.stringify(texts));
    }
    assert(await paidButtons.first().isDisabled(), "预览态按钮应禁用");
    const calls = await page.evaluate(() => window.__fixtureCalls);
    assert(!calls.includes("begin_subscription"), "预览态不得发起订阅");
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("redeem input appears with payment server and calls bridge redeem", async () => {
  const page = await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    assert.equal(await page.locator("#redeemOrderInput").count(), 1, "配置付费服务后出现兑换入口");
    await page.locator("#redeemOrderInput").fill("202609261234");
    await page.locator("#redeemOrderBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("redeem_subscription_order"));
    await page.waitForSelector("#toast:not([hidden])");
    const text = await page.locator("#toast").textContent();
    assert(text.includes("兑换成功"), "兑换成功应有 toast，实际：" + text);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("wechat login entry appears with payment server and polls to success", async () => {
  const page = await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    assert.equal(await page.locator("#wechatLoginBtn").count(), 1, "配置服务后出现微信登录入口");
    await page.locator("#wechatLoginBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("wechat_login_start"));
    await page.waitForFunction(() => window.__fixtureCalls.includes("wechat_login_poll"));
    await page.waitForFunction(() => document.querySelector("#accountState") && document.querySelector("#accountState").textContent.includes("已登录"));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("email code login flow completes and updates account state", async () => {
  const page = await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    assert.equal(await page.locator('#accountAuthModal[aria-hidden="false"]').count(),0,'账户页不会默认弹出登录');
    await openEmailAuth(page);
    assert.equal(await page.locator("#loginEmailInput").count(), 1, "出现邮箱登录入口");
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false,'尚未提交邮箱时不展示验证码步骤');
    await sendEmailCode(page);
    await page.waitForFunction(() => window.__fixtureCalls.includes("email_login_start"));
    assert.equal(await page.locator("#loginCodeInput").inputValue(), "123456", "DEV 模式自动填入验证码");
    await page.locator("#verifyCodeBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("email_login_verify"));
    await page.waitForFunction(() => document.querySelector("#accountState") && document.querySelector("#accountState").textContent.includes("已登录"));
    assert.equal(await page.locator('#accountAuthModal').count(),0);
    assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("google login button starts polling flow", async () => {
  const page = await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    await page.locator("#googleLoginBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("google_login_start"));
    await page.waitForFunction(() => window.__fixtureCalls.includes("wechat_login_poll"));
    await page.waitForFunction(() => document.querySelector("#accountState") && document.querySelector("#accountState").textContent.includes("已登录"));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("logged-in account shows logout and provider hint", async () => {
  const page = await open({paymentServer:'https://pay.example.com', loggedIn:true});
  try {
    await openSubscription(page);
    assert.equal(await page.locator("#accountLogoutBtn").count(), 1, "登录态显示退出按钮");
    assert(await page.locator("#accountState").textContent().then(x => x.includes("微信用户")));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("redeem failure surfaces server error without state change", async () => {
  const page = await open({paymentServer:'https://pay.example.com', redeemFail:true});
  try {
    await openSubscription(page);
    await page.locator("#redeemOrderInput").fill("bad-order");
    await page.locator("#redeemOrderBtn").click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("redeem_subscription_order"));
    const text = await page.locator("#toast").textContent();
    assert(text.includes("其他设备"), "服务端拒绝原因要透出：" + text);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("with checkout configured the paid tier button starts subscription", async () => {
  const page = await open({checkoutUrl:'https://pay.example.com'});
  try {
    await openSubscription(page);
    const standard = page.locator(".plan-card").nth(1);
    const btn = standard.locator(".btn");
    assert(!(await btn.isDisabled()), "配置收款后付费档按钮可用");
    await btn.click();
    await page.waitForFunction(() => window.__fixtureCalls.includes("begin_subscription"));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});


test("only service-configured plans can start checkout", async () => {
  const page = await open({paymentServer:'https://pay.example.com', serviceReady:true, availablePlans:['standard']});
  try {
    await openSubscription(page);
    await page.waitForFunction(() => document.querySelectorAll('.plan-card')[1].querySelector('.btn').disabled === false);
    assert.equal(await page.locator('.plan-card').nth(2).locator('.btn').isDisabled(), true);
    await page.locator('.plan-card').nth(1).locator('.btn').click();
    await page.waitForFunction(() => window.__fixtureCalls.includes('begin_subscription'));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test("a checkout URL alone cannot bypass failed production readiness", async () => {
  const page = await open({paymentServer:'https://pay.example.com',checkoutUrl:'https://pay.example.com',serviceBlocked:true});
  try {
    await openSubscription(page);
    await page.getByRole('button', {name:'检查连接', exact:true}).click();
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(), true);
    assert.equal(await page.locator('.plan-card').nth(2).locator('.btn').isDisabled(), true);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).includes('begin_subscription'), false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});


function offer(months, amount, changes = {}) {
  return {period:months === 1 ? 'month' : 'year',months,amount,credits:months === 1 ? 500 : 6000,currency:'CNY',status:'ready',purchasable:true,...changes};
}
function catalog(id, offers, changes = {}) {
  return {id,status:'ready',purchasable:true,offers,...changes};
}

test("prices use service catalog amounts instead of client plan prices", async () => {
  const page = await open({serviceReady:true});
  try {
    await openSubscription(page);
    await page.waitForFunction(() => document.querySelectorAll('.plan-price')[1].textContent === '¥23.80/月');
    assert.deepEqual(await page.locator('.plan-price').allTextContents(), ['按服务商计费','¥23.80/月','¥47.50/月']);
    assert.deepEqual(await page.locator('.plan-annual').allTextContents(), []);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("year-only historical offer cannot open a new annual purchase",async()=>{
  const page=await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(12,'218.00')])]});
  try{await openSubscription(page);await page.waitForFunction(()=>!document.querySelectorAll('.plan-card')[1].querySelector('.btn').disabled===false);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.equal(await page.locator('.plan-period').count(),0);assert.equal(await page.locator('.plan-annual').count(),0);
    assert.equal((await page.evaluate(()=>__fixtureCalls)).includes('begin_subscription'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test("month-only offer never advertises an unconfigured annual price", async () => {
  const page = await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'31.20')])]});
  try {
    await openSubscription(page);
    await page.waitForFunction(() => document.querySelectorAll('.plan-price')[1].textContent === '¥31.20/月');
    assert.equal(await page.locator('.plan-card').nth(1).locator('.plan-annual').count(),0);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

for (const [name, invalidOffers] of [
  ['ambiguous amount',[offer(1,null,{status:'ambiguous_price',purchasable:false})]],
  ['unsupported period',[offer(3,'55.00',{period:'months'})]],
  ['malformed amount',[offer(1,'23.456')]],
  ['wrong currency',[offer(1,'23.00',{currency:'USD'})]],
  ['duplicate monthly offers',[offer(1,'21.00'),offer(1,'29.00')]],
]) {
  test(`${name} keeps plan prices visible and cannot start checkout`, async () => {
    const page = await open({serviceReady:true,publicCatalog:[catalog('standard',invalidOffers)]});
    try {
      await openSubscription(page);
      const standard=page.locator('.plan-card').nth(1);
      assert.equal(await standard.locator('.plan-price').textContent(),'¥19.80/月');
      assert.equal(await standard.locator('.plan-draft-label').count(),0);
      assert.equal(await standard.locator('.btn').textContent(),'暂未开放');
      assert.equal(await standard.locator('.btn').isDisabled(),true);
      assert.equal((await page.evaluate(() => window.__fixtureCalls)).includes('begin_subscription'),false);
      assert.deepEqual(page.errors,[]);
    } finally { await page.close(); }
  });
}

test("checkout ready without a catalog shows approved draft prices but cannot enable purchase", async () => {
  const page = await open({serviceReady:true,publicCatalog:[]});
  try {
    await openSubscription(page);
    assert.deepEqual(await page.locator('.plan-price').allTextContents(),['按服务商计费','¥19.80/月','¥49.80/月']);
    assert.equal(await page.locator('.plan-draft-label').count(),0);
    for (const card of await page.locator('.plan-card').all()) assert.equal(await card.locator('.btn').isDisabled(),true);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).includes('begin_subscription'),false);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

for (const [name, options] of [
  ['missing email method',{loginMethods:{wechat:true,google:true}}],
  ['email explicitly unavailable',{emailUnavailable:true}],
  ['offline service',{serviceOffline:true}],
  ['failed contract',{contractThrows:true}],
]) {
  test(`${name} disables email submission with a configuration hint`, async () => {
    const page=await open({paymentServer:'https://pay.example.com',...options});
    try {
      await openSubscription(page);
      await openEmailAuth(page);
      assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
      assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),true);
      assert.equal(await page.locator('#loginEmailInput').isEnabled(),true);
      assert.equal(await page.locator('#loginCodeInput').isVisible(),false,'不可用服务不能跳入验证码步骤');
      await page.locator('#loginEmailInput').fill('draft@example.com');
      assert((await page.locator('#emailLoginHint').textContent()).includes('检查服务配置'));
      await page.evaluate(() => { document.querySelector('#sendCodeBtn').onclick(); document.querySelector('#verifyCodeBtn').onclick(); });
      const calls=await page.evaluate(() => window.__fixtureCalls);
      assert(!calls.includes('email_login_start') && !calls.includes('email_login_verify'));
      assert.deepEqual(page.errors,[]);
    } finally { await page.close(); }
  });
}

test("pending service contract keeps login closed until email is explicitly available", async () => {
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{window.__MOCK__.check_subscription_service=()=>new Promise(resolve=>{window.__resolvePendingEmailService=resolve;});});
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('pending@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    await page.evaluate(()=>window.__resolvePendingEmailService({login_methods:{email:true},checkout_ready:false,public_catalog:[],available_plans:[]}));
    await page.waitForFunction(() => document.querySelector('#sendCodeBtn').disabled === false);
    assert.equal(await page.locator('#verifyCodeBtn').isVisible(),false,'能力检查通过仍须先提交邮箱');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("email flow is single-flight while payment configuration is still unavailable", async () => {
  const page=await open({paymentServer:'https://pay.example.com',serviceBlocked:true});
  try {
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('user@example.com');
    await page.waitForFunction(() => !document.querySelector('#sendCodeBtn').disabled);
    await holdFirstReply(page,'email_login_start');
    await page.evaluate(() => { const send=document.querySelector('#sendCodeBtn'); send.onclick(); send.onclick(); document.querySelector('#verifyCodeBtn').onclick(); });
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).filter(call => call === 'email_login_start').length,1);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).filter(call => call === 'email_login_verify').length,0);
    await releaseHeldReply(page);
    await page.waitForFunction(() => document.querySelector('#loginCodeInput').value === '123456');
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),false);
    await holdFirstReply(page,'email_login_verify');
    await page.evaluate(() => { const verify=document.querySelector('#verifyCodeBtn'); verify.onclick(); verify.onclick(); });
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).filter(call => call === 'email_login_verify').length,1);
    await releaseHeldReply(page);
    await page.waitForFunction(() => document.querySelector('#accountState').textContent.includes('已登录'));
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).filter(call => call === 'email_login_start').length,1);
    assert.equal((await page.evaluate(() => window.__fixtureCalls)).filter(call => call === 'email_login_verify').length,1);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("email cooldown never reopens login after a connection downgrade", async () => {
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    await page.clock.install();
    await openEmailAuth(page);
    await sendEmailCode(page);
    await page.waitForFunction(() => document.querySelector('#loginCodeInput').value === '123456');
    await closeEmailAuth(page);
    await page.evaluate(() => { window.__fixtureContractOverride={checkout_ready:false,available_plans:[],public_catalog:[],login_methods:{}}; });
    await page.getByRole('button',{name:'检查连接',exact:true}).click();
    await page.clock.fastForward(60001);
    await openEmailAuth(page);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

for (const [name, extra] of [['offline',{paymentServer:'https://pay.example.com',serviceOffline:true}],['unconfigured',{}]]) {
  test(`logged-in account can always log out when service is ${name}`,async()=>{
    const page=await open({loggedIn:true,...extra});
    try {
      await openSubscription(page);
      assert.equal(await page.locator('#accountLogoutBtn').isEnabled(),true);
      await page.locator('#accountLogoutBtn').click();
      await page.waitForFunction(()=>window.__fixtureCalls.includes('account_logout'));
      assert.equal(await page.locator('#accountLogoutBtn').count(),0);
      assert.deepEqual(page.errors,[]);
    } finally { await page.close(); }
  });
}


test("primary offer credits use confirmed overrides instead of client entitlement labels",async()=>{
  const page=await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'22.80',{credits:725}),offer(12,'209.00',{credits:8800})])]});
  try {
    await openSubscription(page);
    await page.waitForFunction(()=>document.querySelectorAll('.plan-price')[1].textContent==='¥22.80/月');
    const standard=page.locator('.plan-card').nth(1);
    assert.equal(await comparisonCell(page,'月付积分','standard').textContent(),'725 积分/次');
    assert.equal(await page.getByRole('rowheader',{name:'年付积分',exact:true}).count(),0);
    assert.equal(await standard.locator('.btn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("year-only configured credits remain outside new formal purchases",async()=>{
  const page=await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(12,'215.00',{credits:7500})])]});
  try{await openSubscription(page);assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.equal(await page.locator('.plan-annual').count(),0);assert.equal(await page.getByRole('rowheader',{name:'年付积分',exact:true}).count(),0);
    assert.equal((await page.evaluate(()=>__fixtureCalls)).includes('begin_subscription'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for (const credits of [null,0,-1,'500']) {
  test(`unconfirmed credits ${JSON.stringify(credits)} keep known price but block purchase`,async()=>{
    const page=await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'25.00',{credits})])]});
    try {
      await openSubscription(page);
      await page.waitForFunction(()=>document.querySelectorAll('.plan-price')[1].textContent==='¥25.00/月');
      const standard=page.locator('.plan-card').nth(1);
      assert.equal(await comparisonCell(page,'月付积分','standard').textContent(),'积分额度待确认');
      assert.equal(await standard.locator('.btn').isDisabled(),true);
      assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('begin_subscription'),false);
      assert.deepEqual(page.errors,[]);
    } finally { await page.close(); }
  });
}

test("leaving subscription during email request preserves input and single-flight state",async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_start;
      window.__MOCK__.email_login_start=(...args)=>new Promise(resolve=>{window.__resolveOffPageMail=()=>resolve(original(...args));});
    });
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('user@example.com');
    await page.locator('#sendCodeBtn').click();
    await page.waitForFunction(()=>typeof window.__resolveOffPageMail==='function');
    await closeEmailAuth(page);
    await page.locator('.nav-row[data-page="settings"]').click();
    await page.locator('.nav-row[data-page="subscription"]').click();
    await openEmailAuth(page);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('#sendCodeBtn').onclick());
    await page.evaluate(()=>window.__resolveOffPageMail());
    await page.waitForSelector('#loginCodeInput:visible');
    assert.equal(await page.locator('#loginCodeInput').evaluate(node=>document.activeElement===node),true,'发送中关闭并重开后，迟到结果聚焦当前弹窗的验证码');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='email_login_start').length,1);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'user@example.com');
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'','关闭发送中的弹窗后，迟到响应不写回验证码');
    await page.locator('#loginCodeInput').fill('123456');
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),false);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true,'成功发送后的冷却仍保留');
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test('late service response cannot reopen login on a newer subscription page',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',contractReplies:[
    {value:{checkout_ready:true,available_plans:['standard'],public_catalog:[],login_methods:{email:true,wechat:true}}},
    {value:{checkout_ready:false,available_plans:[],public_catalog:[],login_methods:{}}},
    {value:{checkout_ready:false,available_plans:[],public_catalog:[],login_methods:{}}},
  ]});
  try {
    await holdFirstReply(page,'check_subscription_service');
    await openSubscription(page);
    await page.waitForFunction(()=>typeof window.__releaseHeldReply==='function');
    await page.locator('[data-page=settings]').click();
    await openSubscription(page);
    await releaseHeldReply(page);
    assert.equal(await page.locator('#subscriptionServiceGroup [role=status]').textContent(),'邮箱登录尚未开放');
    await openEmailAuth(page);
    await page.waitForFunction(()=>document.querySelector('#emailLoginHint').textContent.includes('暂未开放'));
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='check_subscription_service').length,3,'重开只新增一次真实检查，旧回复不授权');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#wechatLoginBtn').isDisabled(),true);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('email verification completing off-page preserves navigation and shows the account on return',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_verify;
      window.__MOCK__.email_login_verify=(...args)=>new Promise(resolve=>{window.__resolveOffPageVerify=()=>resolve(original(...args));});
    });
    await openSubscription(page);
    await openEmailAuth(page);
    await sendEmailCode(page);
    await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>typeof window.__resolveOffPageVerify==='function');
    await closeEmailAuth(page);
    await page.locator('[data-page=settings]').click();
    await page.evaluate(()=>window.__resolveOffPageVerify());
    await page.waitForFunction(()=>AppState.settings.account_login_token==='login-email-fixture');
    assert.equal(await page.evaluate(()=>AppState.currentView),'settings');
    assert.equal(await page.locator('#pageTitle').textContent(),'设置');
    await openSubscription(page);
    assert((await page.locator('#accountState').textContent()).includes('fixture用户'));
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='email_login_verify').length,1);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

for (const operation of ['checkout','redeem']) {
  test(`${operation} stays single-flight across subscription navigation`,async()=>{
    const page=await open({paymentServer:'https://pay.example.com',serviceReady:true});
    try {
      const method=operation==='checkout'?'begin_subscription':'redeem_subscription_order';
      await holdFirstReply(page,method);
      await openSubscription(page);
      const button=()=>operation==='checkout' ? page.locator('.plan-card').nth(1).locator('.btn') : page.locator('#redeemOrderBtn');
      await page.waitForFunction(operation=>operation==='checkout' ? !document.querySelectorAll('.plan-card .btn')[1].disabled : !document.querySelector('#redeemOrderBtn').disabled,operation);
      if(operation==='redeem') await page.locator('#redeemOrderInput').fill('fixture-order');
      await button().click();
      await page.waitForFunction(()=>typeof window.__releaseHeldReply==='function');
      await page.locator('[data-page=settings]').click();
      await openSubscription(page);
      assert.equal(await button().isDisabled(),true);
      await page.waitForFunction(method=>window.__fixtureCalls.filter(call=>call===method).length===1,method);
      await button().evaluate(node=>node.onclick());
      assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call===method).length,1);
      await releaseHeldReply(page);
      await page.waitForFunction(operation=>operation==='checkout' ? !document.querySelectorAll('.plan-card .btn')[1].disabled : !document.querySelector('#redeemOrderBtn').disabled,operation);
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  });
}

test('failed logout keeps the current account and can be retried',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',loggedIn:true,logoutFail:true});
  try {
    await openSubscription(page);
    await page.locator('#accountLogoutBtn').click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('退出暂时失败'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');
    assert.equal(await page.locator('#accountLogoutBtn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('redeeming into a new account reloads persisted account fields from the bridge',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',redeemAccount:true});
  try {
    await openSubscription(page);
    await page.locator('#redeemOrderInput').fill('fixture-order');
    await page.locator('#redeemOrderBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent.includes('兑换用户'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'redeem-session-fixture');
    assert.equal(await page.locator('#accountLogoutBtn').count(),1);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('invalid refresh reloads the cleared account without a stale logged-in state',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',loggedIn:true,refreshInvalid:true});
  try {
    await openSubscription(page);
    await page.getByRole('button',{name:'同步积分',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent==='未登录');
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'');
    assert.equal(await page.locator('#accountLogoutBtn').count(),0);
    assert.equal(await page.getByRole('button',{name:'同步积分',exact:true}).count(),0,'退出后不保留无效的积分同步入口');
    assert((await page.locator('#toast').textContent()).includes('登录已失效'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('explicit connection check supersedes a delayed initial service contract',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',contractReplies:[
    {value:{checkout_ready:true,available_plans:['standard'],public_catalog:[],login_methods:{email:true}}},
    {value:{configuration_ready:false,gates:['邮箱未配置'],checkout_ready:false,available_plans:[],public_catalog:[],login_methods:{}}},
    {value:{checkout_ready:false,available_plans:[],public_catalog:[],login_methods:{}}},
  ]});
  try {
    await holdFirstReply(page,'check_subscription_service');
    await openSubscription(page);
    await page.waitForFunction(()=>typeof window.__releaseHeldReply==='function');
    await page.getByRole('button',{name:'检查连接',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#subscriptionServiceGroup [role=status]').textContent==='邮箱登录尚未开放');
    await releaseHeldReply(page);
    await openEmailAuth(page);
    await page.waitForFunction(()=>document.querySelector('#emailLoginHint').textContent.includes('暂未开放'));
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='check_subscription_service').length,3);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert((await page.locator('#emailLoginHint').textContent()).includes('暂未开放'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('current paid tier can renew after service readiness instead of blocking repeat purchases',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceReady:true});
  try {
    await page.evaluate(()=>{ window.__MOCK__.get_subscription_status=()=>({active:true,plan_id:'standard',
      plan:{name:'标准版'},monthly_agent_units:500,credit_balance:320,hosted_agent_ready:true}); });
    await openSubscription(page);
    await page.waitForFunction(()=>document.querySelector('.plan-card.is-current .plan-name').textContent==='标准版');
    const card=page.locator('.plan-card.is-current');
    assert.equal(await card.locator('.plan-current-badge').textContent(),'当前方案');
    assert.equal(await card.locator('.btn').textContent(),'续购');
    assert.equal(await card.locator('.btn').isEnabled(),true);
    await card.locator('.btn').click();
    await page.waitForFunction(()=>window.__fixtureCalls.includes('begin_subscription'));
    assert((await page.evaluate(()=>window.__fixtureCallArgs)).some(call=>call[0]==='begin_subscription' && call[1]==='standard'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('renewal stays closed for the current tier when configured service is unavailable',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceReady:true,serviceOffline:true});
  try {
    await page.evaluate(()=>{ window.__MOCK__.get_subscription_status=()=>({active:true,plan_id:'standard',
      plan:{name:'标准版'},monthly_agent_units:500,credit_balance:320}); });
    await openSubscription(page);
    await page.waitForFunction(()=>document.querySelector('.plan-card.is-current .plan-name').textContent==='标准版');
    assert.equal(await page.locator('.plan-card.is-current .btn').isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('begin_subscription'),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('order draft survives navigation and failed redemption without losing user input',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',redeemFail:true});
  try {
    await openSubscription(page);
    await page.locator('#redeemOrderInput').fill('fixture-kept-order');
    await page.locator('[data-page=settings]').click();
    await openSubscription(page);
    assert.equal(await page.locator('#redeemOrderInput').inputValue(),'fixture-kept-order');
    await page.locator('#redeemOrderBtn').click();
    await page.waitForFunction(()=>window.__fixtureCalls.includes('redeem_subscription_order'));
    assert.equal(await page.locator('#redeemOrderInput').inputValue(),'fixture-kept-order');
    await page.locator('[data-page=settings]').click();
    await openSubscription(page);
    assert.equal(await page.locator('#redeemOrderInput').inputValue(),'fixture-kept-order');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('connection saves preserve another active field draft and its text selection',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await holdFirstReply(page,'save_settings');
    await openSubscription(page);
    if (!await page.locator('#subscriptionServiceGroup details').evaluate(node=>node.open)) await page.locator('.set-advanced summary').click();
    const server=page.getByRole('textbox',{name:'服务地址',exact:true});
    const key=page.getByRole('textbox',{name:'许可证公钥',exact:true});
    await server.fill('https://pay-next.example.com');
    await key.click();
    await page.waitForFunction(()=>typeof window.__releaseHeldReply==='function');
    await key.fill('public-key-draft');
    await key.evaluate(input=>input.setSelectionRange(3,8));
    await releaseHeldReply(page);
    await page.waitForFunction(()=>AppState.settings.subscription_server_url==='https://pay-next.example.com');
    assert.equal(await key.inputValue(),'public-key-draft');
    assert.deepEqual(await key.evaluate(input=>[document.activeElement===input,input.selectionStart,input.selectionEnd]),[true,3,8]);
    await key.press('Enter');
    await page.waitForFunction(()=>AppState.settings.subscription_license_public_key==='public-key-draft');
    assert.equal(await key.inputValue(),'public-key-draft');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('confirmed month and year catalog keeps annual purchase hidden and neutral refund guidance',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceReady:true,publicCatalog:[
    catalog('standard',[offer(1,'19.80',{credits:500}),offer(12,'190.08',{credits:6000})]),
    catalog('advanced',[offer(1,'79.80',{credits:3000}),offer(12,'766.08',{credits:36000})]),
  ]});
  try {
    await openSubscription(page);
    await page.waitForFunction(()=>document.querySelectorAll('.plan-price')[2].textContent==='¥79.80/月');
    assert.deepEqual(await page.locator('.plan-price').allTextContents(),['按服务商计费','¥19.80/月','¥79.80/月']);
    assert.deepEqual(await page.locator('.plan-annual').allTextContents(),[]);
    const body=await page.locator('#subscriptionPane').textContent();
    assert(body.includes('退款'));
    assert(!body.includes('不支持退款'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('custom annual amount is hidden without falsely promising an eight-tenths discount',async()=>{
  const page=await open({serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'19.80'),offer(12,'213.84')]) ]});
  try {
    await openSubscription(page);
    const standard=page.locator('.plan-card').nth(1);
    await page.waitForFunction(()=>document.querySelectorAll('.plan-price')[1].textContent==='¥19.80/月');
    assert.equal(await standard.locator('.plan-annual').count(),0);
    assert(!(await standard.textContent()).includes('八折'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('fresh subscription page checks the real service without waiting for cached capabilities',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      window.__freshHealthCalls=0; window.__cachedContractCalls=0;
      window.__MOCK__.get_subscription_contract=()=>{window.__cachedContractCalls++;return {login_methods:{}};};
      window.__MOCK__.check_subscription_service=()=>{
        window.__freshHealthCalls++;
        return new Promise(resolve=>{window.__resolveFreshHealth=resolve;});
      };
    });
    await openSubscription(page);
    await page.waitForFunction(()=>typeof window.__resolveFreshHealth==='function');
    await openEmailAuth(page);
    assert.equal(await page.locator('#loginEmailInput').isEnabled(),true);
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    await page.locator('#loginEmailInput').fill('pending@example.com');
    await closeEmailAuth(page);await openEmailAuth(page);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'pending@example.com');
    assert.equal(await page.evaluate(()=>window.__freshHealthCalls),1,'首次检查尚未返回时重开不重复请求');
    await page.locator('#loginEmailInput').focus();
    await page.evaluate(()=>window.__resolveFreshHealth({login_methods:{email:true},checkout_ready:false,public_catalog:[],available_plans:[],gates:['购买暂未开放']}));
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'pending@example.com');
    assert.equal(await page.locator('#loginEmailInput').evaluate(node=>document.activeElement===node),true);
    assert.deepEqual(await page.evaluate(()=>[window.__freshHealthCalls,window.__cachedContractCalls]),[1,0]);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false,'健康检查不得自动发送验证码');
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('reopening login retries a failed connection once and preserves the editable email draft',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceOffline:true});
  try {
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('recovery@example.com');
    await closeEmailAuth(page);
    await page.evaluate(()=>{
      window.__reopenHealthCalls=0;
      window.__MOCK__.check_subscription_service=()=>{
        window.__reopenHealthCalls++;
        return new Promise(resolve=>{window.__resolveReopenHealth=resolve;});
      };
    });
    await openEmailAuth(page);
    await page.waitForFunction(()=>typeof window.__resolveReopenHealth==='function');
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'recovery@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#loginEmailInput').isEnabled(),true);
    await closeEmailAuth(page);await openEmailAuth(page);
    await page.locator('#loginEmailInput').focus();
    await page.evaluate(()=>{Subscription.openLogin();Subscription.openLogin();});
    assert.equal(await page.evaluate(()=>window.__reopenHealthCalls),1,'重开弹窗只复用当前检查');
    await page.evaluate(()=>window.__resolveReopenHealth({login_methods:{email:true},checkout_ready:false,public_catalog:[],available_plans:[],gates:['订单配置尚未完成','爱发电方案未配置']}));
    await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'recovery@example.com');
    assert.equal(await page.locator('#loginEmailInput').evaluate(node=>document.activeElement===node),true);
    assert.equal(await page.locator('#subscriptionServiceGroup [role=status]').textContent(),'邮箱登录可用','关闭购买不影响登录连接状态');
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    assert.equal(await page.locator('#verifyCodeBtn').isVisible(),false,'服务恢复不会代替显式提交邮箱');
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('connection status distinguishes a failed login check without listing payment gates',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{window.__MOCK__.check_subscription_service=()=>({login_methods:{},readiness:{ready:false,blocking:['服务暂时不可达']},gates:['配置爱发电买家身份验证','购买与兑换暂未开放']});});
    await openSubscription(page);
    await page.waitForFunction(()=>document.querySelector('#subscriptionServiceGroup [role=status]').textContent==='暂时无法连接服务，请重试');
    await page.getByRole('button',{name:'检查连接',exact:true}).click();
    assert.equal(await page.locator('#subscriptionServiceGroup [role=status]').textContent(),'暂时无法连接服务，请重试');
    assert.equal((await page.locator('#subscriptionServiceGroup [role=status]').textContent()).includes('爱发电'),false);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a retry keeps email login closed for unavailable or non-boolean capabilities',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceOffline:true});
  try {
    await openSubscription(page);
    for(const capability of [false,'true']) {
      await page.evaluate(capability=>{window.__MOCK__.check_subscription_service=()=>({login_methods:{email:capability},configuration_ready:true,checkout_ready:false,gates:['支付尚未开放']});},capability);
      await openEmailAuth(page);
      await page.waitForFunction(()=>document.querySelector('#emailLoginHint').textContent.includes('检查服务配置'));
      await page.locator('#loginEmailInput').fill('unavailable@example.com');
      assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
      assert.equal(await page.locator('#subscriptionServiceGroup [role=status]').textContent(),'邮箱登录尚未开放');
      await page.evaluate(()=>document.querySelector('#sendCodeBtn').onclick());
      await closeEmailAuth(page);
    }
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.btn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a late login retry cannot replace the current service capabilities',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceOffline:true});
  try {
    await openSubscription(page);
    await page.evaluate(()=>{window.__MOCK__.check_subscription_service=()=>new Promise(resolve=>{window.__resolveOldLoginRetry=resolve;});});
    await openEmailAuth(page);
    await page.waitForFunction(()=>typeof window.__resolveOldLoginRetry==='function');
    await page.locator('#loginEmailInput').fill('same-draft@example.com');
    await closeEmailAuth(page);
    await page.evaluate(()=>{
      AppState.settings.subscription_server_url='https://pay-next.example.com';
      window.__MOCK__.check_subscription_service=()=>({login_methods:{email:false},checkout_ready:false});
      Subscription.render();
    });
    await page.waitForFunction(()=>document.querySelector('#subscriptionServiceGroup [role=status]').textContent==='邮箱登录尚未开放');
    await page.evaluate(async()=>{window.__resolveOldLoginRetry({login_methods:{email:true},checkout_ready:true});await new Promise(requestAnimationFrame);});
    await openEmailAuth(page);
    await page.waitForFunction(()=>document.querySelector('#emailLoginHint').textContent.includes('检查服务配置'));
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'same-draft@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#subscriptionServiceGroup [role=status]').textContent(),'邮箱登录尚未开放');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('unconfigured first launch has an editable account form and accessible folded service settings',async()=>{
  const page=await open();
  try {
    await openSubscription(page,false,false);
    assert.equal(await page.locator('#subscriptionAccountGroup').count(),1);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('first@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),true);
    await closeEmailAuth(page);
    assert.equal(await page.locator('#accountConnectionBtn').count(),0);
    await page.locator('#subscriptionTools > summary').click();
    await page.locator('#subscriptionConnectionDetails > summary').click();
    assert.equal(await page.locator('#subscriptionServiceGroup details').evaluate(node=>node.open),true);
    await page.locator('#subscription_server_url').focus();
    assert.equal(await page.locator('#subscription_server_url').evaluate(node=>document.activeElement===node),true);
    await openEmailAuth(page);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'first@example.com');
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_start'),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for (const width of [1280,560]) {
  test(`email login dialog traps keyboard focus and restores its trigger at ${width}px`,async()=>{
    const page=await open({paymentServer:'https://pay.example.com'},{width,height:800});
    try {
      await openSubscription(page);
      await openEmailAuth(page);
      assert.equal(await page.getByRole('dialog').count(),1,'登录有明确的对话框语义');
      assert.equal(await page.locator('.app').evaluate(node=>node.inert),true,'弹窗打开时页内控件不能参与交互');
      await page.waitForFunction(()=>document.activeElement?.id==='loginEmailInput');
      await page.locator('#accountAuthClose').focus();
      await page.keyboard.press('Shift+Tab');
      for (let i=0;i<8;i++) {
        assert.equal(await page.evaluate(()=>Boolean(document.activeElement?.closest('#accountAuthModal'))),true,'Tab 焦点保持在登录弹窗内');
        await page.keyboard.press('Tab');
      }
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const bounds=await page.getByRole('dialog').boundingBox();
      assert(bounds.x>=0 && bounds.x+bounds.width<=width,'窄窗中的弹窗主体完整可见');
      await page.keyboard.press('Escape');
      await page.waitForFunction(()=>!document.querySelector('#accountAuthModal'));
      assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);
      assert.equal(await page.evaluate(()=>document.activeElement?.id),'accountEmailLoginBtn','Esc 后恢复登录入口焦点');
      assert.deepEqual(page.errors,[]);
    } finally {await page.close();}
  });
}

test('canceling the code step clears the OTP while preserving its email draft and resend cooldown',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    await page.clock.install();
    await openEmailAuth(page);
    await sendEmailCode(page,'draft@example.com');
    await page.locator('#loginCodeInput').fill('654321');
    await closeEmailAuth(page);
    await openEmailAuth(page);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'draft@example.com');
    assert.equal(await page.locator('#loginEmailInput').isVisible(),false,'重开可继续已提交的验证码步骤');
    assert.equal(await page.locator('#loginCodeInput').isVisible(),true);
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'','取消时不保留验证码');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true,'关闭弹窗不绕过服务器重发冷却');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='email_login_start').length,1,'重开不会自动发码');
    await page.clock.fastForward(60001);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('returning to the email step changes the next recipient only after an explicit submit',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await openSubscription(page);
    await page.clock.install();
    await openEmailAuth(page);
    await sendEmailCode(page);
    await page.locator('#authBackBtn').click();
    assert.equal(await page.locator('#loginEmailInput').isVisible(),true);
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    await page.locator('#loginEmailInput').fill('changed@example.com');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).filter(call=>call==='email_login_start').length,1);
    await page.clock.fastForward(60001);
    await page.locator('#sendCodeBtn').click();
    await page.waitForSelector('#loginCodeInput:visible');
    const recipients=await page.evaluate(()=>window.__fixtureCallArgs.filter(call=>call[0]==='email_login_start').map(call=>call[1]));
    assert.deepEqual(recipients,['user@example.com','changed@example.com']);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});


test('Enter submits only the visible step and a pasted six-digit OTP is accepted without disclosure',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  const messages=[];
  page.on('console',message=>messages.push(message.text()));
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_start;
      window.__MOCK__.email_login_start=async(...args)=>{const result=await original(...args);delete result.dev_code;return result;};
    });
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('user@example.com');
    await page.locator('#loginEmailInput').press('Enter');
    await page.waitForSelector('#loginCodeInput:visible');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_verify'),false,'第一步 Enter 只请求发送');
    await page.locator('#loginCodeInput').focus();
    await page.keyboard.insertText('123456');
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'123456','整段六位文本输入不会被拆分丢失');
    await page.locator('#loginCodeInput').evaluate(node=>{
      node.value='';node.dispatchEvent(new Event('input',{bubbles:true}));
      const clipboard=new DataTransfer();clipboard.setData('text/plain','12 34\n56');
      node.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:clipboard}));
    });
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'123456','粘贴处理会移除空格和换行，不碰系统剪贴板');
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('email_login_verify'),false,'填写或粘贴验证码不会自动验证');
    await page.locator('#loginCodeInput').press('Enter');
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent.includes('已登录'));
    const verify=await page.evaluate(()=>window.__fixtureCallArgs.filter(call=>call[0]==='email_login_verify'));
    assert.deepEqual(verify,[['email_login_verify','user@example.com','123456','login']]);
    assert(!(await page.locator('#toast').textContent()).includes('123456'));
    assert(messages.every(message=>!message.includes('123456')),'验证码不写入控制台');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('a failed send stays on the email step and permits an explicit retry',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_start;window.__sendAttempts=0;
      window.__MOCK__.email_login_start=(...args)=>++window.__sendAttempts===1 ? {ok:false,error:'邮件暂时无法发送'} : original(...args);
    });
    await openSubscription(page);
    await openEmailAuth(page);
    await page.locator('#loginEmailInput').fill('retry@example.com');
    await page.locator('#sendCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountAuthError').textContent.includes('邮件暂时无法发送'));
    assert.equal(await page.locator('#loginEmailInput').isVisible(),true);
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'retry@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),false);
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token || ''),'');
    await page.locator('#sendCodeBtn').click();
    await page.waitForSelector('#loginCodeInput:visible');
    assert.equal(await page.evaluate(()=>window.__sendAttempts),2);
    assert.equal(await page.locator('#accountAuthError').textContent(),'');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('an invalid OTP reports inline without signing in and can be corrected in the same dialog',async()=>{
  const page=await open({paymentServer:'https://pay.example.com'});
  try {
    await page.evaluate(()=>{
      const original=window.__MOCK__.email_login_verify;window.__verifyAttempts=0;
      window.__MOCK__.email_login_verify=(...args)=>++window.__verifyAttempts===1 ? {ok:false,error:'验证码不正确，请重试'} : original(...args);
    });
    await openSubscription(page);
    await openEmailAuth(page);
    await sendEmailCode(page);
    await page.locator('#loginCodeInput').fill('111111');
    await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountAuthError').textContent.includes('验证码不正确'));
    assert.equal(await page.locator('#accountAuthModal').getAttribute('aria-hidden'),'false');
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token || ''),'');
    assert.equal(await page.locator('#verifyCodeBtn').isDisabled(),false);
    assert(!(await page.locator('#toast').textContent()).includes('登录成功'));
    await page.locator('#loginCodeInput').fill('123456');
    await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent.includes('已登录'));
    assert.equal(await page.evaluate(()=>window.__verifyAttempts),2);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('logout clears email, code and binding intent before the next login',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',loggedIn:true});
  try {
    await openSubscription(page);
    await openEmailAuth(page,'bind');
    await sendEmailCode(page,'bound@example.com');
    await closeEmailAuth(page);
    await page.locator('#accountLogoutBtn').click();
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent==='未登录');
    await openEmailAuth(page);
    assert.equal(await page.locator('#loginEmailInput').isVisible(),true);
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'');
    assert.equal(await page.locator('#loginCodeInput').inputValue(),'');
    assert.equal(await page.locator('#loginCodeInput').isVisible(),false);
    assert.equal(await page.locator('#accountBindEmailBtn').count(),0);
    assert.equal(await page.locator('#verifyCodeBtn').textContent(),'登录');
    await page.locator('#loginEmailInput').fill('bound@example.com');
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true,'退出不绕过已有的邮件重发冷却');
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('test channel annual placeholder cannot change confirmed monthly price, credits or checkout',async()=>{
  const page=await open({releaseChannel:'test',paymentServer:'https://pay.example.com',serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'19.80',{credits:500}),offer(12,'190.08',{credits:6000})])]});
  try{await openSubscription(page);const standard=page.locator('.plan-card').nth(1);
    assert.equal(await standard.locator('.plan-period').inputValue(),'month');assert.equal(await standard.locator('option[value=year]').isDisabled(),true);
    assert.equal(await standard.locator('option[value=year]').textContent(),'年付（敬请期待）');
    await standard.locator('.plan-period').evaluate(node=>{node.value='year';node.dispatchEvent(new Event('change',{bubbles:true}));});
    assert.equal(await standard.locator('.plan-period').inputValue(),'month');assert.equal(await standard.locator('.plan-price').textContent(),'¥19.80/月');
    assert.equal(await standard.locator('.plan-selected-quota').textContent(),'本次 500 积分');
    await standard.locator('.btn').click();await page.waitForFunction(()=>__fixtureCallArgs.some(call=>call[0]==='begin_subscription' && call[2]==='month'));
    assert.equal((await page.evaluate(()=>__fixtureCallArgs)).some(call=>call[0]==='begin_subscription' && call[2]==='year'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('formal channel never exposes an annual choice even if legacy catalog includes one',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceReady:true,publicCatalog:[catalog('standard',[offer(1,'19.80'),offer(12,'190.08')])]});
  try{await openSubscription(page);assert.equal(await page.locator('.plan-period').count(),0);assert.equal(await page.locator('.plan-annual').count(),0);
    assert.equal(await page.locator('.plan-card').nth(1).locator('.plan-price').textContent(),'¥19.80/月');
    assert.equal((await page.evaluate(()=>__fixtureCalls)).includes('begin_subscription'),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('draft monthly and annual amounts stay visible without an actionable period selection',async()=>{
  const page=await open({paymentServer:'https://pay.example.com',serviceReady:true,publicCatalog:[]});
  try {
    await openSubscription(page);
    assert.deepEqual(await page.locator('.plan-price').allTextContents(),['按服务商计费','¥19.80/月','¥49.80/月']);
    assert.deepEqual(await page.locator('.plan-annual').allTextContents(),[]);
    assert.equal(await page.locator('.plan-period').count(),0);
    for(const button of await page.locator('.plan-card .btn').all()) assert.equal(await button.isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('begin_subscription'),false);
    await page.evaluate(()=>AppApi.beginSubscription('standard'));
    assert((await page.evaluate(()=>window.__fixtureCallArgs)).some(call=>call[0]==='begin_subscription' && call[1]==='standard' && call[2]==='month'),'API兼容默认月付，仍由后端拒绝未开放购买');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const width of [1280,560]) {
  test(`account precedes accessible comparison table at ${width}px without page overflow`,async()=>{
    const page=await open({paymentServer:'https://pay.example.com'}, {width,height:800});
    try {
      await openSubscription(page);
      const order=await page.locator('#subscriptionPane > div > .set-group').evaluateAll(nodes=>nodes.map(node=>node.id));
      assert.deepEqual(order.slice(0,2),['subscriptionAccountGroup','subscriptionPlansGroup']);
      assert.equal(await page.getByRole('table',{name:'FoilPage 套餐价格、积分与功能对比'}).count(),1);
      assert.equal(await page.locator('.plan-comparison thead th[scope="col"]').count(),4);
      assert.equal(await comparisonCell(page,'本地模型转写','free').textContent(),'包含');
      assert.equal(await comparisonCell(page,'FoilPage 托管正文与标题','free').textContent(),'不包含');
      assert.equal(await comparisonCell(page,'FoilPage 托管正文与标题','advanced').textContent(),'包含');
      assert.equal(await comparisonCell(page,'FoilPage 托管正文与标题','advanced').locator('svg').count(),1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.equal(await page.locator('.plan-features').count(),0,'比较功能只显示一次');
      const geometry=await page.locator('.plan-card').evaluateAll(nodes=>nodes.map(node=>({
        height:node.getBoundingClientRect().height, actionBottom:node.querySelector('.btn').getBoundingClientRect().bottom,
      })));
      assert(Math.max(...geometry.map(item=>item.height))-Math.min(...geometry.map(item=>item.height))<1,'方案表头适应同一行最高内容');
      assert(Math.max(...geometry.map(item=>item.actionBottom))-Math.min(...geometry.map(item=>item.actionBottom))<1,'方案按钮保持同一基线');
      if(width===560) {
        const scroller=page.locator('.plan-comparison-scroll');
        assert.equal(await scroller.evaluate(node=>node.scrollWidth>node.clientWidth),true);
        await scroller.focus(); await page.keyboard.press('End');
        await scroller.evaluate(node=>{node.scrollLeft=node.scrollWidth;});
        const before=await scroller.evaluate(node=>node.scrollLeft);
        assert(before>0,'窄窗可横向查看专业版');
        await page.getByRole('button',{name:'检查连接',exact:true}).evaluate(node=>node.onclick());
        assert.equal(await scroller.evaluate(node=>node.scrollLeft),before,'刷新能力不重置横向比较位置');
      }
      await page.screenshot({path:path.join(shots,`comparison-${width}.png`),fullPage:true});
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}
