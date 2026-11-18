// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import { getToken, health, pair, request, setup } from "./api.js";
import { progressFor, progressLabelFor } from "./popup-model.js";

const $ = (selector) => document.querySelector(selector);
let connectionMode = "checking";
let checking = false;

function showConnection(state, message) {
  $("#connection").className = `connection ${state}`;
  $("#connectionText").textContent = message;
}

function pairingAction(configured) {
  return configured ? "重新配对" : "允许连接";
}

function pairingHint(configured) {
  return `请在桌面连接页点击「${pairingAction(configured)}」，再回来连接。`;
}

function showOpenAction(desktopRunning) {
  $("#openApp").hidden = !desktopRunning;
  $("#launchApp").hidden = desktopRunning;
}

function showConnectionRecovery(message, desktopRunning, needsPairing = false) {
  connectionMode = needsPairing ? "pairing" : "offline";
  showConnection("error", "未连接");
  $("#connectionPanel").hidden = false;
  $("#connectionHeading").textContent = needsPairing ? "在桌面端确认连接" : "恢复桌面连接";
  $("#message").textContent = message;
  $("#connect").textContent = needsPairing ? "连接 FoilPage" : "检查连接";
  showOpenAction(desktopRunning);
}

async function showDisconnected(message, allowPageActions, desktopRunning, needsPairing) {
  showConnectionRecovery(message, desktopRunning, needsPairing);
  if (allowPageActions) await detectCurrentPage();
  else $("#pagePanel").hidden = true;
  await loadJobs();
}

async function checkConnection() {
  if (checking) return false;
  checking = true;
  let desktop;
  try {
    desktop = await health();
    await noteVersion(desktop);
    if (desktop.configured === false || !await getToken()) {
      await showDisconnected(pairingHint(desktop.configured), false, true, true);
      return false;
    }
    // Chromium extension GETs can omit Origin. Verify the extension ID with
    // OPTIONS and the stored token with the authenticated list request.
    const statusPath = "/v1/downloads?limit=1";
    await request(statusPath, { method: "OPTIONS" });
    await request(statusPath);
    connectionMode = "online";
    showConnection("online", "已连接");
    $("#connectionPanel").hidden = true;
    $("#openApp").hidden = true;
    $("#launchApp").hidden = true;
    await Promise.all([loadJobs(), detectCurrentPage()]);
    return true;
  } catch (error) {
    const pairingRequired = ["unauthorized", "origin_denied", "local_api_token_missing"].includes(error.code);
    await showDisconnected(
      pairingRequired ? pairingHint(desktop?.configured) : error.message,
      !pairingRequired && Boolean(await getToken().catch(() => "")),
      Boolean(desktop),
      pairingRequired,
    );
    return false;
  } finally {
    checking = false;
  }
}

/* Installed files and the running manifest are different evidence. Reload can
   only activate the registered directory; a stale copy still needs that fixed path. */
const version = String(chrome.runtime.getManifest?.().version || "");
if (/^\d+(?:\.\d+){0,3}$/.test(version)) {
  $("#extensionVersion").textContent = `v${version}`;
  $("#extensionVersion").hidden = false;
}

async function noteVersion(health) {
  const el = $("#versionNote");
  el.hidden = true;
  try {
    const mine = version;
    const latest = String(health?.extension_latest || "");
    if (!/^\d+(?:\.\d+){0,3}$/.test(mine) || !/^\d+(?:\.\d+){0,3}$/.test(latest)) {
      return;
    }
    const next = latest.split('.').map(Number), current = mine.split('.').map(Number);
    let order = 0;
    for (let i = 0; i < 4; i++) {
      if ((next[i] || 0) === (current[i] || 0)) continue;
      order = (next[i] || 0) > (current[i] || 0) ? 1 : -1; break;
    }
    if (!order) return;
    if (order < 0) el.textContent = `扩展 ${mine}，桌面 ${latest}。版本不一致，请加载随当前 FoilPage 安装的扩展目录。`;
    else {
      let attempted = false;
      try {
        const saved = await chrome.storage.local.get('shengjianSelfReloadTarget');
        const target = saved.shengjianSelfReloadTarget;
        attempted = target?.from === mine && target?.to === latest;
      } catch { /* Version guidance remains available when local storage is busy. */ }
      el.textContent = attempted
        ? `扩展 ${mine}，桌面 ${latest}。尚未切换到新版，请从 FoilPage 重新加载固定扩展目录。`
        : `扩展 ${mine}，桌面 ${latest}。关闭弹窗后，空闲时尝试重载；随后刷新视频页。`;
    }
    el.hidden = false;
  } catch { el.hidden = true; }
}

