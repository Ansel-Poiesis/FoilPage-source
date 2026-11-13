// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Local fixture checks for keyboard paths, themes and narrow advanced forms. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '_webview_ui_shots', 'quality');
fs.mkdirSync(output, {recursive:true});

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:800}});
    const errors = [];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(require('./ui_fixture.cjs'),{nativeChrome:true});
    await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
    await page.waitForSelector('.nav-row[data-page="workbench"]');
    await page.evaluate(()=>switchView('workbench'));
    await page.waitForSelector('.nav-row[aria-current=page]');
    await page.locator('[data-page=workbench]').click();
    await page.waitForSelector('.job-open');
    await page.locator('.job-open').first().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.job-open').first().getAttribute('aria-pressed'),'true');
    assert(await page.locator('#inspector .t').textContent());
    await page.evaluate(()=>{
      AppState.tasks[0].display_title='声音记录与整理';
      Workbench.render();
    });
    assert.equal(await page.locator('.job-title').first().textContent(),'声音记录与整理');
    assert(await page.locator('.job-open').first().evaluate(e=>e===document.activeElement),'task updates keep keyboard focus');

    await page.evaluate(()=>openModal('确认操作','<p>测试对话框</p>','<button class="btn" id="qualityCancel">返回</button>'));
    assert(await page.locator('.modal[role="dialog"][aria-modal="true"]').isVisible());
    assert.equal(await page.locator('.app').evaluate(e=>e.inert),true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.className),'modal-x');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'qualityCancel');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.modal').count(),0);
    assert.equal(await page.locator('.app').evaluate(e=>e.inert),false);
    assert(await page.locator('.job-open').first().evaluate(e=>e===document.activeElement));

    const themeEvidence = [];
    let headingFonts = [];
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    for (const theme of ['dark','paper','dracula','tokyo-night','nord','mocha','gruvbox','rose-pine','latte','nord-light']) {
      await page.evaluate(theme=>{AppState.settings.theme=theme;applyTheme();switchView('workbench');},theme);
      const css = await page.locator('.sidebar').evaluate(e=>({font:getComputedStyle(e).fontFamily,shadow:getComputedStyle(e).boxShadow,bg:getComputedStyle(e).backgroundColor}));
      assert.notEqual(css.shadow,'none',`shell depth resolves for ${theme}`);
      assert.match(css.font,/Segoe UI Variable/);
      await page.mouse.move(0,0);
      await page.screenshot({animations:'disabled',path:path.join(output,`theme-${theme}.png`)});
      themeEvidence.push({theme,...css});
    }
    await page.evaluate(()=>{AppState.settings.theme='dark';applyTheme();});
    for(const width of [1280,960,640,560]) {
      await page.setViewportSize({width,height:800});
      await page.locator('.nav-row[data-page="workbench"]').click();
      await page.locator('.job-open').first().click();
      await page.screenshot({animations:'disabled',path:path.join(output,`tasks-selected-${width}.png`)});
      await page.locator('.nav-row[data-page="library"]').click();
      await page.waitForSelector('.doc-open');
      await page.locator('.doc-open').first().focus();
      await page.keyboard.press('Enter');
      await page.waitForSelector('.reader-body h1');
      await page.locator('#subtitleBtn').click();
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.fmt),'srt');
      await page.keyboard.press('ArrowDown');
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.fmt),'vtt');
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.ctx-menu').count(),0);
      assert(await page.locator('#subtitleBtn').evaluate(e=>e===document.activeElement));
      await page.screenshot({animations:'disabled',path:path.join(output,`reader-${width}.png`)});
      await page.locator('.nav-row[data-page="settings"]').click();
      await page.locator('.sitem').filter({hasText:'文本整理'}).click();
      if (width === 1280) {
        const {root:doc}=await cdp.send('DOM.getDocument');
        const {nodeId}=await cdp.send('DOM.querySelector',{nodeId:doc.nodeId,selector:'#page-settings .set-group h3'});
        headingFonts=(await cdp.send('CSS.getPlatformFontsForNode',{nodeId})).fonts;
      }
      const titleDetails=page.locator('details').filter({has:page.locator('summary').filter({hasText:'标题模型与规则'})});
      await titleDetails.locator('summary').click();
      const textarea=titleDetails.locator('textarea');
      await textarea.scrollIntoViewIfNeeded();
      await page.screenshot({animations:'disabled',path:path.join(output,`title-rules-${width}.png`)});
      assert.equal(await textarea.count(),1);
      const titleBounds=await textarea.evaluate(e=>({width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right}));
      assert(titleBounds.width>170 && titleBounds.right<width,`title rules stay usable at ${width}`);
      await page.locator('.nav-row[data-page="subscription"]').click();
      await page.locator('#subscriptionTools > summary').click();
      const serviceDetails=page.locator('#subscriptionConnectionDetails');
      if (!await serviceDetails.evaluate(node=>node.open)) await serviceDetails.locator('summary').click();
      await serviceDetails.locator('input').last().scrollIntoViewIfNeeded();
      await page.screenshot({animations:'disabled',path:path.join(output,`service-connection-${width}.png`)});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`no horizontal overflow at ${width}`);
      const inputs=await serviceDetails.locator('input').evaluateAll(els=>els.map(e=>({width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})));
      assert(inputs.every(e=>e.width>100 && e.right<=width),`service inputs stay inside window at ${width}`);
    }
    await page.setViewportSize({width:1280,height:800});
    await page.evaluate(()=>{AppState.settings.theme='paper';applyTheme();switchView('library');});
    await page.waitForSelector('.doc-open');
    await page.locator('.doc-open').first().click();
    await page.screenshot({animations:'disabled',path:path.join(output,'reader-paper.png')});
    await page.evaluate(()=>{AppState.settings.theme='dark';applyTheme();switchView('links');});
    await page.mouse.move(0,0);
    await page.screenshot({animations:'disabled',path:path.join(output,'links-dark.png')});
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.locator('#linkForm .btn.primary').evaluate(e=>getComputedStyle(e).transitionDuration),'0s');
    await page.emulateMedia({reducedMotion:'no-preference'});
    for (const size of [{width:1440,height:900},{width:1280,height:800},{width:960,height:800},{width:640,height:800},{width:560,height:500}]) {
      await page.setViewportSize(size);
      for (const view of ['setup','workbench','videos','library','subscription','settings','links','import','browser']) {
        await page.evaluate(view=>switchView(view),view);
        if (view==='videos') await page.waitForSelector('.video-card');
        if (view==='settings') await page.locator('.sitem[data-section="agent"]').click();
        assert.equal(await page.locator('#windowChrome').isVisible(),true);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,view+' stays within '+size.width+'x'+size.height);
        const chrome=await page.locator('.window-control').evaluateAll(els=>els.map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})));
        assert(chrome.every(e=>e.width===44&&e.height===44),'window controls keep full click targets');
        await page.mouse.move(0,0);
        assert(await page.locator('#windowChrome').evaluate(node=>node.parentElement.classList.contains('topbar')),'window controls share the title row');
        await page.screenshot({animations:'disabled',path:path.join(output,`integrated-${view}-${size.width}x${size.height}.png`)});
      }
      await page.evaluate(() => switchView('guide'));
      await page.screenshot({animations:'disabled',path:path.join(output,`integrated-guide-${size.width}x${size.height}.png`)});
      const guide=await page.locator('.ob-card').boundingBox();
      const header=await page.locator('.topbar').boundingBox();
      assert(guide.x>=0&&guide.y>=header.y+header.height&&guide.x+guide.width<=size.width+1&&guide.y+guide.height<=size.height+1,'guide fits below integrated title controls');
      assert(await page.locator('.ob-skip').evaluate(node=>{const r=node.getBoundingClientRect();return document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('.ob-skip')===node;}),'guide dismissal stays clickable');
      await page.keyboard.press('Escape');
      await page.evaluate(()=>openModal('确认操作','<p>内容与窗口控件保持清晰层级。</p>','<button class="btn" id="integratedModalCancel">返回</button>'));
      assert(await page.locator('#windowHide').evaluate(node=>{const r=node.getBoundingClientRect();return !!document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('.modal-mask');}),'modal covers underlying window controls');
      await page.screenshot({animations:'disabled',path:path.join(output,`integrated-modal-${size.width}x${size.height}.png`)});
      await page.keyboard.press('Escape');
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'desktop-evidence.json'),JSON.stringify({widths:[1440,1280,960,640,560],themes:themeEvidence,headingFonts,checks:['keyboard task selection','dialog focus trap + escape + return','native document actions','subtitle menu arrow keys','narrow title rules','narrow subscription inputs','reduced motion','integrated window title controls','all nine pages','video cards','guide and modal layering','1440x900 and 560x500 overflow'],errors},null,2));
    console.log('Desktop keyboard, 10 themes and advanced forms passed at 1280 / 960 / 640 / 560.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
