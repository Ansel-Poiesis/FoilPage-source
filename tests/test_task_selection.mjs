// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';

let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });
async function open() {
  const page = await browser.newPage({viewport: {width: 1280, height: 800}});
  page.setDefaultTimeout(5000); page.errors = [];
  page.on('pageerror', error => page.errors.push(error.message));
  await page.addInitScript(installMock, {nativeChrome: true});
  await page.goto(pathToFileURL(path.resolve(import.meta.dirname, '../app/webview_ui/index.html')).href);
  await page.waitForFunction(() => window.__appReady);
  await page.locator('[data-page=workbench]').click();
  await page.locator('.job').first().waitFor();
  return page;
}
const selected = page => page.evaluate(() => [...AppState.selected]);

test('single clicks select and cancel rows without replacing the list or moving its scroll', async () => {
  const page = await open();
  try {
    await page.evaluate(() => { window.__selectionList = document.querySelector('#jobList'); window.__selectionRow = document.querySelector('.job'); });
    const button = page.locator('.job-open').first(), checkbox = page.locator('.job-check-input').first();
    await button.click();
    assert(await checkbox.isChecked()); assert.equal(await button.getAttribute('aria-pressed'), 'true');
    await button.click();
    assert.deepEqual(await selected(page), []); assert(!(await checkbox.isChecked()));
    assert.equal(await button.getAttribute('aria-pressed'), 'false');
    assert(await page.evaluate(() => __selectionList === document.querySelector('#jobList') && __selectionRow === document.querySelector('.job')));
    assert(await page.getByText('查看任务详情', {exact: true}).isVisible());
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('checkbox artwork and labels toggle once, Ctrl adds, and clicking a selected row cancels only that row', async () => {
  const page = await open();
  try {
    const rows = page.locator('.job');
    const ids = await rows.evaluateAll(nodes => nodes.map(node => node.dataset.taskId));
    await rows.nth(0).locator('.job-check-visual').click({force: true});
    assert.deepEqual(await selected(page), [ids[0]]);
    await rows.nth(0).locator('.job-check').click();
    assert.deepEqual(await selected(page), []);
    await rows.nth(0).locator('.job-open').click();
    await rows.nth(2).locator('.job-open').click({modifiers: ['Control']});
    assert.deepEqual(new Set(await selected(page)), new Set([ids[0], ids[2]]));
    await rows.nth(0).locator('.job-open').click();
    assert.deepEqual(await selected(page), [ids[2]]);
    await rows.nth(1).locator('.job-open').click();
    assert.deepEqual(await selected(page), [ids[1]]);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('Ctrl+A selects the filtered list and Escape clears it while preserving row focus', async () => {
  const page = await open();
  try {
    await page.locator('[data-filter="已完成"]').click();
    const ids = await page.locator('.job').evaluateAll(rows => rows.map(row => row.dataset.taskId));
    await page.locator('.job-open').first().focus();
    await page.keyboard.press('Control+a');
    assert.deepEqual(new Set(await selected(page)), new Set(ids));
    assert.equal(await page.locator('.job-check-input:checked').count(), ids.length);
    await page.keyboard.press('Escape');
    assert.deepEqual(await selected(page), []);
    assert.equal(await page.evaluate(() => AppState.anchorId), null);
    assert(await page.locator('.job-open').first().evaluate(node => node === document.activeElement));
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('clearing from the batch panel keeps long-list nodes, scroll and a usable row focus', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      const sample = AppState.tasks[0];
      const tasks = Array.from({length: 60}, (_, index) => ({...sample, task_id: 'job:selection-' + index, display_title: '合成任务 ' + index, updated_at: ''}));
      __MOCK__.get_tasks = () => tasks;
      AppState.tasks = tasks; Workbench.render();
    });
    await page.locator('.job-open').nth(22).click();
    await page.locator('.job-check-input').nth(23).click();
    const scroll = await page.locator('#jobList').evaluate(node => { window.__heldSelectionList = node; return node.scrollTop; });
    await page.getByRole('button', {name: '取消选择', exact: true}).click();
    assert.deepEqual(await selected(page), []);
    assert.equal(await page.locator('#jobList').evaluate(node => node.scrollTop), scroll);
    assert(await page.evaluate(() => __heldSelectionList === document.querySelector('#jobList')));
    assert.equal(await page.evaluate(() => document.activeElement.closest('.job')?.dataset.taskId), 'job:selection-23');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('pointer check drawing is interruptible and rapid select-cancel-select settles on the real state', async () => {
  const page = await open();
  try {
    const animated = await page.locator('.job-check-input').first().evaluate(async input => {
      const row = input.closest('.job'), tick = row.querySelector('.job-check-tick');
      getComputedStyle(tick).strokeDashoffset;
      input.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 1}));
      await new Promise(requestAnimationFrame);
      const drawing = tick.getAnimations().length > 0;
      input.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 1}));
      input.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 1}));
      await new Promise(requestAnimationFrame);
      row.getAnimations({subtree: true}).forEach(animation => animation.finish());
      return {drawing, checked: input.checked, selected: row.classList.contains('selected'), dash: getComputedStyle(tick).strokeDashoffset};
    });
    assert(animated.drawing); assert(animated.checked && animated.selected); assert.equal(parseFloat(animated.dash), 0);
    assert.equal((await selected(page)).length, 1);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('keyboard, reduced motion and forced colors retain immediate native checkbox feedback', async () => {
  const page = await open();
  try {
    const checkbox = page.locator('.job-check-input').first();
    await checkbox.focus(); await page.keyboard.press('Space');
    assert(await checkbox.isChecked());
    assert.equal(await page.locator('.job').first().evaluate(row => row.getAnimations({subtree: true}).length), 0);
    await page.emulateMedia({reducedMotion: 'reduce'});
    await checkbox.click(); assert(!(await checkbox.isChecked()));
    assert.equal(await page.locator('.job-check-tick').first().evaluate(node => parseFloat(getComputedStyle(node).strokeDashoffset)), 1);
    await page.emulateMedia({forcedColors: 'active', reducedMotion: 'reduce'});
    assert.equal(await checkbox.evaluate(node => getComputedStyle(node).appearance), 'auto');
    assert.equal(await page.locator('.job-check-visual').first().evaluate(node => getComputedStyle(node).display), 'none');
    await page.keyboard.press('Space'); assert(await checkbox.isChecked());
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('modal editing keeps Ctrl+A and Escape local instead of changing the task selection', async () => {
  const page = await open();
  try {
    await page.locator('.job-open').first().click(); const before = await selected(page);
    await page.evaluate(() => openModal('合成编辑', '<input id="selectionFixtureText" value="保留这段文字">', '<button class="btn">保留</button>'));
    const input = page.locator('#selectionFixtureText'); await input.focus();
    await page.keyboard.press('Control+a');
    assert.deepEqual(await input.evaluate(node => [node.selectionStart, node.selectionEnd]), [0, 6]);
    await page.keyboard.press('Escape');
    assert.deepEqual(await selected(page), before);
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('checkbox interactions play tactile check sound and respect silent notification pack', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      window.__checkSounds = [];
      const orig = window.AppSound.playCheck;
      window.AppSound.playCheck = (checked) => {
        const played = orig(checked);
        window.__checkSounds.push({checked, played});
        return played;
      };
    });
    const checkbox = page.locator('.job-check-input').first();
    await checkbox.click();
    await page.waitForTimeout(50);
    await checkbox.click();
    await page.waitForTimeout(50);
    await page.evaluate(() => { AppState.settings.notification_sound_pack = 'silent'; });
    await checkbox.click();
    const events = await page.evaluate(() => window.__checkSounds);
    assert.equal(events.length, 3);
    assert.equal(events[0].checked, true);
    assert.equal(events[1].checked, false);
    assert.equal(events[2].checked, true);
    assert.equal(events[2].played, false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
