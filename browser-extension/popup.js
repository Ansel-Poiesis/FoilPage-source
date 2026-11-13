// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import { getToken, health, setup } from "./api.js";
import { progressFor, progressLabelFor } from "./popup-model.js";

const $ = (selector) => document.querySelector(selector);

function showConnection(state, message) {
  $("#connection").className = `connection ${state}`;
  $("#connectionText").textContent = message;
}

async function checkConnection() {
  try {
    const data = await health();
    await noteVersion(data);
    if (!await getToken()) throw new Error("请在 FoilPage 中完成一键配置");
    showConnection("online", "已连接");
    $("#message").textContent = "已连接 FoilPage，处理条件请在准备页检查";
    $("#connect").textContent = "重新连接";
    $("#connect").classList.add('secondary');
    $("#openApp").style.display = "none";
    await loadJobs();
    await detectCurrentPage();
    return true;
  } catch (error) {
    showConnection("error", "未连接");
    $("#message").textContent = error.message;
    $("#connect").textContent = "连接 FoilPage";
    $("#connect").classList.remove("secondary");
    await loadJobs();
    if (await getToken()) await detectCurrentPage();
    else $("#pagePanel").hidden = true;
    $("#openApp").style.display = "";
    return false;
  }
}

/* Installed files and the running manifest are different evidence. Reload can
   only activate the registered directory; a stale copy still needs that fixed path. */
async function noteVersion(health) {
  const el = $("#versionNote");
  try {
    const mine = String(chrome.runtime.getManifest?.().version || "");
    const latest = String(health?.extension_latest || "");
    if (!/^\d+(?:\.\d+){0,3}$/.test(mine) || !/^\d+(?:\.\d+){0,3}$/.test(latest)) {
      el.style.display = "none"; return;
    }
    const next = latest.split('.').map(Number), current = mine.split('.').map(Number);
    let order = 0;
    for (let i = 0; i < 4; i++) {
      if ((next[i] || 0) === (current[i] || 0)) continue;
      order = (next[i] || 0) > (current[i] || 0) ? 1 : -1; break;
    }
    if (!order) el.textContent = `扩展 ${mine}：固定目录随桌面更新，升级后刷新视频页。`;
    else if (order < 0) el.textContent = `扩展 ${mine}，桌面 ${latest}。版本不一致，请加载随当前 FoilPage 安装的扩展目录。`;
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
    el.style.display = "";
  } catch { el.style.display = "none"; }
}

// 兜底入口：即使页面内按钮未注入，也可从弹窗对当前标签页发起下载。
// activeTab 权限在用户点击扩展图标时授予，弹窗打开即可读取当前页 URL。
async function detectCurrentPage() {
  const panel = $("#pagePanel");
  panel.hidden = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tab?.url || "";
    const isBili = /^https:\/\/(www\.)?bilibili\.com\/video\//i.test(url);
    const isDouyin = /^https:\/\/(www\.)?douyin\.com\/(video|share\/video)\//i.test(url);
    // YouTube 与 content-youtube.js 的 videoIdFromLocation 保持同一页面集合。
    const isYoutube = /^https:\/\/(www\.|m\.)?youtube\.com\/watch/i.test(url)
      || /^https:\/\/youtu\.be\/[A-Za-z0-9_-]{6,}/i.test(url);
    if (!isBili && !isDouyin && !isYoutube) {
      panel.hidden = false;
      $("#pageDesc").textContent = "打开视频页面，即可下载或转写";
      $("#pageDownload").disabled = true;
      $("#pageTranscribe").disabled = true;
      return;
    }
    $("#pageDownload").disabled = false;
    $("#pageTranscribe").disabled = false;
    panel.hidden = false;
    $("#pageDesc").textContent = tab?.title?.replace(/\s*[-_]\s*(YouTube|哔哩哔哩|抖音).*$/u, "") || (isBili
      ? 'B 站（本地下载）'
      : isYoutube
        ? 'YouTube（本地下载）'
        : '抖音（本地下载）');
    $("#pageState").hidden = true;
    const onPageState = (message, error = false) => {
      const state = $("#pageState");
      state.textContent = message;
      state.hidden = false;
      state.classList.toggle("err", error);
    };
    const submitPage = (transcribe) => {
      const dl = $("#pageDownload");
      const tr = $("#pageTranscribe");
      dl.disabled = true;
      tr.disabled = true;
      onPageState("正在提交…");
      // 抖音必须带页面捕获的直链：无直链的提交会撞平台签名墙必败。
      // B 站走本地 yt-dlp 轨，不需要这一步。
      const begin = (mediaUrl, playUrl = "", mediaCandidates = []) => chrome.runtime.sendMessage({
        type: "shengjian-page-submit",
        url,
        mediaUrl,
        playUrl, mediaCandidates, title: $("#pageDesc").textContent,
        transcribe,
      }, (response) => {
        dl.disabled = false;
        tr.disabled = false;
        if (chrome.runtime.lastError || !response?.ok) {
          onPageState((response?.message) || (chrome.runtime.lastError?.message) || "提交失败", true);
          return;
        }
        onPageState(response.job?.queued ? "已排队，等待 FoilPage" : "已提交下载");
        void loadJobs();
      });
      if (!isDouyin) { begin(""); return; }
      const videoId = url.match(/\/video\/(\d{10,24})/)?.[1] || "";
      chrome.tabs.sendMessage(tab.id, { type: "shengjian-fresh-media", videoId }, (fresh) => {
        if (chrome.runtime.lastError || !fresh?.ok || String(fresh.videoId || "") !== videoId) { begin(""); return; }
        begin(fresh.mediaUrl || "", fresh.playUrl || "", fresh.mediaCandidates || []);
      });
    };
    $("#pageDownload").onclick = () => submitPage(false);
    $("#pageTranscribe").onclick = () => submitPage(true);
  } catch {
    panel.hidden = true;
  }
}