// 兜底入口：即使页面内按钮未注入，也可从弹窗对当前标签页发起下载。
// activeTab 权限在用户点击扩展图标时授予，弹窗打开即可读取当前页 URL。
let currentPageKey;
async function detectCurrentPage() {
  const panel = $("#pagePanel");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tab?.url || "";
    const pageKey = `${tab?.id ?? ""}:${url}`;
    if (pageKey === currentPageKey) {
      panel.hidden = false;
      return;
    }
    currentPageKey = pageKey;
    const isBili = /^https:\/\/(www\.)?bilibili\.com\/video\//i.test(url);
    const isDouyin = /^https:\/\/(www\.)?douyin\.com\/(video|share\/video)\//i.test(url);
    // YouTube 与 content-youtube.js 的 videoIdFromLocation 保持同一页面集合。
    const isYoutube = /^https:\/\/(www\.|m\.)?youtube\.com\/watch/i.test(url)
      || /^https:\/\/youtu\.be\/[A-Za-z0-9_-]{6,}/i.test(url);
    const platform = isBili ? "B 站" : isDouyin ? "抖音" : isYoutube ? "YouTube" : "";
    $("#pagePlatform").textContent = platform;
    $("#pagePlatform").hidden = !platform;
    $("#pageActions").hidden = !platform;
    $("#pageHint").hidden = Boolean(platform);
    $("#pageState").hidden = true;
    const title = tab?.title?.replace(/\s*[-_]\s*(YouTube|哔哩哔哩|抖音).*$/u, "").trim() || "当前视频";
    $("#pageDesc").textContent = platform ? title : "当前标签页没有可提交的视频";
    $("#pageDesc").title = platform ? title : "";
    panel.hidden = false;
    if (!platform) return;
    const onPageState = (message, error = false) => {
      const state = $("#pageState");
      state.textContent = message;
      state.hidden = false;
      state.classList.toggle("err", error);
    };
    const controls = [$("#pageDownload"), $("#pageTranscribe")];
    let submitting = false;
    const setSubmitting = (value) => {
      submitting = value;
      controls.forEach(button => button.setAttribute("aria-disabled", String(value)));
    };
    setSubmitting(false);
    const submitPage = (transcribe) => {
      if (submitting) return;
      setSubmitting(true);
      onPageState("正在提交…");
      const restore = () => setSubmitting(false);
      // 抖音必须带页面捕获的直链：无直链的提交会撞平台签名墙必败。
      // B 站走本地 yt-dlp 轨，不需要这一步。
      const begin = (mediaUrl, playUrl = "", mediaCandidates = []) => chrome.runtime.sendMessage({
        type: "shengjian-page-submit",
        url,
        mediaUrl,
        playUrl, mediaCandidates, title,
        transcribe,
      }, (response) => {
        restore();
        if (chrome.runtime.lastError || !response?.ok) {
          onPageState((response?.message) || (chrome.runtime.lastError?.message) || "提交失败", true);
          if (response?.needs_connection) void checkConnection();
          return;
        }
        if (response.job?.queued || response.job?.waiting_connection) {
          showConnectionRecovery("打开 FoilPage，连接恢复后继续处理任务。", false);
          onPageState(response.job.queued ? "任务已暂存，打开 FoilPage 后自动提交"
            : "任务已保留，打开 FoilPage 后继续处理");
        } else onPageState("已提交下载");
        void loadJobs();
      });
      const startBegin = () => {
        if (!isDouyin) { begin(""); return; }
        const videoId = url.match(/\/video\/(\d{10,24})/)?.[1] || "";
        chrome.tabs.sendMessage(tab.id, { type: "shengjian-fresh-media", videoId }, (fresh) => {
          if (chrome.runtime.lastError || !fresh?.ok || String(fresh.videoId || "") !== videoId) { begin(""); return; }
          begin(fresh.mediaUrl || "", fresh.playUrl || "", fresh.mediaCandidates || []);
        });
      };
      // 已下载的文件需要确认重下，进行中的下载仍可追加转写。
      chrome.runtime.sendMessage({ type: "shengjian-check-existing", url }, (check) => {
        if (chrome.runtime.lastError || !check?.ok) { startBegin(); return; }
        if (check.active && (!transcribe || check.activeTranscribing)) {
          restore();
          onPageState("该视频正在处理中，无需重复提交（进度见 FoilPage 任务页）");
          return;
        }
        if (check.done && !transcribe && !window.confirm("该视频已下载完成，重新下载将新增一份文件。仍要提交吗？")) {
          restore();
          onPageState("已取消提交");
          return;
        }
        startBegin();
      });
    };
    $("#pageDownload").onclick = () => submitPage(false);
    $("#pageTranscribe").onclick = () => submitPage(true);
  } catch {
    panel.hidden = true;
  }
}

