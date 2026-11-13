// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 文本库页：文档列表（可缩放宽度、右键删除）+ Markdown 阅读器 */
'use strict';

window.Library = (() => {
  let docsRequest = 0, readerRequest = 0, tagRequest = 0, pageObserver = null;
  const pendingSaves = new Set(), deleting = new Set();
  let menuEvents = null;
  let state = {
    query: "",
    tagFilter: "",
    selectedId: null,
    tab: "final",
    docs: [],
    readerText: "",
    readerRevision: null,
    readerTruncated: false,
    editing: false,
    readerReady: false,
    // TASK-8.24 翻译视图：off=原文；replace=原文页内替换；segments=逐段对照；
    // page=整页译文。数据懒取自 get_reader(tab=translation)。
    translationView: "off",
    translationData: null,
    translationFetching: false,
  };
  let translationTimer = null, translationTarget = "", translationViewTask = "";

  const ICONS = {
    copy: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    edit: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/></svg>`,
    save: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>`,
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
    ++docsRequest; ++readerRequest; closeContextMenu();
    return true;
  }

  function sortedDocs(docs) {
    const order = (AppState.settings || {}).task_sort_order || "newest";
    return [...docs].sort((a, b) => {
      const ta = String(a.updated_at || "");
      const tb = String(b.updated_at || "");
      if (!ta && !tb) return 0;
      if (!ta) return 1;
      if (!tb) return -1;
      return order === "oldest" ? ta.localeCompare(tb) : tb.localeCompare(ta);
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function applyTagFilter(docs, filter = state.tagFilter) {
    if (!filter) return docs;
    return docs.filter((d) => Array.isArray(d.tags) && d.tags.includes(filter));
  }

  async function loadTagIndex() {
    const sel = document.getElementById("libTagFilter");
    if (!sel) return;
    let items = [];
    try { items = (await AppApi.tagIndex()) || []; } catch (e) { return; }
    if (!sel.isConnected) return;
    const current = state.tagFilter;
    sel.innerHTML = `<option value="">全部标签</option>` + items
      .map((i) => `<option value="${esc(i.tag)}">${esc(i.tag)} (${i.count})</option>`)
      .join("");
    sel.value = current;
  }

  function renderDocTags(host, doc) {
    host.innerHTML = "";
    (doc.tags || []).forEach((t) => {
      const s = document.createElement("span");
      s.className = "tag";
      s.textContent = t;
      host.appendChild(s);
    });
  }

  function renderDocList() {
    const list = document.getElementById("docList");
    if (!list) return;
    const focused = document.activeElement, oldRow = focused?.closest?.('.doc');
    const focusedId = oldRow?.dataset.docId, focusDelete = focused?.classList.contains('job-delete');
    const scroll = list.scrollTop;
    list.innerHTML = "";
    if (!state.docs.length) {
      const searching = Boolean(state.query || state.tagFilter);
      list.innerHTML = `<div class="empty"><span class="empty-symbol" aria-hidden="true">${AppIcons('tasks')}</span><div class="big">${searching ? '没有找到文本' : '文字会在这里留下'}</div>${searching ? '<button class="btn" type="button" id="clearEmptyFilter">查看全部文本</button>' : ''}</div>`;
      list.querySelector('#clearEmptyFilter')?.addEventListener('click', clearFilters);
      return;
    }
    state.docs.forEach((doc) => {
      const el = document.createElement("div");
      el.className = "doc" + (doc.task_id === state.selectedId ? " selected" : "");
      el.setAttribute("role", "listitem");
      el.dataset.docId = doc.task_id;
      el.innerHTML = `<button class="doc-open" type="button" aria-current="${doc.task_id === state.selectedId}"><span class="doc-title"></span><span class="doc-tags"></span></button>`;
      el.querySelector(".doc-title").textContent = doc.title;
      el.querySelector(".doc-title").title = doc.title;
      renderDocTags(el.querySelector(".doc-tags"), doc);
      bindDocRow(el, doc);
      list.appendChild(el);
    });
    list.scrollTop = scroll;
    if (focusedId) {
      const row = [...list.querySelectorAll('.doc')].find(el => el.dataset.docId === focusedId) || list.querySelector('.doc');
      row?.querySelector(focusDelete ? '.job-delete' : '.doc-open')?.focus({preventScroll:true});
    }
  }

  function clearFilters() {
    discardBefore(() => {
      state.query = ''; state.tagFilter = '';
      const query = document.getElementById('libSearch'), tags = document.getElementById('libTagFilter');
      if (query) query.value = ''; if (tags) tags.value = '';
      loadDocs();
    });
  }

  function renderTagEditor() {
    const host = document.getElementById("readerTags");
    if (!host) return;
    const doc = state.docs.find((d) => d.task_id === state.selectedId);
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
      <details class="tag-details"><summary>标签${tags.length ? '（' + tags.length + '）' : ''}</summary><div class="tag-row">
        <div class="tag-pills"></div>
        <input id="tagInput" class="tag-input" aria-label="添加标签" placeholder="添加标签，回车确认" spellcheck="false">
      </div></details>`;
    const pills = host.querySelector(".tag-pills");
    const input = host.querySelector("#tagInput");
    const renderPills = () => {
      pills.innerHTML = "";
      tags.forEach((t) => {
        const p = document.createElement("span");
        p.className = "tag tag-editable";
        const txt = document.createElement("span");
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
        doc.tags = next;
        if (input.value === typed) input.value = '';
        loadTagIndex();
        if (serial === tagRequest && state.selectedId === doc.task_id && AppState.currentView === 'library') {
          renderDocList(); renderTagEditor();
          await loadDocs(true);
          if (state.selectedId === doc.task_id && document.activeElement === document.body) document.getElementById('tagInput')?.focus({preventScroll:true});
        }
      } catch (failure) { toast(failure.message || '保存标签失败', 'error'); }
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
    if (wasFocused) input.focus({preventScroll:true});
  }

  function buildLayout() {
    const host = document.getElementById("page-library");
    host.innerHTML = `
      <div class="lib">
        <div class="lib-list" id="libList">
          <div class="lib-tools">
            <div class="search-box"><span class="k">⌕</span><input id="libSearch" aria-label="搜索文本" placeholder="搜索文本" spellcheck="false"></div>
            <select id="libTagFilter" class="lib-tag-filter" title="按标签筛选" aria-label="按标签筛选"><option value="">全部标签</option></select>
            <button id="libResetFilters" class="icon-btn" type="button" aria-label="清空文本筛选" title="清空文本筛选" hidden>${ICONS.close}</button>
          </div>
          <div class="doc-list" id="docList" role="list" aria-label="文本列表"></div>
        </div>
        <div class="lib-resizer" id="libResizer" title="拖动调整宽度"></div>
        <div class="reader">
          <div class="reader-toolbar">
            <button class="icon-btn reader-back" id="readerBack" title="返回文本列表" aria-label="返回文本列表">${AppIcons('back')}</button>
            <div class="reader-tabs" id="readerTabs" role="group" aria-label="文本版本"></div>
            <div class="reader-trans-tabs" id="readerTransTabs" role="group" aria-label="译文视图" hidden></div>
            <div class="reader-actions">
              <button class="icon-btn" id="copyReaderBtn" title="复制 Markdown" aria-label="复制 Markdown" disabled>${ICONS.copy}</button>
              <button class="icon-btn" id="editReaderBtn" title="编辑 Markdown" aria-label="编辑 Markdown" disabled>${ICONS.edit}</button>
              <button class="icon-btn is-save" id="saveReaderBtn" title="保存修改" aria-label="保存修改" hidden>${ICONS.save}</button>
              <button class="icon-btn is-cancel" id="cancelReaderBtn" title="放弃修改" aria-label="放弃修改" hidden>${ICONS.close}</button>
              <button class="icon-btn" id="subtitleBtn" title="导出字幕" aria-label="导出字幕">${ICONS.subtitles}</button>
              <button class="icon-btn" id="openBtn" title="打开文件位置" aria-label="打开文件位置">${ICONS.folder}</button>
              <button class="icon-btn danger" id="deleteBtn" title="删除文本" aria-label="删除文本">${AppIcons('trash')}</button>
            </div>
          </div>
          <div class="reader-tags" id="readerTags"></div>
          <div class="reader-body" id="readerBody">
            <div class="reader-empty"><div class="big">选择一篇文本</div></div>
          </div>
        </div>
      </div>`;
    document.getElementById("libSearch").oninput = (e) => {
      const query = e.target.value.trim();
      if (hasDraft()) e.target.value = state.query;
      discardBefore(() => { state.query = query; e.target.value = query; loadDocs(); });
    };
    document.getElementById("deleteBtn").onclick = deleteCurrent;
    document.getElementById('readerBack').onclick = () => host.querySelector('.lib').classList.remove('reading');
    document.getElementById("subtitleBtn").onclick = showSubtitleMenu;
    document.getElementById("openBtn").onclick = openReaderFile;
    document.getElementById("copyReaderBtn").onclick = copyReaderText;
    document.getElementById("editReaderBtn").onclick = beginEditing;
    document.getElementById("saveReaderBtn").onclick = saveEditing;
    document.getElementById("cancelReaderBtn").onclick = cancelEditing;
    // 先生 2026-10-19：正文选中后右键可复制——WebView2 默认右键菜单仅在
    // debug 构建 enabled，生产环境在此补齐；编辑态不弹菜单。
    // 先生 2026-10-25：无选中时也提供「复制视频地址」「复制全部 Markdown」。
    const readerBodyEl = document.getElementById("readerBody");
    if (readerBodyEl) {
      readerBodyEl.oncontextmenu = (e) => {
        if (e.target.closest(".reader-editor")) return;
        const sel = String(window.getSelection ? window.getSelection() : "");
        if (!state.readerReady || state.editing) return;
        e.preventDefault();
        showReaderCopyMenu(e.clientX, e.clientY, sel.trim() ? sel : "");
      };
    }
    document.getElementById("libTagFilter").onchange = (e) => {
      const filter = e.target.value;
      if (hasDraft()) e.target.value = state.tagFilter;
      discardBefore(() => { state.tagFilter = filter; e.target.value = filter; loadDocs(); });
    };
    document.getElementById("libSearch").value = state.query;
    document.getElementById('libResetFilters').onclick = clearFilters;
    loadTagIndex();
    initResizer();
  }

  function showSubtitleMenu() {
    if (!state.selectedId) return;
    const btn = document.getElementById("subtitleBtn");
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const menu = document.createElement("div");
    closeContextMenu();
    menu.className = "ctx-menu";
    menu.innerHTML = `
      <div class="menu-list">
        <button class="ctx-item menu-item" type="button" data-fmt="srt"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>导出 SRT（通用字幕）</button>
        <button class="ctx-item menu-item" type="button" data-fmt="vtt"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/></svg>导出 VTT（Web 字幕）</button>
      </div>`;
    document.body.appendChild(menu);
    menu.style.left = Math.min(Math.max(rect.right - menu.offsetWidth, 8), window.innerWidth - menu.offsetWidth - 8) + "px";
    menu.style.top = Math.min(Math.max(rect.bottom + 4, 8), window.innerHeight - menu.offsetHeight - 8) + "px";
    menu.querySelectorAll(".ctx-item").forEach((item) => {
      item.onclick = () => {
        closeContextMenu(true);
        exportSubtitle(item.dataset.fmt || "srt");
      };
    });
    bindMenuKeyboard(menu, btn);
  }

  async function exportSubtitle(fmt) {
    const id = state.selectedId;
    if (!id || !state.readerReady) return;
    const doc = state.docs.find(d => d.task_id === id);
    const base = String(doc?.title || '字幕').replace(/[\\/:*?"<>|]+/g, '_').slice(0,60);
    try {
      const picked = await AppApi.pickSaveFile(base + (fmt === 'vtt' ? '.vtt' : '.srt'));
      if (!picked?.ok || !picked.path) return;
      const result = await AppApi.exportSubtitles(id, fmt, picked.path);
      if (!result?.ok) { toast(result?.error || '导出失败', 'error'); return; }
      toast(`已导出 ${result.count} 条字幕`);
      if (state.selectedId === id && AppState.currentView === 'library') {
        const mask = openModal('字幕已导出', '<p>字幕文件已保存到选择的位置。</p>', '<button class="btn ghost" id="exportDone" type="button">完成</button><button class="btn" id="exportOpen" type="button">打开文件</button>');
        mask.querySelector('#exportDone').onclick = closeModal;
        mask.querySelector('#exportOpen').onclick = () => { closeModal(); AppApi.openPath(result.path || picked.path).catch(() => toast('无法打开字幕文件', 'error')); };
      }
    } catch (_) { toast('导出失败，请重试', 'error'); }
  }

  function initResizer() {
    const list = document.getElementById("libList");
    const rz = document.getElementById("libResizer");
    if (!list || !rz) return;
    try {
      const saved = parseInt(localStorage.getItem("shengjian.libWidth"), 10);
      if (saved && saved >= 220) list.style.width = saved + "px";
    } catch (e) { /* ignore */ }
    rz.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = list.getBoundingClientRect().width;
      const onMove = (ev) => {
        const w = Math.min(Math.max(startW + (ev.clientX - startX), 220), window.innerWidth * 0.55);
        list.style.width = w + "px";
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        try { localStorage.setItem("shengjian.libWidth", String(Math.round(list.getBoundingClientRect().width))); } catch (e) { /* ignore */ }
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  /* 文本库右键小菜单：只提供“删除”入口，点击后再走 ConfirmDelete（受设置控制） */
  function closeContextMenu(restoreFocus) {
    const old = document.querySelector(".ctx-menu");
    if (old) {
      menuEvents?.abort(); menuEvents = null;
      old.remove();
      if (restoreFocus === true && old._returnFocus?.isConnected) old._returnFocus.focus();
    }
  }

  function bindMenuKeyboard(menu, returnFocus) {
    menu.setAttribute("role", "menu");
    const items = [...menu.querySelectorAll(".ctx-item")];
    items.forEach(item => item.setAttribute("role", "menuitem"));
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
    document.addEventListener('pointerdown', ev => { if (!menu.contains(ev.target)) closeContextMenu(); }, {capture:true,signal});
    window.addEventListener('resize', () => closeContextMenu(), {signal});
    document.addEventListener('scroll', ev => { if (!menu.contains(ev.target)) closeContextMenu(); }, {capture:true,signal});
    menu._returnFocus = returnFocus;
    items[0]?.focus();
  }

  function showContextMenu(doc, x, y) {
    closeContextMenu();
    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    // 先生 2026-10-19：单篇右键补充「复制 Markdown 地址」「打开视频」——
    // 各自仅在对应内容存在时出现；删除组语义保持原样。
    // 先生 2026-10-25：再补「复制视频地址」「重新转写…」「翻译…」。
    const utility = [];
    if (doc.has_final || doc.has_raw) {
      utility.push(`<button class="ctx-item menu-item" type="button" data-act="copy-md"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>复制 Markdown 地址</button>`);
    }
    if (doc.source_path) {
      utility.push(`<button class="ctx-item menu-item" type="button" data-act="copy-video-path"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/></svg>复制视频地址</button>`);
      utility.push(`<button class="ctx-item menu-item" type="button" data-act="open-video"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m10 8 6 4-6 4Z"/><rect x="2" y="4" width="20" height="16" rx="2"/></svg>打开视频</button>`);
    }
    if (doc.has_final || doc.has_raw) {
      utility.push(`<button class="ctx-item menu-item" type="button" data-act="retranscribe"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>重新转写…</button>`);
      utility.push(`<button class="ctx-item menu-item" type="button" data-act="translate"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/></svg>翻译…</button>`);
    }
    const utilityHtml = utility.length ? utility.join("") + `<div class="menu-sep"></div>` : "";
    menu.innerHTML = `
      <div class="menu-list">
        ${utilityHtml}
        <button class="ctx-item menu-item" type="button" data-act="delete-text"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>删除文本</button>
        <button class="ctx-item menu-item" type="button" data-act="delete-files"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>删除文件</button>
        <div class="menu-sep"></div>
        <button class="ctx-item menu-item danger" type="button" data-act="delete-all"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>删除视频和文件</button>
      </div>`;
    document.body.appendChild(menu);
    const left = Math.min(Math.max(x, 8), window.innerWidth - menu.offsetWidth - 8);
    const top = Math.min(Math.max(y, 8), window.innerHeight - menu.offsetHeight - 8);
    menu.style.left = left + "px";
    menu.style.top = top + "px";
    // 即时动作（先生 2026-10-19）：不走 ConfirmDelete。
    const UTILITY = {
      "copy-md": () => {
        AppApi.reader(doc.task_id, doc.has_final ? "final" : "raw").then((d) => {
          if (d && d.ok && d.path) copyText(d.path, "Markdown 地址已复制");
          else toast("未找到 Markdown 文件", "error");
        });
      },
      "copy-video-path": () => {
        if (doc.source_path) copyText(doc.source_path, "视频地址已复制");
        else toast("该文本没有视频文件地址", "error");
      },
      "open-video": () => {
        AppApi.playMedia(doc).then((res) => {
          if (!res || res.ok === false) toast((res && res.error) || "视频文件无法打开", "error");
        });
      },
      "retranscribe": () => showRetranscribeDialog(doc),
      "translate": () => showTranslateDialog(doc),
    };
    const ACTIONS = {
      "delete-text": {
        message: `删除「${doc.title}」的文本？文本记录与转写产物将移除，视频文件保留，可随时重新转写。`,
        api: (id) => AppApi.deleteTexts([id]),
        done: "文本已删除，视频已保留",
        clearSelected: true,
      },
      "delete-files": {
        message: `删除「${doc.title}」的文件？转写产物与视频文件将移除，任务记录保留。`,
        api: (id) => AppApi.deleteFiles([id]),
        done: "文件已删除，记录已保留",
        clearSelected: false,
      },
      "delete-all": {
        message: `彻底删除「${doc.title}」？任务记录、转写产物与视频文件将一并移除，删除后不可恢复。`,
        api: (id) => AppApi.deleteTasks([id]),
        done: "已彻底删除",
        clearSelected: true,
      },
    };
    menu.querySelectorAll(".ctx-item").forEach((item) => {
      item.onclick = (e) => {
        e.stopPropagation();
        closeContextMenu(true);
        if (UTILITY[item.dataset.act]) { UTILITY[item.dataset.act](); return; }
        const act = ACTIONS[item.dataset.act];
        if (!act) return;
        ConfirmDelete.run({
          message: act.message,
          onConfirm: () => performDelete(doc, act.api, act.done, act.clearSelected),
        });
      };
    });
    bindMenuKeyboard(menu, document.activeElement);

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
    items.push(`<button class="ctx-item menu-item" type="button" data-act="copy-all"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13"/><path d="M16 12h5"/><path d="M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></svg>复制全部 Markdown</button>`);
    if (doc && doc.source_path) {
      items.push(`<button class="ctx-item menu-item" type="button" data-act="copy-video-path"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/></svg>复制视频地址</button>`);
    }
    menu.innerHTML = `<div class="menu-list">${items.join("")}</div>`;
    document.body.appendChild(menu);
    const left = Math.min(Math.max(x, 8), window.innerWidth - menu.offsetWidth - 8);
    const top = Math.min(Math.max(y, 8), window.innerHeight - menu.offsetHeight - 8);
    menu.style.left = left + "px";
    menu.style.top = top + "px";
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
    el.onclick = () => {
      document.querySelector('#page-library .lib')?.classList.add('reading');
      if (state.selectedId === doc.task_id && (state.editing || state.readerReady)) return;
      discardBefore(() => {
        state.selectedId = doc.task_id;
        state.tab = doc.has_final ? "final" : doc.has_raw ? "raw" : "source";
        renderDocs(); loadReader(doc.task_id, true);
      });
    };
    el.onkeydown = e => {
      if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
        e.preventDefault(); e.stopPropagation();
        const r = el.getBoundingClientRect(); showContextMenu(doc, r.left + 20, r.bottom);
      }
    };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      showContextMenu(doc, e.clientX, e.clientY);
    };
    const del = document.createElement("button");
    del.className = "job-delete";
      del.setAttribute("aria-label", "删除文本 " + doc.title);
    del.title = "删除";
    del.innerHTML = `<span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></span><span class="lbl">删除</span>`;
    del.onclick = (e) => {
      e.stopPropagation();
      deleteDoc(doc);
    };
    el.appendChild(del);
  }

  function renderTabs(task) {
    const wrap = document.getElementById("readerTabs");
    if (!wrap) return;
    wrap.innerHTML = "";
    const tabs = [
      { key: "final", label: "整理稿", enabled: !!(task && task.has_final) },
      { key: "raw", label: "原稿", enabled: !!(task && task.has_raw) },
      { key: "source", label: "源文件", enabled: !!(task && task.source_path) },
    ];
    tabs.forEach((t) => {
      const el = document.createElement("button");
      el.type = "button";
      el.disabled = !t.enabled;
      el.setAttribute("aria-pressed", String(t.key === state.tab));
      el.className = "seg" + (t.key === state.tab ? " active" : "") + (t.enabled ? "" : " disabled");
      el.textContent = t.label;
      el.onclick = () => {
        if (!t.enabled) return;
        if (state.tab === t.key && state.editing) return;
        discardBefore(() => { state.tab = t.key; loadReader(state.selectedId, true); });
      };
      wrap.appendChild(el);
    });
  }

  async function loadDocs(light = false) {
    const list = document.getElementById("docList");
    if (!list) return;
    const serial = ++docsRequest, query = state.query, filter = state.tagFilter;
    const reset = document.getElementById('libResetFilters'); if (reset) reset.hidden = !query && !filter;
    let docs;
    try {
      docs = await AppApi.documents(query, "全部");
      if (!Array.isArray(docs)) throw new Error('Invalid document list');
    } catch (_) {
      if (serial === docsRequest && AppState.currentView === 'library') {
        toast('读取文本列表失败，请重试', 'error');
        if (!state.docs.length) list.innerHTML = '<div class="empty" role="alert"><div class="big">读取文本失败</div><button class="btn small" type="button">重新读取</button></div>';
        list.querySelector('.empty button')?.addEventListener('click', () => loadDocs());
      }
      return;
    }
    if (serial !== docsRequest || AppState.currentView !== 'library' || !list.isConnected) return;
    docs = sortedDocs(applyTagFilter(docs, filter));
    const fingerprint = arr => JSON.stringify(arr.map(d => [d.task_id,d.title,d.status,d.updated_at,d.tags,d.has_final,d.has_raw,d.source_path,d.has_translation,d.translation_lang]));
    if (light && fingerprint(docs) === fingerprint(state.docs)) return;
    state.docs = docs;
    // 文档指纹更新（如翻译完成后 has_translation 置位）时同步译文控制条。
    renderTranslationTabs();
    const present = docs.some(d => d.task_id === state.selectedId);
    if (state.editing && !present) { renderDocList(); return; } // Keep draft if removed by an external refresh.
    if (!docs.length) {
      state.selectedId = null; state.readerReady = false; state.editing = false;
      state.readerText = ''; ++readerRequest;
      renderDocList(); renderTabs(null); renderTagEditor(); updateReaderActions();
      document.getElementById('readerBody').innerHTML = '<div class="reader-empty"><div class="big">选择一篇文本</div></div>';
      document.querySelector('#page-library .lib')?.classList.remove('reading');
      return;
    }
    const needReader = !present || !state.readerReady;
    if (!present) {
      state.selectedId = docs[0].task_id;
      state.tab = docs[0].has_final ? 'final' : docs[0].has_raw ? 'raw' : 'source';
    }
    renderDocList();
    if (needReader && !state.editing) loadReader(state.selectedId, true);
    else if (!state.editing) renderTagEditor();
  }

  /* ------------------------------------------------------------ 译文视图（TASK-8.24）
     三种阅读方式（先生 2026-10-25）：replace=原文页内替换；segments=逐段对照；
     page=整页译文。控制条只在当前文本已有译文时出现。 */
  const TRANSLATION_VIEWS = [
    { key: "replace", label: "替换原文" },
    { key: "segments", label: "逐段对照" },
    { key: "page", label: "整页译文" },
  ];

  function currentDoc() {
    return state.docs.find((d) => d.task_id === state.selectedId) || null;
  }

  function stopTranslationPoll() {
    if (translationTimer) { clearInterval(translationTimer); translationTimer = null; }
    translationTarget = "";
  }

  function renderTranslationTabs() {
    const host = document.getElementById("readerTransTabs");
    if (!host) return;
    const doc = currentDoc();
    const available = !!doc?.has_translation && state.tab !== "source" && state.readerReady;
    host.hidden = !available;
    if (!available) { host.innerHTML = ""; return; }
    host.innerHTML = "";
    const views = [{ key: "off", label: "原文" }, ...TRANSLATION_VIEWS];
    views.forEach((view) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "seg" + (state.translationView === view.key ? " active" : "");
      el.setAttribute("aria-pressed", String(state.translationView === view.key));
      el.textContent = view.label;
      el.onclick = () => { discardBefore(() => setTranslationView(view.key)); };
      host.appendChild(el);
    });
  }

  async function ensureTranslationData() {
    const doc = currentDoc();
    if (!doc || !doc.has_translation) return false;
    if (state.translationData && state.translationData.task_id === state.selectedId) return true;
    if (state.translationFetching) return false;
    state.translationFetching = true;
    try {
      const data = await AppApi.reader(state.selectedId, "translation");
      if (!data || !data.ok) { toast((data && data.error) || "译文不可用", "error"); return false; }
      if (state.selectedId !== doc.task_id) return false;
      state.translationData = {
        task_id: doc.task_id,
        text: String(data.text || ""),
        segments: Array.isArray(data.segments) ? data.segments : [],
        lang: String(data.lang || ""),
        direction: String(data.direction || ""),
      };
      return true;
    } catch (_) { toast("译文读取失败", "error"); return false; }
    finally { state.translationFetching = false; }
  }

  function renderTranslationBody() {
    const body = document.getElementById("readerBody");
    const data = state.translationData;
    if (!body || !data) return;
    if (state.translationView === "segments") {
      const pairs = data.segments.length ? data.segments : [{ src: data.text, dst: data.text }];
      const box = document.createElement("div");
      box.className = "translation-segments";
      pairs.forEach((pair) => {
        const item = document.createElement("div");
        item.className = "seg-pair";
        const src = document.createElement("div");
        src.className = "seg-src";
        src.innerHTML = Markdown.render(pair.src || "");
        const dst = document.createElement("div");
        dst.className = "seg-dst";
        dst.innerHTML = Markdown.render(pair.dst || "");
        item.append(src, dst);
        box.appendChild(item);
      });
      body.innerHTML = "";
      body.appendChild(box);
      return;
    }
    // replace 与 page 同渲染整篇译文；page 顶部带方向徽标，replace 保持正文原样。
    if (state.translationView === "page") {
      const badge = document.createElement("div");
      badge.className = "translation-badge";
      const dirLabel = data.direction === "zh-en" ? "中 → 英"
        : data.direction === "en-zh" ? "英 → 中" : (data.lang === "en" ? "中 → 英" : "英 → 中");
      badge.textContent = `译文（${dirLabel}）`;
      body.innerHTML = "";
      body.appendChild(badge);
      const article = document.createElement("div");
      article.innerHTML = Markdown.render(data.text);
      body.appendChild(article);
    } else {
      body.innerHTML = Markdown.render(data.text);
    }
  }

  async function setTranslationView(view) {
    if (view === state.translationView) return;
    if (view === "off") {
      state.translationView = "off";
      renderTranslationTabs(); updateReaderActions();
      loadReader(state.selectedId, false);
      return;
    }
    if (!(await ensureTranslationData())) return;
    state.translationView = view;
    translationViewTask = state.selectedId;
    state.editing = false;
    renderTranslationTabs(); updateReaderActions();
    renderTranslationBody();
  }

  function startTranslationPoll(taskId, desiredView) {
    stopTranslationPoll();
    translationTarget = taskId;
    let ticks = 0;
    translationTimer = setInterval(async () => {
      ticks += 1;
      if (ticks > 300) { stopTranslationPoll(); toast("翻译仍在进行，稍后请在文本库刷新查看", "error"); return; }
      try {
        const status = await AppApi.translationStatus(taskId);
        if (translationTarget !== taskId) { stopTranslationPoll(); return; }
        if (status?.status === "failed") {
          stopTranslationPoll();
          toast(status?.error || "翻译失败", "error");
          return;
        }
        if (status?.status === "done" && !status?.running) {
          stopTranslationPoll();
          toast("翻译完成");
          state.translationData = null; // 强制重取
          await loadDocs(true);
          if (state.selectedId === taskId) {
            await setTranslationView(desiredView);
          }
        }
      } catch (_) { /* 桌面桥抖动：下个轮询再试 */ }
    }, 2000);
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
       <div class="modal-field"><label for="translateDisplay">阅读方式</label>
        <select id="translateDisplay" class="ctrl" style="width:100%">
          <option value="replace">原文替换（译文直接替换正文显示）</option>
          <option value="segments">逐段对照（每段原文下附译文）</option>
          <option value="page">整页翻译（完整译文单独成页）</option>
        </select></div>
       <p class="modal-note">${billing ? "托管模式：翻译按 AI 积分计费（与整理同口径）。" : "BYOK 模式：使用自配 API Key 翻译。"}已有译文会被新翻译覆盖。</p>`,
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
        toast("翻译已开始，完成后自动展示");
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
    const requestedTab = state.tab, serial = ++readerRequest;
    state.editing = false; state.readerReady = false;
    state.readerText = "";
    state.readerRevision = null;
    state.readerTruncated = false;
    state.translationData = null;
    if (taskId !== translationViewTask || state.tab === "source") state.translationView = "off";
    stopTranslationPoll();
    updateReaderActions();
    renderTranslationTabs();
    renderTagEditor();
    if (rebuildTabs) renderTabs(state.docs.find(doc => doc.task_id === taskId));
    body.innerHTML = '<div class="reader-empty" role="status"><div class="big">正在读取文本</div></div>';
    try {
      const data = await AppApi.reader(taskId, requestedTab);
      if (serial !== readerRequest || taskId !== state.selectedId || requestedTab !== state.tab || AppState.currentView !== "library" || !body.isConnected) return;
      if (!data || !data.ok) {
        body.innerHTML = `<div class="reader-empty"><div class="big"></div></div>`;
        body.querySelector(".reader-empty .big").textContent = data && data.error ? data.error : "文件不可用";
        updateReaderActions();
        return;
      }
      state.tab = ['final','raw','source'].includes(data.tab) ? data.tab : requestedTab;
      state.readerReady = true;
      if (state.tab === "source") {
        body.innerHTML = `<div style="padding:20px 0"><h1 style="font-family:var(--font-ui);font-size:22px;margin-bottom:10px"></h1>
          <div style="margin-top:18px;display:flex;gap:8px"><button class="btn primary small" id="srcPlay">播放</button>
          <button class="btn ghost small" id="srcOpen">打开位置</button></div>
          <hr style="margin:22px 0">
          <div class="meta-line" style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted);padding:7px 0"><span>位置</span><span style="max-width:70%;word-break:break-all;text-align:right"></span></div></div>`;
        body.querySelector("h1").textContent = data.title;
        body.querySelector(".meta-line span:last-child").textContent = data.path;
        body.querySelector("#srcPlay").onclick = () => AppApi.playMedia({ kind: "transcription", source_path: data.path });
        body.querySelector("#srcOpen").onclick = () => AppApi.openPath(data.path);
      } else {
        state.readerText = data.text || "";
        state.readerRevision = typeof data.revision === "string" ? data.revision : null;
        state.readerTruncated = !!data.truncated;
        body.innerHTML = Markdown.render(data.text);
      }
      updateReaderActions(state.tab);
      renderTranslationTabs();
      if (state.translationView !== "off" && state.tab !== "source") {
        await setTranslationView(state.translationView);
      }
      if (rebuildTabs) {
        const doc = state.docs.find((d) => d.task_id === taskId);
        renderTabs(doc);
      }
    } catch (_) {
      if (serial !== readerRequest || taskId !== state.selectedId || requestedTab !== state.tab || AppState.currentView !== 'library' || !body.isConnected) return;
      state.readerReady = false;
      body.innerHTML = '<div class="reader-empty" role="alert"><div class="big">读取失败</div></div>';
      updateReaderActions();
    }
  }

  function updateReaderActions(tab) {
    const copy = document.getElementById("copyReaderBtn");
    const edit = document.getElementById("editReaderBtn");
    const save = document.getElementById("saveReaderBtn");
    const cancel = document.getElementById("cancelReaderBtn");
    const ready = state.readerReady && !!state.selectedId;
    const inTranslation = state.translationView !== "off";
    const editable = ready && (tab || state.tab) !== "source" && !state.readerTruncated && !inTranslation;
    for (const id of ['subtitleBtn','openBtn','deleteBtn']) {
      const button = document.getElementById(id); if (button) button.disabled = !ready;
    }
    if (copy) copy.disabled = !ready || state.editing;
    if (edit) edit.disabled = !editable || state.editing;
    if (save) { save.hidden = !state.editing; save.disabled = pendingSaves.has(state.selectedId + "@" + state.tab); }
    if (cancel) cancel.hidden = !state.editing;
  }

  function copyReaderText() {
    if (state.translationView !== "off") {
      if (state.translationData?.text) copyText(state.translationData.text, "译文已复制");
      return;
    }
    if (!state.readerText || state.readerTruncated) return;
    copyText(state.readerText, "Markdown 已复制");
  }

  function beginEditing() {
    if (!state.readerReady || !state.selectedId || state.tab === "source" || state.readerTruncated) return;
    const body = document.getElementById("readerBody");
    if (!body) return;
    state.editing = true;
    body.innerHTML = "";
    const editor = document.createElement("textarea");
    editor.className = "reader-editor";
    editor.value = state.readerText;
    editor.setAttribute("aria-label", "Markdown 编辑器");
    body.appendChild(editor);
    updateReaderActions();
    editor.focus();
  }

  async function saveEditing() {
    const editor = document.querySelector('#readerBody .reader-editor');
    if (!editor || !state.selectedId || !state.readerReady) return;
    const id = state.selectedId, tab = state.tab, text = editor.value, revision = state.readerRevision, key = id + '@' + tab;
    if (pendingSaves.has(key)) return;
    pendingSaves.add(key); editor.readOnly = true; updateReaderActions();
    try {
      const result = await AppApi.updateReaderText(id, tab, text, revision);
      if (!result?.ok) { toast(result?.error || '保存失败', 'error'); return; }
      const current = AppState.currentView === 'library' && state.selectedId === id && state.tab === tab
        && state.editing && document.querySelector('#readerBody .reader-editor') === editor;
      if (current) {
        state.readerText = text; state.editing = false;
        await loadReader(id, false);
        if (state.selectedId === id && state.tab === tab && !state.editing) document.getElementById('editReaderBtn')?.focus({preventScroll:true});
      }
      toast('修改已保存');
    } catch (_) { toast('保存失败', 'error'); }
    finally { pendingSaves.delete(key); editor.readOnly = false; updateReaderActions(); }
  }

  function cancelEditing() {
    if (!state.editing || !state.selectedId) return;
    discardBefore(() => { state.editing = false; loadReader(state.selectedId, false); });
  }

  async function performDelete(doc, api, done, clearSelected = true) {
    const id = doc.task_id;
    if (deleting.has(id)) return;
    deleting.add(id); ++docsRequest;
    try {
      const result = await api(id);
      const n = Array.isArray(result?.removed) ? result.removed.length : 0;
      if (!n) { toast(result?.error || '删除失败', 'error'); return; }
      ++docsRequest; ++readerRequest;
      if (state.selectedId === id) {
        state.readerReady = false; state.editing = false;
        if (clearSelected) state.selectedId = null;
      }
      await loadDocs(); toast(done);
      if (AppState.currentView === 'library' && document.activeElement === document.body) (document.querySelector('.doc.selected .doc-open') || document.getElementById('libSearch'))?.focus({preventScroll:true});
    } catch (_) { toast('删除失败，请重试', 'error'); }
    finally { deleting.delete(id); }
  }

  function deleteDoc(doc) {
    if (!doc || deleting.has(doc.task_id)) return;
    ConfirmDelete.run({
      message: `删除「${doc.title || '当前文本'}」的文本？转写文本将移除，原视频保留，可重新转写。`,
      onConfirm: () => performDelete(doc, id => AppApi.deleteTexts([id]), '文本已删除，视频已保留'),
    });
  }

  function deleteCurrent() {
    if (!state.selectedId) return;
    const doc = state.docs.find((d) => d.task_id === state.selectedId);
    deleteDoc(doc);
  }

  function openReaderFile() {
    if (!state.selectedId) return;
    AppApi.reader(state.selectedId, state.tab).then((d) => {
      if (d && d.ok) AppApi.openPath(d.path);
    });
  }

  /* tick 轻刷新：仅重拉列表（后端带指纹），不打断阅读 */
  async function refreshLight() {
    if (AppState.currentView === 'library') await loadDocs(true);
  }

  async function render() {
    if (state.editing && document.querySelector('#readerBody .reader-editor')) { await refreshLight(); return; }
    state.readerReady = false;
    buildLayout(); updateReaderActions();
    if (!pageObserver) {
      const host = document.getElementById('page-library');
      pageObserver = new MutationObserver(() => {
        if (!host.classList.contains('active')) { ++docsRequest; ++readerRequest; closeContextMenu(); stopTranslationPoll(); }
      });
      pageObserver.observe(host,{attributes:true,attributeFilter:['class']});
    }
    await loadDocs();
  }

  return { render, refreshLight, canLeave };
})();
