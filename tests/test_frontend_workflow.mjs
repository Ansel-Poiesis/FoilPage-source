// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';
import installMock from './ui_fixture.cjs';

const root = path.resolve(import.meta.dirname, '..');
let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });

async function open(options = {}) {
  const page = await browser.newPage({viewport: {width: options.width || 1280, height: options.height || 800}});
  page.setDefaultTimeout(5000);
  page.errors = [];
  page.on('pageerror', error => page.errors.push(error.message));
  await page.addInitScript(installMock, options);
  await page.addInitScript(() => {
    window.__workflowCalls = [];
    window.__workflowDocs = ['one', 'two', 'archived'].map((id, index) => ({
      task_id: 'job:' + id, title: '同名素材', has_raw: true, has_final: true, status: 'done',
      source_path: 'C:\\fixture\\' + id + '.mp4', tags: [], updated_at: '2026-11-' + (10 + index),
    }));
    window.__workflowTasks = __workflowDocs.slice(0, 2).map(doc => ({...doc,
      display_title: doc.title, kind: 'transcription', raw_path: 'raw.md', final_path: 'final.md', progress: 1,
    }));
    window.__workflowVideos = [
      {id: 'video-one', title: '同名素材', available: true, task_ids: ['job:one'], source_path: 'C:\\fixture\\one.mp4'},
      {id: 'video-two', title: '同名素材', available: true, task_ids: ['job:two', 'job:archived'], source_path: 'C:\\fixture\\two.mp4'},
      {id: 'video-missing', title: '已经移动的课程', available: false, task_ids: [], source_path: 'D:\\原目录\\course.mp4'},
    ];
    window.__workflowText = Object.fromEntries(__workflowDocs.map(doc => [doc.task_id, {
      raw: '# 原稿 ' + doc.task_id + '\n\n保留的段落。\n\n旧内容。',
      final: '# 整理稿 ' + doc.task_id + '\n\n保留的段落。\n\n新内容。',
    }]));
    const bootstrap = __MOCK__.get_bootstrap;
    __MOCK__.get_bootstrap = () => ({...bootstrap(), tasks: structuredClone(__workflowTasks)});
    __MOCK__.get_tasks = () => structuredClone(__workflowTasks);
    __MOCK__.get_documents = query => structuredClone(__workflowDocs.filter(doc => !query || doc.title.includes(query)));
    __MOCK__.get_downloaded_videos = () => ({ok: true, items: structuredClone(__workflowVideos)});
    __MOCK__.get_reader = (id, tab) => ({ok: true, tab, text: __workflowText[id]?.[tab] || '', revision: id + '/' + tab});
    __MOCK__.get_tag_index = () => [];
    __MOCK__.batch_agent_translate = (ids, level) => {
      __workflowCalls.push({action: 'batch', ids, level});
      window.__workflowRun = 'run-' + __workflowCalls.length;
      return {ok: true, count: ids.length, run_id: __workflowRun};
    };
    __MOCK__.get_batch_agent_status = () => ({run_id: __workflowRun, running: false, done: 1, failed: 1, skipped: 0,
      failed_ids: ['job:two'], errors: ['two: FixtureFailure']});
  });
  await page.goto(pathToFileURL(path.join(root, 'app/webview_ui/index.html')).href);
  await page.waitForFunction(() => window.__appReady);
  await page.locator('[data-page=workbench]').click();
  return page;
}

async function library(page, taskId = 'job:one') {
  await page.evaluate(taskId => MaterialNavigation.go('library', {taskIds: [taskId]}), taskId);
  await page.waitForFunction(taskId => Library.currentTaskId() === taskId
    && !document.querySelector('#readerBody .reader-empty') && !document.getElementById('copyReaderBtn').disabled, taskId);
}

