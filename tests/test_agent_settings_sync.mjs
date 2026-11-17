// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Changed revisions use a real page and mocked local bridge, never a live profile. */
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';
const root=path.resolve(import.meta.dirname,'..'),initial='a'.repeat(64),next='b'.repeat(64),latest='c'.repeat(64);
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});after(async()=>{await browser?.close();});
async function open(){const page=await browser.newPage({viewport:{width:1280,height:800}});page.setDefaultTimeout(5000);page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{loggedIn:true,paymentServer:'https://pay.example.invalid',settings:{agent_settings_revision:initial,agent_model:'old-model',title_skill_prompt:'old rules'}});
  await page.addInitScript(()=>{const original=__MOCK__.get_settings;window.__externalReads=[];window.__externalSnapshot=null;__MOCK__.get_settings=()=>{__externalReads.push('read');return __externalSnapshot?{...original(),...__externalSnapshot}:original();};});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);await page.waitForSelector('.nav-row[aria-current=page]');await page.locator('[data-page=settings]').click();await page.locator('.sitem[data-section=agent]').click();return page;}
async function push(page,revision,changes={}){await page.evaluate(({revision,changes})=>{__externalSnapshot={agent_settings_revision:revision,...changes};window.__onState({agent_settings_revision:revision});},{revision,changes});await page.waitForFunction(revision=>AppState.settings.agent_settings_revision===revision,revision);}

async function prepareProviderSettings(page) {
  await page.evaluate(() => {
    Object.assign(AppState.settings, __MOCK__.save_settings({
      agent_provider: 'mimo', agent_base_url: 'https://api.xiaomimimo.com/v1',
      agent_api_key: 'synthetic-old-key',
    }));
    const original = __MOCK__.save_settings;
    __MOCK__.save_settings = changes => {
      const previous = __MOCK__.get_settings();
      const changedProvider = Object.hasOwn(changes, 'agent_provider') && changes.agent_provider !== previous.agent_provider;
      const changedEndpoint = Object.hasOwn(changes, 'agent_base_url') && changes.agent_base_url !== previous.agent_base_url;
      const cleared = (changedProvider || changedEndpoint) && !changes.agent_api_key;
      const saved = original(cleared ? {...changes, agent_api_key: ''} : changes);
      return {...saved, agent_key_cleared: cleared};
    };
    Settings.render();
    document.querySelectorAll('#settingsPane details').forEach(node => { node.open = true; });
  });
}

