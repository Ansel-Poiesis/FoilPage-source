// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Downloaded media stays local; preview URLs are issued by the desktop bridge. */
'use strict';
window.Videos = (() => {
  let request = 0, playRequest = 0, lastTaskKey = "", lastItemsKey = "";
  let contextMenu = null, menuAnchor = null, menuEvents = null, pageObserver = null, activePlayer = null;
  let scanTimer = null, scanGeneration = 0;
  const deleting = new Set();
  let currentItems = new Map();
  const playIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7Z"/></svg>';
  function mediaUrl(value) {
    if (typeof value !== "string") return "";
    if (/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(value)) return value;
    try {
      const url = new URL(value);
      return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) && !url.username && !url.password ? url.href : "";
    } catch (_) { return ""; }
  }
  function duration(value) {
    const seconds = Math.floor(Number(value));
    if (!Number.isFinite(seconds) || seconds < 1) return "";
    return (seconds >= 3600 ? Math.floor(seconds / 3600) + ":" : "")
      + (seconds >= 3600 ? String(Math.floor(seconds / 60) % 60).padStart(2,"0") : Math.floor(seconds / 60))
      + ":" + String(seconds % 60).padStart(2,"0");
  }
  function setStatus(message, error) {
    const node = document.getElementById("videosStatus");
    if (node) { node.textContent = message; node.setAttribute("role",error ? "alert" : "status"); }
  }
  function stopScanPoll() { ++scanGeneration; clearTimeout(scanTimer); scanTimer = null; }
  function updateCancelButton(scan) {
    const button = document.getElementById('cancelVideoScan');
    if (!button) return;
    const running = scan?.running === true;
    const cancelling = running && scan?.state === 'cancelling';
    button.hidden = !running;
    button.disabled = cancelling;
    button.textContent = cancelling ? '正在取消…' : '取消扫描';
    button.setAttribute('aria-label', cancelling ? '正在取消视频扫描' : '取消视频扫描');
  }
  function scanStatus(scan) {
    if (!scan || scan.running !== true) return;
    const visited = Math.max(0, Number(scan.visited) || 0);
    updateCancelButton(scan);
    setStatus(scan.state === 'cancelling'
      ? '正在取消扫描（已读取 ' + visited + ' 个视频）'
      : '正在扫描（已读取 ' + visited + ' 个视频）');
    document.getElementById('videoGrid')?.setAttribute('aria-busy', 'true');
    if (!currentItems.size) {
      listState('正在读取视频');
      const detail = document.querySelector('#videosReadState p');
      if (detail) detail.textContent = '已读取 ' + visited + ' 个视频';
    }
  }
  function followScan(scan) {
    stopScanPoll();
    if (scan?.running !== true || typeof AppApi.videoScanStatus !== 'function') return;
    const generation = scanGeneration;
    function failed() {
      if (currentItems.size) {
        setStatus('扫描暂时中断，可重新刷新', true);
        document.getElementById('videoGrid')?.setAttribute('aria-busy', 'false');
      } else listState('扫描暂时中断', true);
    }
    async function poll() {
      if (generation !== scanGeneration || AppState.currentView !== 'videos') return;
      try {
        const result = await AppApi.videoScanStatus();
        if (generation !== scanGeneration || AppState.currentView !== 'videos') return;
        if (!result?.ok) { failed(); return; }
        if (result.scan?.running === true) {
          scanStatus(result.scan);
          scanTimer = setTimeout(poll, 1000);
        } else {
          updateCancelButton(result.scan);
          // Discovery completion may add records without changing task progress.
          // Fetch the catalog once; polling itself does not regenerate covers.
          await refresh(false);
          if (result.scan?.state === 'cancelled') {
            setStatus('扫描已取消，已登记的视频保留');
          }
        }
      } catch (_) {
        if (generation === scanGeneration && AppState.currentView === 'videos') failed();
      }
    }
    scanTimer = setTimeout(poll, 1000);
  }
  async function cancelScan() {
    const button = document.getElementById('cancelVideoScan');
    if (!button || button.disabled || typeof AppApi.cancelVideoScan !== 'function') return;
    button.disabled = true;
    button.textContent = '正在取消…';
    setStatus('正在停止扫描，已登记的视频会保留');
    try {
      const result = await AppApi.cancelVideoScan();
      if (!result?.ok) throw new Error(result?.error || result?.message || '无法取消扫描');
      if (result.scan?.running === true) {
        scanStatus(result.scan);
        followScan(result.scan);
      } else {
        updateCancelButton(result.scan);
        await refresh(false);
        if (result.scan?.state === 'cancelled') {
          setStatus('扫描已取消，已登记的视频保留');
        }
      }
    } catch (error) {
      updateCancelButton({running:true,state:'scanning'});
      setStatus(error?.message || '无法取消扫描，请重试', true);
    }
  }
  function closeMenu(restoreFocus = false) {
    menuEvents?.abort(); menuEvents = null;
    contextMenu?.remove(); contextMenu = null;
    const anchor = menuAnchor; menuAnchor = null;
    anchor?.setAttribute("aria-expanded", "false");
    if (restoreFocus && anchor?.isConnected) anchor.focus({preventScroll:true});
  }
  function stopPlayer() {
    ++playRequest;
    if (activePlayer?.node.isConnected) closeModal();
    activePlayer = null;
  }
  async function openLocation(item) {
    try {
      const result = await AppApi.openVideoLocation(item.id);
      if (!result?.ok) toast(result?.error || result?.message || "暂时无法打开视频位置", "error");
    } catch (_) { toast("暂时无法打开视频位置，请重试", "error"); }
  }
  function confirmDelete(item) {
    if (item.deletable !== true || deleting.has(item.id)) return;
    stopPlayer();
    const mask = openModal(item.delete_label || "删除视频", '<p class="video-delete-title"></p><p class="video-delete-note"></p><div class="video-delete-error" role="alert"></div>', '<button class="btn" id="videoDeleteCancel" type="button">取消</button><button class="btn danger" id="videoDeleteConfirm" type="button">删除</button>');
    mask.querySelector(".video-delete-title").textContent = item.title || "未命名视频";
    mask.querySelector(".video-delete-note").textContent = item.deletion_note || "该操作将移除 FoilPage 中的视频，无法撤销。请确认后继续。";
    const cancel = mask.querySelector("#videoDeleteCancel"), confirm = mask.querySelector("#videoDeleteConfirm"), error = mask.querySelector(".video-delete-error");
    confirm.textContent = item.delete_label || "删除";
    cancel.onclick = closeModal;
    confirm.onclick = async () => {
      if (deleting.has(item.id)) return;
      deleting.add(item.id); confirm.disabled = true; cancel.disabled = true; error.textContent = "";
      const index = [...document.querySelectorAll(".video-card")].findIndex(card => card.dataset.videoId === item.id);
      try {
        const result = await AppApi.deleteVideo(item.id);
        if (!result?.ok) throw new Error(result?.error || result?.message || "视频未删除，请重试");
        if (activePlayer?.id === item.id) stopPlayer();
        if (mask.isConnected) closeModal();
        if (Array.isArray(result.tasks)) AppState.tasks = result.tasks;
        else {
          try { const tasks = await AppApi.tasks(); if (Array.isArray(tasks)) AppState.tasks = tasks; } catch (_) { /* The next task snapshot will reconcile the list. */ }
        }
        if (AppState.currentView === "videos") {
          if (Array.isArray(result.items)) { ++request; applyItems(result.items); }
          else await refresh();
          const anchors = [...document.querySelectorAll(".video-menu-trigger")];
          (anchors[Math.min(Math.max(index,0),anchors.length-1)] || document.getElementById("refreshVideos"))?.focus({preventScroll:true});
        }
        toast(result.message || "视频已删除");
      } catch (failure) {
        const message = failure.message || "视频未删除，请重试";
        if (mask.isConnected) error.textContent = message;
        else toast(message,"error");
      } finally {
        deleting.delete(item.id);
        if (mask.isConnected) { confirm.disabled = false; cancel.disabled = false; }
      }
    };
  }
  function showMenu(item, anchor, point) {
    closeMenu();
    menuAnchor = anchor; anchor.setAttribute("aria-expanded", "true");
    const menu = document.createElement("div"); contextMenu = menu;
    menu.className = "ctx-menu video-context-menu"; menu.setAttribute("role","menu"); menu.setAttribute("aria-label", "视频操作"); menu.tabIndex = -1;
    const actions = [
      {label:"打开所在位置",icon:'<path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h7l2 2"/>',disabled:item.available !== true,run:()=>openLocation(item)},
      {label:item.delete_label || "删除",icon:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',disabled:item.deletable !== true || deleting.has(item.id),danger:true,run:()=>confirmDelete(item)},
    ];
    actions.forEach(action => {
      const button = document.createElement("button"); button.type = "button"; button.className = "ctx-item" + (action.danger ? " danger" : ""); button.setAttribute("role","menuitem"); button.disabled = action.disabled;
      button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+action.icon+'</svg>';
      const label = document.createElement("span"); label.textContent = action.label; button.append(label);
      button.onclick = () => { closeMenu(true); action.run(); }; menu.append(button);
    });
    if (item.deletable !== true && item.deletion_note) { const note = document.createElement("div"); note.className = "video-menu-note"; note.textContent = item.deletion_note; menu.append(note); }
    document.body.append(menu);
    const r = anchor.getBoundingClientRect(), box = menu.getBoundingClientRect();
    menu.style.left = Math.max(12,Math.min(point?.x ?? r.right-box.width,innerWidth-box.width-12)) + "px";
    menu.style.top = Math.max(12,Math.min(point?.y ?? r.bottom+6,innerHeight-box.height-12)) + "px";
    const buttons = [...menu.querySelectorAll("button:not(:disabled)")];
    menuEvents = new AbortController(); const signal = menuEvents.signal;
    document.addEventListener("pointerdown", event => { if (!menu.contains(event.target) && !anchor.contains(event.target)) closeMenu(); }, {capture:true,signal});
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeMenu(true); return; }
      if (event.key === "Tab") { closeMenu(true); return; }
      if (!["ArrowDown","ArrowUp","Home","End"].includes(event.key)) return;
      event.preventDefault();
      const index = buttons.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length-1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }, {capture:true,signal});
    window.addEventListener("resize",()=>closeMenu(),{signal});
    document.addEventListener("scroll",event=>{if(!menu.contains(event.target))closeMenu();},{capture:true,signal});
    (buttons[0] || menu).focus({preventScroll:true});
  }
  async function play(item, button) {
    if (button.disabled || item.available !== true || deleting.has(item.id)) return;
    closeMenu(); stopPlayer();
    const serial = ++playRequest;
    button.disabled = true;
    setStatus("正在打开视频…");
    try {
      const result = await AppApi.videoPreview(item.id);
      if (serial !== playRequest || AppState.currentView !== "videos" || currentItems.get(item.id)?.available !== true) return;
      const url = result?.ok && mediaUrl(result.preview_url);
      if (!url || url.startsWith("data:")) { setStatus(result?.error || "视频暂时无法打开",true); return; }
      const mask = openModal(result.title || item.title || "视频", '<video class="video-player" controls playsinline preload="metadata"></video><div class="video-player-state" role="status"></div>');
      const video = mask.querySelector("video"), status = mask.querySelector(".video-player-state");
      activePlayer = {id:item.id,node:video};
      // 显式按内在比例定尺寸（先生 2026-10-25「有声无画」修复的一部分）：
      // 不放大、不整宽黑框，loadedmetadata 后一次性算好宽高，避免 WebView2
      // 对 width/height:auto 视频的渲染边角。
      const fitPlayer = () => {
        const vw = video.videoWidth, vh = video.videoHeight;
        const bodyWidth = video.parentElement?.clientWidth || 0;
        if (!vw || !vh || !bodyWidth) return;
        const maxHeight = Math.round(window.innerHeight * 0.72);
        let width = Math.min(bodyWidth, vw), height = width * vh / vw;
        if (height > maxHeight) { height = maxHeight; width = Math.min(bodyWidth, height * vw / vh); }
        video.style.width = Math.round(width) + "px";
        video.style.height = Math.round(height) + "px";
      };
      video.addEventListener("loadedmetadata", fitPlayer);
      video.src = url;
      const poster = mediaUrl(result.thumbnail_url || item.thumbnail_url);
      if (poster) video.poster = poster;
      video.setAttribute("aria-label", result.title || item.title || "视频播放");
      video.addEventListener("error", () => { status.textContent = "视频暂时无法播放，请检查文件"; status.setAttribute("role","alert"); });
      video.play().catch(() => { if (video.isConnected && !video.error) status.textContent = "点击播放开始观看"; });
      setStatus("");
    } catch (_) { if (serial === playRequest) setStatus("视频暂时无法打开，请重试",true); }
    finally { if (button.isConnected) button.disabled = item.available !== true || deleting.has(item.id); }
  }
  function cards(items) {
    const grid = document.getElementById("videoGrid");
    if (!grid) return;
    const active = contextMenu?.contains(document.activeElement) ? menuAnchor : document.activeElement;
    const previous = active?.closest?.('.video-card'), focusId = previous?.dataset.videoId;
    const focusCover = active?.classList.contains('video-cover');
    const allCards = () => [...grid.querySelectorAll('.video-card')];
    const oldIndex = allCards().indexOf(previous), scroll = grid.scrollTop;
    closeMenu(); grid.replaceChildren();
    if (!items.length) {
      grid.innerHTML = '<div class="empty"><span class="empty-symbol" aria-hidden="true">'+playIcon+'</span><div class="big">视频会留在这里</div><div class="sub">下载完成后，点击封面即可播放。</div><div class="empty-actions"><button class="btn primary small" type="button">下载视频</button></div></div>';
      grid.querySelector("button").onclick = () => switchView("links");
      return;
    }
    const built = [];
    items.forEach(item => {
      const card = document.createElement("article"); card.className = "video-card"; card.dataset.videoId = item.id;
      // 封面区：图片自然比例撑高（先生 2026-10-23：混合比例瀑布流），
      // 标题/来源/日期收进悬停信息层。
      const wrap = document.createElement("div"); wrap.className = "video-cover-wrap";
      const button = document.createElement("button"); button.className = "video-cover"; button.type = "button";
      button.disabled = item.available !== true || deleting.has(item.id); button.setAttribute("aria-label", "播放 " + (item.title || "未命名视频"));
      const thumbnail = mediaUrl(item.thumbnail_url);
      if (thumbnail) {
        const image = document.createElement("img"); image.src = thumbnail; image.alt = ""; image.loading = "lazy";
        // 列归属在初配时定死，图片后载只影响列的自然高度，不重排
        // （重排会挪卡丢焦点）。
        image.onload = () => image.classList.add("is-loaded");
        image.onerror = () => image.remove();
        button.append(image);
      } else {
        button.classList.add("no-thumb");
      }
      const glyph = document.createElement("span"); glyph.className = "video-play"; glyph.innerHTML = playIcon; button.append(glyph);
      const time = duration(item.duration_seconds);
      if (time) { const badge = document.createElement("span"); badge.className = "video-duration"; badge.textContent = time; button.append(badge); }
      button.onclick = () => play(item,button);
      const more = document.createElement("button"); more.type = "button"; more.className = "icon-btn video-menu-trigger"; more.setAttribute("aria-label", "视频操作 " + (item.title || "未命名视频")); more.setAttribute("aria-haspopup","menu"); more.setAttribute("aria-expanded","false");
      more.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
      more.onclick = () => contextMenu && menuAnchor === more ? closeMenu(true) : showMenu(item,more);
      card.oncontextmenu = event => { event.preventDefault(); showMenu(item,more,{x:event.clientX,y:event.clientY}); };
      card.onkeydown = event => {
        if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); event.stopPropagation(); showMenu(item,more); }
      };
      const info = document.createElement("div"); info.className = "video-info";
      const title = document.createElement("h2"); title.className = "video-name"; title.textContent = item.title || "未命名视频"; title.title = title.textContent;
      const meta = document.createElement("div"); meta.className = "video-meta";
      const source = document.createElement("span"); source.textContent = item.available === true ? (item.source || "本地视频") : "文件不可用";
      const date = document.createElement("span"); date.textContent = String(item.downloaded_at || "").slice(0,10);
      meta.append(source,date);
      if (time) { const length = document.createElement("span"); length.textContent = time; meta.append(length); }
      info.append(title,meta);
      wrap.append(button, more, info); card.append(wrap); built.push(card);
    });
    layoutMasonry(grid, built);
    watchGrid(grid);
    grid.scrollTop = scroll;
    if (focusId) {
      const rows = allCards();
      const row = rows.find(card => card.dataset.videoId === focusId) || rows[Math.min(Math.max(oldIndex,0),rows.length-1)];
      const control = row?.querySelector(focusCover ? '.video-cover:not(:disabled)' : '.video-menu-trigger') || row?.querySelector('.video-menu-trigger') || document.getElementById('refreshVideos');
      control?.focus({preventScroll:true});
    }
  }
  // 瀑布流改列分配：CSS 多列在定高容器里会向右溢出成横向滚动（先生 2026-10-25
  // 实机复现），改为 JS 把卡片分进等宽列、容器纵向滚动，保留混排与填充。
  let masonryCards = [], masonryFrame = 0, gridObserver = null, lastGridWidth = 0;
  function scheduleMasonry() {
    if (masonryFrame) return;
    masonryFrame = requestAnimationFrame(() => { masonryFrame = 0; const grid = document.getElementById("videoGrid"); if (grid?.querySelector(".video-col")) layoutMasonry(grid, masonryCards); });
  }
  function layoutMasonry(grid, cardsList) {
    if (!cardsList.length) return;
    masonryCards = cardsList;
    const gap = 20, minCol = 240;
    const inner = grid.clientWidth - 8; // 左右 padding 4px
    lastGridWidth = inner;
    const count = Math.max(1, Math.min(cardsList.length, Math.floor((inner + gap) / (minCol + gap))));
    const colWidth = (inner - (count - 1) * gap) / count;
    const columns = Array.from({length: count}, () => document.createElement("div"));
    columns.forEach(column => column.className = "video-col");
    const columnOf = new Map(columns.map((column, index) => [column, index]));
    const heights = new Array(count).fill(0);
    cardsList.forEach(card => {
      // 列归属尽量保持（图片后载/窗口微调不挪卡，悬停与滚动位置稳定），
      // 只有新卡或所属列被裁掉时才投给最矮列。
      let index = columnOf.get(card.parentElement);
      if (index === undefined) {
        index = 0;
        for (let i = 1; i < count; i++) if (heights[i] < heights[index]) index = i;
      }
      columns[index].append(card);
      const image = card.querySelector(".video-cover img");
      const ratio = image?.naturalWidth && image?.naturalHeight ? image.naturalHeight / image.naturalWidth : 9 / 16;
      heights[index] += colWidth * ratio + 24;
    });
    grid.replaceChildren(...columns);
  }
  // ResizeObserver 初次 observe 必回调：只能在渲染时挂一次且按宽度变化
  // 门禁，否则初次回调就会挪卡丢焦点。
  function watchGrid(grid) {
    if (gridObserver) gridObserver.disconnect();
    gridObserver = new ResizeObserver(entries => {
      const width = entries[entries.length - 1]?.contentRect.width || 0;
      if (Math.abs(width - lastGridWidth) > 1) scheduleMasonry();
    });
    gridObserver.observe(grid);
  }
  function applyItems(items) {
    listState('');
    const safeItems = items.filter(item => item && typeof item.id === "string");
    currentItems = new Map(safeItems.map(item=>[item.id,item]));
    if (activePlayer && currentItems.get(activePlayer.id)?.available !== true) stopPlayer();
    const key = JSON.stringify(safeItems);
    if (key !== lastItemsKey || !document.querySelector('#videoGrid > *')) { lastItemsKey = key; cards(safeItems); }
    const missing = safeItems.filter(item => item.available !== true).length;
    setStatus(safeItems.length ? safeItems.length + ' 个视频' + (missing ? '，' + missing + ' 个位置待恢复' : '') : "暂无视频");
  }
  function listState(message, failed = false) {
    const node = document.getElementById('videosReadState');
    if (!node) return;
    node.hidden = !message;
    node.classList.toggle('is-error', failed);
    node.setAttribute('role', failed ? 'alert' : 'status');
    node.querySelector('h2').textContent = message || '';
    node.querySelector('p').textContent = failed ? '请重试，或在准备页检查本地服务。' : '正在整理本地视频与封面';
    node.querySelector('button').hidden = !failed;
    document.getElementById('videoGrid')?.setAttribute('aria-busy', String(!!message && !failed));
    document.querySelector('.videos-content')?.classList.toggle('is-reading', !!message && !failed);
  }
  async function refresh(forceScan = false) {
    const serial = ++request;
    stopScanPoll();
    setStatus('');
    // Existing media remains usable while discovery runs or a refresh fails.
    if (!currentItems.size) listState('正在读取视频');
    try {
      const result = await AppApi.videos(forceScan === true);
      if (serial !== request || AppState.currentView !== "videos") return;
      if (!result?.ok || !Array.isArray(result.items)) {
        if (currentItems.size) setStatus(result?.error || '暂时无法读取视频', true);
        else listState(result?.error || "暂时无法读取视频",true);
        return;
      }
      listState('');
      applyItems(result.items);
      scanStatus(result.scan); followScan(result.scan);
    } catch (_) {
      if (serial === request && AppState.currentView === 'videos') {
        if (currentItems.size) setStatus('暂时无法读取视频', true);
        else listState("暂时无法读取视频",true);
      }
    }
  }
  function render() {
    const host = document.getElementById("page-videos");
    closeMenu(); stopPlayer(); stopScanPoll();
    if (!pageObserver) {
      pageObserver = new MutationObserver(() => {
        if (!host.classList.contains("active")) { ++request; closeMenu(); stopPlayer(); stopScanPoll(); leave(); }
      });
      pageObserver.observe(host,{attributes:true,attributeFilter:["class"]});
    }
    currentItems = new Map(); lastItemsKey = '';
    host.innerHTML = '<div class="videos-layout"><div class="videos-toolbar"><span id="videosStatus" class="videos-status" role="status" aria-live="polite"></span></div><div class="videos-content"><div id="videoGrid" class="video-grid" aria-label="已下载的视频"></div><div id="videosReadState" class="media-read-state" role="status" aria-live="polite" hidden><span class="media-read-symbol" aria-hidden="true">'+playIcon+'</span><h2>正在读取视频</h2><p>正在整理本地视频与封面</p><button class="btn small" type="button" hidden>重新读取</button></div></div></div>';
    // 刷新按钮上移顶栏：位于「视频」标题右侧的动作区（先生 2026-10-25）。
    const actions = document.getElementById("viewActions");
    actions.replaceChildren();
    const refreshButton = document.createElement("button");
    refreshButton.id = "refreshVideos"; refreshButton.type = "button"; refreshButton.className = "btn ghost small";
    refreshButton.textContent = "刷新";
    refreshButton.onclick = () => refresh(true);
    const cancelButton = document.createElement("button");
    cancelButton.id = "cancelVideoScan"; cancelButton.type = "button";
    cancelButton.className = "btn ghost small"; cancelButton.hidden = true;
    cancelButton.textContent = "取消扫描";
    cancelButton.onclick = cancelScan;
    actions.append(refreshButton, cancelButton);
    document.querySelector('#videosReadState button').onclick = () => refresh(true);
    refresh(true);
  }
  function leave() {
    if (document.getElementById("refreshVideos")?.closest("#viewActions"))
      document.getElementById("viewActions")?.replaceChildren();
  }
  function refreshLight() {
    const key = (AppState.tasks || []).map(task => [task.task_id,task.status,task.display_title,task.downloaded_path].join("|")).join(";");
    if (key !== lastTaskKey) { lastTaskKey = key; refresh(); }
  }
  return {render,refreshLight};
})();
