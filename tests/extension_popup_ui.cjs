// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Popup interaction tests use the real frontend and fixture-only Chrome and desktop responses.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const output = process.env.FOILPAGE_POPUP_SCREENSHOTS || path.join(root, '.ui-audit', 'extension-popup');
const source = path.join(root, 'browser-extension');
const popupOrigin = 'http://127.0.0.1:49091';
const desktopOrigin = 'http://127.0.0.1:18765';
const longTitle = '把访谈变成可以反复阅读的笔记，保留真实的上下文与一个很长的中英混合标题而不挤出任务操作';
const firstJob = {id:'active-task', title:longTitle, status:'downloading', progress:.42};
const secondJob = {id:'asr-task', title:'把视频变成文本之后，如何快速回看关键段落', status:'transcribing', progress:.68};
const doneJob = {id:'done-task', title:'注意力与长期思考', status:'done', job_id:'written-specimen'};
const fixtureToken = 'local-popup-test-token-'.repeat(2);
const video = {id:1, url:'https://www.youtube.com/watch?v=fixture123', title:longTitle + ' - YouTube'};

async function openPopup(browser, options = {}) {
  const page = await browser.newPage({
    viewport:{width:options.width || 360,height:options.height || 600},
    colorScheme:options.theme || 'light', reducedMotion:'reduce',
  });
  page.setDefaultTimeout(6000);
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(({options, token, video, defaultJobs}) => {
    const fixture = window.fixture = {
      tasks:options.jobs || defaultJobs,
      statusReads:0,
      cancelIds:[],
      submissions:[],
      connection:options.connection || 'online',
      statusFailure:false,
      cancelFailure:true,
      storedToken:options.connection === 'unpaired' ? '' : token,
    };
    globalThis.chrome = {
      storage:{local:{
        get:async () => ({localApiToken:fixture.storedToken}),
        set:async values => { fixture.storedToken = values.localApiToken; },
      }},
      tabs:{query:async () => [options.tab || video]},
      runtime:{
        getManifest:() => ({version:'0.1.5'}),
        async sendMessage(message, callback) {
          let reply = {ok:true};
          if (message.type === 'shengjian-authorized-status') {
            ++fixture.statusReads;
            reply = fixture.statusFailure
              ? {ok:false,tasks:[],message:'任务状态暂不可用'}
              : {ok:true,tasks:structuredClone(fixture.tasks),connection:fixture.connection};
          }
          if (message.type === 'shengjian-cancel-task') {
            fixture.cancelIds.push(message.id);
            const task = fixture.tasks.find(candidate => candidate.id === message.id);
            if (fixture.cancelFailure) reply = {ok:false,message:'暂时无法取消，请稍后重试'};
            else {task.status = 'cancelled'; reply = {ok:true,job:structuredClone(task)};}
          }
          if (message.type === 'shengjian-page-submit') {
            fixture.submissions.push(message);
            reply = fixture.connection === 'offline' ? {ok:true,job:{queued:true}} : {ok:true,job:{}};
          }
          callback?.(reply);
          return reply;
        },
      },
    };
  }, {options,token:fixtureToken,video,defaultJobs:[firstJob,secondJob,doneJob]});
  const approved = {value:options.connection !== 'unpaired'};
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin === popupOrigin) {
      const name = url.pathname.slice(1);
      if (!['popup.html','popup.css','popup.js','popup-model.js','api.js','icons/icon-32.png','icons/icon-48.png','icons/icon-128.png'].includes(name)) {
        return route.abort();
      }
      const contentType = name.endsWith('.css') ? 'text/css'
        : name.endsWith('.js') ? 'text/javascript; charset=utf-8'
          : name.endsWith('.png') ? 'image/png' : 'text/html; charset=utf-8';
      return route.fulfill({status:200,body:await fs.readFile(path.join(source,name)),contentType});
    }
    if (url.origin !== desktopOrigin || options.connection === 'offline') return route.abort('connectionrefused');
    const method = route.request().method();
    const headers = {
      'access-control-allow-origin':popupOrigin,
      'access-control-allow-methods':'GET, POST, OPTIONS',
      'access-control-allow-headers':'Authorization, Content-Type',
    };
    const json = (body,status=200) => route.fulfill({json:body,status,headers});
    if (url.pathname === '/health') return json({
      service:'VideoToArticle',api_version:'1',configured:approved.value,extension_latest:'0.1.5',
    });
    if (url.pathname === '/v1/downloads') {
      if (method === 'OPTIONS') return route.fulfill({status:204,headers});
      assert.equal(route.request().headers().authorization,`Bearer ${fixtureToken}`);
      return json({jobs:[]});
    }
    if (method === 'OPTIONS') return route.fulfill({status:204,headers});
    if (url.pathname === '/v1/setup' || url.pathname === '/v1/pair') {
      if (!approved.value) return json({error:{code:'pairing_closed',message:'请先允许连接'}},403);
      return json({token:fixtureToken});
    }
    if (url.pathname === '/v1/show') return json({shown:true});
    return route.abort();
  });
  await page.goto(`${popupOrigin}/popup.html`);
  await page.locator('#jobsPanel').waitFor();
  await page.locator('#connectionText').getByText(/已连接|未连接/, {exact:true}).waitFor();
  return {page,pageErrors,approved};
}

