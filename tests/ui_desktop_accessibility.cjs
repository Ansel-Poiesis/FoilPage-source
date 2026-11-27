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
    await page.addInitScript(require('./ui_fixture.cjs'),{nativeChrome:true,taskCover:true});
    await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
    await page.waitForSelector('.nav-row[data-page="workbench"]');
    for (const width of [1280,1120,960,700,640,560]) {
      await page.setViewportSize({width,height:640});
      await page.locator('.nav-row[data-page="settings"]').scrollIntoViewIfNeeded();
      const creditLayout=await page.evaluate(()=>({
        footer:document.querySelector('#authorCredit').getBoundingClientRect().toJSON(),
        nav:document.querySelector('#nav').getBoundingClientRect().toJSON(),
        settings:document.querySelector('.nav-row[data-page="settings"]').getBoundingClientRect().toJSON(),
        height:innerHeight,
      }));
      assert(creditLayout.footer.bottom<=creditLayout.height,'author credit remains inside short windows');
      assert(creditLayout.nav.bottom<=creditLayout.footer.top,'navigation cannot overlap the author credit');
      assert(creditLayout.settings.bottom<=creditLayout.footer.top,'settings remains reachable above the credit');
      await page.evaluate(()=>switchView('workbench'));
      await page.waitForSelector('.job-cover');
      const taskLayout=await page.evaluate(()=>{
        const list=document.querySelector('#jobList');
        const row=document.querySelector('.job:has(.job-cover)');
        const rect=selector=>row.querySelector(selector).getBoundingClientRect().toJSON();
        return {scrollWidth:list.scrollWidth,width:list.clientWidth,title:rect('.job-title'),cover:rect('.job-cover'),stages:rect('.job-stages'),remove:rect('.job-delete')};
      });
      await page.screenshot({animations:'disabled',path:path.join(output,`cover-layout-${width}.png`)});
      assert(taskLayout.scrollWidth<=taskLayout.width+1,`task list has no horizontal overflow at ${width}`);
      assert(taskLayout.cover.right<=taskLayout.title.left,`cover stays beside the task title at ${width}`);
      assert(taskLayout.title.right<=taskLayout.stages.left,`task title stays before stages at ${width}`);
      assert(taskLayout.stages.right<=taskLayout.remove.left,`delete stays after stages at ${width}`);
      const shellLayout=await page.evaluate(()=>{
        const brand=document.querySelector('.brand');
        const nav=document.querySelector('#nav');
        return {brandWidth:brand.clientWidth,brandContent:brand.scrollWidth,navWidth:nav.clientWidth,navContent:nav.scrollWidth,credit:document.querySelector('#authorCredit').getBoundingClientRect().toJSON()};
      });
      assert(shellLayout.brandContent<=shellLayout.brandWidth+1,`brand fits the sidebar at ${width}`);
      assert(shellLayout.navContent<=shellLayout.navWidth+1,`navigation has no horizontal scrollbar at ${width}`);
      assert(shellLayout.credit.width>30 && shellLayout.credit.left>=0,`author link remains inside the sidebar at ${width}`);
      if(width<=1000) {
        await page.evaluate(()=>{AppState.selected.clear();Workbench.render();});
        await page.locator('.job[data-task-id="job:1"] .job-open').click();
        const details=await page.locator('#inspector').evaluate(node=>{
          const panel=node.getBoundingClientRect();
          const cover=node.querySelector('.inspector-cover').getBoundingClientRect();
          const actions=node.querySelector('.actions').getBoundingClientRect();
          return {coverHeight:cover.height,actionsVisible:actions.top>=panel.top && actions.bottom<=panel.bottom+1};
        });
        assert(details.coverHeight<=72,`compact inspector uses a thumbnail at ${width}`);
        assert(details.actionsVisible,`compact inspector keeps actions visible at ${width}`);
      }
    }
    await page.setViewportSize({width:560,height:500});
    await page.evaluate(()=>{
      AppState.settings.font_scale=1.3;
      AppState.tasks[1].display_title='从声音到文字：保留访谈原话、例子与限定条件，让一篇完整的长记录清晰可读';
      applyTheme(); Workbench.render();
    });
    const compactActions=await page.locator('#inspector .actions button').evaluateAll(buttons=>buttons.filter(button=>button.getClientRects().length).map(button=>{
      const rect=button.getBoundingClientRect();
      return !!document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2)?.closest('.actions');
    }));
    assert(compactActions.length>0 && compactActions.every(Boolean),'inspector actions remain reachable in a scaled 560x500 window');
    assert(await page.locator('#inspector').evaluate(node=>node.querySelector('.pct').getBoundingClientRect().bottom<=node.querySelector('.actions').getBoundingClientRect().top),'compact progress stays above the pinned actions');
    await page.evaluate(()=>{AppState.settings.font_scale=1;applyTheme();});
    await page.setViewportSize({width:1280,height:800});
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
    const libraryFonts = {};
    let headingFonts = [];
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    async function wenKaiGlyphs(selector) {
      await page.evaluate(()=>document.fonts.ready);
      const style = await page.locator(selector).first().evaluate(node=>({
        family:getComputedStyle(node).fontFamily, editor:node instanceof HTMLTextAreaElement,
      }));
      assert.match(style.family,/^"LXGW WenKai Screen"/,selector+' uses WenKai first');
      const {root:doc} = await cdp.send('DOM.getDocument');
      let {nodeId} = await cdp.send('DOM.querySelector',{nodeId:doc.nodeId,selector});
      if (style.editor) {
        // Chromium lays out textarea values in its own shadow DOM.
        const {node} = await cdp.send('DOM.describeNode',{nodeId,depth:3,pierce:true});
        const editable = node.shadowRoots?.[0]?.children?.[0];
        assert(editable?.backendNodeId,selector+' contains rendered editable text');
        const {nodeIds} = await cdp.send('DOM.pushNodesByBackendIdsToFrontend',{backendNodeIds:[editable.backendNodeId]});
        [nodeId] = nodeIds;
      }
      const {fonts} = await cdp.send('CSS.getPlatformFontsForNode',{nodeId});
      assert(fonts.some(font=>font.postScriptName==='LXGWWenKaiScreen' && font.glyphCount>0 && font.isCustomFont),
        selector+' renders with the bundled WenKai glyphs');
      return fonts;
    }
    for (const theme of ['dark','paper','dracula','tokyo-night','nord','mocha','gruvbox','rose-pine','latte','nord-light']) {
      await page.evaluate(theme=>{AppState.settings.theme=theme;applyTheme();switchView('workbench');},theme);
      const css = await page.locator('.sidebar').evaluate(e=>({font:getComputedStyle(e).fontFamily,shadow:getComputedStyle(e).boxShadow,bg:getComputedStyle(e).backgroundColor}));
      assert.notEqual(css.shadow,'none',`shell depth resolves for ${theme}`);
      assert.doesNotMatch(css.font,/^"LXGW WenKai Screen"/,'Operate UI chrome uses clean sans-serif');
      assert.match(css.font,/Noto Sans SC|Microsoft YaHei UI|Segoe UI|sans-serif/,'Operate UI chrome resolves modern sans-serif stack');
      const navigationFonts = ['dark','paper'].includes(theme) ? await page.locator('.nav-row[data-page="library"]').evaluate(n=>getComputedStyle(n).fontFamily) : undefined;
      const scrollbarContrast=await page.locator('#nav').evaluate(node=>{
        const canvas=document.createElement('canvas'); canvas.width=canvas.height=1;
        const context=canvas.getContext('2d',{willReadFrequently:true});
        const luminance=color=>{
          context.fillStyle=color; context.fillRect(0,0,1,1);
          const channels=[...context.getImageData(0,0,1,1).data].slice(0,3).map(value=>{
            const channel=value/255;
            return channel<=.04045 ? channel/12.92 : ((channel+.055)/1.055)**2.4;
          });
          return channels[0]*.2126+channels[1]*.7152+channels[2]*.0722;
        };
        const thumb=luminance(getComputedStyle(node,'::-webkit-scrollbar-thumb').backgroundColor);
        const surface=luminance(getComputedStyle(node.closest('.sidebar')).backgroundColor);
        return (Math.max(thumb,surface)+.05)/(Math.min(thumb,surface)+.05);
      });
      assert(scrollbarContrast>=3,`scrollbar remains visible in ${theme}, contrast ${scrollbarContrast.toFixed(2)}`);
      await page.mouse.move(0,0);
      await page.screenshot({animations:'disabled',path:path.join(output,`theme-${theme}.png`)});
      themeEvidence.push({theme,...css,scrollbarContrast,navigationFonts});
    }
    await page.evaluate(()=>{AppState.settings.theme='dark';applyTheme();});
    for(const width of [1280,960,640,560]) {
      await page.setViewportSize({width,height:800});
      await page.locator('.nav-row[data-page="workbench"]').click();
      await page.locator('.job-open').first().click();
      await page.screenshot({animations:'disabled',path:path.join(output,`tasks-selected-${width}.png`)});
      await page.locator('.nav-row[data-page="library"]').click();
      if(width<=1000 && await page.locator('#readerBack').isVisible()) await page.locator('#readerBack').click();
      await page.waitForSelector('.doc-open');
      await page.locator('.doc-open').first().focus();
      await page.keyboard.press('Enter');
      await page.waitForSelector('.reader-body p');
      if (width === 1280) {
        libraryFonts.directory = await page.locator('.doc-title').first().evaluate(n=>getComputedStyle(n).fontFamily);
        assert.doesNotMatch(libraryFonts.directory,/^"LXGW WenKai Screen"/,'catalog list uses clean sans-serif');
        libraryFonts.heading = await wenKaiGlyphs('.reader-doc-title');
        libraryFonts.body = await wenKaiGlyphs('#readerBody > p');
        await page.locator('#editReaderBtn').click();
        libraryFonts.editor = await wenKaiGlyphs('.reader-editor');
        await page.locator('#cancelReaderBtn').click();
        await page.waitForSelector('#readerBody > p');
      }
      const documentMenu = page.locator('#readerMenuBtn');
      await documentMenu.focus();
      await page.keyboard.press('Enter');
      const subtitles = page.getByRole('menuitem',{name:'导出转写字幕…',exact:true});
      await subtitles.focus();
      await page.keyboard.press('Enter');
      await page.waitForFunction(()=>document.activeElement.dataset.fmt==='srt');
      await page.keyboard.press('ArrowDown');
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.fmt),'vtt');
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.ctx-menu').count(),0);
      assert(await documentMenu.evaluate(e=>e===document.activeElement));
      await page.screenshot({animations:'disabled',path:path.join(output,`reader-${width}.png`)});
      if (width > 1000) {
        const separator = page.getByRole('separator',{name:'文稿列表宽度'});
        await separator.focus();
        const originalWidth = Number(await separator.getAttribute('aria-valuenow'));
        await page.keyboard.press('ArrowRight');
        assert(Number(await separator.getAttribute('aria-valuenow')) > originalWidth,'keyboard can widen the document list');
        await page.keyboard.press('Enter');
        assert.equal(Number(await separator.getAttribute('aria-valuenow')),originalWidth,'keyboard can restore the default list width');
      } else {
        await page.locator('#readerBack').focus();
        await page.keyboard.press('Enter');
        assert(await page.locator('.doc-open').first().evaluate(e=>e===document.activeElement),'return restores focus to the document');
      }
      await page.locator('.nav-row[data-page="settings"]').click();
      await page.locator('.sitem').filter({hasText:'文本整理'}).click();
      if (width === 1280) {
        headingFonts = await page.locator('#page-settings .set-group h3').first().evaluate(n=>getComputedStyle(n).fontFamily);
        assert.doesNotMatch(headingFonts,/^"LXGW WenKai Screen"/,'settings headings use clean sans-serif');
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
    libraryFonts.paperHeading = await wenKaiGlyphs('.reader-doc-title');
    libraryFonts.paperBody = await wenKaiGlyphs('#readerBody > p');
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
    fs.writeFileSync(path.join(output,'desktop-evidence.json'),JSON.stringify({widths:[1440,1280,960,640,560],themes:themeEvidence,headingFonts,libraryFonts,checks:['dual-voice typography: sans-serif shell + bundled WenKai reader','keyboard task selection','dialog focus trap + escape + return','native document actions','subtitle menu arrow keys','narrow title rules','narrow subscription inputs','reduced motion','integrated window title controls','all nine pages','video cards','guide and modal layering','1440x900 and 560x500 overflow'],errors},null,2));
    console.log('Desktop keyboard, 10 themes and advanced forms passed at 1280 / 960 / 640 / 560.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
