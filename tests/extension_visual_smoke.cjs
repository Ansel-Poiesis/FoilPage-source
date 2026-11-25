// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..', 'browser-extension');
const output = process.env.SHENGJIAN_EXTENSION_SCREENSHOTS || path.resolve(__dirname, '..', '.ui-audit', 'extension-queue-review');
const results = [];
const playerHtml = platform => `<!doctype html><meta charset="utf-8"><style>
body{margin:0;background:#181d1a;color:#eef4ee;font:14px system-ui}header{height:110px;padding:24px 36px;box-sizing:border-box;color:#b9c7bd}.layout{margin:0 36px;max-width:840px}.xgplayer,.bpx-player-container,#player-container-outer{height:470px;position:relative;background:radial-gradient(ellipse at 25% 20%,#4d6959,#25392c 60%,#17221b)}video{display:block;width:100%;height:100%;background:transparent}.caption{position:absolute;bottom:56px;left:50%;transform:translateX(-50%);white-space:nowrap;font-size:18px;padding:8px 16px;background:#0009;border-radius:6px}.xg-right-grid{position:absolute;bottom:8px;right:16px;display:flex}.site-title{padding:20px 0;font-size:20px}.bpx-player-control-bottom{position:absolute;bottom:0;left:0;right:0;height:36px}.description{padding:18px 0;color:#aabbaf;line-height:1.7}</style>
<header>${platform} · 扩展播放器结构夹具</header><main class="layout"><div class="site-title">视频与正文的自然连接</div><div id="player-container-outer" class="${platform==='抖音'?'xgplayer':platform==='B 站'?'bpx-player-container':''}"><video></video><div class="caption">字幕保留在这里，不被通知遮挡</div>${platform==='抖音'?'<div class="xg-right-grid"></div>':''}${platform==='B 站'?'<div class="bpx-player-control-bottom"></div>':''}</div><div class="description">本页只验证扩展布局与反馈，不代表真实平台下载结果。</div></main>`;

