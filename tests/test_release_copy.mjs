// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Signed engine status -> rendered UI; public synthetic keys and local fixture only. */
import test,{before,after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
const shots=path.join(process.env.SHENGJIAN_UI_AUDIT_DIR || path.join(root,'.ui-audit','release-current','ui'),'contracts');
const localPython=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const python=process.env.SHENGJIAN_TEST_PYTHON || (fs.existsSync(localPython)?localPython:'python');
const source=`
import base64,json
from datetime import datetime,timedelta,timezone
from engine import ed25519
from engine.license import sign_license
from engine.subscription import status
now=datetime.now(timezone.utc)
seed=bytes(range(32))
public=base64.b64encode(ed25519.public_key(seed)).decode()
def state(hours,beta=False,review=False,refreshed=False):
    expires=(now+timedelta(hours=hours)).isoformat()
    claims={"sub":"copy-fixture","device_id":"copy-device","iat":now.timestamp(),"exp":now.timestamp()+900,
        "plan_id":"free" if refreshed else "standard","credit_balance":498,"max_concurrency":2,
        "subscription_expires_at":expires,
        "beta_access":{"version":1,"active":beta,"unlimited_credits":beta,"expires_at":None,"revoked":False},
        "order_review":{"paused":review,"count":1 if review else 0}}
    settings={"account_user_id":"copy-fixture","account_login_token":"synthetic-session",
        "subscription_license_public_key":public,"license_token":sign_license(claims,signing_key=base64.b64encode(seed).decode(),alg="EdDSA"),
        "subscription_plan":"free" if refreshed else "standard","subscription_active":not refreshed,"credit_balance":498,
        "subscription_expires_at":None if refreshed else expires,"subscription_server_url":"https://copy.example.invalid",
        "subscription_expiry_notice_history":{"account":"copy-fixture","service":"https://copy.example.invalid","expires_at":expires} if refreshed else None}
    return status(settings,now=now,device_id="copy-device")
print(json.dumps({"expired":state(-2),"refreshed":state(-2,refreshed=True),"hours":state(6),"soon":state(50),"far":state(100),"beta":state(-2,beta=True),"review":state(-2,review=True)},ensure_ascii=False))
`;
let browser,states;
before(async()=>{
  const result=spawnSync(python,['-X','utf8','-c',source],{cwd:root,encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);states=JSON.parse(result.stdout);
  assert.equal(states.expired.active,false,'the real engine expires paid access before rendering');
  assert.equal(states.beta.unlimited_credits,true,'beta comes from a verified synthetic Ed25519 license');
  assert.equal(states.refreshed.plan_id,'free');assert.equal(states.refreshed.expires_at,null);
  fs.mkdirSync(shots,{recursive:true});fs.writeFileSync(path.join(shots,'engine-status.json'),JSON.stringify(states,null,2));
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();});
async function open(status,options={}){
  const page=await browser.newPage({viewport:{width:560,height:700},reducedMotion:'reduce'});
  page.setDefaultTimeout(5000);page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{loggedIn:true,paymentServer:'https://copy.example.invalid',scale:1.3,...options});
  await page.addInitScript(status=>{__MOCK__.get_subscription_status=()=>status;},status);
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.nav-row[aria-current=page]');return page;
}
for(const [name,expected] of [['expired',/云端套餐已到期/],['refreshed',/云端套餐已到期/],['hours',/24 小时内/],['soon',/2 天后/]]){
  test(`real signed ${name} status preserves an accurate cloud expiry notice`,async()=>{
    const page=await open(states[name]);try{
      await page.locator('[data-page=subscription]').click();
      await page.waitForFunction(()=>document.querySelector('#planExpiryNotice')?.textContent.length>0);
      const notice=page.locator('#planExpiryNotice');assert(await notice.isVisible());
      assert.match(await notice.textContent(),expected);assert.match(await notice.textContent(),/软件使用权单独计算/);
      assert.doesNotMatch(await notice.textContent(),/0 天后|本地转写不受影响/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(shots,'cloud-expiry-'+name+'.png')});
      assert.deepEqual(page.errors,[]);
    }finally{await page.close();}
  });
}
for(const name of ['far','beta','review'])test(`real signed ${name} status does not show a misleading renewal reminder`,async()=>{
  const page=await open(states[name]);try{
    await page.locator('[data-page=subscription]').click();
    await page.waitForFunction(()=>document.querySelector('#accountSummary')?.textContent.includes('498') || document.querySelector('#accountSummary')?.textContent.includes('∞') || !document.querySelector('#orderReviewNotice')?.hidden);
    assert(await page.locator('#planExpiryNotice').isHidden());
    if(name==='beta')assert.match(await page.locator('#accountSummary').textContent(),/∞/);
    if(name==='review')assert(await page.locator('#orderReviewNotice').isVisible());
    await page.screenshot({path:path.join(shots,'cloud-expiry-'+name+'.png')});
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('short browser installation windows retain step instructions and a reachable diagram',async()=>{
  const page=await open(states.far);try{
    await page.setViewportSize({width:560,height:500});await page.locator('[data-page=browser]').click();
    await page.locator('.browser-install-steps button').first().click();
    await page.locator('.tool-details summary').click();
    const readingHeight=await page.locator('.browser-install-content').evaluate(node=>node.getBoundingClientRect().height);
    assert(readingHeight>=160,`fixed controls cannot collapse the reading area (${readingHeight}px)`);
    await page.locator('#browserStepDescription').scrollIntoViewIfNeeded();assert(await page.locator('#browserStepDescription').isVisible());
    await page.screenshot({path:path.join(shots,'browser-instructions-560x500.png')});
    await page.locator('#browserGuideZoom').click();assert(await page.locator('.browser-pairing-large').isVisible());
    await page.keyboard.press('Escape');await page.locator('#browserNext').click();
    assert.equal(await page.locator('#browserStepCount').textContent(),'2 / 3');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('browser guide describes an explicit F5 retry without promising every failure is fixed',async()=>{
  const page=await open(states.far);try{
    await page.evaluate(()=>Onboarding.show({chapter:'browser',resume:false}));
    for(let step=0;step<3;step++)await page.keyboard.press('ArrowRight');
    const copy=await page.locator('.ob-card').textContent();
    assert.match(copy,/F5/);assert.match(copy,/等待画面加载后重试/);assert.match(copy,/任务页查看具体原因/);
    assert.doesNotMatch(copy,/即可恢复|继续顺畅下载/);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
