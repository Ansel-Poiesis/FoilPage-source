// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={}){
  const page=await browser.newPage({viewport:{width:1024,height:700}});page.setDefaultTimeout(6000);
  page.errors=[];page.on('pageerror',e=>page.errors.push(e.message));
  await page.route('https://**/*',r=>r.abort());
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.invalid',termsAccepted:false,...options});
  await page.goto(pathToFileURL(path.resolve('app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=subscription]').click();await page.waitForSelector('#accountEmailLoginBtn');return page;
}
const auth=async page=>{await page.locator('#accountEmailLoginBtn').click();await page.locator('#loginEmailInput').fill('consent@example.com');};
test('a single consent precedes every first OTP; Enter and direct handlers cannot bypass',async()=>{
  const page=await open();try{
    await auth(page);assert(await page.locator('#sendCodeBtn').isDisabled());
    await page.locator('#loginEmailInput').press('Enter');
    await page.evaluate(()=>document.querySelector('#sendCodeBtn').onclick());
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='email_login_start').length),0);
    await page.locator('#termsConsent').check();await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    assert.equal(await page.locator('#crossBorderConsent').count(),0,'TASK-8.32：境外同意已并入单勾选');
    await page.locator('#sendCodeBtn').click();await page.locator('#loginCodeInput').waitFor({state:'visible'});
    assert.equal(await page.evaluate(()=>AppState.settings.terms_accepted_version),2);
    assert.equal(await page.evaluate(()=>AppState.settings.terms_cross_border_accepted_version),2),'单勾选仍写双字段';
    assert.equal(await page.evaluate(()=>AppState.settings.terms_accepted_service),'https://pay.example.invalid');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('the full bundled terms open inside the app offline, then return to auth without losing focus',async()=>{
  const page=await open();try{
    await auth(page);const link=page.locator('.auth-terms .terms-link').filter({hasText:'用户须知'});
    await link.click();const frame=page.frameLocator('.public-terms-dialog iframe');
    await frame.locator('#user-notice').waitFor();
    assert.match(await frame.locator('body').textContent(),/Cloudflare, Inc./);
    assert.match(await frame.locator('body').textContent(),/历史已购的软件解锁费如需退款联系客服核对/);
    assert.equal(await frame.locator('script,form').count(),0);
    await page.locator('.public-terms-dialog button').click();
    assert(await link.evaluate(n=>n===document.activeElement));
    assert.equal(await page.locator('#accountAuthModal').count(),1);
    assert(await page.locator('.auth-dialog').isVisible());assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('same service/version preserves explicit confirmation; another service and a newer server version invalidate it',async()=>{
  const page=await open({termsAccepted:true});try{
    await auth(page);await page.waitForFunction(()=>!document.querySelector('#sendCodeBtn').disabled);
    assert(await page.locator('#termsConsent').isChecked());
    await page.evaluate(()=>{PublicTerms.observe({terms:{version:3}});AccountAuth.refresh();});
    assert(await page.locator('#sendCodeBtn').isDisabled());
    await page.locator('#accountAuthClose').click();await auth(page);
    assert.match(await page.locator('.auth-terms').textContent(),/升级 FoilPage/);
    assert(!(await page.locator('#termsConsent').isChecked()));
    await page.locator('#accountAuthClose').click();
    await page.evaluate(()=>{AppState.settings.subscription_server_url='https://other.example.invalid';Subscription.render();});
    await auth(page);assert(!(await page.locator('#termsConsent').isChecked()));assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('old signed-in accounts cannot redeem until confirmed; cancelling makes no card request',async()=>{
  const page=await open({loggedIn:true});try{
    await page.locator('#betaCardCode').fill('sjbeta_'+'a'.repeat(64));
    await page.locator('#redeemBetaCardBtn').click();await page.locator('.terms-confirm-dialog').waitFor();
    assert(await page.getByRole('button',{name:'同意并继续'}).isDisabled());
    await page.getByRole('button',{name:'取消',exact:true}).click();
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==='redeem_beta_card').length),0);
    assert.match(await page.locator('#subscriptionTermsNotice').textContent(),/2029774968/);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

// ---------------------------------------------------------------- TASK-8.32
// 登录输入记忆：发送验证码成功后邮箱写入缓存，下次打开登录框自动回填。
test('login email is remembered across dialog opens after a code send',async()=>{
  const page=await open();try{
    await auth(page);
    await page.locator('#termsConsent').check();
    await page.locator('#loginEmailInput').fill('remembered@example.invalid');
    await page.evaluate(()=>{window.__sendResult={ok:true,resend_after_seconds:60,dev_code:'123456'};__MOCK__.email_login_start=(email)=>{window.__sentTo=email;return window.__sendResult;};});
    await page.locator('#sendCodeBtn').click();
    await page.locator('#loginCodeInput').waitFor({state:'visible'});
    assert.equal(await page.evaluate(()=>window.__sentTo),'remembered@example.invalid');
    await page.evaluate(()=>{window.__MOCK__.email_login_start=()=>({ok:true,resend_after_seconds:60});});
    // 关闭再重开（模拟下次登录），邮箱已回填
    await page.locator('#accountAuthClose').click();
    await page.evaluate(()=>Subscription.openLogin({}));
    assert.equal(await page.locator('#loginEmailInput').inputValue(),'remembered@example.invalid','邮箱从缓存回填');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
