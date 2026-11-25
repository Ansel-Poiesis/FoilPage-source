// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// FoilPage B 站视频页助手：在 bilibili 播放页提供「下载 / 转写」按钮。
// B 站防盗链需要 Referer，不适合浏览器下载管理器直提；这里只把当前视频页
// canonical URL 交给后台，以 mode=local 走桌面 yt-dlp 下载轨（带 Referer，
// 公开视频免登录）。本脚本不读取任何站点登录凭据。
(function () {
  "use strict";

  // 只在顶层页面运行（iframe 内不注入，与抖音 content.js 一致）。
  if (window.top !== window) return;

  const VIDEO_PATH = /^\/(?:video|bangumi\/play)\/([A-Za-z0-9]+)/;
  const PILL_CLASS = "shengjian-bili-pill";

  function videoIdFromLocation() {
    const m = location.pathname.match(VIDEO_PATH);
    return m ? m[1] : "";
  }

  function canonicalUrl(videoId) {
    // 番剧页 canonical 是 bangumi，保持用户当前 URL 即可；普通视频统一为
    // /video/BVxxx 形态，方便后端 detect_platform。
    if (location.pathname.startsWith("/video/")) {
      return `${location.origin}/video/${videoId}`;
    }
    return `${location.origin}${location.pathname}`;
  }

  function pageTitle() {
    return String(document.title || "")
      .replace(/\s*_哔哩哔哩_bilibili\s*$/u, "")
      .replace(/\s*-\s*哔哩哔哩\s*$/u, "")
      .replace(/\s+$/u, "")
      .trim()
      .slice(0, 240);
  }

  // ---------------------------------------------------------------------------
  // 反馈弹窗（pill）：替代过去的整行横幅。点击按钮后在按钮上方弹出，
  // 下载期间显示平滑爬升的百分比，完成时数字收缩、勾勾划入；点击可关闭。
  // ---------------------------------------------------------------------------

  const {pillStatusFor, pillHoldFor, PILL_HOLD_MS} = globalThis.FoilPageContract;

  const pillTimers = { hide: 0, stall: 0, done: 0, fade: 0, raf: 0 };
  let pillPercentShown = 0;

  function findPill() {
    return document.querySelector(`.${PILL_CLASS}`);
  }

  function pillMount() {
    const player = findPlayerMount();
    if (!player) return null;
    const controls = document.querySelector('.shengjian-bili-host.above-player');
    return controls || player.parentElement;
  }

  function ensurePill() {
    let pill = findPill();
    if (pill) return pill;
    const mount = pillMount();
    if (!mount) return null;
    pill = document.createElement("div");
    pill.className = PILL_CLASS;
    const stage = document.createElement("span");
    stage.className = "sj-stage";
    const num = document.createElement("span");
    num.className = "sj-num";
    num.setAttribute("role", "progressbar");
    num.setAttribute("aria-label", "视频下载进度");
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
    text.setAttribute("role", "status");
    const launch = document.createElement("a");
    launch.className = "sj-launch";
    launch.href = "foilpage://launch";
    launch.textContent = "打开 FoilPage";
    launch.addEventListener("click", (event) => event.stopPropagation());
    const close = document.createElement("button");
    close.type = "button";
    close.className = "sj-dismiss";
    close.textContent = "×";
    close.setAttribute("aria-label", "关闭提示");
    stage.append(num, check, text, launch);
    pill.append(stage, close);
    pill.addEventListener("click", () => dismissPill());
    pill.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      dismissPill();
    });
    const player = findPlayerMount();
    if (player?.parentElement === mount) mount.insertBefore(pill, player);
    else mount.prepend(pill);
    return pill;
  }

  function clearPillTimers() {
    for (const key of ["hide", "stall", "done", "fade"]) clearTimeout(pillTimers[key]);
    if (pillTimers.raf) cancelAnimationFrame(pillTimers.raf);
    pillTimers.raf = 0;
  }

  function dismissPill() {
    const pill = findPill();
    if (!pill) return;
    clearPillTimers();
    if (pill.contains(document.activeElement)) {
      document.querySelector(".shengjian-bili-button:not(:disabled)")?.focus({ preventScroll: true });
    }
    pill.inert = true;
    pill.classList.add("is-fading");
    pillTimers.fade = setTimeout(() => pill.remove(), 420);
  }

  function armPillHide(ms) {
    clearTimeout(pillTimers.hide);
    pillTimers.hide = setTimeout(() => {
      if (!findPill()?.contains(document.activeElement)) dismissPill();
    }, ms);
  }

  function showPillText(text, options = {}) {
    const pill = ensurePill();
    if (!pill) return;
    clearPillTimers();
    pill.inert = false;
    pill.classList.remove("is-fading", "is-done", "is-percent");
    pill.classList.toggle("is-error", Boolean(options.error));
    pill.classList.toggle("has-launch", Boolean(options.launch));
    const textEl = pill.querySelector(".sj-text");
    textEl.textContent = String(text || "");
    pill.title = String(options.title || textEl.textContent);
    if (!options.launch) armPillHide(options.autoHideMs || pillHoldFor(text, options.error ? "error" : "text"));
  }

  function tweenPillPercent(target, duration, onDone) {
    const pill = findPill();
    if (!pill) return;
    const valueEl = pill.querySelector(".sj-num b");
    const from = pillPercentShown;
    const start = performance.now();
    if (pillTimers.raf) cancelAnimationFrame(pillTimers.raf);
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const value = Math.round(from + (target - from) * eased);
      valueEl.textContent = String(value);
      pillPercentShown = value;
      if (t < 1) {
        pillTimers.raf = requestAnimationFrame(step);
      } else {
        pillTimers.raf = 0;
        onDone?.();
      }
    };
    pillTimers.raf = requestAnimationFrame(step);
  }

  function showPillPercent(target) {
    const pill = ensurePill();
    if (!pill) return;
    clearPillTimers();
    pill.inert = false;
    pill.classList.remove("is-fading", "is-done", "is-error", "has-launch");
    pill.classList.add("is-percent");
    pill.querySelector(".sj-text").textContent = "";
    pill.querySelector(".sj-num").setAttribute("aria-valuenow", String(target));
    pill.title = `正在下载 ${target}%`;
    tweenPillPercent(target, 480);
    // 进度消息停更（页面后台/任务卡住）时安静淡出，不常驻遮挡。
    clearTimeout(pillTimers.stall);
    pillTimers.stall = setTimeout(dismissPill, 60000);
  }

  function showPillDone(message = "下载成功") {
    const pill = ensurePill();
    if (!pill) return;
    const finish = () => {
      pill.inert = false;
      pill.classList.remove("is-error", "has-launch", "is-fading");
      pill.classList.add("is-percent", "is-done");
      pill.title = String(message);
      pill.querySelector('.sj-text').textContent = String(message);
      armPillHide(PILL_HOLD_MS.done);
    };
    if (pill.classList.contains("is-percent")) {
      // 先把数字补满到 100，再让它收缩成勾。
      clearPillTimers();
      tweenPillPercent(100, 240, finish);
    } else {
      clearPillTimers();
      pillPercentShown = 100;
      const valueEl = pill.querySelector(".sj-num b");
      if (valueEl) valueEl.textContent = "100";
      finish();
    }
  }

  function applyPillStatus(message, error = false) {
    const status = pillStatusFor(message, error);
    if (status.mode === "done") showPillDone(message);
    else if (status.mode === "percent") showPillPercent(status.percent);
    else showPillText(status.text, { error: status.mode === "error", title: message });
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
    button.className = "shengjian-bili-button";
    button.title = action === "download" ? "用 FoilPage 下载当前视频" : "用 FoilPage 下载并转写当前视频";
    button.setAttribute("aria-label", button.title);
    button.dataset.shengjianAction = action;
    const icon = document.createElement("span");
    icon.className = "shengjian-bili-glyph";
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
    feedbackRevision++;
    button.disabled = true;
    button.classList.add("is-busy");
    showPillText("正在提交…");
    const restore = () => {
      button.disabled = false;
      button.classList.remove("is-busy");
    };
    const beginSubmission = () => {
      showPillText("正在提交…");
      sendRuntimeMessage({
        type: "shengjian-page-submit",
        url: canonicalUrl(videoId),
        title: pageTitle(),
        transcribe: action === "transcribe",
      }, (response) => {
        restore();
        if (videoIdFromLocation() !== videoId) return;
        if (chrome.runtime.lastError) {
          showPillText("扩展已更新，请刷新页面后重试", { error: true });
          return;
        }
        if (response?.ok && (response.job?.queued || response.job?.waiting_connection)) {
          showPillText("任务已保留，等待 FoilPage 连接", { launch: true });
          return;
        }
        if (!response?.ok) {
          if (response?.needs_connection === true) {
            showPillText("FoilPage 未连接，请连接后重试", { error: true, launch: true, title: response.message });
            return;
          }
          if (response?.feedback_sent) return;
          showPillText(response?.message || "FoilPage 下载任务未能创建", { error: true });
          return;
        }
        // Background reports desktop acceptance separately.
      });
    };
    sendRuntimeMessage({ type: "shengjian-check-existing", url: canonicalUrl(videoId) }, (check) => {
      if (videoIdFromLocation() !== videoId) { restore(); return; }
      if (chrome.runtime.lastError || !check?.ok) { beginSubmission(); return; }
      if (check.active && (action === "download" || check.activeTranscribing)) {
        restore();
        showPillText(check.activeTranscribing && action === "transcribe"
          ? "该视频已在处理中，转写会自动接续（进度见 FoilPage 任务页）"
          : "该视频正在处理中，无需重复提交（进度见 FoilPage 任务页）");
        return;
      }
      if (check.done && action === "download"
          && !window.confirm("该视频已下载完成，重新下载将新增一份文件。仍要提交吗？")) {
        restore();
        showPillText("已取消提交");
        return;
      }
      beginSubmission();
    });
  }

  function findPlayerMount() {
    // 只选择播放器的外层容器。过去的通配选择器会命中播放器内部的
    // 控件节点，导致工具条被插入进视频层并覆盖进度条。
    const candidates = Array.from(document.querySelectorAll(
      "#bilibili-player, #bilibiliPlayer, #playerWrap, .bpx-player-container"
    ));
    return candidates.find((el) => {
      const rect = el.getBoundingClientRect?.();
      return rect && rect.width > 300 && rect.height > 160;
    }) || candidates[0] || null;
  }

  function ensureControls() {
    if (!videoIdFromLocation()) return false;
    const player = findPlayerMount();
    if (!player?.parentElement) return false;
    const bottom = player.querySelector('.bpx-player-control-bottom');
    const left = bottom?.querySelector('.bpx-player-control-bottom-left');
    const right = bottom?.querySelector('.bpx-player-control-bottom-right');
    const lr = left?.getBoundingClientRect();
    const rr = right?.getBoundingClientRect();
    const br = bottom?.getBoundingClientRect();
    // Only occupy measured free space between native controls, below the
    // seek bar. The host inherits the native control bar's auto-hide state.
    const gap = lr && rr && br && br.width > 0 && rr.left - lr.right >= 148;
    let host = document.querySelector(".shengjian-bili-host");
    if (!host) {
      host = document.createElement("div");
      host.className = "shengjian-bili-host";
    }
    if (!host.querySelector(".shengjian-bili-button[data-shengjian-action='download']")) {
      host.append(makeButton("download", "下载", "↓"));
    }
    if (!host.querySelector(".shengjian-bili-button[data-shengjian-action='transcribe']")) {
      host.append(makeButton("transcribe", "转写", "文"));
    }
    const pill = findPill();
    if (gap) {
      if (pill?.parentElement === host) player.parentElement.insertBefore(pill, player);
      if (host.parentElement !== bottom) bottom.insertBefore(host, right);
      host.className = 'shengjian-bili-host in-controls';
      host.style.removeProperty('width');
      host.style.removeProperty('margin-left');
      if (getComputedStyle(bottom).position === 'static') bottom.style.position = 'relative';
      host.style.left = Math.round(rr.left - br.left - 136) + 'px';
    } else {
      if (host.parentElement !== player.parentElement || host.nextElementSibling !== player) player.parentElement.insertBefore(host, player);
      if (pill?.parentElement === player.parentElement) host.prepend(pill);
      host.className = 'shengjian-bili-host above-player';
      host.style.removeProperty('left');
      const rect = player.getBoundingClientRect();
      const parentRect = player.parentElement.getBoundingClientRect();
      const inset = parseFloat(getComputedStyle(player.parentElement).paddingLeft) || 0;
      host.style.setProperty('width', Math.round(rect.width) + 'px', 'important');
      host.style.setProperty('margin-left', Math.max(0, Math.round(rect.left - parentRect.left - inset)) + 'px', 'important');
    }
    return true;
  }

  // B 站是 SPA：路径变化后重新挂载，播放器就绪后再注入也可靠。
  let feedbackRevision = 0;
  let restoredVideoId = "";
  function restoreTaskFeedback() {
    const videoId = videoIdFromLocation();
    if (!videoId || videoId === restoredVideoId) return;
    restoredVideoId = videoId;
    const revision = ++feedbackRevision;
    sendRuntimeMessage({type:"shengjian-task-state",url:canonicalUrl(videoId)}, response => {
      if (revision !== feedbackRevision || videoIdFromLocation() !== videoId || !response?.job) return;
      const job = response.job;
      if (job.status === "done") showPillText(job.transcription_label || (job.job_id ? "已转写" : "已下载"),{error:job.transcription_error === true});
      else if (job.status === "failed") showPillText(job.download_complete ? "转写失败，请在 FoilPage 查看" : "下载失败，请在 FoilPage 查看",{error:true});
      else if (job.status === "cancelled") showPillText("任务已取消");
      else if (job.queued || job.waiting_connection) showPillText("任务已保留，等待 FoilPage 连接", { launch: true });
      else if (["transcribing","queued","imported"].includes(job.status)) showPillText("正在转写");
      else applyPillStatus(job.status === "downloading" ? `正在下载 ${Math.round(Number(job.progress || 0)*100)}%` : "正在下载");
    });
  }
  let lastPath = location.pathname;
  const check = () => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      restoredVideoId = "";
      clearPillTimers();
      document.querySelectorAll(".shengjian-bili-host, .shengjian-bili-pill").forEach((el) => el.remove());
    }
    if (!ensureControls()) {
      document.querySelectorAll(".shengjian-bili-host").forEach((el) => el.remove());
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
  window.addEventListener('resize', check, { passive: true });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "shengjian-toast") {
      if (message.taskUrl && message.taskUrl !== canonicalUrl(videoIdFromLocation())) return undefined;
      feedbackRevision++;
      applyPillStatus(String(message?.message || ""), Boolean(message?.error));
      sendResponse?.({ ok: true });
    }
    return undefined;
  });

  // Test/diagnostics hook（与 content.js 一致，Node vm 可用）。
  globalThis.__shengjianBiliInternals = {
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
