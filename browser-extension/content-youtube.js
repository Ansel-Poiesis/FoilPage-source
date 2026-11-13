// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// FoilPage YouTube 视频页助手：在 YouTube 播放页提供「下载 / 转写」按钮。
// 与 B 站轨同一隐私姿态：只把当前视频 canonical URL 交给后台，以 mode=local
// 走桌面 yt-dlp 下载轨；不读取任何登录凭据，不触碰播放器媒体地址。
// YouTube 完整解析依赖桌面端 yt-dlp[default] + JavaScript 运行时（见 README）。
(function () {
  "use strict";

  // 只在顶层页面运行（iframe 内不注入，与抖音/B 站脚本一致）。
  if (window.top !== window) return;

  const HOST_CLASS = "shengjian-yt-host";

  function videoIdFromLocation() {
    // /watch?v=ID（含 preview/live 等共用路径）、/shorts/ID、youtu.be/ID。
    try {
      const url = new URL(location.href);
      if (url.pathname === "/watch") {
        return String(url.searchParams.get("v") || "").trim();
      }
      const shorts = url.pathname.match(/^\/(?:shorts|live|embed)\/([A-Za-z0-9_-]{6,})/);
      if (shorts) return shorts[1];
      if (url.hostname === "youtu.be") {
        const id = url.pathname.replace(/^\/+/, "").split("/")[0];
        return /^[A-Za-z0-9_-]{6,}$/.test(id) ? id : "";
      }
      return "";
    } catch {
      return "";
    }
  }

  function canonicalUrl(videoId) {
    // 统一为桌面端 detect_platform 可识别的 watch 形态（去掉 list/ t 等噪音参数）。
    return `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`;
  }

  function pageTitle() {
    return String(document.title || "")
      .replace(/\s*-\s*YouTube\s*$/u, "")
      .replace(/\s+$/u, "")
      .trim()
      .slice(0, 240);
  }

  // ---------------------------------------------------------------------------
  // 反馈 pill：与 B 站脚本同一状态语言（百分比爬升/完成勾/错误文案），
  // 但挂载方式不同——YouTube 播放器 DOM 被 SPA 频繁重建，这里用固定定位的
  // 浮动控件簇（按钮 + pill 一体），对站点改版最鲁棒。
  // ---------------------------------------------------------------------------

  function pillStatusFor(message, error) {
    const text = String(message || "");
    if (error) {
      return {
        mode: "error",
        text: /(?:正文|标题)处理失败/.test(text) ? text
          : /刷新/.test(text) ? "请刷新页面后重试"
          : /转写/.test(text) ? "转写失败，请在 FoilPage 查看"
          : /导入/.test(text) ? "已下载，导入 FoilPage 失败"
          : /回到原视频页|回原视频页/.test(text) ? "请回原视频页重试"
          : /打开|未运行|未连接/.test(text) ? "请先打开 FoilPage"
          : /运行时|JavaScript/.test(text) ? "需 JavaScript 运行时，见 FoilPage 设置"
          : "下载失败，请在 FoilPage 查看",
      };
    }
    if (/任务完成|已完成|下载成功|转写成功/.test(text)) return { mode: "done" };
    const percent = text.match(/(\d+)%/);
    if (percent) return { mode: "percent", percent: Math.max(0, Math.min(100, Number(percent[1]))) };
    if (/转写/.test(text)) return { mode: "text", text: "正在转写" };
    if (/解析/.test(text)) return { mode: "text", text: "正在解析" };
    if (/浏览器下载中/.test(text)) return { mode: "text", text: "浏览器下载中" };
    if (/已在处理|已合并/.test(text)) return { mode: "text", text: "已在处理中" };
    if (/改用|切换|兜底/.test(text)) return { mode: "text", text: "已换通道重试" };
    if (/已加入|已提交|已接收/.test(text)) return { mode: "text", text: "已提交下载" };
    if (/提交|读取/.test(text)) return { mode: "text", text: "正在提交" };
    // 兜底不再硬截 12 字（会截出病句）：去括号补充后整句交给 pill，
    // 宽度与省略交给 CSS，title 保留全文。
    return { mode: "text", text: text.replace(/（[^）]*）/g, "") };
  }

  // 驻留按内容长度伸缩（与抖音信息窗同一公式）：短确认 ~2.2s 即走，
  // 长错误最长 8s（要读完指引）；完成固定 4.5s 看勾勾变形；进度态不计时。
  const PILL_HOLD_MS = { done: 4500 };
  function pillHoldFor(text, kind) {
    const len = Math.max(1, String(text || "").replace(/（[^）]*）/g, "").length);
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    if (kind === "done") return PILL_HOLD_MS.done;
    if (kind === "error") return clamp(2600 + 280 * len, 4000, 8000);
    return clamp(1200 + 220 * len, 2000, 4000);
  }

  const pillTimers = { hide: 0, fade: 0 };
  function findHost() {
    return document.querySelector(`.${HOST_CLASS}`);
  }
  function findPill() {
    return document.querySelector(".shengjian-yt-pill");
  }

  function ensureHost() {
    let host = findHost();
    if (host) return host;
    host = document.createElement("div");
    host.className = HOST_CLASS;
    const pill = document.createElement("div");
    // is-hidden：控件簇先就位，pill 等第一条反馈再显示，避免空泡常驻。
    pill.className = "shengjian-yt-pill is-hidden";
    pill.setAttribute("role", "status");
    const stage = document.createElement("span");
    stage.className = "sj-stage";
    const num = document.createElement("span");
    num.className = "sj-num";
    const numValue = document.createElement("b");
    numValue.textContent = "0";
    const numUnit = document.createElement("i");
    numUnit.textContent = "%";
    num.append(numValue, numUnit);
    const svgNS = "http://www.w3.org/2000/svg";
    const check = document.createElementNS(svgNS, "svg");
    check.setAttribute("class", "sj-check");
    check.setAttribute("viewBox", "0 0 16 16");
    check.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", "M3.4 8.3l3 3.1 6.2-6.9");
    path.setAttribute("pathLength", "1");
    check.append(path);
    const text = document.createElement("span");
    text.className = "sj-text";
    stage.append(num, check, text);
    pill.append(stage);
    pill.addEventListener("click", () => dismissPill());
    host.append(pill);
    const player = document.querySelector("#player-container-outer, #player-container, ytd-player");
    if (player?.parentElement) player.parentElement.insertBefore(host, player.nextSibling);
    else (document.body || document.documentElement).append(host);
    return host;
  }

  function dismissPill() {
    const pill = findPill();
    if (!pill) return;
    for (const key of ["hide", "fade"]) clearTimeout(pillTimers[key]);
    pill.classList.add("is-fading");
    pillTimers.fade = setTimeout(() => pill.remove(), 420);
  }

  function showPillText(text, options = {}) {
    const pill = findPill() || ensureHost().querySelector(".shengjian-yt-pill");
    if (!pill) return;
    for (const key of ["hide", "fade"]) clearTimeout(pillTimers[key]);
    pill.classList.remove("is-fading", "is-done", "is-percent", "is-hidden");
    pill.classList.toggle("is-error", Boolean(options.error));
    pill.querySelector(".sj-text").textContent = String(text || "");
    pill.title = String(options.title || text || "");
    pillTimers.hide = setTimeout(dismissPill, options.autoHideMs || pillHoldFor(text, options.error ? "error" : "text"));
  }

  function applyPillStatus(message, error = false) {
    const status = pillStatusFor(message, error);
    const pill = findPill();
    if (!pill) {
      // 后台广播到达时控件可能尚未创建（如任务页完成回调）：只显错误/完成。
      if (status.mode === "error") showPillText(status.text, { error: true, autoHideMs: 7000 });
      return;
    }
    if (status.mode === "done") {
      pill.classList.remove("is-error", "is-fading", "is-hidden");
      pill.classList.add("is-percent", "is-done");
      const valueEl = pill.querySelector(".sj-num b");
      if (valueEl) valueEl.textContent = "100";
      pill.title = String(message);
      pill.querySelector('.sj-text').textContent = String(message);
      for (const key of ["hide", "fade"]) clearTimeout(pillTimers[key]);
      pillTimers.hide = setTimeout(dismissPill, PILL_HOLD_MS.done);
      return;
    }
    if (status.mode === "percent") {
      pill.classList.remove("is-fading", "is-done", "is-error", "is-hidden");
      pill.classList.add("is-percent");
      const valueEl = pill.querySelector(".sj-num b");
      if (valueEl) valueEl.textContent = String(status.percent);
      pill.title = `正在下载 ${status.percent}%`;
      return;
    }
    showPillText(status.text, { error: status.mode === "error", title: message });
  }

  function sendRuntimeMessage(message, callback) {
    try {
      chrome.runtime.sendMessage(message, callback);
    } catch {
      callback?.(null);
    }
  }

  function makeButton(action, label, glyph) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "shengjian-yt-button";
    button.title = action === "download" ? "用 FoilPage 下载当前视频" : "用 FoilPage 下载并转写当前视频";
    button.setAttribute("aria-label", button.title);
    button.dataset.shengjianAction = action;
    const icon = document.createElement("span");
    icon.className = "shengjian-yt-glyph";
    icon.textContent = glyph;
    const text = document.createElement("span");
    text.textContent = label;
    button.append(icon, text);
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      submit(button, action);
    });
    return button;
  }

  function submit(button, action) {
    const videoId = videoIdFromLocation();
    if (!videoId) {
      showPillText("未识别当前视频，请在视频播放页重试", { error: true });
      return;
    }
    button.disabled = true;
    button.classList.add("is-busy");
    showPillText("正在提交…");
    sendRuntimeMessage({
      type: "shengjian-page-submit",
      url: canonicalUrl(videoId),
      title: pageTitle(),
      transcribe: action === "transcribe",
    }, (response) => {
      button.disabled = false;
      button.classList.remove("is-busy");
      if (videoIdFromLocation() !== videoId) return;
      if (chrome.runtime.lastError) {
        showPillText("扩展已更新，请刷新页面后重试", { error: true });
        return;
      }
      if (!response?.ok) {
        if (response?.feedback_sent) return;
        showPillText(response?.message || "FoilPage 下载任务未能创建", { error: true });
        return;
      }
      // Background sends accepted or offline-queued feedback exactly once.
    });
  }

  function ensureControls() {
    if (!videoIdFromLocation()) return false;
    const host = ensureHost();
    if (!host.querySelector(".shengjian-yt-button[data-shengjian-action='download']")) {
      host.append(makeButton("download", "下载", "↓"));
    }
    if (!host.querySelector(".shengjian-yt-button[data-shengjian-action='transcribe']")) {
      host.append(makeButton("transcribe", "转写", "文"));
    }
    return true;
  }

  // YouTube 是 SPA：路径切换后整个页面重渲染，浮动簇是 body 级节点通常
  // 存活，但保险起见在非视频页主动移除，避免残留到首页/搜索页。
  let restoredVideoId = "";
  function restoreTaskFeedback() {
    const videoId = videoIdFromLocation();
    if (!videoId || videoId === restoredVideoId) return;
    restoredVideoId = videoId;
    sendRuntimeMessage({type:"shengjian-task-state",url:canonicalUrl(videoId)}, response => {
      if (videoIdFromLocation() !== videoId || !response?.job) return;
      const job = response.job;
      if (job.status === "done") showPillText(job.transcription_label || (job.job_id ? "已转写" : "已下载"),{error:job.transcription_error === true});
      else if (job.status === "failed") showPillText(job.download_complete ? "转写失败，请在 FoilPage 查看" : "下载失败，请在 FoilPage 查看",{error:true});
      else if (job.status === "cancelled") showPillText("任务已取消");
      else if (job.queued || job.waiting_connection) showPillText("等待 FoilPage 连接，任务已保留");
      else if (["transcribing","queued","imported"].includes(job.status)) showPillText("正在转写");
      else applyPillStatus(job.status === "downloading" ? `正在下载 ${Math.round(Number(job.progress || 0)*100)}%` : "正在下载");
    });
  }
  let lastKey = location.pathname + location.search;
  const check = () => {
    const key = location.pathname + location.search;
    if (key !== lastKey) {
      lastKey = key;
      findHost()?.remove();
    }
    if (!ensureControls() && !videoIdFromLocation()) {
      findHost()?.remove();
    }
    restoreTaskFeedback();
  };
  check();
  setInterval(check, 1200);
  new MutationObserver(check).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  window.addEventListener("popstate", check, { passive: true });
  window.addEventListener("yt-navigate-finish", check, { passive: true });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "shengjian-toast") {
      if (message.taskUrl && message.taskUrl !== canonicalUrl(videoIdFromLocation())) return undefined;
      applyPillStatus(String(message?.message || ""), Boolean(message?.error));
      sendResponse?.({ ok: true });
    }
    return undefined;
  });

  // Test/diagnostics hook（与 content.js / content-bili.js 一致，Node vm 可用）。
  globalThis.__shengjianYoutubeInternals = {
    videoIdFromLocation,
    canonicalUrl,
    pageTitle,
    ensureControls,
    pillStatusFor,
    applyPillStatus,
    dismissPill,
    pillHoldFor,
  };
})();
