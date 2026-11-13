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
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.com',loggedIn:true,buyerRequired:true,buyerConfigured:true,...options});
  // Fixture navigation is not a cold font/file-load performance benchmark;
  // controls and business checks still keep their separate 5s timeout.
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded',timeout:30000});
  await page.locator('.nav-row[data-page="subscription"]').click();
  await page.waitForSelector('#accountEmailLoginBtn');
  await page.evaluate(()=>{document.querySelector('#subscriptionTools').open=true;document.querySelectorAll('#subscriptionTools details').forEach(node=>node.open=true);});
  await page.waitForSelector('#verifyBuyerBtn');
  return page;
}

test('required buyer identity keeps redemption closed before verified authorization',async()=>{
  const page=await open();
  try{
    await page.waitForSelector('#buyerVerificationGroup:not([hidden])');
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
    await page.locator('#redeemOrderInput').fill('fixture-order');
    await page.evaluate(()=>document.querySelector('#redeemOrderBtn').onclick());
    assert(!(await page.evaluate(()=>window.__fixtureCalls)).includes('redeem_subscription_order'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('buyer verification preserves the current account and opens its redemption gate',async()=>{
  const page=await open();
  try{
    await page.locator('#verifyBuyerBtn').click();
    await page.waitForFunction(()=>document.querySelector('#verifyBuyerBtn').textContent==='已验证');
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),false);
    assert((await page.locator('#accountState').textContent()).includes('微信用户'));
    assert((await page.locator('#buyerVerificationHint').textContent()).includes('账户保持不变'));
    await page.locator('#redeemOrderInput').fill('fixture-order');await page.locator('#redeemOrderBtn').click();
    await page.waitForFunction(()=>window.__fixtureCalls.includes('redeem_subscription_order'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('cancelling a pending buyer authorization keeps login and redemption stays closed',async()=>{
  const page=await open({buyerPendingPolls:10});
  try{
    await page.locator('#verifyBuyerBtn').click();
    await page.locator('#cancelBuyerVerificationBtn').click();
    await page.waitForFunction(()=>window.__fixtureCalls.includes('buyer_verification_stop'));
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
    assert((await page.locator('#accountState').textContent()).includes('已登录'));
    assert((await page.locator('#toast').textContent()).includes('已取消'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('unconfigured buyer verification explains the gate without launching OAuth',async()=>{
  const page=await open({buyerConfigured:false});
  try{
    assert.equal(await page.locator('#verifyBuyerBtn').isDisabled(),true);
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
    assert((await page.locator('#buyerVerificationHint').textContent()).includes('尚未开放'));
    assert(!(await page.evaluate(()=>window.__fixtureCalls)).includes('buyer_verification_start'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a signed-in account already verified by the service can redeem without reauthorizing',async()=>{
  const page=await open({buyerVerified:true});
  try{
    await page.waitForFunction(()=>document.querySelector('#verifyBuyerBtn').textContent==='已验证');
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),false);
    assert.equal(await page.locator('#verifyBuyerBtn').isDisabled(),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('failed buyer verification never opens redemption',async()=>{
  const page=await open({buyerPollFail:true});
  try{
    await page.locator('#verifyBuyerBtn').click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('未完成'));
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
    assert.equal(await page.locator('#verifyBuyerBtn').isDisabled(),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('verified buyer still cannot redeem while payments are intentionally closed',async()=>{
  const page=await open({buyerVerified:true,paymentsEnabled:false});
  try{
    await page.waitForFunction(()=>document.querySelector('#verifyBuyerBtn').textContent==='已验证');
    assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
    assert((await page.locator('#redeemOrderBtn').getAttribute('title')).includes('尚未开放'));
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const width of [1280,560]){
  test(`buyer identity panel stays legible and keeps its gate at ${width}px`,async()=>{
    const page=await open({},width);
    try{
      await page.locator('#buyerVerificationGroup').scrollIntoViewIfNeeded();
      assert.equal(await page.locator('#redeemOrderBtn').isDisabled(),true);
      const shots=path.join(root,'_webview_ui_shots','buyer-verification');fs.mkdirSync(shots,{recursive:true});
      await page.screenshot({path:path.join(shots,`buyer-pending-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
  test(`paused order review shows retained balance and actual hosted pause at ${width}px`,async()=>{
    const page=await open({buyerVerified:true,orderReview:true,settings:{agent_billing_mode:'hosted'}},width);
    try{
      await page.waitForSelector('#orderReviewNotice:not([hidden])');
      const notice=await page.locator('#orderReviewNotice').textContent();
      assert(notice.includes('托管已暂停')&&notice.includes('积分记录保留'));
      assert((await page.locator('#accountSummary').textContent()).includes('保留 500 积分'));
      assert((await page.locator('#accountState').textContent()).includes('已登录'));
      assert(!(await page.locator('#orderReviewNotice').textContent()).includes('现金已退款'));
      const shots=path.join(root,'_webview_ui_shots','buyer-verification');fs.mkdirSync(shots,{recursive:true});
      await page.locator('#orderReviewNotice').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(shots,`review-paused-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}