async function capture(page, name) {
  const width = await page.evaluate(() => ({
    viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,
  }));
  assert.ok(width.document <= width.viewport && width.body <= width.viewport, JSON.stringify(width));
  await page.screenshot({path:path.join(output,`${name}.png`),fullPage:true});
  return width;
}

async function captureOnline(browser, theme) {
  const {page,pageErrors} = await openPopup(browser, {theme});
  try {
    await page.locator('#pageDesc').getByText(longTitle,{exact:true}).waitFor();
    assert.equal(await page.locator('#connectionText').textContent(),'已连接');
    assert.equal(await page.locator('#connectionPanel').isHidden(),true);
    assert.equal(await page.locator('#pagePlatform').textContent(),'YouTube');
    await page.getByRole('button',{name:'仅下载',exact:true}).waitFor();
    await page.getByRole('button',{name:'下载并转写',exact:true}).waitFor();
    assert.match(await page.locator('#jobsCount').textContent(),/3/);
    assert.ok((await page.locator('.page-btn:visible').evaluateAll(buttons =>
      buttons.map(button => button.getBoundingClientRect().height))).every(height => height >= 44));
    assert.match(await page.locator('#jobs').textContent(),/正在下载 42%/);
    assert.match(await page.locator('#jobs').textContent(),/正在转写 68%/);
    assert.match(await page.locator('#jobs').textContent(),/转写成功/);
    const bounds = await capture(page,`online-${theme}`);
    if (theme === 'light') {
      await page.getByRole('button',{name:'下载并转写',exact:true}).click();
      await page.locator('#pageState').getByText(/已提交/).waitFor();
      const submitted = await page.evaluate(() => fixture.submissions.map(item => [item.url,item.transcribe]));
      assert.deepEqual(submitted,[[video.url,true]]);
    }
    assert.deepEqual(pageErrors,[]);
    return bounds;
  } finally { await page.close(); }
}

async function exerciseJobs(browser) {
  const {page,pageErrors} = await openPopup(browser);
  try {
    const firstRow = page.locator('.job-row').filter({hasText:longTitle});
    const cancel = firstRow.getByRole('button',{name:/取消任务/});
    await page.evaluate(() => {
      const url = 'https://www.douyin.com/video/1234567890123456';
      Object.assign(fixture.tasks[0], {url,status:'awaiting_browser',error:{diagnostic:{fallback_pending:'browser'}}});
      fixture.tasks.push({id:'last-attempt',url,title:'旧尝试',status:'failed'});
      fixture.tasks.push({id:'different-video',url:url+'0',title:'另一条视频',status:'failed'});
    });
    await firstRow.getByText('正在重试下载',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-job-id="last-attempt"]').count(),0);
    assert.equal(await page.locator('[data-job-id="different-video"]').count(),1);
    await page.evaluate(() => {
      Object.assign(fixture.tasks[0],{status:'downloading',error:null});
      fixture.tasks.splice(3);
    });
    await cancel.focus();
    await page.evaluate(() => { fixture.tasks[0].progress = .63; });
    await firstRow.getByText('正在下载 63%',{exact:true}).waitFor();
    const keptFocus = await cancel.evaluate(button => document.activeElement === button);
    assert.equal(keptFocus,true,'updating the same task keeps its cancel control focused');

    await page.evaluate(() => { fixture.statusFailure = true; });
    await page.getByRole('button',{name:'刷新任务'}).click();
    await page.locator('#jobsStatus:visible').waitFor();
    assert.match(await page.locator('#jobsStatus').textContent(),/刷新|读取|不可用|失败/);
    assert.equal(await firstRow.isVisible(),true,'failed refresh preserves last successful task snapshot');
    await capture(page,'refresh-error');
    await page.evaluate(() => { fixture.statusFailure = false; });
    await page.getByRole('button',{name:'刷新任务'}).click();
    await page.locator('#jobsStatus').waitFor({state:'hidden'});

    await cancel.focus();
    await page.keyboard.press('Enter');
    await firstRow.locator('.job-error').getByText(/无法取消|取消失败/).waitFor();
    assert.equal(await cancel.isEnabled(),true);
    assert.deepEqual(await page.evaluate(() => fixture.cancelIds),['active-task']);
    await capture(page,'cancel-error');
    await page.evaluate(() => {
      fixture.cancelFailure = false;
      fixture.statusFailure = true;
    });
    await cancel.click();
    await firstRow.getByText('已取消',{exact:true}).waitFor();
    await page.locator('#jobsStatus:visible').waitFor();
    assert.match(await page.locator('#jobsStatus').textContent(),/刷新|读取|不可用|失败/);
    assert.deepEqual(await page.evaluate(() => fixture.cancelIds),['active-task','active-task']);
    assert.equal(await firstRow.locator('.job-error:visible').count(),0);
    assert.deepEqual(pageErrors,[]);
    return {keptFocus,keptCachedTasks:true,cancelFailureRetried:true,confirmedCancellationSurvivesFailedRefresh:true};
  } finally { await page.close(); }
}

