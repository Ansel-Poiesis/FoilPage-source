// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Local fixture verification only; no user data, network downloads or API calls.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '_webview_ui_shots', 'product');
fs.mkdirSync(output, {recursive:true});

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:800}, deviceScaleFactor:1});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(require('./ui_fixture.cjs'));
    await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
    await page.waitForSelector('.nav-row[aria-current=page]');
    await page.locator('[data-page=workbench]').click();
    await page.waitForSelector('.job');
    for (const width of [1280,960,640,560]) {
      await page.setViewportSize({width,height:800});
      await page.evaluate(()=>switchView('workbench'));
      assert.equal(await page.locator('.job-prog, .fp-track').count(),0,'obsolete progress bars must be removed');
      const collisions = await page.locator('.job').evaluateAll(rows => rows.some(row => {
        const stage = row.querySelector('.job-stages').getBoundingClientRect();
        const del = row.querySelector('.job-delete').getBoundingClientRect();
        return stage.right + 8 > del.left || del.right > row.getBoundingClientRect().right;
      }));
      assert.equal(collisions,false,'status and delete must keep their own columns');
      await page.screenshot({path:path.join(output,`tasks-${width}.png`)});
      const filters = await page.locator('.filter').evaluateAll(els=>els.map(e=>({height:e.getBoundingClientRect().height, text:e.textContent})));
      assert(filters.every(f=>f.height<=40), 'filter labels must stay on one line');
      await page.evaluate(()=>switchView('library'));
      await page.waitForSelector('.doc');
      assert.equal(await page.locator('.doc-title').first().textContent(), await page.evaluate(()=>window.__fixtureTitle));
      const clipped = await page.locator('.doc-title').first().evaluate(e=>e.scrollHeight>e.clientHeight+1);
      assert.equal(clipped,false,'library title must not be clipped');
      await page.screenshot({path:path.join(output,`library-${width}.png`)});
      await page.locator('.doc').first().click();
      await page.waitForSelector('.reader-body h1');
      await page.screenshot({path:path.join(output,`reader-${width}.png`)});
      if(width<=1000) { await page.locator('#readerBack').click(); assert(await page.locator('.lib-list').isVisible()); }
      await page.evaluate(()=>switchView('settings'));
      await page.locator('.sitem').filter({hasText:'文本整理'}).click();
      assert.equal(await page.locator('select').filter({has:page.locator('option[value="light"]')}).count(),1);
      await page.screenshot({path:path.join(output,`settings-${width}.png`)});
      await page.evaluate(()=>switchView('subscription'));
      await page.waitForSelector('#page-subscription.active .plan-card');
      await page.screenshot({path:path.join(output,`subscription-${width}.png`)});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'subscription stays within the window');
      for(const key of ['links','import','browser']) {
        await page.evaluate(key=>switchView(key),key);
        await page.screenshot({path:path.join(output,`${key}-${width}.png`)});
      }
      const overflow = await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
      assert.equal(overflow,false,'no window-level horizontal scrolling');
    }
    await page.setViewportSize({width:1280,height:800});
    await page.evaluate(()=>{AppState.settings.theme='paper';applyTheme();switchView('library');});
    await page.waitForSelector('.doc');
    await page.locator('.doc').first().click();
    await page.screenshot({path:path.join(output,'library-paper.png')});
    await page.evaluate(()=>switchView('links'));
    await page.evaluate(()=>{AppApi.downloadFromUrl=async()=>{await new Promise(resolve=>setTimeout(resolve,250));return {ok:true,job:{id:'fixture'}};};});
    await page.locator('#linkUrl').fill('9.74 10/23 @某人: 《标题》观看后 #知识推荐 https://v.douyin.com/short-id/复制此链接，打开抖音搜索，直接观看视频！');
    await page.locator('#linkForm button').click();
    assert.equal(await page.locator('#linkState').textContent(),'正在请求已配对浏览器提供一次性抖音授权…');
    await page.waitForFunction(()=>document.querySelector('#linkState').textContent==='已加入任务');
    await page.waitForFunction(()=>!document.querySelector('#linkForm button').disabled);
    await page.locator('#linkUrl').fill('https://www.bilibili.com/video/BV1test');
    await page.locator('#linkForm button').click();
    await page.waitForFunction(()=>document.querySelector('#linkState').textContent==='已加入任务');

    await page.setViewportSize({width:640,height:800});
    await page.evaluate(()=>switchView('settings'));
    for(const label of ['界面','识别','文本整理','模型','数据','启动','关于']) {
      await page.locator('.sitem').filter({hasText:label}).click();
      await page.screenshot({path:path.join(output,`setting-${label}.png`)});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`settings overflow: ${label}`);
    }

    const popup = await browser.newPage({viewport:{width:360,height:600}});
    popup.on('pageerror', e=>errors.push(e.message));
    await popup.route('**/*', async route => {
      const url = new URL(route.request().url());
      if(url.hostname==='127.0.0.1') return route.fulfill({json:{service:'VideoToArticle',api_version:'1'}});
      const local = path.join(root,'browser-extension',url.pathname);
      const ext = path.extname(local);
      return route.fulfill({body:fs.readFileSync(local), contentType:({'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.woff2':'font/woff2'})[ext]});
    });
    await popup.addInitScript(()=>{
      window.__sjSubmitted=[];
      window.chrome={storage:{local:{get:async()=>({localApiToken:'fixture-token'})}}, tabs:{query:async()=>[{url:'https://www.bilibili.com/video/BV1test'}]},runtime:{sendMessage:(msg,cb)=>{
        if(msg.type==='shengjian-page-submit')window.__sjSubmitted.push(msg);
        const data = msg.type==='shengjian-authorized-status' ? {tasks:[{title:'从声音到文字：一段完整的访谈记录与整理体验',status:'downloading',progress:.46},{title:'保留课堂里的每一个例子',status:'done',progress:1},{title:'关于创作的谈话',status:'queued'}]} : {ok:true};
        if(cb)cb(data);return Promise.resolve(data);
      }}};
    });
    await popup.goto('https://shengjian.test/popup.html');
    await popup.waitForSelector('#pagePanel:not([hidden])');
    await popup.locator('#pageTranscribe').click();
    assert.equal(await popup.locator('#pageState').textContent(),'已提交下载');
    assert.equal(await popup.evaluate(()=>window.__sjSubmitted.length),1,'a click submits one task');
    assert.equal(await popup.evaluate(()=>window.__sjSubmitted[0].transcribe),true,'transcribe intent reaches the background');
    assert.equal(await popup.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'popup must not overflow horizontally');
    assert((await popup.locator('.page-btn').evaluateAll(buttons=>buttons.map(button=>button.getBoundingClientRect().height))).every(height=>height>=44),'popup actions keep a 44px target');
    await popup.screenshot({path:path.join(output,'extension-popup.png')});

    // Feedback is in document flow outside the player's picture and subtitles.
    const assertOutsidePlayer = async (target, feedbackSelector, playerSelector) => {
      const overlaps = await target.evaluate(({feedbackSelector,playerSelector})=>{
        const feedback=document.querySelector(feedbackSelector), player=document.querySelector(playerSelector);
        if(!feedback || !player)throw new Error('fixture requires both feedback and player');
        const a=feedback.getBoundingClientRect(), b=player.getBoundingClientRect();
        return a.left<b.right && a.right>b.left && a.top<b.bottom && a.bottom>b.top;
      },{feedbackSelector,playerSelector});
      assert.equal(overlaps,false,`${feedbackSelector} must stay outside the picture and subtitles`);
    };
    const bili = await browser.newPage({viewport:{width:1200,height:800}});
    bili.on('pageerror',e=>errors.push(e.message));
    await bili.route('https://www.bilibili.com/**',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:`<style>body{margin:40px;font:14px sans-serif}#bilibili-player{width:900px;height:500px;background:#18191c;position:relative}video{width:100%;height:100%}.caption{position:absolute;bottom:60px;left:40%;color:white}.bpx-player-control-bottom{position:absolute;bottom:0;width:100%;height:40px;display:flex;justify-content:space-between;color:white}.bpx-player-control-bottom-left{width:600px}.bpx-player-control-bottom-right{width:260px}</style><h2>测试播放器</h2><div id="bilibili-player"><video></video><div class="caption">字幕不应被覆盖</div><div class="bpx-player-control-bottom"><div class="bpx-player-control-bottom-left">播放 · 时间 · 原生控件</div><div class="bpx-player-control-bottom-right">画质 · 音量 · 设置 · 全屏</div></div></div><p>播放器下方的内容不应被覆盖。</p>`}));
    await bili.addInitScript(()=>{window.__sjListeners=[];window.chrome={runtime:{onMessage:{addListener:(fn)=>window.__sjListeners.push(fn)},sendMessage:(msg,cb)=>{if(msg.type==='shengjian-page-submit')for(const listener of window.__sjListeners)listener({type:'shengjian-toast',message:'已提交下载',stage:'submitted',taskUrl:msg.url},null,()=>{});cb({ok:true});}}};});
    await bili.goto('https://www.bilibili.com/video/BV1test');
    await bili.addStyleTag({path:path.join(root,'browser-extension/content-bili.css')});
    await bili.addScriptTag({path:path.join(root,'browser-extension/content-bili.js')});
    assert(await bili.locator('.shengjian-bili-host').evaluate(e=>e.classList.contains('above-player')),'crowded native controls use the external action row');
    await assertOutsidePlayer(bili,'.shengjian-bili-host','#bilibili-player');
    await bili.screenshot({path:path.join(output,'bili-external-controls.png')});
    await bili.locator('.shengjian-bili-button').first().click();
    await bili.waitForSelector('.shengjian-bili-pill');
    assert.equal(await bili.locator('.shengjian-bili-pill .sj-text').textContent(),'已提交下载');
    const pushStatus = (message,error=false)=>bili.evaluate(({message,error})=>{for(const fn of window.__sjListeners)fn({type:'shengjian-toast',taskUrl:'https://www.bilibili.com/video/BV1test',message,error},null,()=>{});},{message,error});
    await assertOutsidePlayer(bili,'.shengjian-bili-pill','#bilibili-player');
    await pushStatus('正在下载 42%');
    await bili.waitForSelector('.shengjian-bili-pill.is-percent');
    await bili.waitForFunction(()=>document.querySelector('.shengjian-bili-pill .sj-num b').textContent==='42');
    await assertOutsidePlayer(bili,'.shengjian-bili-pill','#bilibili-player');
    await bili.screenshot({path:path.join(output,'bili-pill-percent.png')});
    await pushStatus('正在下载 88%');
    await bili.waitForFunction(()=>document.querySelector('.shengjian-bili-pill .sj-num b').textContent==='88');
    await pushStatus('下载成功');
    await bili.waitForSelector('.shengjian-bili-pill.is-done');
    assert.equal(await bili.locator('.shengjian-bili-pill .sj-text').textContent(),'下载成功');
    assert(await bili.locator('.shengjian-bili-pill .sj-text').isVisible(),'success needs explicit visible text');
    await assertOutsidePlayer(bili,'.shengjian-bili-pill','#bilibili-player');
    await bili.screenshot({path:path.join(output,'bili-pill-download-success.png')});
    await pushStatus('正在转写');
    assert.equal(await bili.locator('.shengjian-bili-pill').evaluate(e=>e.classList.contains('is-done')),false,'transcribing must leave the download terminal state');
    assert.equal(await bili.locator('.shengjian-bili-pill .sj-text').textContent(),'正在转写');
    await pushStatus('转写成功');
    await bili.waitForSelector('.shengjian-bili-pill.is-done');
    assert.equal(await bili.locator('.shengjian-bili-pill .sj-text').textContent(),'转写成功','download completion must not swallow transcription completion');
    assert.equal(await bili.locator('.shengjian-bili-pill').count(),1,'one feedback surface is reused across both outcomes');
    await assertOutsidePlayer(bili,'.shengjian-bili-pill','#bilibili-player');
    await bili.screenshot({path:path.join(output,'bili-pill-transcribe-success.png')});
    await bili.locator('.shengjian-bili-pill').click();
    await bili.waitForSelector('.shengjian-bili-pill',{state:'detached'});
    await bili.evaluate(()=>{document.querySelector('#bilibili-player').style.width='460px';document.querySelector('.bpx-player-control-bottom-left').style.width='180px';window.__shengjianBiliInternals.ensureControls();});
    assert(await bili.locator('.shengjian-bili-host').evaluate(e=>e.classList.contains('above-player')));
    await bili.locator('.shengjian-bili-button[data-shengjian-action="transcribe"]').click();
    assert.equal(await bili.locator('.shengjian-bili-pill .sj-text').textContent(),'已提交下载');
    await assertOutsidePlayer(bili,'.shengjian-bili-pill','#bilibili-player');
    await bili.screenshot({path:path.join(output,'bili-outside-narrow.png')});

    // YouTube uses the same independent outcomes below its actual player.
    const yt = await browser.newPage({viewport:{width:1200,height:800}});
    yt.on('pageerror',e=>errors.push(e.message));
    await yt.route('https://www.youtube.com/**',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:`<style>body{margin:40px;font:14px Roboto,sans-serif;background:#0f0f0f;color:#f1f1f1}.watch-layout{width:900px}#player-container-outer{width:900px;height:500px;background:#1c2420;position:relative}video{width:100%;height:100%}.caption{position:absolute;bottom:60px;left:40%}</style><h2>测试播放页</h2><main class="watch-layout"><div id="player-container-outer"><video></video><div class="caption">字幕不应被覆盖</div></div><p>视频说明继续留在播放器下面。</p></main>`}));
    await yt.addInitScript(()=>{window.__sjListeners=[];window.chrome={runtime:{onMessage:{addListener:(fn)=>window.__sjListeners.push(fn)},sendMessage:(msg,cb)=>{if(msg.type==='shengjian-page-submit')for(const listener of window.__sjListeners)listener({type:'shengjian-toast',message:'已提交下载',stage:'submitted',taskUrl:msg.url},null,()=>{});cb({ok:true});}}};});
    await yt.goto('https://www.youtube.com/watch?v=abcdef12345');
    await yt.addStyleTag({path:path.join(root,'browser-extension/content-youtube.css')});
    await yt.addScriptTag({path:path.join(root,'browser-extension/content-youtube.js')});
    await yt.waitForSelector('.shengjian-yt-host');
    await assertOutsidePlayer(yt,'.shengjian-yt-host','#player-container-outer');
    await yt.screenshot({path:path.join(output,'yt-buttons.png')});
    await yt.locator('.shengjian-yt-button').first().click();
    await yt.waitForSelector('.shengjian-yt-pill:not(.is-hidden)');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-text').textContent(),'已提交下载');
    const pushYtStatus = (message,error=false)=>yt.evaluate(({message,error})=>{for(const fn of window.__sjListeners)fn({type:'shengjian-toast',taskUrl:'https://www.youtube.com/watch?v=abcdef12345',message,error},null,()=>{});},{message,error});
    // The full merged-task message remains available without clipping its meaning.
    await pushYtStatus('该视频已在处理中，本次点击已合并（可在 FoilPage 任务页查看进度）');
    await yt.waitForFunction(()=>document.querySelector('.shengjian-yt-pill .sj-text')?.textContent==='已在处理中');
    assert.equal(await yt.locator('.shengjian-yt-pill').evaluate(e=>e.title),'该视频已在处理中，本次点击已合并（可在 FoilPage 任务页查看进度）','title keeps the full message');
    await pushYtStatus('正在下载 42%');
    await yt.waitForSelector('.shengjian-yt-pill.is-percent');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-num b').textContent(),'42');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-text').isVisible(),false,'percent state uses the numeric progress');
    await assertOutsidePlayer(yt,'.shengjian-yt-pill','#player-container-outer');
    await yt.screenshot({path:path.join(output,'yt-pill-percent.png')});
    await pushYtStatus('下载成功');
    await yt.waitForSelector('.shengjian-yt-pill.is-done');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-text').textContent(),'下载成功');
    assert(await yt.locator('.shengjian-yt-pill .sj-text').isVisible(),'success needs explicit visible text');
    await assertOutsidePlayer(yt,'.shengjian-yt-pill','#player-container-outer');
    await yt.screenshot({path:path.join(output,'yt-pill-download-success.png')});
    await pushYtStatus('正在转写');
    assert.equal(await yt.locator('.shengjian-yt-pill').evaluate(e=>e.classList.contains('is-done')),false,'transcribing must leave the download terminal state');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-text').textContent(),'正在转写');
    await pushYtStatus('转写成功');
    await yt.waitForSelector('.shengjian-yt-pill.is-done');
    assert.equal(await yt.locator('.shengjian-yt-pill .sj-text').textContent(),'转写成功','download completion must not swallow transcription completion');
    assert.equal(await yt.locator('.shengjian-yt-pill').count(),1,'one feedback surface is reused across both outcomes');
    await assertOutsidePlayer(yt,'.shengjian-yt-pill','#player-container-outer');
    await yt.screenshot({path:path.join(output,'yt-pill-transcribe-success.png')});
    await yt.locator('.shengjian-yt-pill').click();
    await yt.waitForSelector('.shengjian-yt-pill',{state:'detached'});

    assert.deepEqual(errors,[],'no uncaught frontend errors');
    console.log('Product UI smoke passed at 1280 / 960 / 640 / 560; Bili and YouTube pills passed.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
