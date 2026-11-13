// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Real Chromium interactions; backend operations and stored progress use isolated fixtures.
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import installMock from "./ui_fixture.cjs";

const root = path.resolve(import.meta.dirname, "..");
const shots = path.join(root, "_webview_ui_shots", "onboarding");
const chapters = [{key:"environment",length:5,start:0},{key:"browser",length:4,start:5},{key:"application",length:7,start:9}];
let browser;
before(async () => { fs.mkdirSync(shots, {recursive:true}); browser = await chromium.launch({headless:true}); });
after(async () => { await browser?.close(); });
async function open(options = {}, size = {width:1280,height:800}) {
  const page = await browser.newPage({viewport:size});
  page.setDefaultTimeout(5000);
  page.errors = [];
  page.on("pageerror", e => page.errors.push(e.message));
  await page.addInitScript(installMock, options);
  // Fixture navigation is not a cold font/file-load performance benchmark.
  // Keep the separate 5s control and business assertions unchanged.
  await page.goto(pathToFileURL(path.join(root, "app/webview_ui/index.html")).href, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector(".nav-row[aria-current=page]");
  if (!options.firstRun) { await page.locator(".nav-row[data-page=workbench]").click(); await page.waitForSelector(".job"); }
  return page;
}
async function settle(page) { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); }
async function begin(page, chapter) { await page.locator(`.ob-chapter-choice[data-chapter='${chapter}'] .ob-start`).click(); await settle(page); }
async function state(page) { return page.evaluate(() => Onboarding.__shengjianOnboardingInternals.state()); }
async function saved(page) { await page.evaluate(() => Onboarding.__shengjianOnboardingInternals.saved()); return page.evaluate(() => __MOCK__.get_settings()); }
async function go(page, chapter) {
  await page.locator(".ob-index-summary").click();
  await page.locator(".ob-chapter-tab").nth(chapter).click();
  await settle(page);
}
async function geometry(page, description, targetSelector = '') {
  await settle(page);
  const measure = () => page.evaluate(targetSelector => {
    const box = node => { const r = node.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}; };
    const card = document.querySelector(".ob-card"), ring = document.querySelector(".ob-ring");
    const c = box(card), r = ring.hidden ? null : box(ring);
    const overlap = r && c.left < r.right && c.right > r.left && c.top < r.bottom && c.bottom > r.top;
    const skip = box(document.querySelector(".ob-skip")), head = box(document.querySelector(".ob-head").firstElementChild);
    const target = targetSelector && document.querySelector(targetSelector), t = target && box(target);
    const hit = t && document.elementFromPoint(t.left+t.width/2,t.top+t.height/2);
    return {inside:c.left>=0 && c.right<=innerWidth+1 && c.top>=0 && c.bottom<=innerHeight+1, overlap:!!overlap, closeClear:skip.left>=head.right, overflow:document.documentElement.scrollWidth>innerWidth, spotlight:!!r,
      ...(targetSelector ? {hit:hit===target,target:t,ring:r,card:c,blockedBy:hit && {tag:hit.tagName,id:hit.id,className:hit.className}} : {})};
  }, targetSelector);
  // Cards and fonts keep settling after the double-rAF; measuring a transitional
  // frame intermittently failed "must fit viewport" under load. Re-measure until
  // the snapshot stops changing (same stability rule as waitInstalledModelTarget)
  // before the assertions run — the fit requirements themselves are unchanged.
  let result = await measure(), fingerprint = JSON.stringify(result), stable = 0;
  for (let i = 0; i < 10 && stable < 2; i++) {
    await settle(page);
    const next = await measure(), nextFingerprint = JSON.stringify(next);
    stable = nextFingerprint === fingerprint ? stable + 1 : 0;
    fingerprint = nextFingerprint; result = next;
  }
  assert(result.inside, description + ": card must fit viewport");
  assert(!result.overlap, description + ": card must not cover target");
  assert(result.closeClear, description + ": dismissal must not overlap header");
  assert(!result.overflow, description + ": window must not scroll horizontally");
  return result;
}