async function exerciseRecovery(browser) {
  const unpaired = await openPopup(browser, {connection:'unpaired',jobs:[]});
  try {
    const {page,approved,pageErrors} = unpaired;
    assert.equal(await page.locator('#pagePanel').isHidden(),true);
    await page.getByRole('button',{name:'打开桌面连接页'}).waitFor();
    await capture(page,'unpaired-light');
    await page.getByRole('button',{name:'打开桌面连接页'}).click();
    await page.getByText(/已在 FoilPage.*打开连接页/).waitFor();
    approved.value = true;
    await page.locator('#connect').click();
    await page.locator('#pageDesc').getByText(longTitle,{exact:true}).waitFor();
    assert.equal(await page.locator('#connectionText').textContent(),'已连接');
    assert.equal(await page.locator('#connectionPanel').isHidden(),true);
    assert.deepEqual(pageErrors,[]);
  } finally { await unpaired.page.close(); }
  const offline = await openPopup(browser, {
    connection:'offline',theme:'dark',
    jobs:[
      {id:'held-job',title:'等待桌面端恢复后的下载',status:'handoff_pending',queued:true},
      {...firstJob,waiting_connection:true},
    ],
  });
  try {
    const {page,pageErrors} = offline;
    assert.equal(await page.getByRole('link',{name:'打开 FoilPage'}).getAttribute('href'),'foilpage://launch');
    await page.locator('#pageDownload').click();
    await page.locator('#pageState').getByText(/已暂存/).waitFor();
    assert.match(await page.locator('#jobs').textContent(),/连接恢复后继续/);
    assert.deepEqual(await page.evaluate(() => fixture.submissions.map(item => [item.url,item.transcribe])),[[video.url,false]]);
    assert.deepEqual(pageErrors,[]);
    await capture(page,'offline-dark');
  } finally { await offline.page.close(); }
}

async function captureUnsupported(browser) {
  const {page,pageErrors} = await openPopup(browser, {
    width:320,
    tab:{id:1,url:'https://example.test/non-video',title:'不支持的视频页'},
    jobs:[],
  });
  try {
    await page.locator('#pagePanel').getByText(/打开.*视频/).waitFor();
    assert.equal(await page.locator('.page-btn:visible').count(),0);
    assert.deepEqual(pageErrors,[]);
    return await capture(page,'non-video-light');
  } finally { await page.close(); }
}

async function main() {
  await fs.mkdir(output,{recursive:true});
  const browser = await chromium.launch({headless:true});
  try {
    const online = {};
    for (const theme of ['light','dark']) online[theme] = await captureOnline(browser,theme);
    const jobs = await exerciseJobs(browser);
    await exerciseRecovery(browser);
    const unsupported = await captureUnsupported(browser);
    const report = {fixtureOnly:true,online,jobs,unsupported,pairing:true,offline:true};
    await fs.writeFile(path.join(output,'popup-results.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
