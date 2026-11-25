// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {after, before, test} from 'node:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';

const ui = path.resolve(import.meta.dirname, '../app/webview_ui/index.html');
const evidence = process.env.FOILPAGE_MOTION_EVIDENCE;
let browser;
before(async () => {
  if (evidence) await fs.mkdir(evidence, {recursive: true});
  browser = await chromium.launch();
});
after(async () => { await browser?.close(); });

async function settings(theme, reducedMotion = 'no-preference', animations = true, uiLanguage = 'zh-CN') {
  const page = await browser.newPage({viewport: {width: 1280, height: 840}, reducedMotion});
  page.setDefaultTimeout(5000);
  await page.addInitScript(installMock, {nativeChrome: true, settings: {theme, confirm_delete: false, animations, ui_language: uiLanguage}});
  await page.goto(pathToFileURL(ui).href);
  await page.locator('[data-page=settings]').click();
  await page.locator('.sitem[data-section=ui]').click();
  return page;
}

async function shot(target, name) {
  if (evidence) await target.screenshot({path: path.join(evidence, name + '.png')});
}

async function pressWithoutTravel(page, target, visual = target) {
  await target.hover();
  const bounds = await visual.boundingBox();
  await page.mouse.down();
  try {
    assert(await target.evaluate(node => node.matches(':active')));
    assert.equal(await visual.evaluate(node => getComputedStyle(node).transform), 'none');
    assert.deepEqual(await visual.boundingBox(), bounds);
  } finally {
    // Leave before releasing. A mailto link and a native window command
    // can both be exercised without opening an external application.
    await page.mouse.move(1, 1);
    await page.mouse.up();
  }
}

async function timeline(page) {
  // Freeze the actual CSS animation created by the saved switch. Seeking it
  // makes the screenshots independent of how long file I/O takes on the host.
  await page.evaluate(() => {
    document.addEventListener('transitionrun', event => {
      if (!event.target.matches('.toggle[data-switch-changed]') || event.pseudoElement !== '::after' || event.propertyName !== 'transform') return;
      const motion = event.target.getAnimations({subtree: true})
        .find(animation => animation.transitionProperty === 'transform');
      motion.pause();
      motion.currentTime = 0;
      window.testThumbMotion = motion;
    });
  });
}

async function sampleSwitch(control, fraction) {
  return control.evaluate((node, position) => {
    const animation = window.testThumbMotion;
    animation.currentTime = position * animation.effect.getComputedTiming().duration;
    return new DOMMatrix(getComputedStyle(node, '::after').transform).m41;
  }, fraction);
}