test('hosted catalog preserves every multiplier and updates the selected cost hint', async () => {
  const page = await open();
  try {
    await page.evaluate(async () => {
      __MOCK__.get_subscription_status = () => ({hosted_agent_ready: true, hosted_agent_models: [
        {model: 'qwen-turbo', cost_multiplier: 0.5},
        {model: 'qwen-plus', label: '忠实整理', cost_multiplier: 1},
        {model: 'deepseek-flash', cost_multiplier: 3},
        {model: 'qwen-max', cost_multiplier: 5},
      ]});
      await Settings.save({agent_billing_mode: 'hosted', agent_hosted_model: 'qwen-max'}, false);
      Settings.render();
    });
    const model = page.getByRole('combobox', {name: '托管模型', exact: true});
    await model.locator('option[value="qwen-max"]').waitFor({state: 'attached'});
    assert.deepEqual(await model.locator('option').allTextContents(), [
      '默认（服务端推荐）', 'qwen-turbo（0.5 倍积分）', '忠实整理（基础积分）',
      'deepseek-flash（3 倍积分）', 'qwen-max（5 倍积分）',
    ]);
    const row = model.locator('..').locator('..');
    assert.match(await row.innerText(), /每次整理消耗 5 倍积分/);
    await model.selectOption('qwen-turbo');
    await page.waitForFunction(() => AppState.settings.agent_hosted_model === 'qwen-turbo');
    assert.match(await row.innerText(), /每次整理消耗 0\.5 倍积分/);
    await page.setViewportSize({width: 560, height: 500});
    await page.evaluate(() => { AppState.settings.font_scale = 1.3; applyFontScale(); });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('provider changes synchronize the cleared key into state and the password control', async () => {
  const page = await open();
  try {
    await prepareProviderSettings(page);
    await page.getByRole('combobox', {name: '服务商', exact: true}).selectOption('deepseek');
    await page.waitForFunction(() => AppState.settings.agent_provider === 'deepseek');
    assert.equal(await page.evaluate(() => AppState.settings.agent_api_key), '');
    assert.equal(await page.locator('[data-tour="agent-key"]').inputValue(), '');
    assert.equal(await page.evaluate(() => Object.hasOwn(AppState.settings, 'agent_key_cleared')), false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('manual endpoint changes clear the stale key control and explain the required action', async () => {
  const page = await open();
  try {
    await prepareProviderSettings(page);
    const endpoint = page.getByRole('textbox', {name: '接口地址', exact: true});
    await endpoint.fill('https://new.example.invalid/v1');
    await endpoint.press('Enter');
    await page.waitForFunction(() => AppState.settings.agent_base_url === 'https://new.example.invalid/v1');
    assert.equal(await page.evaluate(() => AppState.settings.agent_api_key), '');
    assert.equal(await page.locator('[data-tour="agent-key"]').inputValue(), '');
    assert.match(await page.locator('.toast').innerText(), /API Key.*清空.*重新填写/);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('a queued new key is preserved across the preceding endpoint-clear response', async () => {
  const page = await open();
  try {
    await prepareProviderSettings(page);
    await page.evaluate(() => {
      window.__endpointSave = Settings.save({agent_base_url: 'https://new.example.invalid/v1'}, false);
      window.__newKeySave = Settings.save({agent_api_key: 'synthetic-new-key'}, false);
    });
    await page.evaluate(() => Promise.all([__endpointSave, __newKeySave]));
    assert.equal(await page.evaluate(() => AppState.settings.agent_api_key), 'synthetic-new-key');
    assert.equal(await page.evaluate(() => __MOCK__.get_settings().agent_api_key), 'synthetic-new-key');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('an unsaved new key survives endpoint confirmation and can be submitted afterwards', async () => {
  const page = await open();
  try {
    await prepareProviderSettings(page);
    await page.evaluate(() => {
      const original = __MOCK__.save_settings;
      __MOCK__.save_settings = changes => {
        const saved = original(changes);
        return changes.agent_base_url
          ? new Promise(resolve => { window.__confirmEndpoint = () => resolve(saved); })
          : saved;
      };
    });
    const endpoint = page.getByRole('textbox', {name: '接口地址', exact: true});
    await endpoint.fill('https://new.example.invalid/v1');
    await endpoint.press('Enter');
    await page.waitForFunction(() => typeof __confirmEndpoint === 'function');
    const key = page.locator('[data-tour="agent-key"]');
    await key.fill('synthetic-draft-key');
    await page.evaluate(() => __confirmEndpoint());
    await page.waitForFunction(() => AppState.settings.agent_api_key === '');
    assert.equal(await key.inputValue(), 'synthetic-draft-key');
    await key.press('Enter');
    await page.waitForFunction(() => AppState.settings.agent_api_key === 'synthetic-draft-key');
    assert.equal(await page.evaluate(() => __MOCK__.get_settings().agent_api_key), 'synthetic-draft-key');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
test('same or malformed revision does not read settings, while a changed revision updates clean controls once',async()=>{
  const page=await open();try{const baseline=await page.evaluate(()=>__externalReads.length);await page.evaluate(initial=>{__onState({agent_settings_revision:initial});__onState({agent_settings_revision:'x'});},initial);assert.equal(await page.evaluate(()=>__externalReads.length),baseline);
    await push(page,next,{agent_model:'agent-new-model'});assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'agent-new-model');
    await page.evaluate(next=>__onState({agent_settings_revision:next}),next);assert.equal(await page.evaluate(()=>__externalReads.length),baseline+1);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('external prompt change preserves the focused local draft, cursor and DOM until explicit refresh',async()=>{
  const page=await open();try{await page.locator('textarea[aria-label="标题附加规则"]').evaluate(node=>node.closest('details').open=true);const field=page.getByRole('textbox',{name:'标题附加规则',exact:true});await field.fill('my unsaved title rules');await field.evaluate(node=>{node.setSelectionRange(3,7);window.__draftNode=node;});
    await push(page,next,{title_skill_prompt:'agent title rules',agent_model:'agent model'});
    assert.equal(await field.inputValue(),'my unsaved title rules');assert.deepEqual(await field.evaluate(node=>[node.selectionStart,node.selectionEnd]),[3,7]);assert(await field.evaluate(node=>node===document.activeElement&&node===__draftNode));
    assert.equal(await page.evaluate(()=>AppState.settings.title_skill_prompt),'agent title rules');assert(await page.locator('#externalSettingsNotice').isVisible());
    await page.getByRole('button',{name:'更新显示',exact:true}).click();assert.equal(await field.inputValue(),'my unsaved title rules');assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'agent model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a newer push rejects the earlier pending snapshot',async()=>{
  const page=await open();try{await page.evaluate(()=>{window.__heldSettings=[];__MOCK__.get_settings=()=>new Promise(resolve=>__heldSettings.push(resolve));});
    await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>__heldSettings.length===1);
    await page.evaluate(latest=>__onState({agent_settings_revision:latest}),latest);await page.waitForFunction(()=>__heldSettings.length===2);
    await page.evaluate(latest=>__heldSettings[1]({...AppState.settings,agent_settings_revision:latest,agent_model:'newest'}),latest);await page.waitForFunction(latest=>AppState.settings.agent_settings_revision===latest,latest);
    await page.evaluate(next=>__heldSettings[0]({...AppState.settings,agent_settings_revision:next,agent_model:'stale'}),next);await page.waitForTimeout(20);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'newest');assert.equal(await page.evaluate(()=>AppState.settings.agent_settings_revision),latest);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a settings response from a departed account cannot replace current settings',async()=>{
  const page=await open();try{await page.evaluate(()=>{__MOCK__.get_settings=()=>new Promise(resolve=>{window.__releaseOldAccountSettings=resolve;});});await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>typeof __releaseOldAccountSettings==='function');
    await page.evaluate(next=>{const old={...AppState.settings,agent_settings_revision:next,agent_model:'stale-account-model'};AppState.settings.account_login_token='new-account';__releaseOldAccountSettings(old);},next);await page.waitForTimeout(20);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'old-model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('local confirmed save begun after an external read keeps its newer value',async()=>{
  const page=await open();try{await page.evaluate(()=>{__MOCK__.get_settings=()=>new Promise(resolve=>{window.__releaseSettings=resolve;});});await page.evaluate(next=>__onState({agent_settings_revision:next}),next);await page.waitForFunction(()=>typeof __releaseSettings==='function');
    await page.evaluate(async()=>{await Settings.save({agent_model:'local confirmed'},false);});
    await page.evaluate(next=>__releaseSettings({...AppState.settings,agent_settings_revision:next,agent_model:'older agent value',title_model:'agent title model'}),next);await page.waitForFunction(next=>AppState.settings.agent_settings_revision===next,next);
    assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'local confirmed');assert.equal(await page.evaluate(()=>AppState.settings.title_model),'agent title model');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('external settings cannot replace balances, signed licenses or account sessions',async()=>{
  const page=await open();try{await push(page,next,{agent_model:'new model',credit_balance:9999,subscription_active:true,license_token:'invented license',account_login_token:'invented account'});
    assert.notEqual(await page.evaluate(()=>AppState.settings.credit_balance),9999);assert.notEqual(await page.evaluate(()=>AppState.settings.license_token),'invented license');assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('an external connection change locks the former account instead of carrying old entitlement across services',async()=>{
  const page=await open();try{await push(page,next,{subscription_server_url:'https://new.example.invalid',account_login_token:'',account_user_id:'',license_token:''});
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('an off-page confirmed change is visible on entering the settings section without redirecting navigation',async()=>{
  const page=await open();try{await page.locator('[data-page=workbench]').click();await push(page,next,{agent_model:'background updated'});assert.equal(await page.evaluate(()=>AppState.currentView),'workbench');
    await page.locator('[data-page=settings]').click();await page.locator('.sitem[data-section=agent]').click();assert.equal(await page.getByRole('textbox',{name:'模型',exact:true}).inputValue(),'background updated');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
test('a snapshot with a different revision is ignored and retried on the next push',async()=>{
  const page=await open();try{await page.evaluate(({next,latest})=>{__externalSnapshot={agent_settings_revision:latest,agent_model:'unmatched'};__onState({agent_settings_revision:next});},{next,latest});await page.waitForFunction(()=>__externalReads.length===1);await page.waitForTimeout(20);assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'old-model');
    await push(page,next,{agent_model:'matched'});assert.equal(await page.evaluate(()=>AppState.settings.agent_model),'matched');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
