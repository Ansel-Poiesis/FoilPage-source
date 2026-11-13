// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Actual page interactions; guide reads/copies never run a backend repair or model.
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';

const root=path.resolve(import.meta.dirname,'..');
const guide='# 当前 FoilPage 实例\n\n版本 9.8.7，源码位置 D:\\actual checkout。\n\n## 检查与回滚\n\n先记录原值，运行准备页检查。<script>window.badGuide=true</script>';
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});

async function open(options={}){
  const page=await browser.newPage({viewport:{width:options.width||1280,height:options.height||800}});
  page.setDefaultTimeout(5000);page.errors=[];page.on('pageerror',e=>page.errors.push(e.message));
  await page.addInitScript(installMock);
  await page.addInitScript(({guide,options})=>{
    window.__MOCK__.get_agent_setup_guide=async()=>{
      window.__fixtureCalls.push('get_agent_setup_guide');
      if(options.delay) await new Promise(resolve=>setTimeout(resolve,options.delay));
      if(options.fail) return {ok:false,error:'fixture secret must not appear'};
      return {ok:true,markdown:guide,file_name:'fixture.md'};
    };
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{
      if(options.clipboardFail) throw new Error('denied');
      window.__copiedGuide=text;
    }}});
    document.execCommand=()=>{window.__fallbackCopyCalled=true;return options.fallbackCopySuccess===true;};
  },{guide,options});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
  await page.waitForSelector('.nav-row[aria-current=page]');
  await page.locator('[data-page=settings]').click();await page.locator('.sitem[data-section=agent]').click();
  return page;
}

test('local control configuration copies only the selected MCP scope and paths',async()=>{
  const page=await open();try{
    await page.evaluate(()=>__MOCK__.get_agent_control_config=allowWrite=>{
      window.__requestedAgentWrite=allowWrite;
      return {ok:true,available:true,configuration:{mcpServers:{shengjian:{command:'fixture-agent.exe',args:allowWrite?['mcp']:['mcp','--read-only']}}}};
    });
    await page.locator('.sitem[data-section=control]').click();
    const edit=page.getByRole('button',{name:'完整 MCP',exact:true});await edit.click();
    await page.getByRole('dialog',{name:'连接您的 Agent'}).waitFor();
    assert.equal(await page.evaluate(()=>__requestedAgentWrite),true);
    assert((await page.locator('.modal-body').textContent()).includes('可管理内容、模型与转写提示词，并控制任务'));
    await page.getByRole('button',{name:'复制 MCP 配置',exact:true}).click();await page.waitForFunction(()=>!!__copiedGuide);
    const copied=JSON.parse(await page.evaluate(()=>__copiedGuide));
    assert.deepEqual(copied.mcpServers.shengjian.args,['mcp']);
    await page.keyboard.press('Escape');assert(await edit.evaluate(e=>e===document.activeElement));
    await page.getByRole('button',{name:'只读 MCP',exact:true}).click();
    await page.getByRole('dialog',{name:'连接您的 Agent'}).waitFor();
    assert.equal(await page.evaluate(()=>__requestedAgentWrite),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('guide is readable, safe, keyboard dismissible and contained in the minimum window',async()=>{
  const page=await open({width:560,height:500});try{
    const read=page.getByRole('button',{name:'阅读配置指南',exact:true});await read.focus();await page.keyboard.press('Enter');
    await page.getByRole('dialog',{name:'Agent 配置与修复指南'}).waitFor();
    assert((await page.locator('.modal-body').textContent()).includes('D:\\actual checkout'));
    assert.equal(await page.locator('.modal-body script').count(),0);assert.equal(await page.evaluate(()=>window.badGuide),undefined);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const box=await page.locator('.modal').boundingBox();assert(box.x>=0&&box.y>=0&&box.x+box.width<=561&&box.y+box.height<=501);
    await page.getByRole('button',{name:'复制这份指南给 Agent'}).click();await page.waitForFunction(()=>!!window.__copiedGuide);
    assert.equal(await page.evaluate(()=>window.__copiedGuide),guide);
    await page.keyboard.press('Escape');assert.equal(await page.locator('.modal').count(),0);assert(await read.evaluate(e=>e===document.activeElement));
    const calls=await page.evaluate(()=>window.__fixtureCalls);
    for(const name of ['apply_setup_fixes','save_settings','download_model','batch_agent_translate','begin_subscription']) assert(!calls.includes(name),name);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('same-page copy fetches a fresh guide and reports only actual copy success',async()=>{
  const page=await open();try{
    await page.getByRole('button',{name:'复制配置指南给 Agent'}).click();await page.waitForFunction(()=>!!window.__copiedGuide);
    assert.equal(await page.evaluate(()=>window.__copiedGuide),guide);assert.equal(await page.locator('.modal').count(),0);
    await page.evaluate(()=>{window.__copiedGuide=null;window.__MOCK__.get_agent_setup_guide=async()=>({ok:true,markdown:'new runtime snapshot'});});
    await page.getByRole('button',{name:'复制配置指南给 Agent'}).click();await page.waitForFunction(()=>window.__copiedGuide==='new runtime snapshot');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('unavailable guide and denied clipboard show useful errors without leaking raw backend text',async()=>{
  const failure=await open({fail:true});try{
    await failure.getByRole('button',{name:'阅读配置指南'}).click();await failure.getByRole('alert').waitFor();
    assert.equal(await failure.locator('.modal').count(),0);assert(!(await failure.locator('#toast').textContent()).includes('fixture secret'));
    assert(await failure.getByRole('button',{name:'阅读配置指南'}).isEnabled());
  }finally{await failure.close();}
  const page=await open({clipboardFail:true});try{
    await page.getByRole('button',{name:'复制配置指南给 Agent'}).click();await page.getByRole('alert').waitFor();
    assert.equal(await page.evaluate(()=>window.__fallbackCopyCalled),true);assert((await page.locator('#toast').textContent()).includes('复制未完成'));
    assert.equal(await page.locator('textarea[aria-label="待复制的配置指南"]').count(),0);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('leaving the settings section prevents a late guide dialog or clipboard write',async()=>{
  const page=await open();try{
    await page.evaluate(()=>{
      const original=__MOCK__.get_agent_setup_guide;
      __MOCK__.get_agent_setup_guide=(...args)=>new Promise(resolve=>{window.__releaseLateGuide=()=>resolve(original(...args));});
    });
    await page.getByRole('button',{name:'阅读配置指南'}).evaluate(node=>{window.__lateGuideButton=node;});
    await page.getByRole('button',{name:'阅读配置指南'}).click();
    await page.waitForFunction(()=>typeof window.__releaseLateGuide==='function');
    await page.locator('.sitem[data-section=ui]').click();
    await page.evaluate(()=>window.__releaseLateGuide());
    await page.waitForFunction(()=>window.__lateGuideButton.disabled===false);
    assert.equal(await page.locator('.modal').count(),0);assert.equal(await page.evaluate(()=>window.__copiedGuide),undefined);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});