async function waitInstalledModelTarget(page) {
  await page.evaluate(() => { window.__fixtureOnboardingHit = null; });
  // Model refreshes can replace the selector after the list first becomes visible.
  // Wait for the same live control and its spotlight, within the normal 5s budget.
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-tour="setup-default-model"]');
    const ring = document.querySelector('.ob-ring');
    const r = node?.getBoundingClientRect(), s = ring?.getBoundingClientRect();
    const x = r && r.left+r.width/2, y = r && r.top+r.height/2;
    const ready = node?.isConnected && !node.disabled && ring && !ring.hidden
      && !document.querySelector('[data-tour="model-small"] button[data-state="idle"]')
      && r.width>0 && r.height>0 && x>s.left && x<s.right && y>s.top && y<s.bottom
      && document.elementFromPoint(x,y)===node;
    const previous = window.__fixtureOnboardingHit;
    window.__fixtureOnboardingHit = {node,frames:ready ? previous?.node===node ? previous.frames+1 : 1 : 0};
    return window.__fixtureOnboardingHit.frames>=3;
  });
}

for (const [width,height,scale,theme] of [[1280,800,1,"dark"],[960,640,1,"paper"],[640,600,1.25,"dark"],[560,500,1.25,"paper"]]) {
  test("chapter hub and all 16 steps fit: " + width + "x" + height + " scale " + scale, async () => {
    const page = await open({scale,theme,nativeChrome:true}, {width,height});
    try {
      await page.evaluate(() => switchView('guide'));
      assert.equal((await state(page)).mode,"hub");
      assert.deepEqual(await page.locator(".ob-choice-head h3").allTextContents(),["环境配置","浏览器连接","应用使用"]);
      await geometry(page,width+" chapter hub");
      await page.screenshot({path:path.join(shots,width+"-hub.png")});
      for(const chapter of chapters) {
        await begin(page,chapter.key);
        for(let local=0;local<chapter.length;local++) {
          const index=chapter.start+local;
          assert.equal((await state(page)).index,index);
          if(index===2) await page.waitForSelector("#setupModelList .model-item");
          const g=await geometry(page,width+" step "+index,index===5 ? "#extensionDir" : "");
          if (![4,8,15].includes(index)) assert(g.spotlight,"real target needs a spotlight at step "+index+" "+JSON.stringify(g));
          if([0,2,5,7,9,13,15].includes(index)) await page.screenshot({path:path.join(shots,width+"-"+index+".png")});
          await page.locator(".ob-next").click();
        }
        assert.equal((await state(page)).active,false,"chapter completion leaves the guide on the applicable page");
        assert.equal(await page.evaluate(()=>AppState.currentView),chapter.key === "environment" ? "setup" : chapter.key === "browser" ? "browser" : "workbench");
        await page.evaluate(() => switchView('guide'));
        assert.equal((await state(page)).progress.chapters[chapter.key].completed,true);
        assert.equal(await page.locator(".ob-chapter-choice[data-completed='true']").count(),chapters.indexOf(chapter)+1);
      }
      await page.locator(".ob-skip").click();
      assert.equal(await page.locator(".ob-card").count(),0);
      const calls = await page.evaluate(() => window.__fixtureCalls);
      for(const action of ["download_model","delete_model","pick_and_import","begin_browser_pairing","batch_agent_translate","begin_subscription","import_files","apply_setup_fixes"]) assert(!calls.includes(action),"reading must not execute "+action);
      assert.deepEqual(page.errors,[]);
    } finally { await page.close(); }
  });
}