for (const theme of ['dark', 'paper']) {
  test('saved settings switch settles from the displayed position in ' + theme, async () => {
    const page = await settings(theme);
    try {
      await timeline(page);
      const control = page.getByRole('switch', {name: '删除前确认', exact: true});
      const row = page.locator('.set-row').filter({has: control});
      await control.hover();
      await page.mouse.down();
      await page.waitForFunction(() => getComputedStyle(document.querySelector('.toggle:active'), '::after').scale === '0.88');
      await shot(row, theme + '-switch-press');
      await page.mouse.up();
      await page.waitForFunction(() => window.testThumbMotion);
      assert.equal(await control.getAttribute('aria-checked'), 'true');
      const samples = [];
      for (const fraction of [0, .5, 1]) {
        samples.push(await sampleSwitch(control, fraction));
        await shot(row, theme + '-switch-' + fraction * 100);
      }
      assert.equal(samples[0], 0);
      assert(samples[1] > 0 && samples[1] < 18);
      assert.equal(samples[2], 18);
      await sampleSwitch(control, .5);
      await page.evaluate(() => { window.testThumbMotion = null; });
      await page.keyboard.press('Space');
      await page.waitForFunction(() => window.testThumbMotion);
      assert.equal(await control.getAttribute('aria-checked'), 'false');
      const reversed = await sampleSwitch(control, 0);
      assert(Math.abs(reversed - samples[1]) < .05, 'rapid reversal starts at the visible thumb');
      assert.equal(await sampleSwitch(control, 1), 0);
      assert(await control.evaluate(node => document.activeElement === node && node.matches(':focus-visible')));
      assert.equal(await page.locator('.toggle[data-switch-changed]').count(), 1);
      if (evidence) await fs.writeFile(path.join(evidence, theme + '-motion.json'), JSON.stringify({samples, reversed}, null, 2));
    } finally { await page.close(); }
  });

  test('command contact, release and disabled input in ' + theme, async () => {
    const page = await settings(theme);
    try {
      await page.locator('.sitem[data-section=startup]').click();
      const button = page.getByRole('button', {name: '测试通知', exact: true});
      await button.scrollIntoViewIfNeeded();
      const row = page.locator('.set-row').filter({has: button});
      await page.mouse.move(0, 0);
      const rest = await button.evaluate(node => getComputedStyle(node).borderColor);
      await shot(row, theme + '-button-rest');
      await button.hover();
      await button.evaluate(async node => { await Promise.all(node.getAnimations().map(a => a.finished)); });
      assert.notEqual(await button.evaluate(node => getComputedStyle(node).borderColor), rest);
      await shot(row, theme + '-button-hover');
      await page.evaluate(() => {
        window.testNotificationCount = 0;
        __MOCK__.system_notify = () => {
          window.testNotificationCount++;
          return new Promise(resolve => { window.finishNotification = () => resolve({ok: true}); });
        };
      });
      await page.mouse.down();
      await page.waitForFunction(() => {
        const button = document.querySelector('#settingsPane button.btn:active');
        return button && new DOMMatrix(getComputedStyle(button).transform).m42 === 1;
      });
      await shot(row, theme + '-button-down');
      await page.mouse.up();
      await page.waitForFunction(() => window.finishNotification);
      assert(await button.isDisabled());
      await page.mouse.down();
      await page.mouse.up();
      assert.equal(await page.evaluate(() => window.testNotificationCount), 1);
      await page.evaluate(() => finishNotification());
      await page.waitForFunction(() => !document.querySelector('#settingsPane button[aria-busy=true]'));
      await button.evaluate(async node => { await Promise.all(node.getAnimations().map(a => a.finished)); });
      assert.equal(await button.evaluate(node => getComputedStyle(node).transform), 'none');
      await shot(row, theme + '-button-release');
      for (const key of ['ui', 'data', 'ui', 'startup']) {
        await page.locator('.sitem[data-section=' + key + ']').press('Enter');
        assert.equal(await page.locator('#settingsPane').getAttribute('data-section'), key);
        assert.deepEqual(await page.locator('.sitem[aria-pressed=true]').evaluateAll(items => items.map(item => item.dataset.section)), [key]);
        assert(await page.locator('.sitem.active').evaluate(item => item === document.activeElement));
      }
    } finally { await page.close(); }
  });
}

for (const [name, preference, animations] of [
  ['system-reduced', 'reduce', true], ['product-off', 'no-preference', false],
]) {
  test(name + ' keeps saved states and keyboard focus without travel', async () => {
    const page = await settings('paper', preference, animations);
    try {
      const control = page.getByRole('switch', {name: '删除前确认', exact: true});
      const row = page.locator('.set-row').filter({has: control});
      await control.focus();
      await page.keyboard.down('Space');
      assert.equal(await control.evaluate(node => getComputedStyle(node, '::after').scale), 'none');
      await page.keyboard.up('Space');
      await page.waitForFunction(() => AppState.settings.confirm_delete === true);
      assert.equal(await control.getAttribute('aria-checked'), 'true');
      assert.equal(await control.evaluate(node => new DOMMatrix(getComputedStyle(node, '::after').transform).m41), 18);
      assert.equal(await control.evaluate(node => node.getAnimations({subtree: true}).length), 0);
      assert(await control.evaluate(node => node.matches(':focus-visible') && getComputedStyle(node).outlineStyle !== 'none'));
      await shot(row, name + '-switch');
      await page.locator('.sitem[data-section=startup]').click();
      const button = page.getByRole('button', {name: '测试通知', exact: true});
      await button.hover();
      await page.mouse.down();
      assert.equal(await button.evaluate(node => getComputedStyle(node).transform), 'none');
      assert.equal(await button.evaluate(node => node.getAnimations().length), 0);
      await page.mouse.up();
    } finally { await page.close(); }
  });
  test(name + ' keeps shared link, icon, palette and navigation targets still on contact', async () => {
    const page = await settings('dark', preference, animations);
    try {
      const accent = page.locator('.accent-swatch').first();
      const selected = await accent.getAttribute('aria-pressed');
      await accent.hover();
      assert.equal(await accent.evaluate(node => getComputedStyle(node).transform), 'none');
      assert.notEqual(await accent.evaluate(node => getComputedStyle(node).boxShadow), 'none');
      await pressWithoutTravel(page, accent);
      assert.equal(await accent.getAttribute('aria-pressed'), selected);
      await pressWithoutTravel(page, page.locator('#windowMinimize'));
      await page.locator('.sitem[data-section=about]').click();
      await pressWithoutTravel(page, page.getByRole('link', {name: '邮件反馈', exact: true}));
      const library = page.locator('[data-page=library]');
      await pressWithoutTravel(page, library);
      await library.click();
      await page.locator('.doc-open').first().click();
      const edit = page.locator('#page-library').getByRole('button', {name: '编辑', exact: true});
      await pressWithoutTravel(page, edit);
      assert.equal(await library.getAttribute('aria-current'), 'page');
      await page.locator('[data-page=videos]').click();
      const cover = page.locator('.video-cover:enabled').first();
      await pressWithoutTravel(page, cover, cover.locator('.video-play'));
    } finally { await page.close(); }
  });
}