function renderJobs(jobs) {
  const panel = $("#jobsPanel");
  const root = $("#jobs");
  root.replaceChildren();
    panel.hidden = false;
    if (!jobs.length) {
      const empty = document.createElement("p");
      empty.className = "message";
      empty.textContent = "还没有任务，从当前视频开始。";
      root.append(empty);
    }
  const visible = [...jobs.filter(job => !["done","failed","cancelled"].includes(job.status)), ...jobs.filter(job => ["done","failed","cancelled"].includes(job.status))];
  for (const job of visible.slice(0, 5)) {
    const row = document.createElement("article");
    row.className = "job-row";
    const title = document.createElement("div");
    title.className = "job-title";
    title.textContent = job.title || job.url || "未命名视频";
    title.title = title.textContent;
    const status = document.createElement("div");
    status.className = "job-status";
    status.textContent = progressLabelFor(job);
    const track = document.createElement("div");
    track.className = "job-track";
    track.hidden = !['downloading', 'awaiting_browser', 'transcribing', 'running'].includes(job.status);
    const fill = document.createElement("div");
    fill.className = "job-fill";
    fill.style.width = `${Math.round(progressFor(job) * 100)}%`;
    track.append(fill);
    row.append(title, status, track);
    if (!["done","failed","cancelled"].includes(job.status)) {
      const cancel=document.createElement("button");
      cancel.type="button"; cancel.className="job-cancel"; cancel.textContent="取消";
      cancel.setAttribute("aria-label",`取消任务：${title.textContent}`);
      cancel.disabled=Boolean(job.cancel_requested);
      cancel.onclick=async()=>{
        cancel.disabled=true;
        try { await chrome.runtime.sendMessage({type:"shengjian-cancel-task",id:job.id}); await loadJobs(); }
        catch { cancel.disabled=false; }
      };
      row.append(cancel);
    }
    root.append(row);
  }
}

async function loadJobs() {
  try {
    const data = await chrome.runtime.sendMessage({ type: "shengjian-authorized-status" });
    renderJobs(Array.isArray(data?.tasks) ? data.tasks : []);
  } catch {
    $("#jobsPanel").hidden = false;
  }
}

$("#connect").addEventListener("click", async () => {
  const button = $("#connect");
  button.disabled = true;
  try {
    await health();
    await setup();
    await checkConnection();
    $("#message").textContent = "已连接，后台任务将继续执行";
    await chrome.runtime.sendMessage({type:"shengjian-authorized-status"});
  } catch (error) {
    showConnection("error", "未连接");
    $("#message").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#openApp").addEventListener("click", async () => {
  const button = $("#openApp");
  button.disabled = true;
  try {
    const res = await fetch("http://127.0.0.1:18765/v1/show", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: "browser" }),
    });
    if (!res.ok) throw new Error(String(res.status));
    $("#message").textContent = "已在 FoilPage 中打开连接页：点击「允许连接」后回到这里";
  } catch {
    $("#message").textContent = "FoilPage 桌面端未运行：请从开始菜单启动 FoilPage";
  } finally {
    button.disabled = false;
  }
});

$("#refreshJobs").addEventListener("click", loadJobs);
checkConnection();
setInterval(loadJobs, 2500);