test("model, directory and pairing controls remain usable; keyboard editing never advances", async () => {
  const page=await open();
  try {
    await page.evaluate(() => switchView('guide'));
    await begin(page,"environment");
    await page.locator(".ob-next").click();
    await page.locator("#setupDownloadDirBtn").click();
    assert.equal((await state(page)).index,1);
    await page.waitForFunction(()=>__fixtureCalls.filter(x=>x==="pick_setting_dir").length===1);
    await page.locator(".ob-next").click();
    await page.waitForSelector("#setupModelList .model-item");
    await settle(page);
    await page.locator("[data-tour='model-small'] button[data-state='idle']").click();
    assert.equal((await state(page)).index,2,"download must not auto-skip feedback");
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==="download_model").length),1);
    await go(page,1);
    await page.locator(".ob-next").click();
    await page.locator(".ob-next").click();
    await page.locator("#pairBrowser").click();
    assert.equal(await page.evaluate(()=>__fixtureCalls.filter(x=>x==="begin_browser_pairing").length),1);
    assert.equal((await state(page)).index,7);
    await go(page,2);
    for(let i=0;i<4;i++) await page.locator(".ob-next").click();
    const input=page.locator("[data-tour='agent-key']");
    await input.fill("fixture-only");
    await input.press("ArrowLeft");
    await input.press("Enter");
    assert.equal((await state(page)).index,13,"input editing keys must not advance tutorial");
    await page.locator(".ob-next").focus();
    await page.keyboard.press("Enter");
    assert.equal((await state(page)).index,14,"Enter must advance only once");
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("dismissal keeps resumable progress without completing chapters; completed chapters reread from the start", async () => {
  const page=await open({firstRun:true});
  let stored;
  try {
    await page.waitForSelector("#page-setup.active .setup-slide:not([hidden]) .tool-card");
    assert.equal(await page.locator(".ob-card").count(),0,"first run remains on setup until the reader chooses the guide");
    await page.locator("#setupGuideBtn").click();
    await page.mouse.click(4,796);
    assert.equal((await state(page)).active,false,"empty hub space dismisses selection");
    await page.evaluate(() => switchView('guide'));
    await begin(page,"environment");
    await page.mouse.click(4,796);
    assert.equal((await state(page)).index,1,"empty tour space advances");
    await page.locator(".ob-back").click();
    assert.equal((await state(page)).index,0);
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    assert.equal((await state(page)).index,2);
    await page.locator(".ob-skip").click();
    assert.equal(await page.locator(".ob-overlay").count(),0,"close skips the whole current run");
    stored=await saved(page);
    assert.equal(stored.onboarding_chapter_progress.chapters.environment.step,2);
    assert.equal(stored.onboarding_chapter_progress.chapters.environment.completed,false,"dismissal is not completion");
    assert(!stored.onboarding_chapter_progress.chapters.browser);
    assert(!stored.onboarding_chapter_progress.chapters.application);
    await page.evaluate(()=>Onboarding.maybeShow());
    assert.equal(await page.locator(".ob-card").count(),0,"explicit skip suppresses automatic reread");
    await page.locator(".nav-row[data-page='settings']").click();
    await page.locator(".sitem[data-section='about']").click();
    await page.getByRole("button",{name:"重新阅读新手引导"}).click();
    assert.equal((await state(page)).mode,"hub");
    await page.keyboard.press("Escape");
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
  const resumed=await open({settings:stored});
  try {
    await resumed.evaluate(() => switchView('guide'));
    await resumed.getByRole("button",{name:"环境配置：继续阅读"}).click();
    assert.equal((await state(resumed)).index,2,"independent saved settings restore reading position");
    for(let i=0;i<3;i++) await resumed.locator(".ob-next").click();
    await resumed.evaluate(() => switchView('guide'));
    await resumed.getByRole("button",{name:"环境配置：重新阅读"}).click();
    assert.equal((await state(resumed)).index,0);
    assert((await state(resumed)).progress.chapters.environment.completed,"rereading does not erase prior completion");
    await resumed.keyboard.press("Escape");
    assert.deepEqual(resumed.errors,[]);
  } finally { await resumed.close(); }
});

test("delayed/installed/missing targets recover; reopening, keyboard focus and resize leave no blockers", async () => {
  const page=await open({modelDelay:180,installed:true,nativeChrome:true});
  try {
    for(let i=0;i<6;i++) {
      await page.evaluate(() => switchView('guide'));
      for(let n=0;n<8;n++) await page.keyboard.press("Tab");
      assert(await page.evaluate(()=>document.activeElement.closest(".ob-card")!==null),"hub Tab stays within selection");
      await begin(page,"environment");
      // The direct entry resets just this reading position, without invalidating other chapters.
      await page.keyboard.press("Escape");
      await page.evaluate(()=>Onboarding.show({chapter:"environment",resume:false}));
      for(let n=0;n<2;n++) await page.locator(".ob-next").click();
      await page.waitForSelector(".model-item[data-model='small']");
      await waitInstalledModelTarget(page);
      const g=await geometry(page,"installed model","[data-tour='setup-default-model']");
      assert(g.hit,"installed model highlights selection, never its delete button: "+JSON.stringify(g));
      await page.setViewportSize({width:i%2?560:960,height:500});
      await geometry(page,"resize "+i);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator(".ob-shade").count(),0);
    }
    await page.evaluate(()=>Onboarding.show({chapter:"application",resume:false}));
    for(let i=0;i<4;i++) await page.locator(".ob-next").click();
    await page.evaluate(()=>document.querySelector("[data-tour='agent-key']").remove());
    await settle(page);
    await geometry(page,"missing API key");
    await page.locator(".ob-next").click();
    assert.equal((await state(page)).index,14,"missing target must not trap the reader");
    for(let n=0;n<18;n++) await page.keyboard.press("Tab");
    assert(await page.evaluate(()=>document.activeElement.closest(".ob-card")!==null || document.activeElement.dataset.tour==="editing-level"),"tour Tab stays within guide and its control");
    await page.keyboard.press("Escape");
    await page.locator(".nav-row[data-page='workbench']").click();
    assert.equal(await page.locator(".ob-overlay").count(),0);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("slow/failing progress saves cannot reorder chapters or trap dismissal", async () => {
  const page=await open();
  try {
    await page.evaluate(()=>{
      const original=__MOCK__.save_settings;
      window.__saveFinished=[];
      let request=0;
      __MOCK__.save_settings=async changes=>{
        const at=++request;
        await new Promise(resolve=>setTimeout(resolve,at===1?80:1));
        if(at===2) throw new Error("fixture progress save failed");
        const result=await original(changes); __saveFinished.push(at); return result;
      };
    });
    await page.evaluate(()=>Onboarding.show({chapter:"environment",resume:false}));
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Escape");
    const settings=await saved(page);
    assert.equal(settings.onboarding_chapter_progress.chapters.environment.step,2,"latest position wins despite a transient bridge failure");
    assert.equal(settings.onboarding_chapter_progress.chapters.environment.completed,false);
    assert.deepEqual(await page.evaluate(()=>__saveFinished),[1,3,4,5],"writes are sequential and continue after failure");
    assert.equal(await page.locator(".ob-overlay").count(),0);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("legacy completed marker and malformed chapter data never force a new tour", async () => {
  const page=await open({settings:{onboarding_completed_version:"0.16.1",onboarding_chapter_progress:{schema_version:99,chapters:{environment:{step:999,completed:true}}}}});
  try {
    await page.evaluate(()=>Onboarding.maybeShow());
    assert.equal(await page.locator(".ob-overlay").count(),0);
    await page.evaluate(() => switchView('guide'));
    assert.equal(await page.locator(".ob-chapter-choice[data-completed='true']").count(),0,"unknown schema does not invent completion");
    await begin(page,"environment");
    assert.equal((await state(page)).index,0);
    await page.keyboard.press("Escape");
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});

test("an unrelated older settings reply cannot erase another chapter's reading progress", async () => {
  const page=await open();
  try {
    const oldSettings=await page.evaluate(()=>__MOCK__.get_settings());
    await page.evaluate(()=>Onboarding.show({chapter:"environment",resume:false}));
    for(let i=0;i<5;i++) await page.keyboard.press("ArrowRight");
    assert((await state(page)).progress.chapters.environment.completed);
    // Settings controls replace their full snapshots. A delayed response from a
    // different setting must not become the authority for in-flight guide state.
    await page.evaluate(old=>{AppState.settings=old;},oldSettings);
    await page.evaluate(() => switchView('guide'));
    await begin(page,"browser");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Escape");
    const settings=await saved(page);
    assert(settings.onboarding_chapter_progress.chapters.environment.completed);
    assert.equal(settings.onboarding_chapter_progress.chapters.browser.step,1);
    assert.equal(settings.onboarding_chapter_progress.chapters.browser.completed,false);
    assert.deepEqual(page.errors,[]);
  } finally { await page.close(); }
});