test('a delayed or refused save keeps the thumb at its confirmed value', async () => {
  const page = await settings('dark');
  try {
    const control = page.getByRole('switch', {name: '删除前确认', exact: true});
    await page.evaluate(() => {
      __MOCK__.save_settings = () => new Promise(resolve => {
        window.refuseSave = () => resolve({ok: false, error: 'fixture refusal'});
      });
    });
    await control.click();
    await page.waitForFunction(() => window.refuseSave);
    assert(await control.isDisabled());
    assert.equal(await control.getAttribute('aria-checked'), 'false');
    assert.equal(await control.evaluate(node => new DOMMatrix(getComputedStyle(node, '::after').transform).m41), 0);
    await page.evaluate(() => refuseSave());
    await page.waitForFunction(() => document.querySelector('[role=switch][aria-invalid=true]'));
    assert(!(await control.isDisabled()));
    assert.equal(await control.getAttribute('aria-checked'), 'false');
    assert.equal(await page.locator('[data-switch-changed]').count(), 0);
  } finally { await page.close(); }
});

test('English switch restores its own keyboard focus and motion after saving', async () => {
  const page = await settings('dark', 'no-preference', true, 'en');
  try {
    const control = page.getByRole('switch', {name: 'Confirm before deleting', exact: true});
    await timeline(page);
    await control.press('Space');
    await page.waitForFunction(() => AppState.settings.confirm_delete === true);
    assert.equal(await control.getAttribute('aria-checked'), 'true');
    assert(await control.evaluate(node => node === document.activeElement), 'focus returns to the English switch');
    await page.waitForFunction(() => window.testThumbMotion);
    assert.equal(await sampleSwitch(control, 0), 0);
    assert(await control.evaluate(node => node.getAnimations({subtree: true}).includes(window.testThumbMotion)));
    assert.equal(await sampleSwitch(control, 1), 18);
  } finally { await page.close(); }
});

test('English text size retains focus and binds a pending save to the rebuilt native range', async () => {
  const page = await settings('paper', 'no-preference', true, 'en');
  try {
    const range = page.getByRole('slider', {name: 'Interface text size', exact: true});
    await range.press('ArrowRight');
    await page.waitForFunction(() => AppState.settings.font_scale === 1.05);
    assert(await range.evaluate(node => node === document.activeElement));
    await page.evaluate(() => {
      __MOCK__.save_settings = patch => new Promise(resolve => {
        window.finishSizeSave = () => resolve(patch);
      });
    });
    await range.press('ArrowRight');
    await page.waitForFunction(() => window.finishSizeSave);
    assert(await range.isDisabled());
    await page.locator('.sitem[data-section=recognition]').click();
    await page.locator('.sitem[data-section=ui]').click();
    assert.equal(await range.getAttribute('aria-busy'), 'true', 'replacement range retains its pending write');
    assert(await range.isDisabled());
    await page.evaluate(() => finishSizeSave());
    await page.waitForFunction(() => AppState.settings.font_scale === 1.1);
    assert(!(await range.isDisabled()));
    assert.equal(await range.inputValue(), '1.1');
    assert(await page.locator('.sitem[data-section=ui]').evaluate(node => node === document.activeElement), 'background completion preserves current focus');
  } finally { await page.close(); }
});
