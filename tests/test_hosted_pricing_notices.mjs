// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 标题优化计费确认与托管模型推荐：隔离的设置页交互，无原生、模型、网络或用户数据。 */
import assert from 'node:assert/strict';
import {after,before,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..');
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});

async function openAgent(settings) {
  const page=await browser.newPage({viewport:{width:1280,height:860},reducedMotion:'reduce'});
  page.setDefaultTimeout(6000); page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{hostedReady:true,settings});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=settings]').click();
  await page.locator('.sitem[data-section=agent]').click();
  return page;
}
const saved=page=>page.evaluate(()=>window.__fixtureCallArgs.filter(x=>x[0]==='save_settings').map(x=>x[1]));
const TITLE='[aria-label="AI 优化标题"]', MODE='select[aria-label="AI 调用方式"]', MODEL='select[aria-label="托管模型"]';
const settled=async(page,selector)=>{await page.getByRole('dialog').waitFor({state:'detached'});await page.waitForFunction(s=>{const el=document.querySelector(s);return el&&!el.disabled;},selector);};

test('托管下打开标题优化：未勾选不能确认，取消后开关保持关闭且不算失败', async()=>{
  const page=await openAgent({agent_billing_mode:'hosted'});
  try {
    const toggle=page.locator(TITLE);
    assert.equal(await toggle.getAttribute('aria-checked'),'false');
    assert.match(await page.locator('.set-row',{has:toggle}).innerText(),/标题优化每条转写另计 1 次基数积分/);
    assert.match(await page.locator('#hostedModelAdvice').innerText(),/推荐：日常整理用默认档/);
    await toggle.click();
    const dialog=page.getByRole('dialog'); await dialog.waitFor();
    // 2026-11-25：标题阶段固定 1 个基数（服务端已同步），不再说"与正文同费率、消耗翻倍"。
    assert.match(await dialog.innerText(),/每条固定 1 次基数/);
    assert.doesNotMatch(await dialog.innerText(),/翻倍|与正文相同的费率/);
    assert.equal((await page.locator('#titleBillingOk').innerText()).trim(),'确认并开启');
    assert.equal(await page.locator('#titleBillingOk').isDisabled(),true);
    await page.locator('#titleBillingAck').check();
    assert.equal(await page.locator('#titleBillingOk').isDisabled(),false);
    await page.locator('#dlgCancel').click();
    await settled(page,TITLE);
    assert.equal(await toggle.getAttribute('aria-checked'),'false');
    assert.equal(await toggle.getAttribute('aria-invalid'),null);
    assert.deepEqual((await saved(page)).filter(x=>'title_optimization_enabled' in x),[]);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('Esc 关闭视为取消；勾选后确认才保存开启', async()=>{
  const page=await openAgent({agent_billing_mode:'hosted'});
  try {
    await page.locator(TITLE).click(); await page.getByRole('dialog').waitFor();
    await page.keyboard.press('Escape');
    await settled(page,TITLE);
    assert.equal(await page.locator(TITLE).getAttribute('aria-checked'),'false');
    await page.locator(TITLE).click(); await page.getByRole('dialog').waitFor();
    await page.locator('#titleBillingAck').check(); await page.locator('#titleBillingOk').click();
    await page.getByRole('dialog').waitFor({state:'detached'});
    await page.waitForFunction(s=>document.querySelector(s).getAttribute('aria-checked')==='true',TITLE);
    assert.deepEqual((await saved(page)).filter(x=>'title_optimization_enabled' in x),[{title_optimization_enabled:true}]);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('自带密钥下打开不弹计费确认，只提示由模型服务商计费', async()=>{
  const page=await openAgent({agent_billing_mode:'byok'});
  try {
    const toggle=page.locator(TITLE);
    assert.match(await page.locator('.set-row',{has:toggle}).innerText(),/每条转写额外请求一次，由你的模型服务商计费/);
    await toggle.click();
    await page.waitForFunction(s=>document.querySelector(s).getAttribute('aria-checked')==='true',TITLE);
    assert.equal(await page.getByRole('dialog').count(),0);
    assert.deepEqual((await saved(page)).filter(x=>'title_optimization_enabled' in x),[{title_optimization_enabled:true}]);
  } finally {await page.close();}
});

test('标题优化已开启时切到托管：先确认，取消则调用方式保持不变', async()=>{
  const page=await openAgent({agent_billing_mode:'byok',title_optimization_enabled:true});
  try {
    await page.locator(MODE).selectOption('hosted');
    const dialog=page.getByRole('dialog'); await dialog.waitFor();
    assert.match(await dialog.innerText(),/已开启的标题优化会按积分计费/);
    assert.equal((await page.locator('#titleBillingOk').innerText()).trim(),'确认并切换');
    await page.locator('#dlgCancel').click();
    await settled(page,MODE);
    assert.equal(await page.locator(MODE).inputValue(),'byok');
    assert.equal(await page.locator(MODE).getAttribute('aria-invalid'),null);
    assert.deepEqual((await saved(page)).filter(x=>'agent_billing_mode' in x),[]);
    await page.locator(MODE).selectOption('hosted'); await page.getByRole('dialog').waitFor();
    await page.locator('#titleBillingAck').check(); await page.locator('#titleBillingOk').click();
    await page.waitForFunction(s=>document.querySelector(s)?.value==='hosted',MODE);
    assert.deepEqual((await saved(page)).filter(x=>'agent_billing_mode' in x),[{agent_billing_mode:'hosted'}]);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('托管模型推荐随所选模型变化', async()=>{
  const page=await openAgent({agent_billing_mode:'hosted'});
  try {
    await page.evaluate(()=>{const original=window.__MOCK__.get_subscription_status;
      window.__MOCK__.get_subscription_status=()=>({...original(),hosted_agent_models:[
        {model:'qwen-plus',label:'qwen-plus 忠实整理',cost_multiplier:1},{model:'deepseek-flash',label:'deepseek-flash 润色优化',cost_multiplier:3},
        {model:'qwen-turbo',label:'qwen-turbo 轻快整理',cost_multiplier:0.5},{model:'qwen-max',label:'qwen-max 重型整理',cost_multiplier:5}]});});
    await page.getByRole('button',{name:'检查托管服务'}).click();
    await page.waitForFunction(s=>{const el=document.querySelector(s);return el&&!el.disabled;},MODEL);
    assert.match(await page.locator('#hostedModelAdvice').innerText(),/推荐：日常整理用默认档/);
    assert.match(await page.locator('#hostedModelAdvice').innerText(),/turbo 只在转写超过 2000 字/);
    await page.locator(MODEL).selectOption('qwen-turbo');
    await page.waitForFunction(()=>document.querySelector('#hostedModelAdvice').textContent.includes('更短的两者同价'));
    await page.locator(MODEL).selectOption('deepseek-flash');
    await page.waitForFunction(()=>document.querySelector('#hostedModelAdvice').textContent.includes('4235 字约 19 秒'));
    assert.match(await page.locator(MODEL).locator('xpath=..').innerText(),/3 倍积分/);
    assert.deepEqual((await saved(page)).filter(x=>'agent_hosted_model' in x),[{agent_hosted_model:'qwen-turbo'},{agent_hosted_model:'deepseek-flash'}]);
    // 目录以服务端为准：未在架的档位不出现在推荐里（turbo/max 配额未开通时服务端会摘下）。
    await page.evaluate(()=>{const original=window.__MOCK__.get_subscription_status;
      window.__MOCK__.get_subscription_status=()=>({...original(),hosted_agent_models:[
        {model:'qwen-plus',label:'qwen-plus 忠实整理',cost_multiplier:1},{model:'deepseek-flash',label:'deepseek-flash 润色优化',cost_multiplier:3}]});});
    await page.getByRole('button',{name:'检查托管服务'}).click();
    await page.locator(MODEL).selectOption('');
    await page.waitForFunction(()=>{const text=document.querySelector('#hostedModelAdvice').textContent;return text.includes('deepseek-flash')&&!text.includes('turbo');});
    assert.equal(await page.locator(MODEL).locator('option').count(),3);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});

test('所选托管档位已被服务端摘下时，自动回到默认档而不是每次整理都报不可用', async()=>{
  const page=await openAgent({agent_billing_mode:'hosted',agent_hosted_model:'qwen-turbo'});
  try {
    await page.evaluate(()=>{const original=window.__MOCK__.get_subscription_status;
      window.__MOCK__.get_subscription_status=()=>({...original(),hosted_agent_models:[
        {model:'qwen-plus',label:'qwen-plus 忠实整理',cost_multiplier:1},{model:'deepseek-flash',label:'deepseek-flash 润色优化',cost_multiplier:3}]});});
    await page.getByRole('button',{name:'检查托管服务'}).click();
    await page.waitForFunction(s=>document.querySelector(s)?.value==='',MODEL);
    await page.waitForFunction(()=>window.__fixtureCallArgs.filter(x=>x[0]==='save_settings').some(x=>x[1].agent_hosted_model===''));
    assert.equal(await page.locator(MODEL).locator('option').count(),3);
    assert.doesNotMatch(await page.locator(MODEL).locator('xpath=..').innerText(),/qwen-turbo/);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});