const terminal = new Set(["done", "failed", "cancelled"]);
const measurable = new Set(["downloading", "awaiting_browser", "transcribing", "running"]);
const cancelling = new Set();
const cancelErrors = new Map();
let lastJobs = null;
let jobLoad = null;

function setText(element, copy) {
  if (element.textContent !== copy) element.textContent = copy;
}

function createJobRow(id) {
  const row = document.createElement("article");
  row.className = "job-row";
  row.dataset.jobId = id;
  row.tabIndex = -1;
  row.innerHTML = `<div>
    <h3 class="job-title"></h3>
    <p class="job-status"></p>
    <div class="job-track" aria-hidden="true" hidden><div class="job-fill"></div></div>
  </div><button class="job-cancel" type="button">取消</button><p class="job-error" role="status"></p>`;
  row.querySelector(".job-cancel").onclick = () => cancelJob(id);
  return row;
}

function paintJob(row, job) {
  const title = row.querySelector(".job-title");
  const name = job.title || job.url || "未命名视频";
  const waiting = job.waiting_connection || job.cancel_requested || job.status === "handoff_pending";
  row.dataset.tone = job.status === "failed" || job.transcription_error ? "error"
    : job.status === "done" ? "success" : waiting ? "waiting" : terminal.has(job.status) ? "neutral" : "active";
  setText(title, name);
  title.title = name;
  setText(row.querySelector(".job-status"), progressLabelFor(job));
  row.querySelector(".job-track").hidden = !measurable.has(job.status) || job.progress == null;
  row.querySelector(".job-fill").style.width = `${Math.round(progressFor(job) * 100)}%`;
  const cancel = row.querySelector(".job-cancel");
  cancel.hidden = terminal.has(job.status);
  cancel.setAttribute("aria-disabled", String(cancelling.has(job.id) || Boolean(job.cancel_requested)));
  cancel.setAttribute("aria-label", `取消任务：${name}`);
  setText(cancel, cancelling.has(job.id) ? "取消中…" : "取消");
  setText(row.querySelector(".job-error"), cancelErrors.get(job.id) || "");
}

function renderJobs(jobs) {
  const root = $("#jobs");
  const focused = root.contains(document.activeElement) ? document.activeElement : null;
  const rows = new Map([...root.children].map(row => [row.dataset.jobId, row]));
  const active = jobs.filter(job => !terminal.has(job.status));
  const pendingDownloads = new Set(active.filter(job => !job.download_complete && job.url).map(job => job.url));
  const history = jobs.filter(job => terminal.has(job.status)
    && !(job.status === "failed" && !job.download_complete && pendingDownloads.has(job.url)));
  const visible = [...active, ...history].slice(0, 5);
  const count = $("#jobsCount");
  count.hidden = jobs.length === 0;
  const total = active.length + history.length;
  count.textContent = total > visible.length ? `${visible.length} / ${total} 项` : `${visible.length} 项`;
  for (const [index, job] of visible.entries()) {
    const row = rows.get(job.id) || createJobRow(job.id);
    rows.delete(job.id);
    paintJob(row, job);
    if (row !== root.children[index]) root.insertBefore(row, root.children[index] || null);
  }
  for (const row of rows.values()) row.remove();
  if (focused && (document.activeElement !== focused || focused.hidden)) {
    const row = focused.closest(".job-row");
    const target = !focused.isConnected ? $("#refreshJobs") : focused.hidden ? row : focused;
    target.focus({ preventScroll: true });
  }
}

function showJobsStatus(message, error = false) {
  const status = $("#jobsStatus");
  status.classList.toggle("is-error", error);
  setText(status, message);
}

