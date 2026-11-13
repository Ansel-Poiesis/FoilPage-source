// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';

const root=path.resolve(import.meta.dirname,'..');
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},width=1280){
  const page=await browser.newPage({viewport:{width,height:800}});
  page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.com',...options});
  // Fixture navigation is not a cold font/file-load performance benchmark;
  // controls and business checks still keep their separate 5s timeout.
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded',timeout:30000});
  await page.locator('.nav-row[data-page="subscription"]').click();
  await page.waitForSelector('#accountEmailLoginBtn');
  await page.evaluate(()=>{document.querySelector('#subscriptionTools').open=true;document.querySelectorAll('#subscriptionTools details').forEach(node=>node.open=true);});
  return page;
}
async function verify(page, intent='login'){
  await page.locator(intent==='bind' ? '#accountBindEmailBtn' : '#accountEmailLoginBtn').click();
  await page.waitForSelector('#accountAuthModal[aria-hidden="false"]',{state:'attached'});
  await page.locator('#loginEmailInput').fill('user@example.com');
  await page.locator('#sendCodeBtn').click();
  await page.waitForSelector('#loginCodeInput:visible');
  assert.equal(await page.locator('#loginCodeInput').inputValue(),'123456');
  await page.locator('#verifyCodeBtn').click();
}
async function remove(page,id){
  await page.locator(`[data-device-id="${id}"]`).click();
  await page.locator('#confirmUnbindDevice').click();
}

test('device management shows current device and requires an explicit confirmation',async()=>{
  const page=await open({loggedIn:true});
  try{
    await page.waitForSelector('[data-device-id="device-old"]');
    assert((await page.locator('#accountDevicesList').textContent()).includes('当前设备'));
    assert((await page.locator('#accountDevicesList').textContent()).includes('首次登录 2026-10-01'));
    await page.locator('[data-device-id="device-old"]').click();
    await page.locator('#dlgCancel').click();
    assert(!(await page.evaluate(()=>window.__fixtureCalls)).includes('unbind_account_device'));
    await remove(page,'device-old');
    await page.waitForFunction(()=>document.querySelectorAll('#accountDevicesList .set-row').length===1);
    assert((await page.locator('#accountState').textContent()).includes('已登录'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('removing current device signs out after server confirmation',async()=>{
  const page=await open({loggedIn:true});
  try{
    await page.waitForSelector('[data-device-id="device-fixture"]');
    await remove(page,'device-fixture');
    await page.waitForFunction(()=>document.querySelector('#accountState').textContent==='未登录');
    assert.equal(await page.locator('#accountDevicesList').count(),0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('failed device removal keeps the device and does not show success',async()=>{
  const page=await open({loggedIn:true,unbindFail:true});
  try{
    await page.waitForSelector('[data-device-id="device-old"]');
    await remove(page,'device-old');
    await page.waitForFunction(()=>document.querySelector('#accountDevicesHint').textContent.includes('失败'));
    assert.equal(await page.locator('#accountDevicesList .set-row').count(),2);
    assert(!(await page.locator('#toast').textContent()).includes('设备已解绑'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const width of [1280,560]){
  test(`third device can free one slot then finish verified login at ${width}px`,async()=>{
    const page=await open({deviceLimit:true},width);
    try{
      await verify(page);
      await page.waitForSelector('[data-device-id="device-old"]');
      assert.equal(await page.locator('#accountAuthModal').count(),0,'设备管理接管时关闭登录弹窗');
      assert.equal(await page.locator('.app').evaluate(node=>node.inert),false,'设备管理需要可以继续交互');
      assert.equal(await page.locator('#completeAccountLoginBtn').isDisabled(),true);
      assert.equal(await page.locator('#accountState').textContent(),'未登录');
      await remove(page,'device-old');
      await page.waitForFunction(()=>document.querySelector('#completeAccountLoginBtn').disabled===false);
      const shots=path.join(root,'_webview_ui_shots','account-devices');fs.mkdirSync(shots,{recursive:true});
      await page.screenshot({path:path.join(shots,`device-limit-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.locator('#completeAccountLoginBtn').click();
      await page.waitForFunction(()=>document.querySelector('#accountState').textContent.includes('设备用户'));
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}

test('canceling a full-device login preserves the original account',async()=>{
  const page=await open({loggedIn:true,deviceLimit:true});
  try{
    await verify(page);
    await page.waitForSelector('#cancelAccountLoginBtn:not([hidden])');
    await page.locator('#cancelAccountLoginBtn').click();
    await page.waitForFunction(()=>window.__fixtureCalls.includes('cancel_account_login'));
    assert((await page.locator('#accountState').textContent()).includes('已登录'));
    assert.equal(await page.locator('#cancelAccountLoginBtn:not([hidden])').count(),0);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('email login and account binding have separate explicit intents',async()=>{
  const page=await open({loggedIn:true});
  try{
    assert.equal(await page.locator('#emailLoginIntent').count(),0,'登录与绑定使用独立入口，不共享用途下拉框');
    assert.equal(await page.locator('#accountEmailLoginBtn').textContent(),'切换账户');
    assert.equal(await page.locator('#accountBindEmailBtn').isVisible(),true);
    await verify(page,'bind');
    await page.waitForFunction(()=>window.__fixtureCalls.includes('email_login_verify'));
    const args=await page.evaluate(()=>window.__fixtureCallArgs.find(call=>call[0]==='email_login_verify'));
    assert.equal(args[3],'bind');
    assert((await page.locator('#toast').textContent()).includes('邮箱已绑定'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('email resend uses the server cooldown and retries only when it expires',async()=>{
  const page=await open({emailResendAfter:60});
  try{
    await page.clock.install();
    await page.locator('#accountEmailLoginBtn').click();
    await page.locator('#loginEmailInput').fill('user@example.com');
    await page.locator('#sendCodeBtn').click();
    await page.waitForFunction(()=>document.querySelector('#sendCodeBtn').textContent.includes('秒后重发'));
    await page.clock.fastForward(5001);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),true);
    await page.clock.fastForward(55001);
    assert.equal(await page.locator('#sendCodeBtn').isDisabled(),false);
    assert.equal(await page.locator('#sendCodeBtn').textContent(),'重发验证码');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