async function verifyDouyinFeedbackStartup(browser) {
  const url='https://www.douyin.com/video/1234567890123456';
  const hosts=await fs.readFile(path.join(root,'douyin-hosts.js'),'utf8');
  const source=await fs.readFile(path.join(root,'content.js'),'utf8');
  for(const timing of ['before-first-frame','pending-history']){
    const page=await browser.newPage({viewport:{width:1024,height:840},reducedMotion:'reduce'});
    page.setDefaultTimeout(5000);
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    try{
      await page.route(url,route=>route.fulfill({contentType:'text/html',body:playerHtml('抖音')}));
      await page.addInitScript(()=>{
        window.fixtureHistoryReplies=[];
        globalThis.chrome={runtime:{getManifest:()=>({version:'fixture'}),onMessage:{addListener(listener){window.fixtureMessage=listener;}},sendMessage(message,callback){
          if(message.type==='shengjian-task-state')window.fixtureHistoryReplies.push(callback);
          else callback?.({ok:true});
        }}};
      });
      await page.goto(url);await page.addStyleTag({path:path.join(root,'brand.css')});await page.addStyleTag({path:path.join(root,'content.css')});
      const delivered=`\nfixtureMessage({type:'shengjian-toast',taskUrl:${JSON.stringify(url)},taskId:'fixture-download',stage:'download',message:'下载成功',error:false},{},()=>{});`;
      // Deliver in the same script turn, before the scheduled first frame.
      await page.addScriptTag({content:hosts});
      await page.addScriptTag({content:source+(timing==='before-first-frame'?delivered:'')});
      if(timing==='pending-history'){
        await page.waitForFunction(()=>window.fixtureHistoryReplies.length>0);
        await page.evaluate(url=>{
          fixtureMessage({type:'shengjian-toast',taskUrl:url,taskId:'fixture-download',stage:'download',message:'下载成功',error:false},{},()=>{});
          fixtureHistoryReplies.splice(0).forEach(reply=>reply({ok:true,job:{id:'fixture-download',url,status:'downloading',progress:.12}}));
        },url);
      }
      await page.waitForFunction(()=>document.querySelector('.shengjian-feedback-host')?.shadowRoot.querySelector('.card')?.classList.contains('show'));
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const text=()=>page.locator('.shengjian-feedback-host').evaluate(host=>host.shadowRoot.querySelector('.text').textContent);
      assert.equal(await text(),'下载成功',`${timing}: live terminal feedback must survive startup and late history`);
      await page.evaluate(()=>fixtureMessage({type:'shengjian-toast',taskUrl:'https://www.douyin.com/video/9999999999999999',stage:'failure',message:'下载失败，请在 FoilPage 查看',error:true},{},()=>{}));
      assert.equal(await text(),'下载成功','another video cannot overwrite the current result');
      assert.equal(await page.locator('.shengjian-feedback-host').count(),1,'status updates reuse one card');
      const retryNotice='下载失败，请在 FoilPage 查看（若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。）';
      await page.evaluate(({url,message})=>fixtureMessage({type:'shengjian-toast',taskUrl:url,taskId:'abc123def456',stage:'failure',message,error:true},{},()=>{}),{url,message:retryNotice});
      await page.waitForTimeout(300);
      const retryGuide=await page.locator('.shengjian-feedback-host').evaluate(host=>{
        const root=host.shadowRoot;const card=root.querySelector('.card');const hint=root.querySelector('.hint');
        const cardRect=card.getBoundingClientRect(),captionRect=document.querySelector('.caption').getBoundingClientRect();
        return{main:root.querySelector('.text').textContent,hint:hint.textContent,visible:card.classList.contains('show'),actionable:hint.classList.contains('actionable'),fontSize:getComputedStyle(hint).fontSize,fontWeight:getComputedStyle(hint).fontWeight,coversCaption:cardRect.left<captionRect.right&&cardRect.right>captionRect.left&&cardRect.top<captionRect.bottom&&cardRect.bottom>captionRect.top,card:{top:cardRect.top,bottom:cardRect.bottom},caption:{top:captionRect.top,bottom:captionRect.bottom}};
      });
      assert.equal(retryGuide.main,'下载失败，请在 FoilPage 查看');
      assert.equal(retryGuide.hint,'若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。');
      assert.equal(retryGuide.visible,true,'the retry guidance is visible on terminal failure');
      assert.equal(retryGuide.actionable,true,'the fallback steps use the emphasized hint style');
      assert.equal(retryGuide.fontSize,'13px');
      assert.equal(retryGuide.fontWeight,'600');
      assert.equal(retryGuide.coversCaption,false,`the taller action card remains outside the subtitle/caption area: ${JSON.stringify(retryGuide)}`);
      await page.screenshot({path:path.join(output,`douyin-retry-guide-${timing}.png`),fullPage:true});
      for (const action of ['download','transcribe']) {
        const click = await page.evaluate(action => {
          let preflight;
          chrome.runtime.sendMessage = (msg, cb) => {
            if (msg.type === 'shengjian-check-existing') preflight = cb;
          };
          const start = performance.now();
          document.querySelector(`.shengjian-${action} button`).click();
          const text = document.querySelector('.shengjian-feedback-host').shadowRoot.querySelector('.text').textContent;
          const elapsed = performance.now() - start;
          preflight({ok:true,active:true,activeTranscribing:true});
          return {text,elapsed};
        }, action);
        assert.equal(click.text,'正在提交…','the first click event updates visible status before preflight settles');
        results.push({surface:`douyin-${action}-click`,synchronousFeedbackMs:click.elapsed});
      }
      assert.deepEqual(errors,[]);results.push({surface:`douyin-startup-${timing}`,liveOutcomePreserved:true,lateHistoryIgnored:timing==='pending-history',otherVideoIgnored:true});
      results.push({surface:`douyin-retry-guide-${timing}`,mainNotice:retryGuide.main,actionableHint:true,visible:true});
    }finally{await page.close();}
  }
}

