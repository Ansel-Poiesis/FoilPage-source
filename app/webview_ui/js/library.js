// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 文本库页：文档列表、文本操作和 Markdown 阅读器 */
'use strict';

window.Library = (() => {
  const LibraryModel = window.LibraryModel;
  const {textSignature, documentView} = LibraryModel;
  let docsRequest = 0, readerRequest = 0, tagRequest = 0, pageObserver = null;
  const pendingSaves = new Set(), deleting = new Set();
  let menuEvents = null, activeMenu = null;
  const readingStorageKey = 'foilpage.readerState.v1';
  let reading = {selectedId: null, documents: {}};
  try {
    const saved = JSON.parse(localStorage.getItem(readingStorageKey));
    if (saved && typeof saved.documents === 'object' && saved.documents && !Array.isArray(saved.documents)) reading = saved;
  } catch (_) { /* Reading remains available when browser storage is unavailable. */ }
  let renderedReading = null, saveReadingTimer = null, savedContext = null, targetIds = null;
  let readingFrame = 0;
  let readerObserver = null;
  let tagIndexRequest = 0;
  const readingSignatures = new WeakMap();
  let state = {
    tagFilter: "",
    sortOrder: "",
    activeDoc: null,
    listError: false,
    listLoading: false,
    selectedId: typeof reading.selectedId === 'string' ? reading.selectedId : null,
    tab: "final",
    docs: [],
    readerText: "",
    readerRevision: null,
    readerTruncated: false,
    editing: false,
    readerReady: false,
    comparing: false,
    // TASK-8.24 翻译视图：off=原文；replace=原文页内替换；segments=逐段对照；
    // page=整页译文。数据懒取自 get_reader(tab=translation)。
    translationView: "off",
    translationData: null,
    translationNotice: "",
  };
  const translationActivity = new Map(), translationWatches = new Map();
  let translationViewTask = "";
  let translationViewRequest = 0;

  function writeReadingState() {
    clearTimeout(saveReadingTimer);
    try { localStorage.setItem(readingStorageKey, JSON.stringify(reading)); } catch (_) { /* Session navigation still works. */ }
  }

  function blockSignature(block) {
    if (!readingSignatures.has(block)) readingSignatures.set(block, textSignature(block.textContent));
    return readingSignatures.get(block);
  }

  function saveReadingPosition() {
    const body = document.getElementById('readerBody');
    if (!body?.clientHeight || AppState.currentView !== 'library' || !renderedReading || state.editing || state.comparing
        || body.clientWidth !== renderedReading.width) return;
    const {blocks, start} = readingBlocks(body);
    const top = body.getBoundingClientRect().top;
    let low = start, high = blocks.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (blocks[middle].getBoundingClientRect().bottom <= top + 1) low = middle + 1;
      else high = middle;
    }
    const index = Math.min(low, blocks.length - 1) - start;
    const block = index >= 0 ? blocks[index + start] : null;
    const record = reading.documents[renderedReading.id] || {};
    const positions = record.positions || {};
    if (block) {
      const bounds = block.getBoundingClientRect();
      positions[renderedReading.tab + '/' + renderedReading.translationView] = {
        index, signature: blockSignature(block),
        offset: (top - bounds.top) / Math.max(1, bounds.height),
        ratio: body.scrollTop / Math.max(1, body.scrollHeight - body.clientHeight),
      };
    }
    reading.documents[renderedReading.id] = {...record, tab: renderedReading.tab, translationView: renderedReading.translationView, positions};
    reading.selectedId = renderedReading.id;
    reading.open = true;
    clearTimeout(saveReadingTimer); saveReadingTimer = setTimeout(writeReadingState, 200);
  }

  function restoreReadingPosition(taskId) {
    const body = document.getElementById('readerBody');
    renderedReading = {id: taskId, tab: state.tab, translationView: state.translationView, width: body.clientWidth};
    const position = reading.documents[taskId]?.positions?.[state.tab + '/' + state.translationView];
    body.scrollTop = 0;
    if (position && position.ratio !== 0) {
      const {blocks, start} = readingBlocks(body);
      let block = blocks[position.index + start];
      if (!block || blockSignature(block) !== position.signature) {
        block = Array.prototype.find.call(blocks, (node, index) => index >= start && blockSignature(node) === position.signature);
      }
      body.scrollTop = block
        ? block.getBoundingClientRect().top - body.getBoundingClientRect().top + position.offset * block.getBoundingClientRect().height
        : (Number(position.ratio) || 0) * (body.scrollHeight - body.clientHeight);
    }
    saveReadingPosition();
  }

  function readingBlocks(body) {
    const first = body.firstElementChild?.nextElementSibling;
    return first?.classList.contains('translation-segments')
      ? {blocks: first.children, start: 0}
      : {blocks: body.children, start: 1};
  }

  function chooseDocument(doc) {
    targetIds = null;
    state.selectedId = doc.task_id;
    state.activeDoc = doc;
    const saved = reading.documents[doc.task_id];
    ({tab: state.tab, translationView: state.translationView} = documentView(doc, saved));
    translationViewTask = doc.task_id;
  }

  window.addEventListener('pagehide', () => { saveReadingPosition(); writeReadingState(); });

  const ICONS = {
    copy: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    close: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m18 6-12 12M6 6l12 12"/></svg>`,
    subtitles: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 14h8M8 18h5"/></svg>`,
    folder: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>`,
  };

  function hasDraft() {
    const editor = document.querySelector('#readerBody .reader-editor');
    return state.editing && !!editor && editor.value !== state.readerText;
  }

  function discardBefore(next) {
    if (!hasDraft()) { next(); return; }
    ConfirmDiscard.run({onDiscard: () => { state.editing = false; state.readerReady = false; ++readerRequest; next(); }});
  }

  function canLeave(onLeave) {
    if (hasDraft()) {
      ConfirmDiscard.run({onDiscard: () => { state.editing = false; ++readerRequest; closeContextMenu(); onLeave?.(); }});
      return false;
    }
    saveReadingPosition(); writeReadingState();
    ++docsRequest; ++readerRequest; closeContextMenu();
    return true;
  }

  function listSortOrder() {
    return state.sortOrder || (AppState.settings?.task_sort_order === 'oldest' ? 'oldest' : 'newest');
  }

  function sortedDocs(docs, order = listSortOrder()) {
    return LibraryModel.sortedDocs(docs, order);
  }

  function applyTagFilter(docs, filter = state.tagFilter) {
    return LibraryModel.applyTagFilter(docs, filter);
  }

  async function loadTagIndex() {
    const select = document.getElementById('libTagFilter');
    if (!select) return;
    const request = ++tagIndexRequest;
    let tags;
    try {
      const items = await AppApi.tagIndex();
      if (Array.isArray(items)) tags = items.map(item => item.tag);
    } catch (_) { /* The current results still supply available tags. */ }
    if (request !== tagIndexRequest || !select.isConnected) return;
    if (tags) select.dataset.tagIndexLoaded = 'true';
    fillTagFilter(tags);
  }

  function fillTagFilter(tags = state.docs.flatMap(doc => doc.tags || [])) {
    const select = document.getElementById('libTagFilter');
    if (!select) return;
    const options = new Set(tags);
    if (state.tagFilter) options.add(state.tagFilter);
    select.replaceChildren(new Option('全部标签', ''));
    for (const tag of options) if (tag) select.add(new Option(tag, tag));
    select.value = state.tagFilter;
  }

  function renderDocMetadata(row, doc) {
    const date = row.querySelector('.doc-date');
    const timestamp = String(doc.updated_at || '');
    const parts = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?/.exec(timestamp);
    date.hidden = !parts;
    if (parts) {
      const [, year, month, day, time] = parts;
      date.dateTime = timestamp.replace(' ', 'T');
      date.textContent = `${Number(year) === new Date().getFullYear() ? '' : year + '/'}${month}/${day}${time ? ' ' + time : ''}`;
      date.title = timestamp;
    }
    const tags = doc.tags || [];
    const tagGroup = row.querySelector('.doc-tags');
    tagGroup.hidden = tags.length === 0;
    if (tags.length) {
      const allTags = I18n.t('标签：') + tags.join('、');
      tagGroup.title = allTags;
      tagGroup.querySelector('.doc-tag-label').textContent = allTags;
      const visibleTags = tags.slice(0, 2);
      for (const tag of visibleTags) {
        const chip = document.createElement('span');
        chip.className = 'doc-tag';
        chip.textContent = tag;
        chip.setAttribute('aria-hidden', 'true');
        tagGroup.appendChild(chip);
      }
      const remaining = tags.length - visibleTags.length;
      if (remaining) {
        const more = document.createElement('span');
        more.className = 'doc-tag-more';
        more.textContent = '+' + remaining;
        more.setAttribute('aria-hidden', 'true');
        tagGroup.appendChild(more);
      }
    }
    const status = row.querySelector('.doc-status');
    status.hidden = doc.has_final || !doc.has_raw;
    row.querySelector('.doc-meta').hidden = tagGroup.hidden && status.hidden && date.hidden;
  }

  function renderListStatus() {
    const host = document.getElementById('libResults');
    if (!host) return;
    const bar = document.getElementById('libResultsBar');
    const retry = document.getElementById('libRetry');
    const filtering = Boolean(state.tagFilter);
    const excluded = filtering && currentDoc() && !state.docs.some(doc => doc.task_id === state.selectedId);
    const status = state.listLoading ? '正在查找文稿' : state.listError
      ? (state.docs.length ? '读取失败，显示上次结果' : '读取文本列表失败')
      : excluded ? '当前文稿不在筛选结果中' : '';
    if (bar) bar.hidden = !status && !state.listError;
    host.classList.toggle('is-message', Boolean(status));
    if (host.dataset.statusText !== status) {
      host.dataset.statusText = status;
      host.textContent = status;
    }
    if (retry) {
      if (!state.listError && document.activeElement === retry) document.getElementById('libTagFilter')?.focus();
      retry.hidden = !state.listError;
      retry.disabled = state.listLoading;
    }
  }

  function renderDocList() {
    const list = document.getElementById("docList");
    if (!list) return;
    if (activeMenu && list.contains(activeMenu._returnFocus)) closeContextMenu(true);
    const focused = document.activeElement, oldRow = focused?.closest?.('.doc');
    const focusedId = oldRow?.dataset.docId, focusMenu = focused?.classList.contains('doc-menu-trigger');
    const scroll = list.scrollTop;
    list.innerHTML = "";
    renderListStatus();
    if (!state.docs.length) {
      const filtering = Boolean(state.tagFilter);
      const title = state.listError ? '读取文本失败' : filtering ? '没有找到文本' : '还没有文稿';
      list.innerHTML = `<div class="empty"><span class="empty-symbol" aria-hidden="true">${AppIcons('library')}</span><div class="big">${title}</div>${state.listError ? '' : filtering ? '<button class="btn" type="button" id="clearEmptyFilter">清除筛选</button>' : '<button class="btn" type="button" id="importEmptyLibrary">导入媒体</button>'}</div>`;
      list.querySelector('#clearEmptyFilter')?.addEventListener('click', clearFilters);
      list.querySelector('#importEmptyLibrary')?.addEventListener('click', () => switchView('import'));
      if (focusedId) document.getElementById('libTagFilter')?.focus({preventScroll:true});
      return;
    }
    state.docs.forEach((doc) => {
      const el = document.createElement("div");
      el.className = "doc" + (doc.task_id === state.selectedId ? " selected" : "");
      el.setAttribute("role", "listitem");
      el.dataset.docId = doc.task_id;
      el.innerHTML = `<button class="doc-open" type="button" aria-current="${doc.task_id === state.selectedId}"><span class="doc-title"></span><span class="doc-meta"><span class="doc-tags" data-i18n-skip><span class="doc-tag-label"></span></span><span class="doc-status">仅原始转写</span><time class="doc-date"></time></span></button>`;
      const title = el.querySelector('.doc-title');
      title.textContent = doc.title;
      title.title = doc.tags?.length ? doc.title + '\n' + I18n.t('标签：') + doc.tags.join('、') : doc.title;
      renderDocMetadata(el, doc);
      bindDocRow(el, doc);
      list.appendChild(el);
    });
    list.scrollTop = scroll;
    if (focusedId) {
      const row = [...list.querySelectorAll('.doc')].find(el => el.dataset.docId === focusedId) || list.querySelector('.doc');
      row?.querySelector(focusMenu ? '.doc-menu-trigger' : '.doc-open')?.focus({preventScroll:true});
    }
  }

  function clearFilters() {
    targetIds = null;
    state.tagFilter = '';
    const tags = document.getElementById('libTagFilter');
    if (tags) {
      tags.value = '';
      tags.focus({preventScroll:true});
    }
    reloadList();
  }

  function reloadList() {
    return loadDocs(false, true);
  }

  function bindListControls() {
    const tags = document.getElementById('libTagFilter');
    const sort = document.getElementById('libSort');
    const retry = document.createElement('button');
    retry.id = 'libRetry'; retry.type = 'button'; retry.className = 'btn ghost small lib-retry';
    retry.textContent = '重新读取'; retry.hidden = true;
    document.getElementById('libResults').after(retry);
    retry.onclick = () => { loadTagIndex(); loadDocs(); };
    sort.value = listSortOrder();
    tags.onchange = () => { targetIds = null; state.tagFilter = tags.value; reloadList(); };
    sort.onchange = () => {
      state.sortOrder = sort.value;
      state.docs = sortedDocs(state.docs);
      renderDocList();
    };
    document.getElementById('libResetFilters').onclick = clearFilters;
  }

  function renderTagEditor() {
    const host = document.getElementById("readerTags");
    if (!host) return;
    const doc = currentDoc();
    if (!doc || doc.status !== "done") {
      host.innerHTML = "";
      return;
    }
    const oldId = host.dataset.docId;
    const wasOpen = oldId === doc.task_id && host.querySelector('details')?.open;
    const oldInput = host.querySelector('#tagInput');
    const inputValue = oldId === doc.task_id ? oldInput?.value || '' : '';
    const wasFocused = oldId === doc.task_id && host.contains(document.activeElement);
    host.dataset.docId = doc.task_id;
    const tags = [...(doc.tags || [])];
    host.innerHTML = `
      <details class="tag-details"><summary>
        <span class="reader-tag-preview" data-i18n-skip${tags.length ? '' : ' hidden'}></span>
        <span class="reader-tag-edit-label">${I18n.t(tags.length ? '编辑标签' : '添加标签')}</span>
      </summary><div class="tag-row">
        <div class="tag-pills"></div>
        <input id="tagInput" class="tag-input" aria-label="添加标签" placeholder="添加标签，回车确认" spellcheck="false">
        <div class="reader-tag-status" role="status"></div>
      </div></details>`;
    const preview = host.querySelector('.reader-tag-preview');
    const previewLimit = 3;
    tags.slice(0, previewLimit).forEach(tag => {
      const pill = document.createElement('span');
      pill.className = 'reader-tag-pill';
      pill.textContent = pill.title = tag;
      preview.append(pill);
    });
    if (tags.length > previewLimit) {
      const more = document.createElement('span');
      more.className = 'reader-tag-pill reader-tag-more';
      more.textContent = '+' + (tags.length - previewLimit);
      more.title = tags.slice(previewLimit).join('、');
      preview.append(more);
    }
    const pills = host.querySelector(".tag-pills");
    const input = host.querySelector("#tagInput");
    const renderPills = () => {
      pills.innerHTML = "";
      tags.forEach((t) => {
        const p = document.createElement("span");
        p.className = "tag tag-editable";
        const txt = document.createElement("span");
        txt.setAttribute('data-i18n-skip', '');
        txt.textContent = t;
        const x = document.createElement("button");
        x.type = "button";
        x.className = "tag-x";
        x.setAttribute("aria-label", "移除标签 " + t);
        x.textContent = "×";
        x.onclick = () => saveTags(tags.filter((v) => v !== t));
        p.appendChild(txt);
        p.appendChild(x);
        pills.appendChild(p);
      });
    };
    const saveTags = async next => {
      const serial = ++tagRequest, typed = input.value;
      input.disabled = true;
      pills.querySelectorAll('button').forEach(button => button.disabled = true);
      try {
        const result = await AppApi.setTags(doc.task_id, next);
        if (!result?.ok) throw new Error(result?.error || '保存标签失败');
        doc.tags = result.tags || next;
        if (input.value === typed) input.value = '';
        loadTagIndex();
        if (serial === tagRequest && state.selectedId === doc.task_id && AppState.currentView === 'library') {
          renderDocList(); renderTagEditor();
          await loadDocs(true);
          if (state.selectedId === doc.task_id && document.activeElement === document.body) document.getElementById('tagInput')?.focus({preventScroll:true});
        }
      } catch (failure) {
        const message = failure.message || '保存标签失败';
        const status = input.closest('.tag-row')?.querySelector('.reader-tag-status');
        if (status?.isConnected) status.textContent = I18n.t(message);
        toast(message, 'error');
      }
      finally {
        if (input.isConnected) {
          input.disabled = false;
          pills.querySelectorAll('button').forEach(button => button.disabled = false);
          input.focus({preventScroll:true});
        }
      }
    };
    input.onkeydown = e => {
      if (e.key !== 'Enter' || e.isComposing || input.disabled) return;
      e.preventDefault();
      const next = input.value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
      if (next.length) saveTags([...new Set([...tags,...next])]);
    };
    renderPills();
    host.querySelector('details').open = !!wasOpen;
    input.value = inputValue;
    if (wasFocused) (oldInput === document.activeElement || wasOpen ? input : host.querySelector('summary')).focus({preventScroll:true});
  }

  function buildLayout() {
    const host = document.getElementById("page-library");
    host.innerHTML = `
      <div class="lib">
        <div class="lib-list" id="libList">
          <div class="lib-tools">
            <div class="lib-facets">
              <select id="libTagFilter" class="lib-tag-filter" title="按标签筛选" aria-label="按标签筛选"><option value="">全部标签</option></select>
              <select id="libSort" aria-label="文稿排序"><option value="newest">最近更新</option><option value="oldest">最早更新</option></select>
              <button id="libResetFilters" class="icon-btn" type="button" aria-label="清空文本筛选" title="清空文本筛选" hidden>${ICONS.close}</button>
            </div>
            <div class="lib-results-bar" id="libResultsBar" hidden>
              <span id="libResults" class="lib-results" role="status"></span>
            </div>
          </div>
          <div class="doc-list" id="docList" role="list" aria-label="文本列表"></div>
        </div>
        <div class="lib-resizer" id="libResizer" role="separator" tabindex="0" aria-label="文稿列表宽度" aria-orientation="vertical" aria-controls="libList" title="拖动或按左右方向键调宽，双击恢复"></div>
        <div class="reader">
          <div class="reader-toolbar">
            <button class="btn ghost small reader-back" id="readerBack" type="button">${AppIcons('back')}<span>文稿列表</span></button>
            <div class="reader-views">
              <div class="reader-tabs" id="readerTabs" role="group" aria-label="文本版本"></div>
              <button class="btn ghost small reader-compare-toggle" id="compareReaderBtn" type="button" aria-pressed="false" disabled hidden>对照改动</button>
              <div class="reader-trans-picker" id="readerTranslationPicker" hidden>
                <button class="btn ghost small" id="readerTranslationBtn" type="button" aria-expanded="false">译文</button>
                <div class="reader-trans-tabs" id="readerTransTabs" role="group" aria-label="译文视图" hidden></div>
              </div>
            </div>
            <div class="reader-actions">
              <button class="btn ghost small" id="copyReaderBtn" type="button" disabled>复制</button>
              <button class="btn ghost small" id="editReaderBtn" type="button" disabled>编辑</button>
              <span class="reader-save-status" id="readerSaveStatus" role="status" hidden></span>
              <button class="btn primary small" id="saveReaderBtn" type="button" hidden>保存修改</button>
              <button class="btn ghost small" id="cancelReaderBtn" type="button" hidden>取消</button>
              <button class="icon-btn reader-menu-trigger" id="readerMenuBtn" type="button" aria-label="文本操作" title="更多操作" aria-haspopup="menu" aria-expanded="false" disabled>${AppIcons('more')}</button>
            </div>
          </div>
          <div class="reader-preview-notice" id="readerPreviewNotice" hidden>
            <span role="status">当前显示部分文稿</span>
            <div class="reader-preview-actions">
              <button class="btn ghost small" id="copyReaderPreview" type="button">复制可见部分</button>
              <button class="btn ghost small" id="locateFullDocument" type="button">定位完整文稿</button>
            </div>
          </div>
          <div class="reader-body" id="readerBody">
            <header class="reader-heading" id="readerHeading" hidden>
              <h2 class="reader-doc-title" tabindex="-1" data-i18n-skip></h2>
              <div class="reader-doc-meta">
                <div class="reader-tags" id="readerTags"></div>
                <span class="reader-version-note" id="readerVersionNote" hidden></span>
              </div>
              <div id="readerTranslationStatus" class="reader-translation-status" hidden></div>
            </header>
            <div class="reader-empty"><div class="big">选择一篇文本</div></div>
          </div>
        </div>
      </div>`;
    bindListControls();
    document.getElementById('readerBody').addEventListener('scroll', () => {
      if (readingFrame) return;
      readingFrame = requestAnimationFrame(() => { readingFrame = 0; saveReadingPosition(); });
    }, {passive: true});
    readerObserver?.disconnect();
    readerObserver = new ResizeObserver(() => {
      const body = document.getElementById('readerBody');
      if (body?.clientHeight && renderedReading && !state.editing && !state.comparing
          && body.clientWidth !== renderedReading.width) restoreReadingPosition(renderedReading.id);
    });
    readerObserver.observe(document.getElementById('readerBody'));
    document.getElementById('compareReaderBtn').onclick = () => discardBefore(() => {
      if (state.comparing) loadReader(state.selectedId, false);
      else loadComparison();
    });
    document.getElementById('readerMenuBtn').onclick = event => {
      const doc = currentDoc();
      if (doc) showContextMenu(doc, event.currentTarget);
    };
    document.getElementById('readerBack').onclick = () => {
      saveReadingPosition(); host.querySelector('.lib').classList.remove('reading');
      reading.open = false; writeReadingState();
      (host.querySelector('.doc.selected .doc-open') || document.getElementById('libTagFilter'))?.focus();
    };
    document.getElementById("copyReaderBtn").onclick = copyReaderText;
    document.getElementById("copyReaderPreview").onclick = copyReaderText;
    document.getElementById("locateFullDocument").onclick = openReaderFile;
    document.getElementById("editReaderBtn").onclick = beginEditing;
    document.getElementById("saveReaderBtn").onclick = saveEditing;
    document.getElementById("cancelReaderBtn").onclick = cancelEditing;
    // 先生 2026-10-19：正文选中后右键可复制——WebView2 默认右键菜单仅在
    // debug 构建 enabled，生产环境在此补齐；编辑态不弹菜单。
    // 先生 2026-10-25：无选中时也提供「复制视频地址」「复制全部 Markdown」。
    const readerBodyEl = document.getElementById("readerBody");
    if (readerBodyEl) {
      readerBodyEl.oncontextmenu = (e) => {
        if (e.target.closest(".reader-editor, .reader-doc-meta, button")) return;
        const sel = String(window.getSelection ? window.getSelection() : "");
        if (!state.readerReady || state.editing || state.comparing) return;
        e.preventDefault();
        showReaderCopyMenu(e.clientX, e.clientY, sel.trim() ? sel : "");
      };
    }
    loadTagIndex();
    initResizer();
  }

  async function showSubtitleMenu(doc, anchor) {
    const read = await AppApi.reader(doc.task_id, doc.has_raw ? 'raw' : 'final');
    if (!anchor.isConnected || AppState.currentView !== 'library' || activeMenu) return;
    if (!read?.ok || read.has_subtitle_timeline === false) {
      throw new Error(read?.error || '这篇文稿没有转写时间轴，重新转写后可导出字幕');
    }
    const menu = document.createElement("div");
    closeContextMenu();
    menu.className = "ctx-menu";
    menu.setAttribute('aria-label', '导出转写字幕');
    menu.innerHTML = `
      <div class="menu-list">
        <button class="ctx-item menu-item" type="button" data-fmt="srt"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>导出 SRT（通用字幕）</button>
        <button class="ctx-item menu-item" type="button" data-fmt="vtt"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/></svg>导出 VTT（Web 字幕）</button>
      </div>`;
    document.body.appendChild(menu);
    positionMenu(menu, anchor);
    menu.querySelectorAll(".ctx-item").forEach((item) => {
      item.onclick = () => {
        closeContextMenu(true);
        exportSubtitle(doc, item.dataset.fmt);
      };
    });
    bindMenuKeyboard(menu, anchor, anchor);
  }

  async function exportSubtitle(doc, fmt) {
    const id = doc.task_id;
    const base = String(doc?.title || '字幕').replace(/[\\/:*?"<>|]+/g, '_').slice(0,60);
    try {
      const picked = await AppApi.pickSaveFile(base + (fmt === 'vtt' ? '.vtt' : '.srt'));
      if (!picked?.ok || !picked.path) {
        if (picked?.error) toast(picked.error, 'error');
        return;
      }
      const result = await AppApi.exportSubtitles(id, fmt, picked.path);
      if (!result?.ok) { toast(result?.error || '导出失败', 'error'); return; }
      toast(`已导出 ${result.count} 条字幕`);
      if (state.selectedId === id && AppState.currentView === 'library') {
        const mask = openModal('字幕已导出', '<p class="export-location" data-i18n-skip></p>', '<button class="btn ghost" id="exportDone" type="button">完成</button><button class="btn" id="exportOpen" type="button">在资源管理器中显示</button>');
        const savedPath = result.path || picked.path;
        mask.querySelector('.export-location').textContent = savedPath;
        mask.querySelector('#exportDone').onclick = closeModal;
        mask.querySelector('#exportOpen').onclick = async () => {
          try { await locateFile(savedPath); }
          catch (error) { toast(error.message || '无法定位字幕文件', 'error'); }
        };
      }
    } catch (_) { toast('导出失败，请重试', 'error'); }
  }

  function initResizer() {
    const list = document.getElementById("libList");
    const rz = document.getElementById("libResizer");
    const library = rz.closest('.lib');
    const min = 220;
    const maximum = () => Math.max(min, Math.floor(library.clientWidth * 0.46));
    const refresh = () => {
      rz.setAttribute('aria-valuemin', min);
      rz.setAttribute('aria-valuemax', maximum());
      rz.setAttribute('aria-valuenow', Math.round(list.getBoundingClientRect().width));
    };
    const resize = width => { list.style.width = Math.min(maximum(), Math.max(min, width)) + 'px'; refresh(); };
    const persist = () => {
      try {
        if (list.style.width) localStorage.setItem('shengjian.libWidth', String(Math.round(list.getBoundingClientRect().width)));
        else localStorage.removeItem('shengjian.libWidth');
      } catch (_) { /* Width still applies to the current page. */ }
    };
    try {
      const saved = parseInt(localStorage.getItem("shengjian.libWidth"), 10);
      if (saved >= min) list.style.width = saved + 'px';
    } catch (_) { /* Default width remains available. */ }
    refresh();
    let drag;
    rz.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      drag = {x: e.clientX, width: list.getBoundingClientRect().width};
      rz.setPointerCapture(e.pointerId);
    });
    rz.onpointermove = e => { if (drag) resize(drag.width + e.clientX - drag.x); };
    rz.onlostpointercapture = () => { if (drag) { drag = null; persist(); } };
    const reset = () => { list.style.removeProperty('width'); refresh(); persist(); };
    rz.ondblclick = reset;
    rz.onfocus = refresh;
    rz.onkeydown = e => {
      const width = list.getBoundingClientRect().width;
      const sizes = {ArrowLeft: width - 16, ArrowRight: width + 16, Home: min, End: maximum()};
      if (!Object.hasOwn(sizes, e.key) && e.key !== 'Enter') return;
      e.preventDefault();
      if (e.key === 'Enter') reset();
      else { resize(sizes[e.key]); persist(); }
    };
  }

  function positionMenu(menu, anchor, point) {
    const pad = 8, gap = 6;
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    if (point) {
      const x = point.clientX + mw + pad > window.innerWidth ? point.clientX - mw : point.clientX;
      const y = point.clientY + mh + pad > window.innerHeight ? point.clientY - mh : point.clientY;
      menu.style.left = Math.max(pad, Math.min(x, window.innerWidth - mw - pad)) + 'px';
      menu.style.top = Math.max(pad, Math.min(y, window.innerHeight - mh - pad)) + 'px';
      return;
    }
    const rect = anchor.getBoundingClientRect();
    const preferBelow = rect.bottom + gap + mh <= window.innerHeight - pad || rect.top - gap - mh < pad;
    const top = preferBelow ? rect.bottom + gap : rect.top - mh - gap;
    const left = rect.right - mw;
    menu.style.left = Math.max(pad, Math.min(left, window.innerWidth - mw - pad)) + 'px';
    menu.style.top = Math.max(pad, Math.min(top, window.innerHeight - mh - pad)) + 'px';
  }

  function closeContextMenu(restoreFocus) {
    const old = activeMenu;
    if (old) {
      menuEvents?.abort(); menuEvents = null;
      activeMenu = null; old.remove();
      old._returnFocus?.closest('[aria-haspopup="menu"]')?.setAttribute('aria-expanded', 'false');
      if (restoreFocus === true && old._returnFocus?.isConnected) old._returnFocus.focus({preventScroll:true});
    }
  }

  function bindMenuKeyboard(menu, returnFocus, anchor = null) {
    menu.setAttribute("role", "menu");
    menu.querySelectorAll('.ctx-item').forEach(item => item.setAttribute('role', 'menuitem'));
    const items = [...menu.querySelectorAll('.ctx-item:not(:disabled)')];
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); closeContextMenu(true); return; }
      if (ev.key === "Tab") { closeContextMenu(true); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(ev.key)) return;
      ev.preventDefault();
      const index = items.indexOf(document.activeElement);
      const next = ev.key === "Home" ? 0 : ev.key === "End" ? items.length - 1
        : (index + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    };
    menuEvents = new AbortController();
    const signal = menuEvents.signal;
    document.addEventListener("keydown", onKey, {signal});
    document.addEventListener('pointerdown', ev => {
      if (!menu.contains(ev.target) && !(anchor && anchor.contains(ev.target))) closeContextMenu();
    }, {capture:true,signal});
    window.addEventListener('resize', () => closeContextMenu(), {signal});
    document.addEventListener('scroll', ev => { if (!menu.contains(ev.target)) closeContextMenu(); }, {capture:true,signal});
    menu._returnFocus = returnFocus;
    menu._anchor = anchor;
    activeMenu = menu;
    returnFocus?.closest('[aria-haspopup="menu"]')?.setAttribute('aria-expanded', 'true');
    items[0]?.focus();
  }

  function hasSource(doc) {
    return !!doc?.source_path && doc.source_available !== false;
  }

  function showContextMenu(doc, anchor, point) {
    if (!point && activeMenu?._anchor === anchor) { closeContextMenu(true); return; }
    closeContextMenu();
    const menu = document.createElement('div');
    menu.className = 'ctx-menu doc-context-menu';
    menu.setAttribute('aria-label', '文本操作');
    const available = hasSource(doc), hasDocument = doc.has_final || doc.has_raw;
    const selectedId = state.selectedId;
    const sourceActions = [
      AppState.tasks.some(item => item.task_id === doc.task_id) && {key: 'view-task', label: '查看任务', icon: AppIcons('tasks'), run: () => MaterialNavigation.go('workbench', {taskIds: [doc.task_id]})},
      MaterialNavigation.isVideo(doc.source_path) && {key: 'view-video', label: '在视频库定位', icon: AppIcons('video'), run: button => MaterialNavigation.openVideo(doc.task_id, button, selectedId)},
      available && {key: 'open-video', label: '播放源文件', icon: AppIcons('play'), run: async () => {
        const result = await AppApi.playMedia(doc);
        if (!result?.ok) throw new Error(result?.error || '源文件无法播放');
      }},
      available && {key: 'locate-source', label: '定位源文件', icon: ICONS.folder, run: () => locateFile(doc.source_path)},
    ];
    const fileActions = [
      hasDocument && {key: 'locate-md', label: '定位文稿文件', icon: ICONS.folder, run: () => useDocumentFile(doc, path => locateFile(path))},
      hasDocument && {key: 'copy-md', label: '复制 Markdown 地址', icon: ICONS.copy, run: () => useDocumentFile(doc, path => copyText(path, 'Markdown 地址已复制'))},
      available && {key: 'copy-video-path', label: '复制视频地址', icon: ICONS.copy, run: () => copyText(doc.source_path, '视频地址已复制')},
      hasDocument && {key: 'subtitle', label: '导出转写字幕…', icon: ICONS.subtitles, run: () => showSubtitleMenu(doc, anchor)},
    ];
    const processing = [
      available && {key: 'retranscribe', label: '重新转写…', icon: AppIcons('retry'), run: () => showRetranscribeDialog(doc)},
      hasDocument && {key: 'translate', label: '翻译…', icon: AppIcons('document'), run: () => showTranslateDialog(doc)},
    ];
    const deletion = [
      {key: 'delete-text', label: '仅删除文稿', effects: '原稿、整理稿、译文和转写过程文件将删除。视频和任务记录保留。',
        remove: id => AppApi.deleteTexts([id]), done: '文稿已删除'},
      {key: 'delete-files', label: '删除文稿和下载视频', effects: '文稿、任务生成文件及未被其他任务使用的 FoilPage 下载视频将删除。任务记录和手动导入的源文件保留。',
        remove: id => AppApi.deleteLibraryItems([id], false), done: '文件已删除，任务记录已保留'},
      {key: 'delete-all', label: '删除文件并移除任务', effects: '任务记录、文稿、任务生成文件及未被其他任务使用的 FoilPage 下载视频将删除。手动导入的源文件保留。',
        remove: id => AppApi.deleteLibraryItems([id], true), done: '任务及相关文件已删除'},
    ].map(action => ({...action, icon: AppIcons('trash'), danger: true, run: () => confirmDocumentDelete(doc, action)}));
    const groups = state.editing && anchor.id === 'readerMenuBtn' ? [sourceActions] : [sourceActions, fileActions, processing, deletion];
    const actions = groups.map(group => group.filter(Boolean)).filter(group => group.length);
    const list = document.createElement('div');
    list.className = 'menu-list';
    menu.append(list);
    actions.forEach((group, index) => {
      if (index) {
        const divider = document.createElement('div');
        divider.className = 'menu-sep'; divider.setAttribute('role', 'separator');
        list.append(divider);
      }
      for (const action of group) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ctx-item menu-item' + (action.danger ? ' danger' : '');
        button.disabled = action.danger === true && deleting.has(doc.task_id);
        button.dataset.act = action.key;
        button.innerHTML = action.icon + action.label;
        button.onclick = async () => {
          closeContextMenu(true);
          try { await action.run(button); }
          catch (error) { toast(error.message || '操作失败，请重试', 'error'); }
        };
        list.append(button);
      }
    });
    document.body.appendChild(menu);
    positionMenu(menu, anchor, point);
    bindMenuKeyboard(menu, anchor, point ? null : anchor);
  }

  async function useDocumentFile(doc, action) {
    const tab = doc.task_id === state.selectedId && ['final', 'raw'].includes(state.tab)
      ? state.tab : doc.has_final ? 'final' : 'raw';
    const result = await AppApi.reader(doc.task_id, tab);
    if (!result?.ok || !result.path) throw new Error(result?.error || '无法定位文稿文件');
    await action(result.path);
  }

  async function locateFile(path) {
    const result = await AppApi.openPath(path);
    if (!result?.ok) throw new Error(result?.error || '无法打开文件位置');
  }

  /* 正文右键复制菜单（先生 2026-10-19）：选中非空时提供「复制选中文字」；
     「复制全部 Markdown」与工具栏按钮同链路；先生 2026-10-25 补「复制视频地址」，
     无选中时同样可用。 */
  function showReaderCopyMenu(x, y, selection) {
    closeContextMenu();
    const doc = currentDoc();
    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    const items = [];
    if (selection) {
      items.push(`<button class="ctx-item menu-item" type="button" data-act="copy-selection"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>复制选中文字</button>`);
    }
    const copyLabel = state.readerTruncated && state.translationView === "off" ? "复制可见部分" : "复制全部 Markdown";
    items.push(`<button class="ctx-item menu-item" type="button" data-act="copy-all"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13"/><path d="M16 12h5"/><path d="M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></svg>${copyLabel}</button>`);
    if (hasSource(doc)) {
      items.push(`<button class="ctx-item menu-item" type="button" data-act="copy-video-path"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/></svg>复制视频地址</button>`);
    }
    menu.innerHTML = `<div class="menu-list">${items.join("")}</div>`;
    document.body.appendChild(menu);
    positionMenu(menu, null, {clientX: x, clientY: y});
    menu.querySelectorAll(".ctx-item").forEach((item) => {
      item.onclick = (e) => {
        e.stopPropagation();
        closeContextMenu(true);
        if (item.dataset.act === "copy-selection") copyText(selection, "已复制选中文字");
        else if (item.dataset.act === "copy-video-path") copyText(doc.source_path, "视频地址已复制");
        else copyReaderText();
      };
    });
    bindMenuKeyboard(menu, document.activeElement);
  }

  function bindDocRow(el, doc) {
    const open = el.querySelector('.doc-open');
    el.onclick = () => {
      const layout = el.closest('.lib');
      const showReader = () => {
        layout.classList.add('reading');
        if (!document.getElementById('libList')?.clientWidth) focusReaderHeading();
      };
      if (state.selectedId === doc.task_id && (state.editing || state.readerReady)) { showReader(); return; }
      discardBefore(() => {
        saveReadingPosition(); chooseDocument(doc);
        layout.classList.add('reading');
        renderDocs(); renderListStatus(); loadReader(doc.task_id, true); showReader();
      });
    };
    el.onkeydown = e => {
      if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
        e.preventDefault(); e.stopPropagation();
        showContextMenu(doc, e.target);
      }
    };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      showContextMenu(doc, e.target.closest('.doc-menu-trigger') || open, e);
    };
    const actions = document.createElement('button');
    actions.type = 'button'; actions.className = 'icon-btn doc-menu-trigger';
    actions.setAttribute('aria-label', '文本操作'); actions.title = '文本操作';
    actions.setAttribute('aria-haspopup', 'menu'); actions.setAttribute('aria-expanded', 'false');
    actions.innerHTML = AppIcons('more');
    actions.onclick = e => {
      e.stopPropagation();
      showContextMenu(doc, actions);
    };
    el.append(actions);
  }

  function renderTabs(task) {
    const wrap = document.getElementById("readerTabs");
    if (!wrap) return;
    const tabs = [
      { key: "final", label: "整理稿", enabled: !!(task && task.has_final) },
      { key: "raw", label: "原始转写", enabled: !!(task && task.has_raw) },
    ];
    tabs.forEach((t) => {
      let el = wrap.querySelector('[data-version="' + t.key + '"]');
      if (!el) {
        el = document.createElement('button');
        el.type = 'button';
        el.dataset.version = t.key;
        el.textContent = I18n.t(t.label);
        el.onclick = () => {
          if (el.disabled || (state.tab === t.key && state.readerReady && !state.comparing)) return;
          discardBefore(() => { state.tab = t.key; loadReader(state.selectedId, true); });
        };
        wrap.appendChild(el);
      }
      el.hidden = !t.enabled;
      el.disabled = !t.enabled;
      el.setAttribute("aria-pressed", String(t.key === state.tab));
      el.className = "seg" + (t.key === state.tab ? " active" : "");
    });
    wrap.hidden = !tabs.some(t => t.enabled);
  }

  async function loadDocs(light = false, keepReader = false) {
    const list = document.getElementById("docList");
    if (!list || AppState.currentView !== 'library' || (light && state.listLoading)) return;
    const serial = ++docsRequest, filter = state.tagFilter;
    const reset = document.getElementById('libResetFilters'); if (reset) reset.hidden = !filter;
    if (!light) { state.listLoading = true; renderListStatus(); }
    let docs;
    try {
      docs = await AppApi.documents("", "全部");
      if (!Array.isArray(docs)) throw new Error('Invalid document list');
    } catch (_) {
      if (serial === docsRequest && AppState.currentView === 'library' && list.isConnected) {
        const hadError = state.listError;
        state.listLoading = false; state.listError = true;
        if (!hadError || !list.childElementCount) renderDocList();
        else renderListStatus();
      }
      return;
    }
    if (serial !== docsRequest || AppState.currentView !== 'library' || !list.isConnected) return;
    const repeat = !state.listError && !state.listLoading;
    state.listLoading = false; state.listError = false;
    const selection = docs.find(doc => doc.task_id === state.selectedId);
    const matching = sortedDocs(applyTagFilter(docs, filter));
    const fingerprint = arr => JSON.stringify(arr.map(d => d && [d.task_id,d.title,d.status,d.updated_at,d.tags,d.has_final,d.has_raw,d.source_path,d.source_available,d.has_translation,d.translation_lang,d.translation_source_tab]));
    const previous = currentDoc();
    // A tag filter cannot prove a document was removed. Keep the open record even when the results exclude it.
    state.activeDoc = selection || ((filter || state.editing) ? previous : null);
    if (light && repeat && fingerprint(matching) === fingerprint(state.docs)
        && fingerprint([state.activeDoc]) === fingerprint([previous])) return;
    state.docs = matching;
    if (!document.getElementById('libTagFilter')?.dataset.tagIndexLoaded) fillTagFilter(docs.flatMap(doc => doc.tags || []));
    const sort = document.getElementById('libSort');
    if (sort && sort !== document.activeElement) sort.value = listSortOrder();
    if (targetIds) {
      const target = sortedDocs(matching, 'newest').find(doc => targetIds.includes(doc.task_id));
      if (target) chooseDocument(target);
      else {
        state.activeDoc = null; state.selectedId = null; state.readerReady = false;
        renderDocList(); renderTabs(null); updateReaderActions();
        replaceReaderContent('<div class="reader-empty" role="status"><div class="big">关联文本暂不可用</div><p>转写尚未产出文本，或文稿已经移除。</p></div>');
        return;
      }
    }
    const current = currentDoc();
    const versionRemoved = current && ((state.tab === 'raw' && !current.has_raw) || (state.tab === 'final' && !current.has_final));
    const needReader = current && ((!keepReader && !state.readerReady) || versionRemoved);
    if (!state.editing && !current) {
      if (matching.length && !filter) chooseDocument(matching[0]);
      else {
        state.selectedId = null; state.readerReady = false; state.readerText = ''; ++readerRequest;
        renderTabs(null); updateReaderActions();
        replaceReaderContent(`<div class="reader-empty"><div class="big">${I18n.t('选择一篇文本开始阅读')}</div></div>`);
        document.querySelector('#page-library .lib')?.classList.remove('reading');
      }
    } else if (!state.editing && needReader) {
      chooseDocument(current);
    }
    renderDocList();
    if (keepReader) list.scrollTop = 0;
    if (!state.editing && state.selectedId) {
      if (needReader || !current) loadReader(state.selectedId, true);
      else if (fingerprint([current]) !== fingerprint([previous])) {
        renderTabs(current); renderTranslationTabs(); renderReaderHeader();
      }
    }
  }

  /* ------------------------------------------------------------ 译文视图（TASK-8.24）
     三种阅读方式（先生 2026-10-25）：replace=原文页内替换；segments=逐段对照；
     page=整页译文。控制条只在当前文本已有译文时出现。 */
  const TRANSLATION_VIEWS = [
    { key: "off", label: "原文" },
    { key: "replace", label: "替换原文" },
    { key: "segments", label: "逐段对照" },
    { key: "page", label: "整页译文" },
  ];

  function currentDoc() {
    return state.docs.find((d) => d.task_id === state.selectedId)
      || (state.activeDoc?.task_id === state.selectedId ? state.activeDoc : null);
  }

  function translationSourceLabel(tab) {
    if (tab === 'final') return I18n.t('译自整理稿');
    if (tab === 'raw') return I18n.t('译自原始转写');
    return I18n.t('译文来源版本未知');
  }

  function renderTranslationStatus() {
    const host = document.getElementById('readerTranslationStatus');
    if (!host) return;
    const doc = currentDoc(), run = translationActivity.get(state.selectedId);
    const inTranslation = state.translationView !== 'off' && !state.comparing && !state.editing;
    const messages = [];
    let action = null;
    if (inTranslation) messages.push(translationSourceLabel(state.translationData?.sourceTab));
    if (state.translationNotice) messages.push(I18n.t(state.translationNotice));
    if (run?.status === 'running') messages.push(I18n.t('正在翻译，可继续阅读'));
    if (run?.status === 'unknown') {
      messages.push(I18n.t('暂不能确认翻译结果'));
      action = ['刷新翻译状态', () => refreshTranslationStatus(doc.task_id, true)];
    }
    if (run?.status === 'failed') {
      messages.push(I18n.t(doc?.has_translation || run.hasTranslation ? '本次翻译失败，仍可查看上次译文' : '翻译失败'));
      if (run.error && run.error !== '翻译失败') messages.push(run.error);
      action = ['重新翻译', () => showTranslateDialog(doc)];
    }
    if (run?.status === 'done' && run.preferred) {
      messages.push(I18n.t(run.hasTranslation ? '翻译已完成' : '翻译已完成，译文文件不可用'));
      if (run.hasTranslation) action = ['查看译文', () => discardBefore(() => setTranslationView(run.preferred, true))];
    }
    host.hidden = !doc || !messages.length;
    const message = messages.join(' · ');
    if (host.dataset.taskId === doc?.task_id && host.dataset.message === message
        && host.dataset.action === (action?.[0] || '')) return;
    host.dataset.taskId = doc?.task_id || '';
    host.dataset.message = message;
    host.dataset.action = action?.[0] || '';
    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    status.textContent = message;
    host.replaceChildren(status);
    if (!host.hidden && action) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'btn ghost small';
      button.textContent = I18n.t(action[0]); button.onclick = action[1];
      host.append(button);
    }
  }

  function renderTranslationTabs() {
    const picker = document.getElementById('readerTranslationPicker');
    if (!picker) return;
    const host = document.getElementById('readerTransTabs');
    const toggle = document.getElementById('readerTranslationBtn');
    const doc = currentDoc(), run = translationActivity.get(state.selectedId);
    const available = !!(doc?.has_translation || run?.hasTranslation) && state.readerReady && !state.comparing;
    picker.hidden = !available;
    const show = (open) => {
      host.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
    };
    if (!available) { show(false); renderTranslationStatus(); return; }
    if (!host.childElementCount) {
      toggle.onclick = () => show(host.hidden);
      picker.onfocusout = event => { if (!picker.contains(event.relatedTarget)) show(false); };
      picker.onkeydown = event => {
        if (event.key === 'Escape' && !host.hidden) {
          event.stopPropagation(); show(false); toggle.focus(); return;
        }
        const items = [...host.querySelectorAll('button')];
        if (!items.length) return;
        if (host.hidden && event.target === toggle && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
          event.preventDefault(); show(true);
          (host.querySelector('.active') || items[0]).focus();
          return;
        }
        if (host.hidden || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = items.indexOf(document.activeElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      };
      for (const view of TRANSLATION_VIEWS) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'seg';
        button.dataset.view = view.key;
        button.textContent = I18n.t(view.label);
        button.onclick = () => {
          show(false); toggle.focus({preventScroll: true});
          discardBefore(() => setTranslationView(view.key));
        };
        host.append(button);
      }
    }
    for (const button of host.children) {
      const selected = state.translationView === button.dataset.view;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
    toggle.setAttribute('aria-label', I18n.t('译文阅读方式'));
    toggle.setAttribute('aria-controls', host.id);
    toggle.classList.toggle('active', state.translationView !== 'off');
    toggle.title = translationSourceLabel(state.translationData?.sourceTab || doc?.translation_source_tab);
    renderTranslationStatus();
  }

  async function readTranslationData(refresh) {
    const doc = currentDoc();
    const run = translationActivity.get(state.selectedId);
    if (!doc || !(doc.has_translation || run?.hasTranslation)) return null;
    if (!refresh && !run?.refresh && state.translationData?.task_id === state.selectedId) return state.translationData;
    const serial = readerRequest;
    try {
      const data = await AppApi.reader(state.selectedId, "translation");
      if (serial !== readerRequest || state.selectedId !== doc.task_id || AppState.currentView !== 'library') return null;
      if (!data?.ok) throw new Error(data?.error || "译文不可用");
      return {
        task_id: doc.task_id,
        text: String(data.text || ""),
        segments: Array.isArray(data.segments) ? data.segments : [],
        lang: String(data.lang || ""),
        direction: String(data.direction || ""),
        sourceTab: String(data.source_tab || doc.translation_source_tab || ""),
      };
    } catch (error) {
      if (serial === readerRequest && state.selectedId === doc.task_id) toast(error.message || "译文读取失败", "error");
      return null;
    }
  }

  function renderTranslationBody() {
    const data = state.translationData;
    if (!data) return;
    if (state.translationView === "segments") {
      const box = document.createElement("div");
      box.className = "translation-segments";
      data.segments.forEach((pair) => {
        const item = document.createElement("div");
        item.className = "seg-pair";
        const src = document.createElement("div");
        src.className = "seg-src";
        src.innerHTML = Markdown.render(pair.src || "");
        const dst = document.createElement("div");
        dst.className = "seg-dst";
        dst.innerHTML = Markdown.render(pair.dst || "");
        for (const [node, text] of [[src, '原文'], [dst, '译文']]) {
          const label = document.createElement('span');
          label.className = 'translation-pair-label'; label.textContent = I18n.t(text);
          node.prepend(label);
        }
        item.append(src, dst);
        box.appendChild(item);
      });
      replaceReaderContent(box.outerHTML);
      return;
    }
    // replace 与 page 同渲染整篇译文；page 顶部带方向徽标，replace 保持正文原样。
    if (state.translationView === "page") {
      const badge = document.createElement("div");
      badge.className = "translation-badge";
      const dirLabel = data.direction === "zh-en" ? "中 → 英"
        : data.direction === "en-zh" ? "英 → 中" : (data.lang === "en" ? "中 → 英" : "英 → 中");
      badge.textContent = `${I18n.t('译文')}（${dirLabel}）`;
      replaceReaderContent(badge.outerHTML + Markdown.render(data.text));
    } else {
      replaceReaderContent(Markdown.render(data.text));
    }
  }

  async function setTranslationView(view, refresh = false) {
    const viewRequest = ++translationViewRequest;
    if (view === state.translationView && !refresh) return;
    saveReadingPosition();
    const serial = readerRequest, taskId = state.selectedId;
    if (view === "off") {
      state.translationView = "off";
      state.translationNotice = '';
      renderTranslationTabs(); updateReaderActions();
      loadReader(state.selectedId, false);
      return;
    }
    const data = await readTranslationData(refresh);
    if (!data || viewRequest !== translationViewRequest || serial !== readerRequest || taskId !== state.selectedId
        || AppState.currentView !== 'library' || state.comparing || state.editing) return;
    state.translationData = data;
    const withoutSegments = view === 'segments' && !data.segments.length;
    state.translationNotice = withoutSegments ? '缺少逐段对照数据，正在显示整页译文' : '';
    state.translationView = withoutSegments ? 'page' : view;
    const run = translationActivity.get(taskId);
    if (run?.status === 'done') { run.preferred = ''; run.refresh = false; }
    translationViewTask = state.selectedId;
    state.editing = false;
    renderTranslationTabs(); updateReaderActions();
    renderTranslationBody();
    restoreReadingPosition(taskId);
  }

  function applyTranslationStatus(taskId, status) {
    const previous = translationActivity.get(taskId);
    const phase = status?.running ? 'running' : status?.status;
    if (!['running', 'failed', 'done', 'unknown'].includes(phase)) return;
    if (!previous && phase === 'done') return;
    const ready = phase === 'done' && ['running', 'unknown'].includes(previous?.status);
    translationActivity.set(taskId, {
      ...previous, status: phase, error: status.error,
      hasTranslation: status.has_translation ?? previous?.hasTranslation ?? false,
      refresh: ready || previous?.refresh,
    });
    if (AppState.currentView !== 'library') return;
    if (taskId === state.selectedId) renderTranslationTabs();
    if (ready) void loadDocs(true);
  }

  async function refreshTranslationStatus(taskId, manual = false) {
    if (translationWatches.has(taskId)) return;
    const context = translationActivity.get(taskId);
    try {
      const status = await AppApi.translationStatus(taskId);
      if (translationWatches.has(taskId) || translationActivity.get(taskId) !== context) return;
      if (status?.running) {
        startTranslationPoll(taskId, context?.preferred || 'page');
      } else {
        applyTranslationStatus(taskId, status);
      }
    } catch (_) {
      if (manual && translationActivity.get(taskId) === context) toast('暂不能确认翻译结果', 'error');
    }
  }

  function startTranslationPoll(taskId, preferred) {
    const watch = {ticks: 0, misses: 0};
    translationWatches.set(taskId, watch);
    translationActivity.set(taskId, {...translationActivity.get(taskId), status: 'running', preferred});
    if (state.selectedId === taskId) renderTranslationStatus();
    const poll = async () => {
      if (translationWatches.get(taskId) !== watch) return;
      let status;
      try { status = await AppApi.translationStatus(taskId); } catch (_) { /* Retry a temporary bridge failure. */ }
      if (translationWatches.get(taskId) !== watch) return;
      const terminal = !status?.running && ['done', 'failed'].includes(status?.status);
      if (status?.running || status?.status === 'running') watch.misses = 0;
      else watch.misses += 1;
      if (terminal || ++watch.ticks >= 300 || watch.misses >= 5) {
        translationWatches.delete(taskId);
        applyTranslationStatus(taskId, terminal ? status : {status: 'unknown'});
        return;
      }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  }

  /* 重新转写…（TASK-8.24：可选模式）与 翻译… 弹窗 */
  function showRetranscribeDialog(doc) {
    const s = AppState.settings || {};
    const noRemind = s.reorganize_no_remind === true;
    const lastMode = s.reorganize_last_mode || "article";
    const lastLevel = s.reorganize_last_level || "auto";
    if (noRemind) { startRetranscribe(doc, lastMode, lastLevel); return; }
    const mask = openModal(
      `重新转写「${doc.title || '当前文本'}」`,
      `<div class="modal-field"><label for="retranscribeMode">转写模式</label>
        <select id="retranscribeMode" class="ctrl" style="width:100%">
          <option value="default">按当前设置（整理跟随全局开关）</option>
          <option value="raw">仅原稿（本次不跑 AI 整理，速度快）</option>
          <option value="article">原稿＋AI 整理稿（本次强制整理，消耗 AI 额度）</option>
        </select></div>
       <div class="modal-field"><label for="retranscribeLevel">整理力度</label>
        <select id="retranscribeLevel" class="ctrl" style="width:100%">
          <option value="">沿用当前设置</option>
          <option value="auto">智能（按内容自选）</option>
          <option value="light">轻整理（保真）</option>
          <option value="balanced">适度整理（平衡）</option>
          <option value="structured">结构整理（成篇）</option>
          <option value="rewrite">精炼改写（自由）</option>
        </select></div>
       <div class="modal-field" style="flex-direction:row;align-items:center;gap:8px">
         <input type="checkbox" id="retranscribeNoRemind" style="width:auto" />
         <label for="retranscribeNoRemind" style="margin:0;cursor:pointer">不再每次提醒，下次直接按本次选择执行</label>
       </div>
       <p class="modal-note">需要视频文件仍在本机；转写进度见工作台。整理力度在本次转写跑 AI 整理时生效。</p>`,
      `<button class="btn ghost" id="retranscribeCancel" type="button">取消</button><button class="btn primary" id="retranscribeStart" type="button">开始转写</button>`,
    );
    mask.querySelector("#retranscribeMode").value = lastMode;
    mask.querySelector("#retranscribeLevel").value = lastLevel;
    mask.querySelector("#retranscribeCancel").onclick = closeModal;
    mask.querySelector("#retranscribeStart").onclick = async () => {
      const mode = mask.querySelector("#retranscribeMode").value;
      const level = mask.querySelector("#retranscribeLevel").value;
      const skip = mask.querySelector("#retranscribeNoRemind").checked;
      const button = mask.querySelector("#retranscribeStart");
      button.disabled = true;
      try {
        const patch = { reorganize_no_remind: skip, reorganize_last_mode: mode, reorganize_last_level: level };
        const saved = await AppApi.saveSettings(patch);
        if (saved?.ok === false) throw new Error(saved.error || "保存失败");
        AppState.settings = { ...AppState.settings, ...patch };
        await startRetranscribe(doc, mode, level);
      } catch (error) {
        toast(error.message || "保存失败", "error");
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    };
  }

  async function startRetranscribe(doc, mode, level) {
    try {
      const result = await AppApi.retranscribe([doc.task_id], mode, level);
      if (!result?.ok) throw new Error(result?.error || "重新转写失败");
      closeModal();
      if (Array.isArray(result.created) && result.created.length) {
        toast("已开始重新转写，进度见工作台");
      } else {
        toast("未能重新转写：视频文件可能已不在本机，或同源任务正在处理", "error");
      }
    } catch (error) {
      toast(error.message || "重新转写失败", "error");
    }
  }

  function showTranslateDialog(doc) {
    const billing = (AppState.settings || {}).agent_billing_mode === "hosted";
    const mask = openModal(
      `翻译「${doc.title || '当前文本'}」`,
      `<div class="modal-field"><label for="translateDirection">翻译方向</label>
        <select id="translateDirection" class="ctrl" style="width:100%">
          <option value="auto">自动检测（中文→英文 / 英文→中文）</option>
          <option value="zh-en">中文 → 英文</option>
          <option value="en-zh">英文 → 中文</option>
        </select></div>
       <div class="modal-field"><label for="translateDisplay">首选阅读方式</label>
        <select id="translateDisplay" class="ctrl" style="width:100%">
          <option value="replace">原文替换（译文直接替换正文显示）</option>
          <option value="segments">逐段对照（每段原文下附译文）</option>
          <option value="page">整页翻译（完整译文单独成页）</option>
        </select></div>
       <p class="modal-note">${billing ? "托管模式：翻译按 AI 积分计费（与整理同口径）。" : "BYOK 模式：使用自配 API Key 翻译。"}优先翻译整理稿，整理稿不可用时使用原始转写。新译文完成后替换旧译文。</p>`,
      `<button class="btn ghost" id="translateCancel" type="button">取消</button><button class="btn primary" id="translateStart" type="button">开始翻译</button>`,
    );
    mask.querySelector("#translateCancel").onclick = closeModal;
    mask.querySelector("#translateStart").onclick = async () => {
      const direction = mask.querySelector("#translateDirection").value;
      const display = mask.querySelector("#translateDisplay").value;
      mask.querySelector("#translateStart").disabled = true;
      try {
        const result = await AppApi.translateText(doc.task_id, direction);
        if (!result?.ok) throw new Error(result?.error || "翻译未能开始");
        closeModal();
        toast(I18n.t('翻译已开始，完成后可在文稿中查看'));
        startTranslationPoll(doc.task_id, display);
      } catch (error) {
        mask.querySelector("#translateStart").disabled = false;
        toast(error.message || "翻译未能开始", "error");
      }
    };
  }

  function renderDocs() {
    const list = document.getElementById("docList");
    if (!list) return;
    Array.from(list.children).forEach((el, i) => {
      const doc = state.docs[i];
      el.classList.toggle("selected", !!doc && doc.task_id === state.selectedId);
      el.querySelector(".doc-open")?.setAttribute("aria-current", String(!!doc && doc.task_id === state.selectedId));
    });
  }

  async function loadReader(taskId, rebuildTabs) {
    const body = document.getElementById("readerBody");
    if (!body) return;
    saveReadingPosition(); renderedReading = null;
    state.comparing = false; body.classList.remove('is-comparing');
    const requestedTab = state.tab, serial = ++readerRequest;
    state.editing = false; state.readerReady = false;
    state.readerText = "";
    state.readerRevision = null;
    state.readerTruncated = false;
    state.translationData = null;
    state.translationNotice = '';
    if (taskId !== translationViewTask) state.translationView = "off";
    updateReaderActions();
    renderTranslationTabs();
    if (rebuildTabs) renderTabs(currentDoc());
    replaceReaderContent('<div class="reader-empty" role="status"><div class="big">正在读取文本</div></div>');
    try {
      const data = await AppApi.reader(taskId, requestedTab);
      if (serial !== readerRequest || taskId !== state.selectedId || requestedTab !== state.tab || AppState.currentView !== "library" || !body.isConnected) return;
      if (!data || !data.ok) {
        showReaderError(data?.error || '文件不可用', taskId);
        updateReaderActions();
        return;
      }
      state.tab = ['final','raw'].includes(data.tab) ? data.tab : requestedTab;
      state.readerReady = true;
      state.readerText = data.text || "";
      state.readerRevision = typeof data.revision === "string" ? data.revision : null;
      state.readerTruncated = !!data.truncated;
      replaceReaderContent(Markdown.render(data.text));
      await document.fonts.ready;
      if (serial !== readerRequest || taskId !== state.selectedId || AppState.currentView !== 'library') return;
      updateReaderActions(state.tab);
      renderTranslationTabs();
      refreshTranslationStatus(taskId);
      if (state.translationView !== "off") {
        const desired = state.translationView; state.translationView = 'off';
        await setTranslationView(desired);
      }
      if (serial !== readerRequest || taskId !== state.selectedId || AppState.currentView !== 'library') return;
      restoreReadingPosition(taskId);
      if (rebuildTabs) {
        renderTabs(currentDoc());
      }
    } catch (_) {
      if (serial !== readerRequest || taskId !== state.selectedId || requestedTab !== state.tab || AppState.currentView !== 'library' || !body.isConnected) return;
      state.readerReady = false;
      showReaderError('读取失败', taskId);
      updateReaderActions();
    }
  }

  function updateReaderActions() {
    const copy = document.getElementById("copyReaderBtn");
    const edit = document.getElementById("editReaderBtn");
    const save = document.getElementById("saveReaderBtn");
    const cancel = document.getElementById("cancelReaderBtn");
    const ready = state.readerReady && !!state.selectedId;
    const inTranslation = state.translationView !== "off";
    const editable = ready && !state.readerTruncated && !inTranslation && !state.comparing;
    const preview = ready && state.readerTruncated && !inTranslation && !state.comparing;
    const menu = document.getElementById('readerMenuBtn');
    const selected = currentDoc();
    const hasRelatedActions = selected && (hasSource(selected) || MaterialNavigation.isVideo(selected.source_path)
      || AppState.tasks.some(item => item.task_id === selected.task_id));
    menu.hidden = state.editing && !hasRelatedActions;
    menu.disabled = !selected;
    if (copy) {
      copy.hidden = preview || state.editing;
      copy.disabled = !ready || state.comparing;
      copy.title = I18n.t(inTranslation ? '复制译文' : state.tab === 'raw' ? '复制原始转写' : '复制整理稿');
    }
    document.getElementById("readerPreviewNotice").hidden = !preview;
    if (edit) { edit.hidden = state.editing; edit.disabled = !editable; }
    const editor = document.querySelector('#readerBody .reader-editor');
    const saving = pendingSaves.has(state.selectedId + '@' + state.tab);
    if (save) { save.hidden = !state.editing; save.disabled = saving || editor?.dataset.needsLatest === 'true'; }
    if (cancel) cancel.hidden = !state.editing;
    const status = document.getElementById('readerSaveStatus');
    status.hidden = !state.editing;
    if (state.editing) {
      const label = saving ? '正在保存' : editor?.dataset.needsLatest === 'true' ? '待核对版本' : hasDraft() ? '未保存' : '无未保存修改';
      const translated = I18n.t(label);
      if (status.textContent !== translated) status.textContent = translated;
    }
    const compare = document.getElementById('compareReaderBtn');
    if (compare) {
      const doc = currentDoc();
      compare.hidden = !state.comparing && !(doc?.has_final && doc?.has_raw);
      compare.disabled = !state.comparing && (!ready || compare.hidden);
      compare.textContent = I18n.t(state.comparing ? '退出对照' : '对照改动');
      compare.setAttribute('aria-pressed', String(state.comparing));
      compare.title = doc?.has_final && doc?.has_raw ? '对照原稿与整理稿' : '需要同时存在原稿和整理稿';
    }
  }

  function copyReaderText() {
    if (state.translationView !== "off") {
      if (state.translationData?.text) copyText(state.translationData.text, "译文已复制");
      return;
    }
    if (!state.readerText) return;
    copyText(state.readerText, state.readerTruncated ? "已复制可见部分" : "Markdown 已复制");
  }

  function beginEditing() {
    if (!state.readerReady || !state.selectedId || deleting.has(state.selectedId) || state.translationView !== 'off' || state.readerTruncated || state.comparing) return;
    saveReadingPosition();
    renderedReading = null;
    const body = document.getElementById("readerBody");
    if (!body) return;
    state.editing = true;
    replaceReaderContent('<section class="reader-edit-feedback" hidden><p role="status"></p><div></div></section>');
    const editor = document.createElement("textarea");
    editor.className = "reader-editor";
    editor.value = state.readerText;
    editor.setAttribute("aria-label", "Markdown 编辑器");
    editor.oninput = () => updateReaderActions();
    body.appendChild(editor);
    updateReaderActions();
    editor.focus();
  }

  async function saveEditing() {
    const editor = document.querySelector('#readerBody .reader-editor');
    if (!editor || !state.selectedId || !state.readerReady) return;
    const id = state.selectedId, tab = state.tab, text = editor.value, revision = state.readerRevision, key = id + '@' + tab;
    if (pendingSaves.has(key) || editor.dataset.needsLatest === 'true') return;
    const current = () => AppState.currentView === 'library' && state.selectedId === id && state.tab === tab
      && state.editing && document.querySelector('#readerBody .reader-editor') === editor;
    pendingSaves.add(key); editor.readOnly = true; updateReaderActions();
    try {
      let result;
      try { result = await AppApi.updateReaderText(id, tab, text, revision); }
      catch (_) {
        const latest = await AppApi.reader(id, tab).catch(() => null);
        if (latest?.ok && !latest.truncated && latest.text === text) result = {ok: true};
        else if (latest?.ok && latest.revision === revision) result = {ok: false, error: '修改尚未写入，可以重试'};
        else result = {ok: false, code: 'revision_unknown', error: '保存结果待确认，请查看当前稿件'};
      }
      if (!result?.ok) {
        const review = ['revision_conflict', 'revision_unknown'].includes(result?.code);
        const message = result?.error || '保存失败';
        if (current()) showEditFeedback(editor, message, review);
        toast(message, 'error');
        return;
      }
      if (current()) {
        state.readerText = text; state.editing = false;
        await loadReader(id, false);
        if (state.selectedId === id && state.tab === tab && !state.editing) document.getElementById('editReaderBtn')?.focus({preventScroll:true});
      }
      toast('修改已保存');
    } finally {
      pendingSaves.delete(key);
      editor.readOnly = false;
      if (AppState.currentView === 'library') updateReaderActions();
    }
  }

  function showEditFeedback(editor, message, review = false) {
    const box = editor.previousElementSibling;
    box.hidden = false;
    box.querySelector('p').textContent = I18n.t(message);
    const actions = box.querySelector('div');
    actions.replaceChildren();
    editor.dataset.needsLatest = String(review);
    if (!review) return;
    const id = state.selectedId, tab = state.tab;
    const add = (label, run) => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'btn ghost small';
      button.textContent = I18n.t(label); button.onclick = run;
      actions.append(button);
    };
    add('复制草稿', () => copyText(editor.value));
    add('查看最新稿', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      const latest = await AppApi.reader(id, tab).catch(() => null);
      button.disabled = false;
      if (!editor.isConnected || !state.editing || AppState.currentView !== 'library') return;
      if (!latest?.ok || !latest.revision || latest.truncated) {
        box.querySelector('p').textContent = I18n.t('暂时无法读取完整的最新稿，草稿仍在');
        return;
      }
      const dialog = openModal(I18n.t('最新稿件'),
        '<textarea class="reader-latest-draft" rows="12" readonly aria-label="最新稿件"></textarea><p>当前草稿已保留。回到编辑器合并后再保存。</p>',
        '<button class="btn ghost" id="dlgCancel" type="button">关闭</button><button class="btn" id="mergeReaderDraft" type="button">返回编辑并合并</button>');
      dialog.querySelector('textarea').value = latest.text;
      dialog.querySelector('#dlgCancel').onclick = closeModal;
      dialog.querySelector('#mergeReaderDraft').onclick = () => {
        closeModal();
        state.readerRevision = latest.revision;
        state.readerText = latest.text;
        showEditFeedback(editor, '请合并所需改动后保存');
        updateReaderActions();
        editor.focus();
      };
    });
  }

  function cancelEditing() {
    if (!state.editing || !state.selectedId) return;
    discardBefore(() => { state.editing = false; loadReader(state.selectedId, false); });
  }

  async function performDelete(doc, action) {
    const id = doc.task_id;
    if (deleting.has(id)) return;
    if ((state.selectedId === id && state.editing) || pendingSaves.has(id + '@raw') || pendingSaves.has(id + '@final')) {
      toast('请先保存或取消当前文稿的修改', 'error');
      return;
    }
    deleting.add(id); ++docsRequest;
    try {
      const result = await action.remove(id);
      const removed = Boolean(result?.removed?.length);
      if (removed && state.selectedId === id) {
        ++readerRequest;
        const index = state.docs.findIndex(item => item.task_id === id);
        const neighbor = index < 0 ? null : state.docs[index + 1] || state.docs[index - 1];
        state.activeDoc = null; state.selectedId = null;
        if (neighbor) chooseDocument(neighbor);
        state.readerReady = false; state.editing = false;
      }
      await loadDocs(!removed);
      if (!result?.ok || !removed) toast(result?.error || '删除未完成，请检查文件是否被占用后重试', 'error');
      else toast(result.message || action.done);
      if (AppState.currentView === 'library' && document.activeElement === document.body) (document.querySelector('.doc.selected .doc-open') || document.getElementById('libTagFilter'))?.focus({preventScroll:true});
    } catch (_) { toast('删除未完成，请检查文件是否被占用后重试', 'error'); }
    finally { deleting.delete(id); }
  }

  function confirmDocumentDelete(doc, action) {
    if (deleting.has(doc.task_id)) return;
    if (state.selectedId === doc.task_id && state.editing) {
      toast('请先保存或取消当前文稿的修改', 'error');
      return;
    }
    ConfirmDelete.run({
      message: `「${doc.title}」\n${I18n.t(action.effects)}`,
      onConfirm: () => performDelete(doc, action),
    });
  }

  async function openReaderFile() {
    if (!state.readerReady || !state.selectedId) return;
    const {selectedId, tab} = state;
    const serial = readerRequest;
    const current = () => serial === readerRequest && state.readerReady && selectedId === state.selectedId
      && tab === state.tab && AppState.currentView === "library";
    const reportError = message => {
      if (current()) toast(message || "无法定位文稿文件", "error");
    };
    try {
      // Agent reorganization can replace a document's file while the reader stays open.
      const data = await AppApi.reader(selectedId, tab);
      if (!current()) return;
      if (!data?.ok || !data.path) { reportError(data?.error); return; }
      const result = await AppApi.openPath(data.path);
      if (!result?.ok) reportError(result?.error);
    } catch (_) {
      reportError();
    }
  }

  /* tick 轻刷新：仅重拉列表（后端带指纹），不打断阅读 */
  async function refreshLight() {
    if (AppState.currentView === 'library') await loadDocs(true);
  }

  function renderReaderHeader() {
    const heading = document.getElementById('readerHeading');
    if (!heading) return;
    const doc = currentDoc();
    heading.hidden = !doc;
    if (!doc) return;
    const title = heading.querySelector('.reader-doc-title');
    if (title.textContent !== doc.title) title.textContent = doc.title;
    const versionNote = document.getElementById('readerVersionNote');
    versionNote.hidden = doc.has_raw === doc.has_final;
    versionNote.textContent = I18n.t(doc.has_final ? '仅有整理稿' : '仅有原始转写');
    renderTagEditor();
    renderTranslationStatus();
  }

  function normalizeTitleText(value) {
    return String(value || '').replace(/[\s\p{P}\p{S}]+/gu, '').toLowerCase();
  }

  function replaceReaderContent(html) {
    const body = document.getElementById('readerBody');
    const heading = document.getElementById('readerHeading');
    while (heading.nextSibling) heading.nextSibling.remove();
    body.insertAdjacentHTML('beforeend', html);
    const title = heading.querySelector('.reader-doc-title'), first = heading.nextElementSibling;
    const wasFocused = document.activeElement === title;
    const docTitle = currentDoc()?.title?.trim() || '';
    const firstText = first?.tagName === 'H1' ? first.textContent.trim() : '';
    const exactMatch = Boolean(firstText && docTitle && firstText === docTitle);
    const normFirst = normalizeTitleText(firstText), normDoc = normalizeTitleText(docTitle);
    const prefixOrDuplicate = Boolean(
      firstText && normFirst && normDoc && (
        normFirst === normDoc
        || (normFirst.length >= 3 && normDoc.startsWith(normFirst))
        || (normDoc.length >= 3 && normFirst.startsWith(normDoc))
      )
    );
    if (exactMatch) {
      first.className = 'reader-doc-title';
      first.tabIndex = -1;
      first.dataset.i18nSkip = '';
      title.replaceWith(first);
    } else if (prefixOrDuplicate) {
      first.remove();
      if (title.tagName !== 'H1') {
        const nextTitle = document.createElement('h1');
        nextTitle.className = title.className;
        nextTitle.tabIndex = -1;
        nextTitle.dataset.i18nSkip = '';
        title.replaceWith(nextTitle);
      }
    } else if (title.tagName !== 'H2') {
      const nextTitle = document.createElement('h2');
      nextTitle.className = title.className;
      nextTitle.tabIndex = -1;
      nextTitle.dataset.i18nSkip = '';
      title.replaceWith(nextTitle);
    }
    renderReaderHeader();
    if (wasFocused) focusReaderHeading();
  }

  function focusReaderHeading() {
    document.querySelector('#readerHeading:not([hidden]) .reader-doc-title')?.focus({preventScroll:true});
  }

  function showReaderError(message, taskId) {
    replaceReaderContent('<div class="reader-empty reader-retry"><div class="big" role="alert"></div><button class="btn small" type="button">重新读取</button></div>');
    const retry = document.querySelector('#readerBody .reader-retry');
    retry.querySelector('.big').textContent = I18n.t(message);
    retry.querySelector('button').onclick = async () => { await loadReader(taskId, true); focusReaderHeading(); };
  }

  async function loadComparison() {
    saveReadingPosition(); renderedReading = null;
    const taskId = state.selectedId, serial = ++readerRequest;
    const body = document.getElementById('readerBody');
    state.comparing = true; state.editing = false;
    body.classList.add('is-comparing');
    replaceReaderContent('<div class="reader-empty" role="status"><div class="big">正在对照原稿与整理稿</div></div>');
    updateReaderActions(); renderTranslationTabs();
    try {
      const [raw, final] = await Promise.all([AppApi.reader(taskId, 'raw'), AppApi.reader(taskId, 'final')]);
      if (serial !== readerRequest || taskId !== state.selectedId || AppState.currentView !== 'library' || !state.comparing) return;
      if (!raw?.ok || !final?.ok) throw new Error(raw?.error || final?.error || '文稿暂不可用');
      const groups = TextDiff.compare(raw.text || '', final.text || '');
      replaceReaderContent('<div class="reader-diff"><div class="diff-summary" role="status"></div><div class="diff-controls"><button class="btn ghost small" id="diffPrevious" type="button">上一处</button><button class="btn ghost small" id="diffNext" type="button">下一处</button><button class="btn ghost small" id="diffShowContext" type="button" aria-pressed="false">显示相同段落</button></div><div class="diff-groups"></div><button class="btn ghost small diff-more" type="button" hidden>继续显示</button></div>');
      const box = body.querySelector('.diff-groups'), more = body.querySelector('.diff-more');
      const counts = {added: 0, removed: 0, changed: 0};
      groups.forEach(group => { if (group.kind !== 'same') ++counts[group.kind]; });
      const summary = body.querySelector('.diff-summary');
      const partial = raw.truncated || final.truncated;
      const changes = counts.added || counts.removed || counts.changed;
      summary.textContent = changes
        ? `新增 ${counts.added} 组 · 删减 ${counts.removed} 组 · 调整 ${counts.changed} 组`
        : partial ? '已读取的部分文稿内容一致' : '原稿与整理稿内容一致';
      if (partial && changes) summary.textContent += ' · 当前仅对照已读取的部分文稿';
      const ctx = box.parentElement.querySelector('#diffShowContext');
      const prev = box.parentElement.querySelector('#diffPrevious'), next = box.parentElement.querySelector('#diffNext');
      const changed = groups.filter(group => group.kind !== 'same');
      let visible = changed, rendered = 0, selected = -1;
      const updateNavigation = () => {
        prev.disabled = selected <= 0;
        next.disabled = selected >= changed.length - 1;
      };
      const appendGroups = () => {
        const chunk = visible.slice(rendered, rendered + 80);
        chunk.forEach(group => {
          const row = document.createElement('section'); row.className = 'diff-row diff-' + group.kind;
          const label = {same: '相同', added: '新增', removed: '删减', changed: '调整'}[group.kind];
          row.tabIndex = -1;
          row.setAttribute('aria-label', I18n.t(label));
          group.element = row;
          for (const side of group.kind === 'same' ? ['before'] : ['before', 'after']) {
            const cell = document.createElement('div'); cell.className = 'diff-cell';
            const heading = document.createElement('span'); heading.className = 'diff-cell-label';
            heading.textContent = group.kind === 'same' ? I18n.t('两版相同')
              : I18n.t(side === 'before' ? '原始转写' : '整理稿') + ' · ' + I18n.t(label);
            const text = document.createElement('div'); text.textContent = group[side].join('\n\n') || '—';
            cell.append(heading, text); row.append(cell);
          }
          box.append(row);
        });
        rendered += chunk.length;
        more.hidden = rendered >= visible.length;
        more.textContent = I18n.t('继续显示') + ' · ' + (visible.length - rendered);
      };
      const navigateChange = offset => {
        selected += offset;
        const group = changed[selected];
        while (rendered < visible.length && !group.element?.isConnected) appendGroups();
        updateNavigation();
        group.element.focus({preventScroll:true});
        group.element.scrollIntoView({block:'nearest'});
      };
      prev.onclick = () => navigateChange(-1);
      next.onclick = () => navigateChange(1);
      ctx.hidden = changed.length === groups.length;
      ctx.onclick = () => {
        visible = visible === changed ? groups : changed;
        ctx.setAttribute('aria-pressed', String(visible === groups));
        ctx.textContent = I18n.t(visible === groups ? '仅看改动' : '显示相同段落');
        box.replaceChildren(); rendered = 0; selected = -1;
        appendGroups(); updateNavigation();
      };
      more.onclick = appendGroups;
      appendGroups(); updateNavigation(); body.scrollTop = 0;
    } catch (error) {
      if (serial !== readerRequest || taskId !== state.selectedId || AppState.currentView !== 'library' || !state.comparing) return;
      replaceReaderContent('<div class="reader-empty reader-retry"><div class="big" role="alert">对照读取失败</div><p></p><button class="btn small" type="button">重新读取</button></div>');
      const failure = body.querySelector('.reader-retry');
      failure.querySelector('p').textContent = I18n.t(error.message);
      failure.querySelector('button').onclick = loadComparison;
    }
  }

  function snapshot() {
    saveReadingPosition();
    reading.open = Boolean(document.getElementById('readerBody')?.clientHeight);
    writeReadingState();
    savedContext = {tagFilter: state.tagFilter, sortOrder: state.sortOrder,
      selectedId: state.selectedId, activeDoc: currentDoc(),
      listScroll: document.getElementById('docList')?.scrollTop || 0,
      reading: reading.open};
    return savedContext;
  }

  async function render(context = savedContext) {
    if (state.editing && document.querySelector('#readerBody .reader-editor')) { await refreshLight(); return; }
    renderedReading = null; state.comparing = false;
    if (context?.taskIds) {
      targetIds = context.taskIds; state.tagFilter = '';
    } else if (context) {
      targetIds = null; state.tagFilter = context.tagFilter || '';
      state.selectedId = context.selectedId;
      state.activeDoc = context.activeDoc || null;
      state.sortOrder = context.sortOrder || '';
    }
    state.readerReady = false;
    buildLayout(); updateReaderActions();
    if (context?.taskIds || context?.reading || (!context && reading.selectedId && reading.open !== false)) {
      document.querySelector('#page-library .lib').classList.add('reading');
    }
    if (!pageObserver) {
      const host = document.getElementById('page-library');
      pageObserver = new MutationObserver(() => {
        if (!host.classList.contains('active')) {
          ++docsRequest; ++readerRequest;
          state.listLoading = false;
          closeContextMenu();
        }
      });
      pageObserver.observe(host,{attributes:true,attributeFilter:['class']});
    }
    await loadDocs();
    if (context?.taskIds || (context?.reading && state.selectedId)) document.querySelector('#page-library .lib')?.classList.add('reading');
    if (context?.listScroll) document.getElementById('docList').scrollTop = context.listScroll;
  }

  return { render, refreshLight, canLeave, snapshot, currentTaskId: () => state.selectedId };
})();