async function cancelJob(id) {
  const job = lastJobs?.find(item => item.id === id);
  if (!job || terminal.has(job.status) || job.cancel_requested || cancelling.has(id)) return;
  cancelling.add(id);
  cancelErrors.delete(id);
  renderJobs(lastJobs);
  try {
    const result = await chrome.runtime.sendMessage({ type: "shengjian-cancel-task", id });
    if (!result?.ok) throw new Error(result?.message || "暂时无法取消此任务");
    if (jobLoad) await jobLoad;
    if (result.job?.id === id) {
      lastJobs = lastJobs.map(item => item.id === id ? { ...item, ...result.job } : item);
      renderJobs(lastJobs);
    }
    await loadJobs();
  } catch (error) {
    cancelErrors.set(id, `取消未完成 · ${error.message || "请重试"}`);
  } finally {
    cancelling.delete(id);
    renderJobs(lastJobs);
  }
}

function loadJobs() {
  if (jobLoad) return jobLoad;
  $("#jobsPanel").hidden = false;
  if (!lastJobs && !$("#jobsStatus").textContent) showJobsStatus("正在读取任务…");
  jobLoad = (async () => {
    try {
      const data = await chrome.runtime.sendMessage({ type: "shengjian-authorized-status" });
      if (data?.ok === false || !Array.isArray(data?.tasks)) throw new Error("Task list unavailable");
      lastJobs = data.tasks.filter(job => typeof job?.id === "string" && job.id);
      const ids = new Set(lastJobs.map(job => job.id));
      for (const id of cancelErrors.keys()) {
        if (!ids.has(id)) cancelErrors.delete(id);
      }
      renderJobs(lastJobs);
      if (data.connection === "offline") {
        if (connectionMode === "online") showConnectionRecovery("打开 FoilPage，连接恢复后可查看桌面任务。", false);
        showJobsStatus("离线 · 仅显示此浏览器保存的任务");
      } else {
        showJobsStatus(lastJobs.length ? "" : "还没有任务");
        if (data.connection === "online" && connectionMode === "offline" && !checking) void checkConnection();
      }
    } catch {
      showJobsStatus(lastJobs ? "刷新任务失败，仍显示上次结果。" : "无法读取任务，请重试。", true);
    }
  })().finally(() => { jobLoad = null; });
  return jobLoad;
}

$("#connect").addEventListener("click", async () => {
  const button = $("#connect");
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  let desktop;
  try {
    if (connectionMode === "offline") {
      await checkConnection();
      return;
    }
    desktop = await health();
    if (desktop.configured) await pair();
    else await setup();
    await checkConnection();
  } catch (error) {
    const pairingRequired = ["pairing_closed", "origin_denied"].includes(error.code);
    $("#message").textContent = pairingRequired ? pairingHint(desktop?.configured) : error.message;
    if (!desktop) showConnectionRecovery(error.message, false);
  } finally {
    button.removeAttribute("aria-disabled");
    if ($("#connectionPanel").hidden && document.activeElement === document.body) {
      ($("#pageActions").hidden ? $("#refreshJobs") : $("#pageTranscribe")).focus({ preventScroll: true });
    }
  }
});

$("#openApp").addEventListener("click", async () => {
  const button = $("#openApp");
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  try {
    const res = await fetch("http://127.0.0.1:18765/v1/show", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: "browser" }),
    });
    if (!res.ok || !(await res.json()).shown) throw new Error("连接页未打开");
    const desktop = await health();
    $("#message").textContent = `已在 FoilPage 中打开连接页。点击「${pairingAction(desktop.configured)}」后回到这里。`;
  } catch {
    $("#message").textContent = "未能打开连接页，请打开 FoilPage。";
    const restoreFocus = document.activeElement === button;
    showOpenAction(false);
    if (restoreFocus) $("#launchApp").focus({ preventScroll: true });
  } finally {
    button.removeAttribute("aria-disabled");
  }
});

$("#refreshJobs").addEventListener("click", async ({ currentTarget }) => {
  if (currentTarget.getAttribute("aria-disabled") === "true") return;
  currentTarget.setAttribute("aria-disabled", "true");
  await loadJobs();
  currentTarget.removeAttribute("aria-disabled");
});
checkConnection().finally(() => { $("#connect").disabled = false; });
setInterval(loadJobs, 2500);