async function verifyCompactPlayerFeedback(browser, [id, label, url, script, stylesheet]) {
  const page = await browser.newPage({ viewport: { width: 352, height: 700 }, reducedMotion: 'reduce' });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.route(url, route => route.fulfill({ contentType: 'text/html', body: playerHtml(label) }));
    await page.addInitScript(() => {
      globalThis.chrome = { runtime: {
        getManifest: () => ({ version: 'fixture' }),
        onMessage: { addListener(listener) { globalThis.fixtureMessage = listener; } },
        sendMessage(_message, callback) { callback?.({ ok: true }); },
      } };
    });
    await page.goto(url);
    await page.addStyleTag({ content: '.layout{margin-inline:12px}.caption{white-space:normal;width:max-content;max-width:calc(100% - 44px)}' });
    await page.addStyleTag({ path: path.join(root, 'brand.css') });
    await page.addStyleTag({ path: path.join(root, stylesheet) });
    if (id === 'douyin') await page.addScriptTag({ path: path.join(root, 'douyin-hosts.js') });
    else await page.addScriptTag({ path: path.join(root, 'ui-contract.js') });
    await page.addScriptTag({ path: path.join(root, script) });
    const message = '正文转写成功，标题处理失败。文稿仍已保留，请在 FoilPage 任务页打开这条记录，核对原稿和整理稿，查看标题处理失败的具体原因。';
    await page.evaluate(({ message, url }) => fixtureMessage({
      type: 'shengjian-toast', stage: 'failure', taskId: 'abc123def456', taskUrl: url, message, error: true,
    }, {}, () => {}), { message, url });
    const selector = id === 'douyin' ? '.shengjian-feedback-host .card' : id === 'bilibili' ? '.shengjian-bili-pill' : '.shengjian-yt-pill';
    const card = page.locator(selector);
    const close = card.getByRole('button', { name: '关闭提示' });
    await card.scrollIntoViewIfNeeded();
    const layout = await card.evaluate((element, id) => {
      const rect = element.getBoundingClientRect();
      const picture = document.querySelector('video').getBoundingClientRect();
      const text = element.querySelector(id === 'douyin' ? '.text' : '.sj-text');
      return {
        wraps: text.offsetHeight > parseFloat(getComputedStyle(text).lineHeight),
        fits: rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight + 1,
        pictureCovered: rect.top < picture.bottom && rect.bottom > picture.top,
      };
    }, id);
    assert.ok(layout.wraps && layout.fits, `${id}: long feedback wraps within the window (${JSON.stringify(layout)})`);
    if (id !== 'douyin') assert.equal(layout.pictureCovered, false, `${id}: feedback remains outside its picture`);
    await page.screenshot({ path: path.join(output, `${id}-compact-error.png`), fullPage: true });
    if (id === 'bilibili') {
      await page.evaluate(() => {
        const bar = document.querySelector('.bpx-player-control-bottom');
        for (const side of ['left', 'right']) {
          const group = document.createElement('div');
          group.className = `bpx-player-control-bottom-${side}`;
          group.style.cssText = `position:absolute;${side}:0;width:40px;height:36px`;
          bar.append(group);
        }
      });
      await page.locator('.shengjian-bili-host.in-controls').waitFor();
      assert.equal(await card.evaluate(element => Boolean(element.closest('.bpx-player-container'))), false,
        'Bilibili error remains outside the player when controls move into the native bar');
    }
    await close.focus();
    await page.keyboard.press('Escape');
    assert.equal(await card.evaluate(element => element.inert), true, `${id}: hidden feedback is immediately inert`);
    if (id === 'douyin') {
      assert.equal(await card.evaluate(element => {
        const rect = element.getBoundingClientRect();
        return document.elementFromPoint(rect.x + 10, rect.y + 10) === element.getRootNode().host;
      }), false, 'closed feedback must release the underlying player');
      for (let i = 0; i < 4; i++) {
        await page.keyboard.press('Tab');
        assert.equal(await card.evaluate(element => element.getRootNode().activeElement !== null), false);
      }
    } else {
      assert.equal(await page.evaluate(() => document.activeElement.dataset.shengjianAction), 'download');
    }
    assert.deepEqual(errors, []);
    results.push({ surface: `${id}-compact-error`, ...layout, dismissibleByKeyboard: true });
  } finally { await page.close(); }
}