test('paragraph comparison preserves both full inputs including duplicate and reordered paragraphs', () => {
  const context = {window: {}};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'app/webview_ui/js/text-diff.js'), 'utf8'), context);
  const {compare, paragraphs} = context.window.TextDiff;
  const cases = [['', ''], ['', '新增'], ['删除', ''], ['A\n\nB\n\nA', 'A\n\nC\n\nA'], ['A\n\nB\n\nC', 'C\n\nB\n\nA']];
  let seed = 7;
  const random = () => { seed = Math.imul(seed, 1664525) + 1013904223 | 0; return seed >>> 0; };
  for (let count = 0; count < 250; ++count) {
    const make = () => Array.from({length: random() % 40}, () => '段落' + random() % 12).join('\n\n');
    cases.push([make(), make()]);
  }
  for (const [left, right] of cases) {
    const groups = compare(left, right);
    assert.deepEqual(Array.from(groups.flatMap(group => group.before)), Array.from(paragraphs(left)));
    assert.deepEqual(Array.from(groups.flatMap(group => group.after)), Array.from(paragraphs(right)));
    for (const group of groups.filter(group => group.kind === 'same')) assert.equal(group.before[0], group.after[0]);
  }
  const long = Array.from({length: 20000}, (_, index) => '长文第' + index + '段').join('\n\n');
  assert.equal(compare(long, long + '\n\n新增尾段').at(-1).kind, 'added');
});

test('linked navigation uses record ids for equal titles and restores task selection and filtering', async () => {
  const page = await open();
  try {
    await page.getByRole('button', {name: /^已完成/}).click();
    await page.locator('.job[data-task-id="job:two"] .job-open').click();
    await page.getByRole('button', {name: '在文本库阅读', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#readerBody h1')?.textContent === '整理稿 job:two');
    assert.equal(await page.locator('#materialBack').getAttribute('aria-label'), '返回任务');
    await page.locator('#materialBack').click();
    assert.equal(await page.evaluate(() => AppState.filter), '已完成');
    assert.deepEqual(await page.evaluate(() => [...AppState.selected]), ['job:two']);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('video links include archived text and choose the latest matching transcription', async () => {
  const page = await open();
  try {
    await page.locator('[data-page=videos]').click();
    await page.locator('.video-card[data-video-id=video-two] .video-menu-trigger').click();
    await page.getByRole('menuitem', {name: '查看转写结果', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#readerBody h1')?.textContent === '整理稿 job:archived');
    assert(await page.locator('#readerTaskLink').isDisabled());
    await page.locator('#readerVideoLink').click();
    await page.locator('.video-card.is-located[data-video-id=video-two]').waitFor();
    await page.locator('#materialBack').click();
    await page.waitForFunction(() => Library.currentTaskId() === 'job:archived');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('linked video remains visible and focused in a long catalog after columns resize', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __workflowVideos = Array.from({length: 90}, (_, index) => ({
        id: 'catalog-' + index, title: '合成视频 ' + index, available: true,
        task_ids: index === 80 ? ['job:one'] : [], source_path: 'C:\\fixture\\' + index + '.mp4',
      }));
    });
    await library(page);
    await page.locator('#readerVideoLink').click();
    const target = page.locator('.video-card.is-located[data-video-id=catalog-80]');
    await target.waitFor();
    await page.waitForFunction(() => {
      const card = document.querySelector('.video-card.is-located');
      const rect = card?.getBoundingClientRect(), grid = document.querySelector('#videoGrid')?.getBoundingClientRect();
      return rect && grid && rect.top < grid.bottom && rect.bottom > grid.top;
    });
    await page.setViewportSize({width: 960, height: 800});
    await page.waitForFunction(() => document.querySelectorAll('#videoGrid .video-col').length === 2);
    assert(await target.evaluate(card => {
      const rect = card.getBoundingClientRect(), grid = card.closest('#videoGrid').getBoundingClientRect();
      return rect.top < grid.bottom && rect.bottom > grid.top;
    }), 'the linked video must stay in the visible scroll area after reflow');
    assert.equal(await page.evaluate(() => document.activeElement?.closest('.video-card')?.dataset.videoId), 'catalog-80');
    await page.setViewportSize({width: 1280, height: 800});
    await page.waitForFunction(() => document.querySelectorAll('#videoGrid .video-col').length === 3);
    assert(await target.evaluate(card => {
      const rect = card.getBoundingClientRect(), grid = card.closest('#videoGrid').getBoundingClientRect();
      return rect.top < grid.bottom && rect.bottom > grid.top;
    }), 'expanding the window must also preserve the linked video');
    assert(await page.locator('#videoGrid .video-col').evaluateAll(columns => columns.every(column => column.childElementCount > 0)));
    assert.equal(await page.evaluate(() => document.activeElement?.closest('.video-card')?.dataset.videoId), 'catalog-80');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('late portrait covers keep the linked video visible until the user starts browsing', async () => {
  const page = await open({width: 960});
  let releaseCovers;
  const coversReady = new Promise(resolve => { releaseCovers = resolve; });
  const portrait = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAADwAAABpCAAAAABD1B/qAAAAf0lEQVR4nGNgYOHgERSVlFNS09Y3trBxcPX0DQqLik9OzykoqaxtbO3qmzR99vwlK9Zs3Lpz36Fjp89fvnHnwdOXbz99+/WfYVTzqOZRzaOaRzWPah7VPKp5VPOo5lHNo5pHNY9qHtU8qnlU86jmUc2jmkc1j2oe1TyqeZBqBgBmATaCT64IVgAAAABJRU5ErkJggg==', 'base64');
  try {
    await page.route('http://127.0.0.1:18766/cover/**', async route => {
      await coversReady;
      await route.fulfill({status: 200, contentType: 'image/png', body: portrait});
    });
    await page.evaluate(() => {
      __workflowVideos = Array.from({length: 10}, (_, index) => ({
        id: 'late-' + index, title: '合成封面 ' + index, available: true,
        task_ids: index === 6 ? ['job:one'] : [],
        thumbnail_url: index < 6 ? 'http://127.0.0.1:18766/cover/' + index + '.png' : '',
      }));
    });
    await library(page);
    await page.locator('#readerVideoLink').click();
    await page.locator('.video-card.is-located[data-video-id=late-6]').waitFor();
    releaseCovers();
    await page.waitForFunction(() => [...document.querySelectorAll('#videoGrid img')].every(image => image.classList.contains('is-loaded')));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert(await page.locator('.video-card.is-located').evaluate(card => {
      const rect = card.getBoundingClientRect(), grid = card.closest('#videoGrid').getBoundingClientRect();
      return rect.top < grid.bottom && rect.bottom > grid.top;
    }), 'late-loading portrait covers must not displace the linked video');
    await page.locator('#videoGrid').hover();
    await page.mouse.wheel(0, -5000);
    await page.waitForFunction(() => document.querySelector('#videoGrid').scrollTop < 10);
    await page.evaluate(() => {
      const image = document.querySelector('#videoGrid img');
      image.classList.remove('is-loaded'); image.src += '?reload';
    });
    await page.waitForFunction(() => [...document.querySelectorAll('#videoGrid img')].every(image => image.classList.contains('is-loaded')));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert(await page.locator('#videoGrid').evaluate(grid => grid.scrollTop < 10), 'later cover loads must respect the user scroll');
    assert.deepEqual(page.errors, []);
  } finally { releaseCovers(); await page.close(); }
});

test('cross-page navigation keeps its target and history through dirty-draft cancellation', async () => {
  const page = await open();
  try {
    await library(page);
    await page.locator('#editReaderBtn').click(); await page.locator('.reader-editor').fill('未保存草稿');
    await page.locator('#readerVideoLink').click(); await page.locator('#dlgCancel').click();
    assert.equal(await page.locator('.reader-editor').inputValue(), '未保存草稿');
    assert.equal(await page.locator('#materialBack').getAttribute('aria-label'), '返回任务');
    await page.locator('#readerVideoLink').click(); await page.locator('#discardChanges').click();
    await page.locator('.video-card.is-located[data-video-id=video-one]').waitFor();
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('a delayed video lookup never navigates away from a newly selected document', async () => {
  const page = await open();
  try {
    await library(page);
    await page.evaluate(() => { __MOCK__.get_downloaded_videos = () => new Promise(resolve => window.__finishLookup = resolve); });
    await page.locator('#readerVideoLink').click();
    await page.locator('.doc[data-doc-id="job:two"] .doc-open').click();
    await page.waitForFunction(() => document.querySelector('#readerBody h1')?.textContent === '整理稿 job:two');
    await page.evaluate(() => __finishLookup({ok: true, items: __workflowVideos}));
    assert.equal(await page.evaluate(() => AppState.currentView), 'library');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('video search and availability survive refresh and return, with compact missing-file rows', async () => {
  const page = await open({width: 560, height: 500});
  try {
    await page.locator('[data-page=videos]').click(); await page.locator('.video-card').first().waitFor();
    await page.getByRole('button', {name: /^位置待恢复/}).click();
    assert.equal(await page.locator('.video-grid.is-missing-list .video-card').count(), 1);
    assert.equal(await page.locator('.video-path').textContent(), 'D:\\原目录\\course.mp4');
    await page.locator('#videoSearch').fill('不存在');
    await page.getByText('没有匹配的视频', {exact: true}).waitFor();
    await page.locator('#refreshVideos').click();
    assert.equal(await page.locator('.video-card').count(), 0);
    await page.locator('[data-page=workbench]').click(); await page.locator('[data-page=videos]').click();
    assert.equal(await page.locator('#videoSearch').inputValue(), '不存在');
    await page.getByRole('button', {name: '查看全部视频', exact: true}).click();
    assert.equal(await page.locator('.video-card').count(), 3);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('comparison is read-only safe text and a late comparison cannot replace another document', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __workflowText['job:one'].raw = '<img src=x onerror="window.__unsafe=true">\n\n相同';
      __workflowText['job:one'].final = '<script>window.__unsafe=true</script>\n\n相同\n\n新增';
    });
    await library(page); await page.locator('#compareReaderBtn').click();
    await page.locator('.reader-diff').waitFor();
    assert.equal(await page.locator('.reader-diff img, .reader-diff script').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.__unsafe)), false);
    assert(await page.locator('#editReaderBtn').isDisabled());
    assert.match(await page.locator('.diff-summary').textContent(), /调整/);
    await page.locator('#compareReaderBtn').click();
    await page.evaluate(() => { __MOCK__.get_reader = (id, tab) => id === 'job:one'
      ? new Promise(resolve => (window.__comparisonResponses ||= []).push(() => resolve({ok: true, tab, text: '迟到的对照'})))
      : {ok: true, tab, text: '# 正确的新文稿'}; });
    await page.locator('#compareReaderBtn').click();
    await page.locator('.doc[data-doc-id="job:two"] .doc-open').click();
    await page.getByText('正确的新文稿', {exact: true}).waitFor();
    await page.evaluate(() => __comparisonResponses.forEach(resolve => resolve()));
    assert.equal(await page.locator('.reader-diff').count(), 0);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('long comparisons render incrementally and raw-only documents explain the disabled action', async () => {
  const page = await open({width: 560, height: 500});
  try {
    await page.evaluate(() => {
      const text = Array.from({length: 500}, (_, index) => '第 ' + index + ' 段').join('\n\n');
      __workflowText['job:one'] = {raw: text, final: text + '\n\n最后新增'};
      __workflowDocs.find(doc => doc.task_id === 'job:two').has_final = false;
    });
    await library(page); await page.locator('#compareReaderBtn').click(); await page.locator('.reader-diff').waitFor();
    assert.equal(await page.locator('.diff-row').count(), 80);
    await page.locator('.diff-more').click(); assert.equal(await page.locator('.diff-row').count(), 160);
    assert.equal(await page.locator('#readerBody').evaluate(node => node.scrollWidth > node.clientWidth), false);
    await page.locator('#readerBack').click();
    await page.locator('.doc[data-doc-id="job:two"] .doc-open').click();
    await page.waitForFunction(() => document.querySelector('#readerBody h1')?.textContent === '原稿 job:two');
    assert(await page.locator('#compareReaderBtn').isDisabled());
    assert.match(await page.locator('#compareReaderBtn').getAttribute('title'), /同时存在/);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('reader resumes a paragraph after page navigation and a resized browser reload', async () => {
  const page = await open();
  try {
    const text = '# 长文\n\n' + Array.from({length: 120}, (_, index) => '第' + index + '段。' + '用于核对阅读位置的长句。'.repeat(12)).join('\n\n');
    await page.evaluate(text => { __workflowText['job:two'].raw = text; }, text);
    await library(page, 'job:two');
    await page.getByRole('button', {name: '原稿', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#readerBody h1')?.textContent === '长文');
    await page.locator('#readerBody').evaluate(node => { node.scrollTop = 2200; });
    await page.waitForTimeout(250);
    const visible = () => page.locator('#readerBody').evaluate(body => [...body.children].find(node => node.getBoundingClientRect().bottom > body.getBoundingClientRect().top + 1)?.textContent);
    const original = await visible();
    await page.locator('[data-page=videos]').click(); await page.locator('[data-page=library]').click();
    await page.waitForFunction(() => document.querySelector('#readerBody')?.scrollTop > 1000);
    assert.equal(await visible(), original);
    await page.addInitScript(text => { __workflowText['job:two'].raw = text; }, text);
    await page.setViewportSize({width: 960, height: 800}); await page.reload();
    await page.waitForFunction(() => window.__appReady); await page.locator('[data-page=library]').click();
    await page.waitForFunction(() => document.querySelector('#readerBody')?.scrollTop > 1000);
    assert.equal(await visible(), original);
    assert.equal(await page.locator('#readerTabs [aria-pressed=true]').textContent(), '原稿');
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('batch receipt persists and failed-only retry submits exactly the reported task ids', async () => {
  const page = await open();
  try {
    await page.evaluate(() => { AppState.selected = new Set(['job:one', 'job:two']); Workbench.render(); });
    await page.getByRole('button', {name: '批量整理', exact: true}).click();
    await page.getByRole('button', {name: '沿用当前设置', exact: true}).click();
    await page.getByRole('button', {name: '仅重试失败项（1）', exact: true}).waitFor();
    assert.match(await page.locator('.receipt-summary').textContent(), /成功 1.*失败 1/);
    await page.locator('[data-page=videos]').click(); await page.locator('[data-page=workbench]').click();
    await page.getByRole('button', {name: '仅重试失败项（1）', exact: true}).click();
    await page.getByRole('button', {name: '轻整理（保真）', exact: true}).click();
    await page.waitForFunction(() => __workflowCalls.length === 2);
    assert.deepEqual(await page.evaluate(() => __workflowCalls[1]), {action: 'batch', ids: ['job:two'], level: 'light'});
    await page.getByRole('button', {name: '收起批量回执', exact: true}).click();
    await page.reload(); await page.waitForFunction(() => window.__appReady);
    await page.locator('[data-page=workbench]').click();
    await page.getByRole('button', {name: '查看最近批次回执', exact: true}).waitFor();
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('waiting for the browser is actionable and distinct from automatic processing', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __workflowTasks[0].status = 'awaiting_browser'; __workflowTasks[0].kind = 'download'; __workflowTasks[0].task_id = 'dl:waiting';
      __workflowTasks[1].status = 'resolving';
      __MOCK__.open_video_page = id => { window.__openedWaiting = id; return {ok: true}; };
      AppState.tasks = structuredClone(__workflowTasks); Workbench.render(true);
    });
    await page.getByRole('button', {name: /^需处理/}).click();
    assert.equal(await page.locator('.job').count(), 1);
    await page.locator('.job-open').click();
    await page.getByRole('button', {name: '前往视频页面', exact: true}).click();
    assert.equal(await page.evaluate(() => __openedWaiting), 'dl:waiting');
    await page.getByRole('button', {name: /^处理中/}).click();
    assert.deepEqual(await page.locator('.job').evaluateAll(rows => rows.map(row => row.dataset.taskId)), ['job:two']);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('missing linked text stays explicit when unrelated documents update', async () => {
  const page = await open();
  try {
    await page.evaluate(() => MaterialNavigation.go('library', {taskIds: ['job:removed']}));
    await page.getByText('关联文本暂不可用', {exact: true}).waitFor();
    await page.evaluate(() => { __workflowDocs[0].title = '无关的新标题'; Library.refreshLight(); });
    await page.getByText('无关的新标题', {exact: true}).waitFor();
    assert(await page.getByText('关联文本暂不可用', {exact: true}).isVisible());
    assert(await page.locator('#editReaderBtn').isDisabled());
    await page.locator('.doc[data-doc-id="job:two"] .doc-open').click();
    await page.getByText('整理稿 job:two', {exact: true}).waitFor();
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('video filters entered during initial reading apply to the eventual catalog', async () => {
  const page = await open();
  try {
    await page.evaluate(() => { __MOCK__.get_downloaded_videos = () => new Promise(resolve => window.__finishCatalog = resolve); });
    await page.locator('[data-page=videos]').click();
    await page.locator('#videoSearch').fill('移动'); await page.getByRole('button', {name: /^位置待恢复/}).click();
    assert(await page.locator('#videosReadState').isVisible());
    await page.evaluate(() => __finishCatalog({ok: true, items: __workflowVideos}));
    await page.locator('.video-card[data-video-id=video-missing]').waitFor();
    assert.equal(await page.locator('.video-card').count(), 1);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('batch retry is single flight and an interrupted submission is recorded as unconfirmed', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __MOCK__.retranscribe_tasks = () => { window.__retryCalls = (window.__retryCalls || 0) + 1; return new Promise((resolve, reject) => window.__failRetry = reject); };
      AppState.selected = new Set(['job:one', 'job:two']); Workbench.render();
    });
    await page.getByRole('button', {name: '重试/重新转写', exact: true}).evaluate(button => { button.click(); button.click(); });
    assert.equal(await page.evaluate(() => __retryCalls), 1);
    await page.evaluate(() => __failRetry(new Error('请求中断')));
    await page.getByText('已提交 0 · 跳过 0 · 等待浏览器 0 · 未确认 2', {exact: true}).waitFor();
    await page.locator('.batch-receipt summary').click();
    assert(await page.getByText('请求中断', {exact: true}).isVisible());
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('a superseded batch status remains unknown instead of showing zero successes', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __MOCK__.get_batch_agent_status = () => ({run_id: 'other-run', running: false, done: 0});
      AppState.selected = new Set(['job:one', 'job:two']); Workbench.render();
    });
    await page.getByRole('button', {name: '批量整理', exact: true}).click();
    await page.getByRole('button', {name: '沿用当前设置', exact: true}).click();
    await page.getByText('该批次的进度已不可读取，请检查对应文稿。', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: /^仅重试失败项/}).count(), 0);
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('compact task actions keep the status visible and open secondary actions in a keyboard dialog', async () => {
  const page = await open({width: 560, height: 500});
  try {
    await page.locator('.job[data-task-id="job:one"] .job-open').click();
    const status = await page.locator('#inspector .pct').boundingBox();
    const panel = await page.locator('#inspector').boundingBox();
    assert(status.y >= panel.y && status.y + status.height <= panel.y + panel.height);
    await page.getByRole('button', {name: '更多操作', exact: true}).click();
    await page.getByRole('dialog', {name: '任务操作', exact: true}).waitFor();
    await page.locator('.task-action-list').getByRole('button', {name: '在视频库定位', exact: true}).click();
    await page.locator('.video-card.is-located[data-video-id=video-one]').waitFor();
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});

test('the latest translation-view choice wins while a translation read is pending', async () => {
  const page = await open();
  try {
    await page.evaluate(() => {
      __workflowDocs[0].has_translation = true;
      const reader = __MOCK__.get_reader;
      __MOCK__.get_reader = (id, tab) => tab === 'translation'
        ? new Promise(resolve => window.__finishTranslation = () => resolve({ok: true, text: '迟到的译文'})) : reader(id, tab);
    });
    await library(page);
    await page.locator('#readerTransTabs').getByRole('button', {name: '整页译文', exact: true}).click();
    await page.waitForFunction(() => window.__finishTranslation);
    await page.locator('#readerTransTabs').getByRole('button', {name: '原文', exact: true}).click();
    await page.evaluate(() => __finishTranslation());
    assert.equal(await page.locator('#readerBody h1').textContent(), '整理稿 job:one');
    assert(await page.locator('#editReaderBtn').isEnabled());
    assert.deepEqual(page.errors, []);
  } finally { await page.close(); }
});