async function verifyDouyinPlayerLayout(browser) {
  const url = 'https://www.douyin.com/video/1234567890123456';
  const page = await browser.newPage({ viewport: { width: 1024, height: 840 } });
  page.setDefaultTimeout(5000);
  try {
    await page.route(url, route => route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><meta charset="utf-8"><style>
        body { margin: 0; }
        .feed { position: relative; width: 840px; height: 470px; }
        .xgplayer { position: relative; width: 100%; height: 100%; }
        xg-video-container { display: block; position: absolute; inset: 0 0 48px; overflow: hidden; }
        video { display: block; position: absolute; width: 100%; height: 100%; }
        .xg-right-grid { display: flex; position: absolute; bottom: 8px; right: 16px; width: 160px; height: 32px; }
      </style><div class="feed" data-e2e="feed-active-video" data-e2e-vid="1234567890123456">
        <div class="xgplayer"><xg-video-container class="xg-video-container"><video></video></xg-video-container></div>
      </div>`,
    }));
    await page.addInitScript(() => {
      globalThis.chrome = { runtime: {
        getManifest: () => ({ version: 'fixture' }),
        onMessage: { addListener() {} },
        sendMessage(_message, callback) { callback?.({ ok: true }); },
      } };
    });
    await page.goto(url);
    await page.addStyleTag({ path: path.join(root, 'brand.css') });
    await page.addStyleTag({ path: path.join(root, 'content.css') });
    await page.addScriptTag({ path: path.join(root, 'douyin-hosts.js') });
    await page.addScriptTag({ path: path.join(root, 'content.js') });
    await page.locator('.shengjian-floating-control').first().waitFor();
    const geometry = () => page.evaluate(() => {
      const container = document.querySelector('xg-video-container');
      return {
        position: getComputedStyle(container).position,
        containerHeight: container.getBoundingClientRect().height,
        videoHeight: container.querySelector('video').getBoundingClientRect().height,
        controlParent: document.querySelector('.shengjian-player-control').parentElement.tagName,
        hostCount: document.querySelectorAll('.shengjian-player-host').length,
      };
    });
    await page.evaluate(() => document.querySelector('xg-video-container').classList.add('xgplayer-sr'));
    await page.waitForTimeout(1300);
    const superResolution = await geometry();
    assert.equal(superResolution.position, 'absolute', 'super-resolution must retain the native video positioning');
    assert.equal(superResolution.videoHeight, 422, 'the video must retain its height after super-resolution activates');
    assert.equal(superResolution.controlParent, 'DIV', 'floating controls belong to the outer player');
    assert.equal(superResolution.hostCount, 0, 'a positioned native player needs no extension positioning');

    await page.evaluate(() => {
      const player = document.querySelector('.xgplayer');
      player.style.position = 'absolute';
      document.querySelector('xg-video-container').classList.add('shengjian-player-host');
    });
    await page.waitForTimeout(1300);
    assert.equal(await page.locator('.xgplayer').evaluate(e => getComputedStyle(e).position), 'absolute');
    assert.equal((await geometry()).videoHeight, 422, 'stale inner-host markers must be removed');

    await page.evaluate(() => {
      const player = document.querySelector('.xgplayer');
      player.style.position = 'static';
    });
    await page.waitForTimeout(1300);
    assert.equal(await page.locator('.xgplayer').evaluate(e => getComputedStyle(e).position), 'relative');
    assert.equal((await geometry()).hostCount, 1, 'static fallback hosts still anchor floating controls');

    await page.evaluate(() => {
      const grid = document.createElement('div');
      grid.className = 'xg-right-grid';
      document.querySelector('.xgplayer').appendChild(grid);
    });
    await page.locator('.xg-right-grid .shengjian-player-control').first().waitFor();
    assert.equal((await geometry()).hostCount, 0, 'native controls release the fallback host positioning');
    assert.equal(await page.locator('.shengjian-floating-control').count(), 0);
    assert.equal(await page.locator('.xg-right-grid .shengjian-player-control').count(), 2);
    results.push({ surface: 'douyin-player-layout', superResolution, nativePositionPreserved: true, fallbackReleased: true });
  } finally {
    await page.close();
  }
}

async function verifyPopupPairing(browser, popupUrl) {
  for (const configured of [true, false]) {
    const page = await browser.newPage({viewport:{width:360,height:600},reducedMotion:'reduce'});
    page.setDefaultTimeout(5000);
    let allowed = false, authorized = false;
    const attempts = [];
    const token = 'fixture-token-for-browser-pairing-'.repeat(2);
    try {
      await page.addInitScript(storedToken => {
        let current = storedToken;
        globalThis.chrome = {
          runtime:{getManifest:()=>({version:'0.1.5'}),sendMessage:async()=>({ok:true,tasks:[]})},
          storage:{local:{
            get:async()=>({localApiToken:current}),
            set:async values=>{current=values.localApiToken;},
          }},
          tabs:{query:async()=>[{url:'https://www.youtube.com/watch?v=fixture123'}]},
        };
      }, configured ? token : '');
      await page.route('http://127.0.0.1:18765/**', async route => {
        const endpoint = new URL(route.request().url()).pathname;
        const respond = (json, status = 200) => route.fulfill({
          json, status, headers:{'access-control-allow-origin':'*'},
        });
        if (endpoint === '/health') {
          return respond({service:'VideoToArticle',api_version:'1',configured:configured || authorized,extension_latest:'0.1.5'});
        }
        if (endpoint === '/v1/downloads') {
          if (route.request().method() === 'OPTIONS') {
            return authorized ? route.fulfill({status:204,headers:{'access-control-allow-origin':'*'}})
              : respond({error:{code:'origin_denied',message:'扩展来源未授权'}},403);
          }
          return route.request().headers().authorization === `Bearer ${token}`
            ? respond({jobs:[]}) : respond({error:{code:'unauthorized',message:'连接令牌无效'}},401);
        }
        if (endpoint === '/v1/show') {
          assert.deepEqual(route.request().postDataJSON(),{page:'browser'});
          return respond({shown:true});
        }
        attempts.push(endpoint);
        if (configured && endpoint === '/v1/setup') {
          return respond({error:{code:'origin_denied',message:'已有其他扩展与 FoilPage 连接'}},403);
        }
        if (!allowed) return respond({error:{code:'pairing_closed',message:'请先允许连接'}},403);
        authorized = true;
        return respond({token});
      });

      await page.goto(popupUrl);
      await page.waitForFunction(() => document.querySelector('#connectionText').textContent === '未连接');
      assert.equal(await page.locator('#pagePanel').isVisible(),false,'a saved but rejected token cannot expose page actions');
      if (configured) {
        assert.match(await page.locator('#message').textContent(),/重新配对/);
        await page.locator('#connect').click();
        await page.waitForFunction(() => !document.querySelector('#connect').disabled);
        assert.equal(authorized,false,'switching cannot succeed before desktop approval');
        await page.locator('#openApp').click();
        await page.waitForFunction(() => document.querySelector('#message').textContent.includes('已在 FoilPage'));
        assert.match(await page.locator('#message').textContent(),/重新配对/);
      }
      allowed = true;
      await page.locator('#connect').click();
      await page.waitForFunction(() => document.querySelector('#connectionText').textContent === '已连接');
      await page.locator('#pagePanel').waitFor();
      assert.equal(await page.locator('#openApp').isVisible(),false);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(attempts,configured ? ['/v1/pair','/v1/pair'] : ['/v1/setup']);
    } finally {
      await page.close();
    }
  }
  results.push({surface:'popup-pairing',switchAfterApproval:true,firstSetup:true,rejectedToken:true});
}

async function verifyPlayerLaunch(browser, [id, label, url, script, stylesheet]) {
  const page = await browser.newPage({viewport:{width:1024,height:840},reducedMotion:'reduce'});
  page.setDefaultTimeout(6000);
  try {
    await page.route(url, route => route.fulfill({contentType:'text/html',body:playerHtml(label)}));
    await page.addInitScript(() => {
      window.submissions = [];
      window.launches = [];
      globalThis.chrome = {runtime:{getManifest:()=>({version:'0.1.5'}),onMessage:{addListener(){}},
        sendMessage(message, callback) {
          if (message.type !== 'shengjian-page-submit') { callback?.({ok:true}); return; }
          window.submissions.push(message.transcribe);
          callback(message.transcribe ? {ok:false,needs_connection:true}
            : {ok:true,job:{id:'fixture-request',queued:true}});
        },
      }};
      document.addEventListener('click', event => {
        const link = event.target.closest('a[href="foilpage://launch"]');
        if (!link) return;
        event.preventDefault();
        window.launches.push(navigator.userActivation.isActive);
      }, true);
    });
    await page.goto(url);
    await page.addStyleTag({path:path.join(root,'brand.css')});
    await page.addStyleTag({path:path.join(root,stylesheet)});
    if (id === 'douyin') await page.addScriptTag({path:path.join(root,'douyin-hosts.js')});
    else await page.addScriptTag({path:path.join(root,'ui-contract.js')});
    await page.addScriptTag({path:path.join(root,script)});
    for (const action of ['download','transcribe']) {
      const selector = `[data-shengjian-action="${action}"]`;
      await page.locator(id === 'douyin' ? `${selector} button` : selector).first().click();
      const open = page.getByText('打开 FoilPage',{exact:true});
      if (id === 'douyin') {
        assert.equal(await page.getByRole('dialog').getByRole('button', { name: '取消' }).evaluate(
          button => button.getRootNode().activeElement === button), true);
      }
      await open.focus();
      await page.keyboard.press('Enter');
      const state = await page.evaluate(() => ({submissions:window.submissions, launches:window.launches}));
      assert.deepEqual(state.submissions,action === 'download' ? [false] : [false,true]);
      assert.deepEqual(state.launches,action === 'download' ? [true] : [true,true]);
    }
    results.push({surface:`${id}-open-desktop`,queuedDownload:true,needsPairingTranscribe:true,userActivation:true});
  } finally { await page.close(); }
}

async function main(){
  await fs.mkdir(output,{recursive:true});
  const server = http.createServer(async(req,res)=>{
    try{const name = new URL(req.url,'http://localhost').pathname.replace(/^\//,'') || 'popup.html';
      const target=path.resolve(root,name);if(!target.startsWith(root+path.sep)){res.writeHead(403);res.end();return;}
      const data=await fs.readFile(target);res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript; charset=utf-8':name.endsWith('.css')?'text/css; charset=utf-8':name.endsWith('.png')?'image/png':'text/html; charset=utf-8');res.end(data);
    }catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true});
  try {
    await verifyPopupPairing(browser,`http://127.0.0.1:${server.address().port}/popup.html`);
    await verifyDouyinPlayerLayout(browser);
    await verifyDouyinFeedbackStartup(browser);
    for(const theme of ['light','dark']){
      const page=await browser.newPage({viewport:{width:360,height:600},colorScheme:theme,reducedMotion:'reduce'});
      await page.addInitScript(()=>{globalThis.chrome={runtime:{getManifest:()=>({version:'0.14.0'}),sendMessage:async()=>({ok:true,tasks:[{id:'1',title:'为什么记录比记忆更可靠',status:'downloading',progress:.42},{id:'2',title:'把视频变成可以回看的文章',status:'transcribing',progress:.68},{id:'3',title:'注意力与长期思考',status:'done',job_id:'asr'}]})},storage:{local:{get:async()=>({localApiToken:'fixture-only'})}},tabs:{query:async()=>[{id:1,url:'https://www.youtube.com/watch?v=fixture123',title:'视频与正文的自然连接 - YouTube'}]}};});
      await page.route('http://127.0.0.1:18765/**',route=>route.fulfill({contentType:'application/json',headers:{'access-control-allow-origin':'*'},body:JSON.stringify({ok:true,service:'VideoToArticle',api_version:'1',extension_latest:'0.14.0'})}));
      await page.goto(`http://127.0.0.1:${server.address().port}/popup.html`);
      await page.waitForSelector('.job-row');
      assert.equal(await page.locator('#connectionText').textContent(),'已连接');
      assert.equal(await page.locator('#connectionPanel').isHidden(),true,'confirmed pairing leaves the current video as the primary surface');
      const sizes=await page.locator('.page-btn').evaluateAll(buttons=>buttons.map(button=>button.getBoundingClientRect().height));
      assert.ok(sizes.every(size=>size>=44));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:path.join(output,`popup-${theme}.png`),fullPage:true});
      results.push({surface:`popup-${theme}`,buttons:sizes,overflow:false});await page.close();
    }
    const offline=await browser.newPage({viewport:{width:360,height:600},reducedMotion:'reduce'});
    await offline.addInitScript(()=>{
      window.fixtureJobs=[{id:'fixture-request-000000000001',title:'划走后继续下载的原视频',status:'handoff_pending',queued:true},{id:'abc123def456',title:'离线前已下载到 42% 的视频',status:'downloading',progress:.42,waiting_connection:true}];
      globalThis.chrome={runtime:{getManifest:()=>({version:'fixture'}),sendMessage:async(message,callback)=>{
        if(message.type==='shengjian-cancel-task'){const job=window.fixtureJobs.find(job=>job.id===message.id);if(job)job.status='cancelled';}
        const response=message.type==='shengjian-authorized-status'?{ok:true,tasks:window.fixtureJobs,connection:'offline'}:{ok:true,job:{queued:true}};
        callback?.(response);return response;
      }},storage:{local:{get:async()=>({localApiToken:'fixture-only'})}},tabs:{query:async()=>[{id:1,url:'https://www.youtube.com/watch?v=fixture123',title:'当前视频与后台任务 - YouTube'}]}};
    });
    await offline.route('http://127.0.0.1:18765/**',route=>route.abort('connectionrefused'));
    await offline.goto(`http://127.0.0.1:${server.address().port}/popup.html`);
    await offline.waitForSelector('.job-cancel');
    assert.equal(await offline.locator('#connectionText').textContent(),'未连接');
    assert.equal(await offline.locator('#pagePanel').isVisible(),true,'paired offline popup retains page actions');
    for (const selector of ['#pageDownload','#pageTranscribe']) {
      await offline.locator(selector).click();
      assert.match(await offline.locator('#pageState').textContent(),/任务已暂存/);
      assert.equal(await offline.getByRole('link',{name:'打开 FoilPage'}).getAttribute('href'),'foilpage://launch');
    }
    assert.ok((await offline.locator('#jobs').textContent()).includes('连接恢复后继续'));
    assert.equal(await offline.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await offline.screenshot({path:path.join(output,'popup-offline-queue.png'),fullPage:true});
    await offline.locator('.job-cancel').first().click();
    await offline.waitForFunction(()=>window.fixtureJobs[0].status==='cancelled');
    assert.ok((await offline.locator('#jobs').textContent()).includes('已取消'));
    results.push({surface:'popup-offline',durableWaiting:true,cancellation:true,overflow:false});
    await offline.close();
    const pages=[['douyin','抖音','https://www.douyin.com/video/1234567890123456','content.js','content.css'],['bilibili','B 站','https://www.bilibili.com/video/BV1fixture123','content-bili.js','content-bili.css'],['youtube','YouTube','https://www.youtube.com/watch?v=fixture123','content-youtube.js','content-youtube.css']];
    for(const [id,label,url,js,css] of pages){
      await verifyPlayerLaunch(browser,[id,label,url,js,css]);
      await verifyCompactPlayerFeedback(browser,[id,label,url,js,css]);
      const page=await browser.newPage({viewport:{width:1024,height:840},reducedMotion:'reduce'});
      page.on('pageerror',e=>console.log('PAGEERROR['+id+']:',e.message));
      await page.route(url,route=>route.fulfill({contentType:'text/html',body:playerHtml(label)}));
      await page.addInitScript(()=>{globalThis.chrome={runtime:{getManifest:()=>({version:'0.14.0'}),onMessage:{addListener(listener){globalThis.fixtureMessage=listener;}},sendMessage(message,callback){callback?.({ok:true,job:message.type==='shengjian-task-state'&&window.fixtureRestoredTask?.url===message.url?window.fixtureRestoredTask:null});}}};});
      await page.goto(url);
      await page.addStyleTag({path:path.join(root,'brand.css')});
      await page.addStyleTag({path:path.join(root,css)});
      if (js==='content.js') await page.addScriptTag({path:path.join(root,'douyin-hosts.js')});
      else await page.addScriptTag({path:path.join(root,'ui-contract.js')});
      await page.addScriptTag({path:path.join(root,js)});
      await page.evaluate(()=>fixtureMessage({type:'shengjian-toast',message:'下载成功',error:false}, {},()=>{}));
      await page.waitForTimeout(100);
      const geometry=await page.evaluate(id=>{
        const video=document.querySelector('video').getBoundingClientRect();
        const feedback=id==='douyin'?document.querySelector('.shengjian-feedback-host')?.shadowRoot.querySelector('.card'):document.querySelector(id==='bilibili'?'.shengjian-bili-pill':'.shengjian-yt-host');
        const rect=feedback?.getBoundingClientRect();
        const intersects=rect&&rect.left<video.right&&rect.right>video.left&&rect.top<video.bottom&&rect.bottom>video.top;
        return{intersects:Boolean(intersects),video:{top:video.top,bottom:video.bottom},feedback:rect?{top:rect.top,bottom:rect.bottom}:null};
      },id);
      if (id === 'douyin') {
        // 先生 2026-10-08 裁决：抖音反馈固定在播放器进度条上方居中（允许
        // 短暂覆盖画面下缘），不再做「页面空白处」自适应。
        assert.ok(geometry.feedback, 'douyin feedback card must render');
        assert.ok(Math.abs(geometry.feedback.top - (geometry.video.bottom - 88)) < 24,
          `douyin feedback card top must anchor ~88px above the progress bar: ${JSON.stringify(geometry)}`);
      } else {
        assert.equal(geometry.intersects,false,`${id} feedback must stay outside the picture`);
      }
      if(id !== 'douyin') assert.equal(await page.locator('.sj-text').textContent(),'下载成功');
      await page.screenshot({path:path.join(output,`${id}-fixture.png`),fullPage:true});results.push({surface:id,...geometry});
      // Return to an original video: read its retained progress without
      // replaying a terminal-success event or using the currently selected video.
      await page.evaluate(({url,id})=>{
        window.fixtureRestoredTask={id:'abc123def456',url,title:'原视频',status:'downloading',progress:.64};
        history.pushState({},'',id==='douyin'?'/video/9999999999999999':id==='bilibili'?'/video/BV1different':'/watch?v=different123');
        dispatchEvent(new PopStateEvent('popstate'));
      },{url,id});
      await page.waitForTimeout(1300);
      await page.evaluate(url=>{history.pushState({},'',url);dispatchEvent(new PopStateEvent('popstate'));},url);
      await page.waitForTimeout(1300);
      const restoredText=await page.evaluate(id=>id==='douyin'?document.querySelector('.shengjian-feedback-host')?.shadowRoot.querySelector('.text')?.textContent:document.querySelector('.sj-num b')?.textContent,id);
      if(id==='douyin') {
        assert.equal(restoredText,'已提交下载','Douyin restores the accepted receipt without intermediate progress');
        assert.equal(await page.locator('.shengjian-feedback-host').evaluate(host=>host.shadowRoot.querySelector('.bar')),null,'Douyin feedback has no progress bar');
      } else assert.ok(String(restoredText).includes('64'),`${id} restores the original video progress`);
      await page.screenshot({path:path.join(output,`${id}-restored-progress.png`),fullPage:true});
      results.push({surface:`${id}-restored`,originalProgress:id==='douyin'?null:64,receiptOnly:id==='douyin'});
      if(id==='douyin'){
        await page.evaluate(()=>{const video=document.querySelector('video');video.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;z-index:100;';fixtureMessage({type:'shengjian-toast',stage:'download',taskId:'abc123def456',message:'下载成功',error:false},{},()=>{});});
        const visible=await page.locator('.shengjian-feedback-host').evaluate(host=>host.shadowRoot.querySelector('.card').classList.contains('show'));
        // 先生 2026-10-08：全屏播放同样在进度条上方显示反馈（终态仍另有
        // 原生通知与弹窗），不再因覆盖画面而隐藏。
        assert.equal(visible,true,'Fullscreen video still shows feedback above the progress bar');
        await page.evaluate(()=>{
          for(const [stage,message] of [['progress','浏览器下载中 50%'],['transcribe','转写成功'],['failure','下载失败，请在 FoilPage 查看']])
            fixtureMessage({type:'shengjian-toast',stage,taskId:'abc123def456',message,error:stage==='failure'},{},()=>{});
        });
        assert.equal(await page.locator('.shengjian-feedback-host').evaluate(host=>host.shadowRoot.querySelector('.text').textContent),'下载成功','Intermediate progress, ASR and late errors cannot replace download success');
      }
      await page.close();
    }
    await fs.writeFile(path.join(output,'results.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify({passed:true,output,results},null,2));
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
