// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import { getToken, request } from "./api.js";
import { browserFallbackMediaUrls } from "./fallback-policy.js";
import { pickDouyinResolveCookies } from "./douyin-cookies.js";

/* 更新检查复用已有消息、心跳与恢复事件，不额外保活。仅在弹窗关闭且
   下载/转写/恢复工作均空闲时尝试重载当前注册目录；无法迁移旧解压路径。 */
const SELF_RELOAD_DEBOUNCE_MS = 60000;
const SELF_UPDATE_CHECK_INTERVAL_MS = 30000;
// 2026-10-20 实机：一条永不消亡的 pending import（桌面端任务已不在、Chrome
// 下载记录已被清理，重试永远失败却永远 remaining）把扩展自更新冻死在旧版
// 本，按钮兜底等修复送不到浏览器。任何任务引用超过一天都已超出真实的
// 下载/导入生命周期，不再作为「活跃工作」拦截自更新。
const SELF_UPDATE_ACTIVE_GRACE_MS = 24 * 60 * 60 * 1000;
let _selfUpdateChecking = false;
let _selfUpdateLastCheckAt = -Infinity;
let extensionOperationsInFlight = 0;
let extensionActivityRevision = 0;

async function withExtensionOperation(operation) {
  extensionOperationsInFlight += 1;
  extensionActivityRevision += 1;
  try { return await operation(); }
  finally {
    extensionOperationsInFlight -= 1;
    void selfUpdateCheck();
  }
}

function hasActiveExtensionWork() {
  return Boolean(extensionOperationsInFlight || localTaskWatchers.size
    || inFlightBrowserDownloads.size || desktopRequestsInFlight.size
    || fallbacksInFlight.size || browserRecoveryRunning
    || [...submissionQueue.values()].some(entry => !isTerminalTask(entry.lastJob?.status || entry.status)));
}

function isRecentTimestamp(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && Date.now() - parsed < SELF_UPDATE_ACTIVE_GRACE_MS;
}

// 存储里的「活跃工作」按新鲜度判断：滞留超过一天的下载/导入/提交引用视为
// 死记录，不得拦截自更新（watched 任务本身随终态清理，保持原语义）。
function hasRecentPersistentWork(saved) {
  return Object.values(saved[DOWNLOAD_JOB_MAP_KEY] || {}).some(meta => isRecentTimestamp(meta?.startedAt))
    || (saved[PENDING_IMPORTS_KEY] || []).some(entry => isRecentTimestamp(entry?.queuedAt))
    || (saved[WATCHED_TASKS_KEY] || []).length
    || (saved[SUBMISSION_QUEUE_KEY] || []).some(entry => !isTerminalTask(entry.lastJob?.status || entry.status)
      && isRecentTimestamp(entry?.createdAt));
}

function isNewerExtensionVersion(latest, current) {
  const valid = value => /^\d+(?:\.\d+){0,3}$/.test(value);
  if (!valid(latest) || !valid(current)) return false;
  const next = latest.split('.').map(Number), mine = current.split('.').map(Number);
  for (let i = 0; i < 4; i += 1) {
    if ((next[i] || 0) !== (mine[i] || 0)) return (next[i] || 0) > (mine[i] || 0);
  }
  return false;
}

async function extensionPopupIsOpen() {
  try {
    if (typeof chrome.runtime.getContexts === 'function') {
      return (await chrome.runtime.getContexts({ contextTypes: ['POPUP'] })).length > 0;
    }
    if (typeof chrome.extension?.getViews === 'function') {
      return chrome.extension.getViews({ type: 'popup' }).length > 0;
    }
  } catch { /* An unknown popup state must not close a visible popup. */ }
  return true;
}

// TASK-8.16 热切换配置入库：配置是纯数据（选择器/开关），不是远程代码；
// 版本单调（旧版本不回写），体积封顶防异常响应撑爆 storage。
const CLIENT_CONFIG_STORAGE_KEY = 'shengjianClientConfig';
const CLIENT_CONFIG_MAX_JSON = 16384;
async function storeClientConfig(entry) {
  if (!entry || typeof entry !== 'object') return false;
  const version = Number(entry.version);
  const config = entry.config;
  if (!Number.isInteger(version) || version < 1 || !config || typeof config !== 'object'
      || Array.isArray(config)) return false;
  if (JSON.stringify({ version, config }).length > CLIENT_CONFIG_MAX_JSON) return false;
  const stored = (await chrome.storage.local.get(CLIENT_CONFIG_STORAGE_KEY))[CLIENT_CONFIG_STORAGE_KEY];
  if (stored && Number(stored.version) >= version) return false;
  await chrome.storage.local.set({ [CLIENT_CONFIG_STORAGE_KEY]: { version, config } });
  return true;
}

async function selfUpdateCheck() {
  const now = Date.now();
  if (_selfUpdateChecking || now - _selfUpdateLastCheckAt < SELF_UPDATE_CHECK_INTERVAL_MS) return false;
  if (hasActiveExtensionWork()) return false;
  _selfUpdateChecking = true;
  _selfUpdateLastCheckAt = now;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const res = await fetch("http://127.0.0.1:18765/health", { signal: controller.signal, cache: 'no-store' });
    if (res.ok === false) return false;
    const data = await res.json();
    // 热切换配置（TASK-8.16）：桌面已在服务端验签后透传，这里只做形状/
    // 大小/版本门控再入库；失败绝不影响自更新判定。内容脚本经
    // chrome.storage 的 shengjianClientConfig 读取（8.17 封面 / 8.18 图集消费）。
    if (data?.ok) void storeClientConfig(data.client_config).catch(() => {});
    const mine = chrome.runtime.getManifest?.().version || "";
    const latest = String(data?.extension_latest || '');
    if (!data?.ok || !isNewerExtensionVersion(latest, mine)) return false;
    if (hasActiveExtensionWork() || await extensionPopupIsOpen()) return false;
    const saved = await chrome.storage.local.get([
      'shengjianSelfReloadAt', 'shengjianSelfReloadTarget',
      DOWNLOAD_JOB_MAP_KEY, PENDING_IMPORTS_KEY, WATCHED_TASKS_KEY, SUBMISSION_QUEUE_KEY,
    ]);
    if (hasActiveExtensionWork() || hasRecentPersistentWork(saved)) return false;
    const previousAt = Number(saved.shengjianSelfReloadAt) || 0;
    const previousTarget = saved.shengjianSelfReloadTarget || null;
    // A stale unpacked directory cannot be updated by repeated reloads. One
    // attempt per running/installed version pair leaves manual reload available.
    if (previousTarget?.from === mine && previousTarget?.to === latest) return false;
    if (now - previousAt < SELF_RELOAD_DEBOUNCE_MS) return false;
    const revision = extensionActivityRevision;
    const rollback = () => chrome.storage.local.set({
      shengjianSelfReloadAt: previousAt, shengjianSelfReloadTarget: previousTarget,
    });
    await chrome.storage.local.set({
      shengjianSelfReloadAt: now, shengjianSelfReloadTarget: { from: mine, to: latest },
    });
    // A submission or recovery can start during the asynchronous storage write.
    if (await extensionPopupIsOpen() || hasActiveExtensionWork() || revision !== extensionActivityRevision) {
      await rollback();
      return false;
    }
    try {
      chrome.runtime.reload();
      return true;
    } catch {
      await rollback();
      return false;
    }
  } catch {
    // Offline, blocked popup or debounce waits are retryable on a later event.
    return false;
  } finally {
    clearTimeout(timer);
    _selfUpdateChecking = false;
  }
}
void Promise.resolve().then(selfUpdateCheck);

const DOUYIN_DOMAIN = "douyin.com";
const DESKTOP_EVENTS_URL = "ws://127.0.0.1:18765/v1/extension-events";
const DESKTOP_EVENTS_PROTOCOL = "shengjian-v1";
const BROWSER_DOWNLOAD_SUBDIR = "FoilPage";
const BROWSER_DOWNLOAD_LOOKUP_GRACE_MS = 10000;
const DOWNLOAD_JOB_MAP_KEY = "shengjianBrowserDownloadJobs";
const PENDING_IMPORTS_KEY = "shengjianPendingImports";
const PENDING_FAILURE_REPORTS_KEY = "shengjianPendingFailureReports";
const FAILURE_REPORT_ALARM = "shengjianFailureReports";
const SUBMISSION_QUEUE_KEY = "shengjianSubmissionQueue";
const submissionQueue = new Map();
const queuedMediaHints = new Map(); // Signed URLs are never written to storage.
let submissionQueueLoadPromise = null;
let submissionQueueFlushPromise = null;
let submissionQueueSaveLock = Promise.resolve();
const localTaskWatchers = new Map();
// chrome.downloads.onChanged may fire in the same turn as the download()
// callback, before the async storage write below has completed. Keep a short
// lived in-memory copy so an immediate interrupt can still be linked to the
// desktop task instead of leaving it stuck in awaiting_browser.
const inFlightBrowserDownloads = new Map();
const consumedBrowserDownloads = new Set();
const desktopRequestsInFlight = new Set();
let desktopEventsSocket = null;
let desktopEventsReconnectTimer = null;
let failureQueueLock = Promise.resolve();
let failureFlushPromise = null;
let pendingImportFlushPromise = null;
const browserInterruptsInFlight = new Set();

function withFailureQueueLock(operation) {
  const result = failureQueueLock.then(operation, operation);
  failureQueueLock = result.catch(() => {});
  return result;
}

function safeFailurePageUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    const host = parsed.hostname.toLowerCase();
    const allowed = ["douyin.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be"]
      .some((domain) => host === domain || host.endsWith(`.${domain}`));
    if (!allowed || !["http:", "https:"].includes(parsed.protocol)) return "";
    // Store only the source page path. Query strings can contain tracking data
    // and signed media URLs; they are not needed to explain a failed attempt.
    const videoId = host.endsWith("youtube.com") ? parsed.searchParams.get("v") : "";
    parsed.search = /^[A-Za-z0-9_-]{6,}$/.test(videoId || "") ? `?v=${videoId}` : "";
    parsed.hash = "";
    return parsed.href.slice(0, 4096);
  } catch {
    return "";
  }
}

function failureEventId() {
  const value = globalThis.crypto?.randomUUID?.();
  if (value) return value.replace(/-/g, "").toLowerCase();
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

function safeFailureText(value, limit = 500) {
  return String(value || "")
    .replace(/https?:\/\/[^\s<>"']+/gi, "[URL 已脱敏]")
    .replace(/(cookie|authorization|token|signature|x-bogus|mstoken|verifyfp)\s*[:=]\s*[^\s,;&]+/gi, "$1=[已脱敏]")
    .replace(/[\x00-\x1f\x7f]/g, " ")
    .trim().slice(0, limit);
}

function compactFailureDiagnostic(value) {
  const input = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const result = {};
  for (const key of ["http_status", "response_status", "bytes_received", "total_bytes", "elapsed_ms", "candidate_count", "attempt"]) {
    const number = Number(input[key]);
    if (Number.isSafeInteger(number) && number >= 0) result[key] = number;
  }
  for (const key of ["browser_error", "error_name", "resolver_code", "fallback_code", "extension_version"]) {
    if (input[key] != null) result[key] = safeFailureText(input[key], 80);
  }
  for (const key of ["browser_detail", "resolver_detail", "fallback_detail"]) {
    if (input[key] != null) result[key] = safeFailureText(input[key], 500);
  }
  return result;
}

async function flushPendingFailureReports() {
  if (failureFlushPromise) return failureFlushPromise;
  failureFlushPromise = (async () => {
    const stored = await withFailureQueueLock(async () => {
      const data = await chrome.storage.local.get(PENDING_FAILURE_REPORTS_KEY);
      return Array.isArray(data[PENDING_FAILURE_REPORTS_KEY])
        ? data[PENDING_FAILURE_REPORTS_KEY].slice(-50) : [];
    });
    for (const report of stored) {
      try {
        await request("/v1/downloads/failures", {
          method: "POST", body: JSON.stringify(report),
        });
        await withFailureQueueLock(async () => {
          const data = await chrome.storage.local.get(PENDING_FAILURE_REPORTS_KEY);
          const pending = Array.isArray(data[PENDING_FAILURE_REPORTS_KEY])
            ? data[PENDING_FAILURE_REPORTS_KEY] : [];
          await chrome.storage.local.set({
            [PENDING_FAILURE_REPORTS_KEY]: pending.filter((entry) => entry?.event_id !== report.event_id),
          });
        });
      } catch {
        // Keep the event in extension storage; the next alarm/startup retries it.
        break;
      }
    }
  })().catch(() => {}).finally(() => { failureFlushPromise = null; });
  return failureFlushPromise;
}

async function reportDownloadFailure(input = {}) {
  const url = safeFailurePageUrl(input.url);
  if (!url) return false;
  const report = {
    event_id: /^[a-f0-9]{32}$/.test(String(input.event_id || ""))
      ? String(input.event_id).toLowerCase() : failureEventId(),
    task_id: /^[a-f0-9]{12}$/.test(String(input.task_id || "")) ? String(input.task_id) : "",
    url,
    title: String(input.title || "").replace(/[\x00-\x1f\x7f]/g, " ").trim().slice(0, 240),
    phase: ["desktop_dispatch", "desktop_authorization", "extension_authorization", "local_api_submit", "browser_download_start", "browser_download_interrupted", "browser_download_import", "resolver_fallback"].includes(String(input.phase))
      ? String(input.phase) : "local_api_submit",
    channel: ["desktop_bridge", "browser_extension", "chrome.downloads", "local_api"].includes(String(input.channel))
      ? String(input.channel) : "browser_extension",
    code: String(input.code || "extension_error").replace(/[^a-zA-Z0-9_]/g, "_").slice(0, 64),
    message: safeFailureText(input.message || "扩展未能完成视频下载。", 500),
    retryable: Boolean(input.retryable),
    diagnostic: compactFailureDiagnostic(input.diagnostic),
  };
  let queued = false;
  try {
    await withFailureQueueLock(async () => {
      const data = await chrome.storage.local.get(PENDING_FAILURE_REPORTS_KEY);
      const pending = Array.isArray(data[PENDING_FAILURE_REPORTS_KEY])
        ? data[PENDING_FAILURE_REPORTS_KEY] : [];
      if (!pending.some((entry) => entry?.event_id === report.event_id)) pending.push(report);
      await chrome.storage.local.set({ [PENDING_FAILURE_REPORTS_KEY]: pending.slice(-50) });
    });
    queued = true;
  } catch {
    // Try the app directly if extension storage itself is temporarily unavailable.
  }
  if (queued) void flushPendingFailureReports();
  else {
    try {
      await request("/v1/downloads/failures", {
        method: "POST", body: JSON.stringify(report),
      });
    } catch { /* Caller still receives the immediate extension error. */ }
  }
  return true;
}

chrome.alarms?.create?.(FAILURE_REPORT_ALARM, { periodInMinutes: 1 });
chrome.alarms?.onAlarm?.addListener?.((alarm) => {
  if (alarm?.name === FAILURE_REPORT_ALARM) void flushPendingFailureReports();
});
chrome.runtime?.onStartup?.addListener?.(() => void flushPendingFailureReports());
chrome.runtime?.onInstalled?.addListener?.(() => void flushPendingFailureReports());
void flushPendingFailureReports();

function validVideoId(value) {
  return /^\d{10,24}$/.test(String(value || ""));
}

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function canonicalDetailUrl(videoId) {
  return `https://www.douyin.com/video/${videoId}`;
}

function normalizeSessionCookie(cookie) {
  const domain = String(cookie?.domain || "").toLowerCase();
  if (!(domain === DOUYIN_DOMAIN || domain.endsWith(`.${DOUYIN_DOMAIN}`)) || !cookie?.name || !cookie?.value) return null;
  return {
    name: String(cookie.name),
    value: String(cookie.value),
    domain,
    path: String(cookie.path || "/"),
    secure: Boolean(cookie.secure),
    expirationDate: Number.isFinite(cookie.expirationDate) ? cookie.expirationDate : null,
  };
}

async function oneShotDouyinSession() {
  const cookies = await withTimeout(
    chrome.cookies.getAll({ domain: DOUYIN_DOMAIN }),
    10000,
    "读取抖音登录授权超时，请刷新页面后重试"
  );
  // 按名收窄（00-待办 #5）：只交出解析所需的登录/风控 Cookie，会话长度
  // 以名单为上限，不再携带整个 .douyin.com 域的存储。
  const session = pickDouyinResolveCookies(cookies).map(normalizeSessionCookie).filter(Boolean);
  if (!session.length) {
    throw new Error("未取得抖音登录授权；请在当前抖音页面登录后重试");
  }
  return session;
}

async function handleDesktopDouyinDownload(message) {
  const requestId = String(message?.request_id || "");
  const url = String(message?.url || "").trim().slice(0, 4096);
  if (!/^[a-f0-9]{32}$/.test(requestId) || !isDouyinWebUrl(url)
      || desktopRequestsInFlight.has(requestId)) return;
  desktopRequestsInFlight.add(requestId);
  let claimed = false;
  let session = null;
  let phase = "desktop_dispatch";
  try {
    // Claim before reading credentials. A reconnect or a second paired profile
    // cannot trigger another cookie read for the same desktop click.
    await request(`/v1/desktop-downloads/${requestId}/claim`, {
      method: "POST", body: "{}",
    });
    claimed = true;
    phase = "extension_authorization";
    session = await oneShotDouyinSession();
    phase = "local_api_submit";
    const result = await request(`/v1/desktop-downloads/${requestId}`, {
      method: "POST",
      body: JSON.stringify({
        browser_session: session,
        user_agent: String(globalThis.navigator?.userAgent || "").slice(0, 512),
      }),
    });
    const job = result?.job;
    if (!job?.id) throw new Error("FoilPage 没有返回下载任务");
    notifyIfDeduplicated(job);
    watchLocalTask(job);
  } catch (error) {
    const title = String(message?.title || "").trim().slice(0, 240);
    const detail = String(error?.detail || error?.message || error?.name || "未知扩展错误").slice(0, 500);
    const report = {
      event_id: requestId,
      url,
      title,
      phase,
      channel: "browser_extension",
      code: session ? "extension_error" : "douyin_session_unavailable",
      message: detail,
      diagnostic: {
        error_name: String(error?.name || "Error").slice(0, 80),
        http_status: Number(error?.status) || undefined,
        resolver_code: String(error?.code || "").slice(0, 80),
        extension_version: String(chrome.runtime.getManifest?.().version || "").slice(0, 80),
      },
    };
    if (claimed) {
      try {
        await request(`/v1/desktop-downloads/${requestId}/failure`, {
          method: "POST",
          body: JSON.stringify({
            code: report.code,
            phase,
            detail,
            error_name: report.diagnostic.error_name,
            extension_version: report.diagnostic.extension_version,
          }),
        });
        return;
      } catch {
        // Fall through to the durable extension queue if the broker cannot write.
      }
    }
    await reportDownloadFailure(report);
    console.warn("[FoilPage] 桌面抖音链接授权未能完成");
  } finally {
    if (session) session.length = 0;
    desktopRequestsInFlight.delete(requestId);
  }
}

let desktopEventsConnectPromise = null;
let desktopEventsReconnectDelay = 5000;
let desktopConnectionState = "offline";
function isTemporaryOffline(error) {
  return ["local_api_unavailable", "local_api_timeout"].includes(error?.code)
    || Number(error?.status) >= 500;
}
function scheduleDesktopEventsReconnect() {
  if (desktopEventsReconnectTimer) return;
  const delay = desktopEventsReconnectDelay;
  desktopEventsReconnectDelay = Math.min(60000, delay * 2);
  desktopEventsReconnectTimer = setTimeout(() => {
    desktopEventsReconnectTimer = null;
    void connectDesktopEvents().catch(() => {});
  }, delay);
  desktopEventsReconnectTimer?.unref?.();
}
async function connectDesktopEvents() {
  if (typeof WebSocket !== "function") return;
  if (desktopEventsConnectPromise) return desktopEventsConnectPromise;
  if (desktopEventsSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(desktopEventsSocket.readyState)) return;
  desktopEventsConnectPromise = (async () => {
    const token = await getToken().catch(() => "");
    if (!token) { desktopConnectionState = "unpaired"; return; }
    try {
      // HTTP preflight prevents repeated WebSocket connection-refused errors
      // while the desktop app is closed. A race with shutdown is still handled.
      await request("/v1/downloads?limit=1");
      desktopConnectionState = "connecting";
      const socket = new WebSocket(DESKTOP_EVENTS_URL, [DESKTOP_EVENTS_PROTOCOL, token]);
      desktopEventsSocket = socket;
      socket.onopen = () => {
        desktopConnectionState = "online";
        desktopEventsReconnectDelay = 5000;
        if (desktopEventsReconnectTimer) clearTimeout(desktopEventsReconnectTimer);
        desktopEventsReconnectTimer = null;
        void resumeBackgroundQueue().catch(() => {});
      };
      socket.onmessage = (event) => {
        let message;
        try { message = JSON.parse(String(event.data || "")); } catch { return; }
        if (message?.type === "desktop-douyin-download") {
          void handleDesktopDouyinDownload(message).catch(() => {});
        } else if (message?.type === "heartbeat" && socket.readyState === WebSocket.OPEN) {
          desktopConnectionState = "online";
          desktopEventsReconnectDelay = 5000;
          try { socket.send('{"type":"heartbeat"}'); } catch { socket.close(); }
          void resumeBackgroundQueue().catch(() => {});
          void selfUpdateCheck();
        }
      };
      socket.onclose = () => {
        if (desktopEventsSocket === socket) desktopEventsSocket = null;
        desktopConnectionState = "offline";
        scheduleDesktopEventsReconnect();
      };
      socket.onerror = () => {
        desktopConnectionState = "offline";
        try { socket.close(); } catch { scheduleDesktopEventsReconnect(); }
      };
    } catch (error) {
      desktopEventsSocket = null;
      desktopConnectionState = isTemporaryOffline(error) ? "offline" : "unpaired";
      if (isTemporaryOffline(error)) scheduleDesktopEventsReconnect();
    }
  })();
  try { return await desktopEventsConnectPromise; }
  finally { desktopEventsConnectPromise = null; }
}

function isDouyinWebUrl(value) {
  try {
    const host = new URL(String(value || "")).hostname.toLowerCase();
    return host === DOUYIN_DOMAIN || host.endsWith(`.${DOUYIN_DOMAIN}`);
  } catch {
    return false;
  }
}

void connectDesktopEvents();
chrome.storage?.onChanged?.addListener?.((changes, area) => {
  if (area === "local" && changes.localApiToken) void connectDesktopEvents();
});

async function performAuthorizedDownload(message) {
  const videoId = String(message?.videoId || "");
  if (!validVideoId(videoId)) throw new Error("未识别当前视频");
  const detailUrl = canonicalDetailUrl(videoId);
  const mediaUrl = String(message?.mediaUrl || "").trim().slice(0, 4096);
  const userAgent = String(message?.userAgent || globalThis.navigator?.userAgent || "").slice(0, 512);
  const title = String(message?.title || "").trim().slice(0, 240);
  console.log("[FoilPage] 提交浏览器下载，videoId", videoId);
  // Least privilege: a usable player media hint is handed straight to
  // chrome.downloads and never reaches the yt-dlp resolve path, so that path must
  // not collect Douyin cookies at all. Cookies are only gathered when the extension
  // could not observe a media address and the backend has to resolve it itself.
  let session = null;
  let job = null;
  let phase = "extension_authorization";
  try {
    session = mediaUrl ? null : await oneShotDouyinSession();
    if (session) console.log("[FoilPage] 已取得抖音授权 Cookie 数量", session.length);
    phase = "local_api_submit";
    const payload = {
      url: detailUrl,
      title,
      client_request_id: message.clientRequestId,
      mode: "browser",
      media_url: mediaUrl,
      media_candidates: Array.isArray(message?.mediaCandidates) ? message.mediaCandidates.slice(0, 8) : [],
      // TASK-8.17：封面地址由桌面做白名单复核，非法即静默丢弃（增强项）。
      cover_url: String(message?.coverUrl || "").trim().slice(0, 4096),
      options: {
        quality: "best-available",
        audio_only: false,
        transcribe_after_download: Boolean(message?.transcribe),
        browser_fallback: true,
        language: "zh",
        referer: detailUrl,
        user_agent: userAgent,
      },
    };
    if (session) payload.browser_session = session;
    const result = await request("/v1/downloads", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    job = result.job || null;
    console.log("[FoilPage] 浏览器下载任务已创建", job?.id || "");
    await acceptQueuedJob(message.clientRequestId, job);
    if (submissionQueue.get(message.clientRequestId)?.cancelRequested) {
      const result = await request("/v1/downloads", {method:"POST",body:JSON.stringify({cancel_request_id:message.clientRequestId})});
      if (result.job) { await updateQueuedTask(result.job); watchLocalTask(result.job); return result.job; }
      return job;
    }
    rememberSubmitMedia(job?.id, message);
    // The resolver can fail synchronously while the desktop service still
    // creates the durable row. Treat that row as the result of this click;
    // do not submit a second, detached failure report for the same incident.
    if (job?.status === "failed" || job?.error?.diagnostic?.fallback_pending) {
      watchLocalTask(job);
      return job;
    }
    const direct = job?.browser_download;
    if (!direct?.download_url && job?.id) { watchLocalTask(job); return job; }
    if (!direct?.download_url) {
      const error = new Error("FoilPage 未返回可直接下载的视频地址");
      error.code = "browser_download_url_missing";
      throw error;
    }
    phase = "browser_download_start";
    await startBrowserDownload(job, direct, Boolean(message?.transcribe), {
      videoId,
      title: String(message?.title || "").trim().slice(0, 240),
      userAgent,
      recoveryToYtDlp: message?.recoveryToYtDlp !== false,
    });
    watchLocalTask(job);
    return job;
  } catch (error) {
    if (["login_required"].includes(error?.code)) throw error;
    if (isTemporaryOffline(error)) { if (job?.id) { watchLocalTask(job); return job; } throw error; }
    if (job?.id && phase === "browser_download_start") {
      const result = await request(`/v1/downloads/${job.id}/browser-failure`, {
        method: "POST", body: JSON.stringify({ phase, browser_error: "BROWSER_INTERRUPTED_UNKNOWN", browser_detail: safeFailureText(error?.message || "浏览器未能启动下载") }),
      }).catch(() => null);
      if (result?.job) { watchLocalTask(result.job); return result.job; }
    }
    await reportDownloadFailure({
      task_id: job?.id,
      url: detailUrl,
      title,
      phase,
      channel: phase === "browser_download_start" ? "chrome.downloads" : "browser_extension",
      code: error?.code || (phase === "browser_download_start" ? "browser_download_start_failed" : "extension_error"),
      message: error?.detail || error?.message || "扩展未能提交下载任务。",
      diagnostic: {
        error_name: String(error?.name || "Error"),
        http_status: Number(error?.status) || undefined,
        resolver_code: String(error?.code || ""),
        browser_detail: phase === "browser_download_start" ? String(error?.detail || error?.message || "") : "",
        extension_version: String(chrome.runtime.getManifest?.().version || ""),
      },
    });
    throw error;
  } finally {
    // Do not retain the per-click credential set in extension memory.
    if (session) session.length = 0;
  }
}

/* 去重感知通知：0.12.12 起后端对同 URL 活跃任务直接复用。返回的 job
   若早于本次点击数秒即命中去重——补一条「已合并」提示，让多次点击的
   通知呈现是「合并进既有任务」而不是重复的「已加入」。 */
function notifyIfDeduplicated(job) {
  try {
    const requestedAt = Date.parse(String(job?.requested_at || "").replace(" ", "T"));
    if (!Number.isFinite(requestedAt)) return;
    if (Date.now() - requestedAt > 4000) {
      void notifyTabs("该视频已在处理中，本次点击已合并（可在 FoilPage 任务页查看进度）");
    }
  } catch { /* 时间解析失败即静默 */ }
}

function safeRelativeFilename(value) {
  const cleaned = String(value || "")
    .replace(/[\\/]/g, "_")
    .replace(/[<>:"|?*\x00-\x1f]/g, "_")
    .trim();
  // 截断只作用标题主体，媒体后缀永远拼回：固定长度 slice 会把超长中文
  // 标题的 ".mp4" 一起切掉（2026-10-04/11 实机各留下一个无后缀残留，
  // 导入被永久拒绝并在待导入队列里无限重试）。主体 80 字符足以辨识。
  const known = /\.(mp4|mkv|webm|mov|m4a|mp3|wav|ogg)$/i.exec(cleaned);
  const stem = (known ? cleaned.slice(0, cleaned.length - known[0].length) : cleaned).trim();
  const suffix = known ? known[0] : ".mp4";
  return (stem.slice(0, 80).trim() || "douyin-video") + suffix;
}

function browserDownload(options) {
  return new Promise((resolve, reject) => {
    chrome.downloads.download(options, (downloadId) => {
      if (chrome.runtime.lastError) {
        const error = new Error(chrome.runtime.lastError.message || "Chrome 未能启动下载");
        error.code = "browser_download_start_failed";
        error.name = "ChromeDownloadError";
        reject(error);
        return;
      }
      resolve(downloadId);
    });
  });
}

function searchBrowserDownload(downloadId) {
  return new Promise((resolve) => {
    chrome.downloads.search({ id: downloadId }, (items) => {
      if (chrome.runtime.lastError) {
        // Undefined means lookup unavailable; null means a successful lookup
        // confirmed there is no longer a record for this exact download ID.
        resolve(undefined);
        return;
      }
      resolve((items || [])[0] || null);
    });
  });
}

async function rememberBrowserDownload(downloadId, meta) {
  const key = String(downloadId);
  inFlightBrowserDownloads.set(key, meta);
  await withFailureQueueLock(async () => {
    const data = await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
    const map = data[DOWNLOAD_JOB_MAP_KEY] || {};
    if (consumedBrowserDownloads.has(key)) {
      inFlightBrowserDownloads.delete(key);
      delete map[key];
    } else map[key] = { ...meta, startedAt: Date.now() };
    await chrome.storage.local.set({ [DOWNLOAD_JOB_MAP_KEY]: map });
  });
  // Chrome may finish before its callback has delivered the download ID. A
  // missed early event does not consume an unknown map or import other files.
  if (!consumedBrowserDownloads.has(key)) {
    const item = await searchBrowserDownload(downloadId);
    if (item?.state === "complete") await handleBrowserDownloadComplete(downloadId);
    else if (item?.state === "interrupted") await handleBrowserDownloadInterrupted(downloadId);
  }
}
async function takeBrowserDownload(downloadId) {
  const key = String(downloadId);
  return withFailureQueueLock(async () => {
    const data = await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
    const map = data[DOWNLOAD_JOB_MAP_KEY] || {};
    const meta = map[key] || inFlightBrowserDownloads.get(key) || null;
    if (!meta) return null;
    inFlightBrowserDownloads.delete(key);
    consumedBrowserDownloads.add(key);
    setTimeout(() => consumedBrowserDownloads.delete(key), 30000);
    delete map[key];
    await chrome.storage.local.set({ [DOWNLOAD_JOB_MAP_KEY]: map });
    return meta;
  });
}

async function startBrowserDownload(job, direct, transcribe, recovery = {}) {
  // chrome.downloads.download rejects XHR-forbidden headers such as Referer
  // and User-Agent; keep only the headers the downloads API actually accepts.
  const headers = Object.entries(direct.headers || {})
    .filter(([name]) => String(name).toLowerCase() === "accept")
    .map(([name, value]) => ({ name, value: String(value) }));
  const filename = safeRelativeFilename(direct.filename);
  const downloadId = await browserDownload({
    url: direct.download_url,
    filename: `${BROWSER_DOWNLOAD_SUBDIR}/${filename}`,
    headers,
    conflictAction: "uniquify",
    saveAs: false,
  });
  await rememberBrowserDownload(downloadId, {
    taskId: String(job?.id || ""),
    title: String(job?.title || ""),
    transcribe,
    videoId: String(recovery.videoId || ""),
    userAgent: String(recovery.userAgent || "").slice(0, 512),
    recoveryToYtDlp: Boolean(recovery.recoveryToYtDlp),
  });
  return downloadId;
}

async function importBrowserFile(path, meta) {
  const body = {
    path,
    title: meta?.title || "",
    transcribe: Boolean(meta?.transcribe),
  };
  if (meta?.taskId) { body.download_id = meta.taskId; body.browser_download = true; }
  await request("/v1/imports", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function queuePendingImport(downloadId, meta, reason = "") {
  await withFailureQueueLock(async () => {
    const data = await chrome.storage.local.get(PENDING_IMPORTS_KEY);
    const pending = data[PENDING_IMPORTS_KEY] || [];
    if (!pending.some(entry => entry.downloadId === downloadId)) {
      pending.push({downloadId,taskId:meta?.taskId || "",title:meta?.title || "",transcribe:Boolean(meta?.transcribe),reason:safeFailureText(reason,120),queuedAt:Date.now()});
      await chrome.storage.local.set({[PENDING_IMPORTS_KEY]:pending});
    }
  });
}
async function flushPendingImports() {
  if (pendingImportFlushPromise) return pendingImportFlushPromise;
  pendingImportFlushPromise = (async () => {
    const data = await chrome.storage.local.get(PENDING_IMPORTS_KEY);
    const pending = data[PENDING_IMPORTS_KEY] || [];
    if (!pending.length) return;
    const remaining = new Set();
    for (const entry of pending) {
      // Age alone cannot prove that a completed file is gone: reconnecting
      // after a day must still import it. Only drain stale missing records.
      const queuedAt = Number(entry?.queuedAt);
      const stale = Number.isFinite(queuedAt) && Date.now() - queuedAt >= SELF_UPDATE_ACTIVE_GRACE_MS;
      try {
        if (entry.taskId) {
          let job = null;
          try {
            job = (await request(`/v1/downloads/${entry.taskId}`)).job;
          } catch (lookupError) {
            // 桌面端已不认识这个任务（清理/重装后的正常现象）：导入对象不复
            // 存在，立即丢弃，不必等一天。
            if (String(lookupError?.code) === "not_found" || Number(lookupError?.status) === 404) continue;
            throw lookupError;
          }
          if (job) await updateQueuedTask(job);
          // The owned Chrome transfer can finish after every backend fallback
          // has failed. The desktop accepts that same task's late success.
          // Cancellation and already imported/completed tasks remain final.
          if (job && !["awaiting_browser", "failed"].includes(job.status)) continue;
        }
        const items = await chrome.downloads.search({id:entry.downloadId});
        const item = items[0];
        if (!item?.filename) { if (!stale) remaining.add(entry.downloadId); continue; }
        await importBrowserFile(item.filename,{taskId:entry.taskId,title:entry.title,transcribe:entry.transcribe});
        if (entry.taskId) {
          const {job} = await request(`/v1/downloads/${entry.taskId}`);
          if (job) { await updateQueuedTask(job); watchLocalTask(job); }
        }
      } catch (error) {
        if (error?.code === "media_not_video" && entry.taskId) watchLocalTask({id:entry.taskId});
        else if (String(error?.code) === "unsupported_media_format" || Number(error?.status) === 415) {
          // 无扩展名/不支持的文件重试不可能成功：有任务的先上报终态失败
          // 再丢弃 entry，无任务的脏 entry 直接丢弃——不再无限重试。
          if (entry.taskId) {
            try {
              const result = await request(`/v1/downloads/${entry.taskId}/browser-failure`, {
                method: "POST",
                body: JSON.stringify({
                  browser_error: "UNSUPPORTED_MEDIA_FORMAT",
                  phase: "browser_download_import",
                  bytes_received: null, total_bytes: null,
                }),
              });
              if (result?.job) watchLocalTask(result.job);
            } catch {
              // The import is permanently invalid, but the desktop still owns
              // a live task until it acknowledges the terminal report.
              remaining.add(entry.downloadId);
            }
          }
        }
        else remaining.add(entry.downloadId);
      }
    }
    await withFailureQueueLock(async () => {
      const latest = await chrome.storage.local.get(PENDING_IMPORTS_KEY);
      const examined = new Set(pending.map(entry=>entry.downloadId));
      const next = (latest[PENDING_IMPORTS_KEY] || []).filter(entry=>!examined.has(entry.downloadId)||remaining.has(entry.downloadId));
      await chrome.storage.local.set({[PENDING_IMPORTS_KEY]:next});
    });
  })();
  try { return await pendingImportFlushPromise; }
  finally { pendingImportFlushPromise = null; }
}

async function notifyTabs(message, error = false, job = null, stage = "progress") {
  // Douyin page feedback is limited to the accepted receipt and the completed
  // video. Progress, fallback errors and ASR results remain in the task views.
  // 例外（2026-10-14 实机）：带任务 ID 的终态失败必须让发起页知道一次——
  // 沉默失败会让用户以为正在下载（resolver 被平台校验拦截时连续发生）。
  const submissionRejected = stage === "failure" && !/^[a-f0-9]{12}$/.test(String(job?.id || ""));
  const terminalTaskFailure = stage === "failure" && /^[a-f0-9]{12}$/.test(String(job?.id || ""));
  // "fallback"（2026-10-18 先生要求）：首错后自动换道时给发起页一次安抚——
  // 不算进度回执，也不参与 receipt ack。
  if (isDouyinPageUrl(job?.url) && !["submitted", "download", "fallback"].includes(stage) && !submissionRejected && !terminalTaskFailure) return;
  const tabs = await chrome.tabs.query({
    url: [
      "https://*.douyin.com/*", "https://douyin.com/*",
      "https://*.bilibili.com/*", "https://bilibili.com/*",
      "https://www.youtube.com/*", "https://m.youtube.com/*", "https://youtu.be/*",
    ],
  }).catch(() => []);
  let acknowledged = false;
  const requiresReceiptAck = isDouyinPageUrl(job?.url) && ["submitted", "download"].includes(stage);
  for (const tab of tabs) {
    try {
      const response = await chrome.tabs.sendMessage(tab.id, {
        type: "shengjian-toast",
        message,
        error,
        taskUrl: job?.url || "",
        taskId: job?.id || "",
        sourceTitle: job?.title || "",
        stage,
      });
      if (response?.ok === true || !requiresReceiptAck) acknowledged = true;
    } catch {
      // The tab may have navigated or the content script may not be ready.
    }
  }
  return acknowledged;
}

function isDouyinPageUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    const host = parsed.hostname.toLowerCase();
    return (host === "douyin.com" || host.endsWith(".douyin.com"))
      && /\/(?:video|share\/video)\//i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function transcriptionTerminal(job) {
  return {
    message: safeFailureText(job?.transcription_label || (job?.status === "failed" ? "转写失败" : "转写成功"), 120),
    error: job?.status === "failed" || job?.transcription_error === true,
  };
}

function localTaskProgressMessage(job) {
  const status = String(job?.status || "");
  const percent = Math.max(0, Math.min(100, Math.round(Number(job?.progress || 0) * 100)));
  if (status === "done") return job?.job_id ? transcriptionTerminal(job).message : "下载成功";
  if (status === "failed") return '下载失败，请在 FoilPage 查看';
  if (status === "cancelled") return '任务已取消';
  if (status === "downloading") return `正在下载 ${percent}%`;
  if (["imported", "queued", "transcribing"].includes(status)) return '正在转写';
  if (["received", "resolving"].includes(status)) return '正在解析';
  if (status === "awaiting_browser") return job?.error?.diagnostic?.fallback_pending ? "正在下载" : `浏览器下载中 ${percent}%`;
  return '正在处理';
}

// 提交时短暂记住 media_url：失败回退时优先复用（play 端点带 Cookie 即可
// 成功），取不到新直链时不至于只能提示回页面。仅进程内存，不落盘。
const submitMediaHints = new Map();
function rememberSubmitMedia(taskId, message) {
  if (!taskId) return;
  submitMediaHints.set(String(taskId), {
    mediaUrl: String(message?.mediaUrl || ""),
    mediaCandidates: Array.isArray(message?.mediaCandidates) ? message.mediaCandidates.slice(0, 8) : [],
    playUrl: String(message?.playUrl || ""),
  });
}

function askTabForFreshMedia(tabId, expectedVideoId = "") {
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(tabId, { type: "shengjian-fresh-media", videoId: expectedVideoId }, (response) => {
        // 只有媒体地址时可用；只有 play 端点时同样有用（回退序会用到）；
        // 图集应答带整组图片地址（TASK-8.18），三者皆空才算无效。
        if (chrome.runtime.lastError || !response?.ok
            || (!response?.mediaUrl && !response?.playUrl && !Array.isArray(response?.gallery))) { resolve(null); return; }
        resolve(response);
      });
    } catch {
      resolve(null);
    }
  });
}

function askActiveTabForFreshMedia() {
  // 无 tabs 权限读不到 tab.url：直接向当前活动标签提问，
  // 由抖音 content script 的响应判定（非抖音页无接收者，返回 null）。
  return new Promise((resolve) => {
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = (tabs || []).find((t) => typeof t?.id === "number");
        if (!tab) { resolve(null); return; }
        askTabForFreshMedia(tab.id).then(resolve);
      });
    } catch {
      resolve(null);
    }
  });
}

async function askTabsForFreshMedia(expectedVideoId) {
  // host_permissions 覆盖 douyin.com，因此可以按 URL 匹配该站标签（无需
  // tabs 权限即可见这些标签的 url）。目标视频页未必是当前活动标签（弹窗
  // 提交、多窗口、页面在后台），先逐个询问所有抖音标签，取第一个
  // videoId 匹配的应答；全部落空再退回原活动标签行为。
  if (expectedVideoId) {
    try {
      const tabs = await new Promise((resolve) => {
        chrome.tabs.query({ url: ["https://*.douyin.com/*"] }, (result) => resolve(result));
      });
      for (const tab of (Array.isArray(tabs) ? tabs : []).slice(0, 8)) {
        if (typeof tab?.id !== "number") continue;
        const response = await askTabForFreshMedia(tab.id, expectedVideoId);
        if (response && String(response.videoId || "") === String(expectedVideoId)) {
          return response;
        }
      }
    } catch { /* 查询失败退回活动标签 */ }
  }
  return expectedVideoId ? null : askActiveTabForFreshMedia();
}

function videoIdFromPageUrl(value) {
  // TASK-8.18：/note/ 是图集（图文）页形，与 /video/ 同为 aweme_id 路径。
  return String(value || "").match(/\/(?:video|note)\/(\d{10,24})/)?.[1] || "";
}

const fallbacksInFlight = new Set();
const fallbackReassured = new Set();
async function handleLocalTaskFailed(job) {
  const taskId = String(job?.id || "");
  const pending = job?.error?.diagnostic?.fallback_pending;
  const key = `${taskId}:${pending}`;
  if (!taskId || !pending || fallbacksInFlight.has(key)) return;
  fallbacksInFlight.add(key);
  // 先生 2026-10-18：首错后不能把用户晾在原地——回退一开始就安抚一次
  // （每个任务只一次，不随通道切换反复弹；结果仍由既有终态通知交代）。
  let session = null;
  try {
    if (!fallbackReassured.has(taskId)) {
      fallbackReassured.add(taskId);
      await notifyTabs("别急，正在尝试其他下载办法；结果会在这里通知", false, job, "fallback").catch(() => {});
    }
    const expectedVideoId = videoIdFromPageUrl(job.url);
    const fresh = pending === "browser" ? await askTabsForFreshMedia(expectedVideoId) : null;
    const remembered = submitMediaHints.get(taskId) || {};
    const matchingFresh = String(fresh?.videoId || "") === expectedVideoId ? fresh : null;
    const mediaCandidates = pending === "browser" ? browserFallbackMediaUrls({
      code: String(job?.error?.code || ""), remembered, matchingFresh,
    }) : [];
    const stage = mediaCandidates.length ? "browser" : "resolver";
    if (stage === "resolver") session = await oneShotDouyinSession();
    const payload = { stage, media_candidates: mediaCandidates };
    if (session) payload.browser_session = session;
    const result = await request(`/v1/downloads/${taskId}/fallback`, {
      method: "POST", body: JSON.stringify(payload),
    });
    const continued = result?.job;
    if (!continued?.id) throw new Error("下载任务未能接续");
    const direct = continued.browser_download;
    if (stage === "browser" && direct?.download_url) {
      try {
        await startBrowserDownload(continued, direct, Boolean(job?.options?.transcribe_after_download), {
          videoId: expectedVideoId, userAgent: globalThis.navigator?.userAgent,
          recoveryToYtDlp: true,
        });
      } catch (error) {
        await request(`/v1/downloads/${taskId}/browser-failure`, {
          method: "POST", body: JSON.stringify({ browser_error: "BROWSER_INTERRUPTED_UNKNOWN", phase: "browser_download_start", browser_detail: safeFailureText(error?.message || "浏览器未能启动下载") }),
        });
      }
    }
  } catch (error) {
    if (isTemporaryOffline(error)) return;
    // Only an exhausted/denied fallback becomes terminal. No intermediate
    // channel failure creates another task or another page notification.
    await request(`/v1/downloads/${taskId}/finish-fallback`, {
      method: "POST", body: JSON.stringify({ message: safeFailureText(error?.message || "可用下载通道均未成功") }),
    }).catch(() => {});
  } finally {
    if (session) session.length = 0;
    fallbacksInFlight.delete(key);
    // Retain the original in-memory candidate snapshot until this task is terminal.
  }
}

const WATCHED_TASKS_KEY = "watchedDownloadTasks";
async function saveWatchedTasks() {
  const tasks = [...localTaskWatchers.entries()].map(([id, state]) => ({
    id, downloadNotified: Boolean(state.downloadNotified), transcribeNotified: Boolean(state.transcribeNotified),
    failureNotified: Boolean(state.failureNotified),
    lastJob: safeTaskSnapshot(state.lastJob),
  }));
  await chrome.storage.local.set({ [WATCHED_TASKS_KEY]: tasks }).catch(() => {});
}
async function pollLocalTask(taskId) {
  const state = localTaskWatchers.get(taskId);
  if (!state || state.polling) return;
  state.polling = true;
  try {
    const result = await request(`/v1/downloads/${encodeURIComponent(taskId)}`);
    const job = result?.job;
    if (!job) return;
    state.misses = 0;
    desktopConnectionState = "online";
    if(job.status === "awaiting_browser" && !job.error?.diagnostic?.fallback_pending) {
      const owned=[...inFlightBrowserDownloads.entries()].find(([,meta])=>meta.taskId===taskId);
      if(owned){const transfer=await searchBrowserDownload(Number(owned[0]));if(Number(transfer?.totalBytes)>0)job.progress=Math.min(.999,Number(transfer.bytesReceived||0)/transfer.totalBytes);}
    }
    state.lastJob = safeTaskSnapshot(job);
    await updateQueuedTask(job);
    if (state.recoverHandoff && job.status === "awaiting_browser" && !job.error?.diagnostic?.fallback_pending && job.options?.mode === "browser") {
      const receipt = [...submissionQueue.values()].find(entry => entry.taskId === taskId);
      if (!receipt?.submitting && !browserRecoveryRunning) {
        const stored = await chrome.storage.local.get([DOWNLOAD_JOB_MAP_KEY, PENDING_IMPORTS_KEY]);
        const owned = [...inFlightBrowserDownloads.values(), ...Object.values(stored[DOWNLOAD_JOB_MAP_KEY] || {}), ...(stored[PENDING_IMPORTS_KEY] || [])].some(meta=>meta.taskId===taskId);
        if (!owned) await request(`/v1/downloads/${taskId}/browser-failure`, {method:"POST",body:JSON.stringify({browser_error:"CRASH",phase:"browser_download_interrupted"})});
        state.recoverHandoff = false;
      }
    }
    if (job.status === "awaiting_browser" && job?.error?.diagnostic?.fallback_pending) {
      await saveWatchedTasks();
      await handleLocalTaskFailed(job);
    } else {
      const downloaded = Boolean(job.download_complete);
      const percent = Math.max(0, Math.min(100, Math.round(Number(job?.progress || 0) * 100)));
      const bucket = Math.floor(percent / 10) * 10;
      const terminal = isTerminalTask(job.status);
      if (downloaded && !state.downloadNotified) {
        state.downloadNotified = true;
        await saveWatchedTasks();
        await markTaskNotification(taskId, "downloadNotified");
        await notifyTabs("下载成功", false, job, "download");
      }
      if (job.status === "done" && job.job_id && !state.transcribeNotified) {
        state.transcribeNotified = true;
        await saveWatchedTasks();
        await markTaskNotification(taskId, "transcribeNotified");
        const terminal = transcriptionTerminal(job);
        await notifyTabs(terminal.message, terminal.error, job, "transcribe");
      } else if (job.status !== state.status || bucket !== state.bucket) {
        state.status = job.status; state.bucket = bucket;
        // 失败提示持久去重：同一次失败只弹一次（跨 SW 重启、跨多标签），
        // 避免旧任务的失败记录反复打扰。
        if (job.status === "failed" && !state.failureNotified) {
          state.failureNotified = true;
          await saveWatchedTasks();
          await markTaskNotification(taskId, "failureNotified");
          await notifyTabs(downloaded ? "转写失败，请在 FoilPage 查看" : "下载失败，请在 FoilPage 查看", true, job, "failure");
        }
        else if (job.status === "cancelled") await notifyTabs("任务已取消", false, job, "waiting");
        else if (!terminal && !receiptJustAccepted(taskId)) await notifyTabs(localTaskProgressMessage(job), false, job, "progress");
      }
      await saveWatchedTasks();
      if (terminal) {
        if (job.status === "cancelled") await cancelOwnedBrowserDownloads(taskId);
        localTaskWatchers.delete(taskId);
        submitMediaHints.delete(taskId);
        fallbackReassured.delete(taskId);
        await saveWatchedTasks();
        return;
      }
    }
  } catch (error) {
    if (Number(error?.status) === 404) {
      localTaskWatchers.delete(taskId);
      await updateQueuedTask({ ...(state.lastJob || {}), id: taskId, status: "cancelled" });
      await saveWatchedTasks();
      return;
    }
    state.misses = (state.misses || 0) + 1;
    if (isTemporaryOffline(error)) desktopConnectionState = "offline";
    // No retry count can turn a transport outage into media failure. Keep the
    // durable watcher and last progress; the existing alarm/heartbeat resumes it.
  } finally { state.polling = false; }
  if (!localTaskWatchers.has(taskId)) return;
  const delay = Math.min(30000, 1500 * (2 ** Math.min(state.misses || 0, 5)));
  state.timer = setTimeout(() => void pollLocalTask(taskId).catch(() => {}), delay);
  state.timer?.unref?.();
}
function watchLocalTask(job, restored = null) {
  const taskId = String(job?.id || "");
  if (!taskId || localTaskWatchers.has(taskId)) return;
  const receipt = [...submissionQueue.values()].find(entry => entry.taskId === taskId);
  // status 预置为已知状态：恢复的任务不再把既有终态当成“新变化”重放通知
  // （2026-10-14 实机：旧失败任务随每次 SW 唤醒重弹“下载失败”，穿插在
  // 新任务的正常回执之间）。
  const knownStatus = String((job?.status || restored?.lastJob?.status || "")).slice(0, 32);
  localTaskWatchers.set(taskId, {
    status: knownStatus, bucket: -1, timer: null, lastJob: safeTaskSnapshot(job?.status ? job : restored?.lastJob),
    downloadNotified: Boolean(restored?.downloadNotified || receipt?.downloadNotified),
    transcribeNotified: Boolean(restored?.transcribeNotified || receipt?.transcribeNotified),
    failureNotified: Boolean(restored?.failureNotified || receipt?.failureNotified),
    recoverHandoff: Boolean(restored),
  });
  void saveWatchedTasks();
  void pollLocalTask(taskId).catch(() => {});
}
async function restoreWatchedTasks() {
  await loadSubmissionQueue();
  const saved = await chrome.storage.local.get(WATCHED_TASKS_KEY).catch(() => ({}));
  const all = saved[WATCHED_TASKS_KEY] || [];
  const alive = [];
  for (const task of all) {
    if (!/^[a-f0-9]{12}$/.test(String(task?.id || ""))) continue;
    // 终态任务不再恢复观察（同时从持久列表清理）：失败提示不随 SW 唤醒
    // 重放，先生 2026-10-14 要求失败记录不打扰正常下载回执。
    if (isTerminalTask(String(task?.lastJob?.status || ""))) continue;
    alive.push(task);
    watchLocalTask(task, task);
  }
  if (alive.length !== all.length) {
    await chrome.storage.local.set({ [WATCHED_TASKS_KEY]: alive }).catch(() => {});
  }
}
void withExtensionOperation(restoreWatchedTasks).catch(() => {});
chrome.alarms?.onAlarm?.addListener?.((alarm) => {
  if (alarm?.name === FAILURE_REPORT_ALARM) {
    void withExtensionOperation(restoreWatchedTasks).catch(() => {});
    void resumeBackgroundQueue().catch(() => {});
    void connectDesktopEvents().catch(() => {});
  }
});

async function handleBrowserDownloadComplete(downloadId) {
  const key=String(downloadId);
  // Atomically move an owned Chrome transfer to durable import work before
  // consuming it. A service-worker restart cannot lose a completed file.
  const meta=await withFailureQueueLock(async()=>{
    const data=await chrome.storage.local.get([DOWNLOAD_JOB_MAP_KEY,PENDING_IMPORTS_KEY]);
    const map=data[DOWNLOAD_JOB_MAP_KEY] || {};
    const owned=map[key] || inFlightBrowserDownloads.get(key);
    if(!owned)return null;
    const pending=data[PENDING_IMPORTS_KEY] || [];
    if(!pending.some(entry=>entry.downloadId===downloadId))pending.push({downloadId,taskId:owned.taskId,title:owned.title,transcribe:Boolean(owned.transcribe),queuedAt:Date.now()});
    delete map[key];
    await chrome.storage.local.set({[DOWNLOAD_JOB_MAP_KEY]:map,[PENDING_IMPORTS_KEY]:pending});
    inFlightBrowserDownloads.delete(key);consumedBrowserDownloads.add(key);
    setTimeout(()=>consumedBrowserDownloads.delete(key),30000);
    return owned;
  });
  if(meta)await flushPendingImports();
}
async function handleBrowserDownloadInterrupted(downloadId) {
  const key=String(downloadId);
  if(browserInterruptsInFlight.has(key))return;
  browserInterruptsInFlight.add(key);
  try {
    const item=await searchBrowserDownload(downloadId);
    const stored=await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
    const meta=stored[DOWNLOAD_JOB_MAP_KEY]?.[key] || inFlightBrowserDownloads.get(key);
    if(!meta?.taskId)return;
    const rawCode=String(item?.error || "").trim().toUpperCase();
    const browserError=/^[A-Z0-9_]{1,64}$/.test(rawCode)?rawCode:"BROWSER_INTERRUPTED_UNKNOWN";
    const result=await request(`/v1/downloads/${meta.taskId}/browser-failure`,{
      method:"POST",body:JSON.stringify({browser_error:browserError,bytes_received:Number(item?.bytesReceived)||0,total_bytes:Number(item?.totalBytes)>=0?Number(item.totalBytes):null}),
    });
    await takeBrowserDownload(downloadId);
    if(result?.job)watchLocalTask(result.job);
  } catch(error) {
    // Keep the original owned transfer until the backend acknowledges the
    // interruption. Transport/auth loss never creates a detached terminal task.
    if(isTemporaryOffline(error))desktopConnectionState="offline";
    else if(Number(error?.status)===404)await takeBrowserDownload(downloadId);
  } finally {browserInterruptsInFlight.delete(key);}
}

chrome.downloads.onChanged.addListener((delta) => {
  const state = delta?.state?.current;
  if (!state) return;
  if (state === "complete") {
    void withExtensionOperation(() => handleBrowserDownloadComplete(delta.id)).catch(() => {});
  } else if (state === "interrupted") {
    void withExtensionOperation(() => handleBrowserDownloadInterrupted(delta.id)).catch(() => {});
  }
});

// Recover any browser downloads that finished while the service worker was
// asleep or the local service was unavailable.
void withExtensionOperation(flushPendingImports).catch(() => {});
let browserRecoveryRunning = false;
async function handleMissingBrowserDownload(downloadId, meta) {
  if (!meta?.taskId) return;
  const trackedAt = Number(meta.startedAt ?? meta.missingSince);
  if (!Number.isFinite(trackedAt)) {
    // Legacy entries without a start timestamp get a first-observation grace
    // period. This is evidence about one missing ID, not a task age timeout.
    await withFailureQueueLock(async () => {
      const data = await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
      const map = data[DOWNLOAD_JOB_MAP_KEY] || {};
      if (map[String(downloadId)]) {
        map[String(downloadId)] = { ...map[String(downloadId)], missingSince: Date.now() };
        await chrome.storage.local.set({ [DOWNLOAD_JOB_MAP_KEY]: map });
      }
    });
    return;
  }
  if (Date.now() - trackedAt < BROWSER_DOWNLOAD_LOOKUP_GRACE_MS) return;
  const result = await request(`/v1/downloads/${meta.taskId}/browser-failure`, {
    method: "POST", body: JSON.stringify({ browser_error: "BROWSER_DOWNLOAD_HISTORY_MISSING" }),
  });
  // Do not discard ownership after a lost or malformed acknowledgement.
  if (result?.job?.id !== meta.taskId) return;
  await takeBrowserDownload(downloadId);
  watchLocalTask(result.job);
}
async function recoverBrowserDownloads() {
  if (browserRecoveryRunning) return;
  browserRecoveryRunning = true;
  try {
    const data = await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
    for (const id of Object.keys(data[DOWNLOAD_JOB_MAP_KEY] || {}).slice(0, 100)) {
      if (consumedBrowserDownloads.has(id)) continue;
      inFlightBrowserDownloads.set(id, data[DOWNLOAD_JOB_MAP_KEY][id]);
      const item = await searchBrowserDownload(Number(id));
      if (item?.state === "complete") await handleBrowserDownloadComplete(Number(id));
      else if (item?.state === "interrupted") await handleBrowserDownloadInterrupted(Number(id));
      else if (item === null) await handleMissingBrowserDownload(Number(id), data[DOWNLOAD_JOB_MAP_KEY][id]);
    }
    await flushPendingImports();
  } catch { /* A browser update can temporarily delay its download service. */ }
  finally { browserRecoveryRunning = false; void selfUpdateCheck(); }
}
void recoverBrowserDownloads();
chrome.alarms?.onAlarm?.addListener?.((alarm) => {
  if (alarm?.name === FAILURE_REPORT_ALARM) {
    void recoverBrowserDownloads();
    void selfUpdateCheck();
  }
});

async function status() {
  await resumeBackgroundQueue();
  let jobs = [], connected = false;
  try { const data = await request("/v1/downloads?limit=6"); jobs = data.jobs || []; desktopConnectionState = "online"; connected = true; }
  catch (error) { if (isTemporaryOffline(error)) desktopConnectionState = "offline"; }
  if (connected) {
    // Failed watchers have stopped polling. Their owned receipts can outlive
    // the six recent desktop rows, so reconcile the visible window by ID.
    const checked = new Set(jobs.map(job => job.id));
    const visible = [...submissionQueue.values()].sort((a,b) => b.createdAt-a.createdAt).slice(0,12);
    for (const entry of visible) {
      if (!/^[a-f0-9]{12}$/.test(String(entry.taskId || "")) || checked.has(entry.taskId) || (entry.lastJob?.status || entry.status) !== "failed") continue;
      checked.add(entry.taskId);
      try { const {job} = await request(`/v1/downloads/${entry.taskId}`); if (job) jobs.push(job); }
      catch (error) { if (isTemporaryOffline(error)) { desktopConnectionState = "offline"; break; } }
    }
  }
  jobs = jobs.map(job=>{const saved=[...submissionQueue.values()].find(entry=>entry.taskId===job.id)?.lastJob;return job.status==="awaiting_browser"&&saved ? {...job,progress:Math.max(Number(job.progress)||0,Number(saved.progress)||0)} : job;});
  // Persist authoritative recovery before a later outage falls back to cache.
  for (const job of jobs) await updateQueuedTask(job);
  const seen = new Set(jobs.map(job => job.id));
  const local = [...submissionQueue.values()].sort((a,b) => b.createdAt - a.createdAt).map(entry => queuedTaskView(entry));
  return [...local.filter(job => !seen.has(job.id)), ...jobs].slice(0, 12);
}

function isHlsMediaUrl(value) {
  try {
    return /\.m3u8(?:$|\?)/i.test(new URL(String(value || "")).pathname);
  } catch {
    return false;
  }
}

function isPlayEndpointMediaUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    const host = parsed.hostname.toLowerCase();
    const douyinHost = host === "douyin.com" || host.endsWith(".douyin.com")
      || host.endsWith(".snssdk.com") || host.endsWith(".amemv.com");
    if (!douyinHost) return false;
    return /\/(?:aweme\/v1\/play|video\/play|video\/download|play\/?)(?:[/?]|$)/i
      .test(`${parsed.pathname}${parsed.search}`);
  } catch {
    return false;
  }
}

// B 站 / 其他页面提交：交给桌面 yt-dlp 下载轨（mode=local）。与抖音的
// browser handoff 不同，这里不读播放器地址也不采 Cookie——B 站防盗链需要
// Referer，由后端本地下载带出；公开视频免登录。
// TASK-8.18 图集提交：无媒体提示、无 Cookie——图片是签名直链（取证④），
// 整组地址直接交桌面逐张下载。
async function performGalleryDownload(message) {
  const rawUrl = String(message?.url || "").trim().slice(0, 4096);
  const noteUrl = /\/note\/\d{10,24}/.test(rawUrl)
    ? rawUrl : `https://www.douyin.com/note/${String(message?.videoId || "")}`;
  if (!/\/note\/\d{10,24}/.test(noteUrl)) throw new Error("未识别当前图集");
  const galleryImages = (Array.isArray(message?.galleryImages) ? message.galleryImages
    : Array.isArray(message?.gallery) ? message.gallery : [])
    .map(row => (Array.isArray(row) ? row.map(v => String(v || "").slice(0, 4096)).filter(Boolean).slice(0, 8) : []))
    .filter(row => row.length).slice(0, 60);
  if (!galleryImages.length) throw new Error("图集地址缺失，请刷新页面后重试");
  const payload = {
    url: noteUrl,
    title: String(message?.title || "").trim().slice(0, 240),
    client_request_id: message?.clientRequestId,
    mode: "local",
    cover_url: String(message?.coverUrl || "").trim().slice(0, 4096),
    gallery_images: galleryImages,
    options: {
      quality: "best-available",
      audio_only: false,
      transcribe_after_download: false,
      browser_fallback: false,
      language: "zh",
      referer: noteUrl,
      user_agent: String(message?.userAgent || globalThis.navigator?.userAgent || "").slice(0, 512),
    },
  };
  const result = await request("/v1/downloads", { method: "POST", body: JSON.stringify(payload) });
  const job = result.job || null;
  console.log("[FoilPage] 图集下载任务已创建", job?.id || "");
  await acceptQueuedJob(message?.clientRequestId, job);
  rememberSubmitMedia(job?.id, message);
  watchLocalTask(job);
  return job;
}

async function performPageDownload(message) {
  if (message?.kind === "gallery" || Array.isArray(message?.galleryImages) || Array.isArray(message?.gallery)) {
    return performGalleryDownload(message);
  }
  const rawUrl = String(message?.url || "").trim().slice(0, 4096);
  if (!rawUrl) throw new Error("未识别当前页面链接");
  const mediaUrl = String(message?.mediaUrl || "").trim().slice(0, 4096);
  const playUrl = String(message?.playUrl || "").trim().slice(0, 4096);
  const mediaCandidates = Array.isArray(message?.mediaCandidates)
    ? [...new Set(message.mediaCandidates.map((value) => String(value || "").trim().slice(0, 4096)).filter(Boolean))].slice(0, 8)
    : [];
  const selectedMediaUrl = mediaUrl || playUrl;
  const title = String(message?.title || "").trim().slice(0, 240);
  // Prefer Chrome for every observed single-file Douyin stream. A playlist
  // alone still needs yt-dlp to assemble segments; never save it as a video.
  if (isDouyinPageUrl(rawUrl)) {
    const videoId = videoIdFromPageUrl(rawUrl);
    if (!videoId) throw new Error("未识别当前视频");
    const browserMediaUrl = [mediaUrl, ...mediaCandidates, playUrl]
      .find(value => value && !isHlsMediaUrl(value)) || "";
    if (browserMediaUrl || !selectedMediaUrl) {
      return performAuthorizedDownload({
        videoId,
        title,
        mediaUrl: browserMediaUrl,
        mediaCandidates, playUrl, clientRequestId: message.clientRequestId,
        coverUrl: String(message?.coverUrl || ""),
        transcribe: Boolean(message?.transcribe),
        userAgent: String(message?.userAgent || globalThis.navigator?.userAgent || "").slice(0, 512),
        recoveryToYtDlp: true,
      });
    }
  }
  // Least privilege, mirroring the authorized-download path: a captured player
  // URL lets the local worker download straight from the CDN (signed URLs,
  // Referer-guarded), so no Douyin cookies are collected at all. The cookie
  // bridge is only armed when the backend must resolve the page itself.
  let session = null;
  let job = null;
  let phase = "extension_authorization";
  try {
    session = isDouyinPageUrl(rawUrl) && !mediaUrl ? await oneShotDouyinSession() : null;
    phase = "local_api_submit";
    const payload = {
      url: rawUrl,
      title,
      client_request_id: message.clientRequestId,
      mode: "local",
      options: {
        quality: "best-available",
        // 转写意图只取音轨（B 站 bestaudio 通常比整段视频小一个量级）；
        // 「下载」按钮仍保留完整视频文件。
        audio_only: Boolean(message?.transcribe),
        transcribe_after_download: Boolean(message?.transcribe),
        browser_fallback: isDouyinPageUrl(rawUrl),
        language: "zh",
        referer: rawUrl,
        user_agent: String(message?.userAgent || globalThis.navigator?.userAgent || "").slice(0, 512),
      },
    };
    if (mediaUrl) payload.media_url = mediaUrl;
    if (mediaCandidates.length) payload.media_candidates = mediaCandidates;
    if (session) payload.browser_session = session;
    const result = await request("/v1/downloads", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    job = result.job || null;
    console.log("[FoilPage] 页面本地下载任务已创建", job?.id || "");
    await acceptQueuedJob(message.clientRequestId, job);
    if (submissionQueue.get(message.clientRequestId)?.cancelRequested) {
      const result = await request("/v1/downloads", {method:"POST",body:JSON.stringify({cancel_request_id:message.clientRequestId})});
      if (result.job) { await updateQueuedTask(result.job); watchLocalTask(result.job); return result.job; }
      return job;
    }
    rememberSubmitMedia(job?.id, message);
    watchLocalTask(job);
    return job;
  } catch (error) {
    if (["login_required"].includes(error?.code)) throw error;
    if (isTemporaryOffline(error)) throw error;
    await reportDownloadFailure({
      task_id: job?.id,
      url: rawUrl,
      title,
      phase,
      channel: "browser_extension",
      code: error?.code || "extension_error",
      message: error?.detail || error?.message || "扩展未能提交下载任务。",
      diagnostic: {
        error_name: String(error?.name || "Error"),
        http_status: Number(error?.status) || undefined,
        resolver_code: String(error?.code || ""),
        extension_version: String(chrome.runtime.getManifest?.().version || ""),
      },
    });
    throw error;
  } finally {
    if (session) session.length = 0;
  }
}

function isTerminalTask(status) { return ["done", "failed", "cancelled"].includes(status); }
function safeTaskSnapshot(job) {
  if (!job?.id) return null;
  return {
    id: String(job.id), url: safeFailurePageUrl(job.url), title: String(job.title || "").slice(0, 240),
    status: String(job.status || "received"), progress: Number(job.progress) || 0,
    download_complete: Boolean(job.download_complete), job_id: job.job_id || null,
    ...Object.fromEntries(["body_enabled_snapshot", "title_enabled_snapshot"].filter(key => typeof job[key] === "boolean").map(key => [key, job[key]])),
    ...Object.fromEntries(["body_status", "title_status"].filter(key => ["pending", "running", "done", "skipped", "disabled", "cancelled", "failed"].includes(job[key])).map(key => [key, job[key]])),
    ...(job.transcription_label ? { transcription_label: safeFailureText(job.transcription_label, 120), transcription_error: job.transcription_error === true } : {}),
    options: { transcribe_after_download: Boolean(job.options?.transcribe_after_download), mode: job.options?.mode || "local" },
    error: job.error ? { code: String(job.error.code || ""), message:safeFailureText(job.error.message || "",240), diagnostic: { fallback_pending: job.error.diagnostic?.fallback_pending || "" } } : null,
  };
}
async function loadSubmissionQueue() {
  if (!submissionQueueLoadPromise) submissionQueueLoadPromise = (async () => {
    const saved = await chrome.storage.local.get(SUBMISSION_QUEUE_KEY);
    for (const entry of (Array.isArray(saved[SUBMISSION_QUEUE_KEY]) ? saved[SUBMISSION_QUEUE_KEY] : [])) {
      if (!/^[A-Za-z0-9_-]{12,64}$/.test(String(entry?.clientId || "")) || !entry.url) continue;
      submissionQueue.set(entry.clientId, { ...entry, submitting: false });
    }
  })().catch(error => { submissionQueueLoadPromise = null; throw error; });
  return submissionQueueLoadPromise;
}
function saveSubmissionQueue() {
  const operation = submissionQueueSaveLock.then(async () => {
    const terminal = [...submissionQueue.values()].filter(entry => isTerminalTask(entry.lastJob?.status || entry.status)).sort((a,b) => b.createdAt-a.createdAt);
    for (const entry of terminal.slice(30)) { submissionQueue.delete(entry.clientId); queuedMediaHints.delete(entry.clientId); }
    // Use an explicit allowlist: no credentials, signed media URLs or local paths.
    const safe = [...submissionQueue.values()].map(entry => ({
      clientId: entry.clientId, url: entry.url, videoId: entry.videoId, title: entry.title,
      transcribe: entry.transcribe, createdAt: entry.createdAt, taskId: entry.taskId || "",
      status: entry.status, cancelRequested: Boolean(entry.cancelRequested), submissionAttempted: Boolean(entry.submissionAttempted),
      submittedNotified: Boolean(entry.submittedNotified), waitingNotified: Boolean(entry.waitingNotified),
      downloadNotified: Boolean(entry.downloadNotified), transcribeNotified: Boolean(entry.transcribeNotified),
      failureNotified: Boolean(entry.failureNotified),
      lastJob: safeTaskSnapshot(entry.lastJob),
    }));
    await chrome.storage.local.set({ [SUBMISSION_QUEUE_KEY]: safe });
  });
  submissionQueueSaveLock = operation.catch(() => {});
  return operation;
}
function receiptJustAccepted(taskId) { const entry=[...submissionQueue.values()].find(item=>item.taskId===taskId); return Boolean(entry?.acceptedAt && Date.now()-entry.acceptedAt<1500); }
function queuedTaskView(entry) {
  if (entry.lastJob) return { ...entry.lastJob, submission_failed: !entry.taskId && entry.status === "failed", client_request_id: entry.clientId, waiting_connection: desktopConnectionState === "offline", cancel_requested: Boolean(entry.cancelRequested) };
  return { id: entry.clientId, client_request_id: entry.clientId, url: entry.url, title: entry.title,
    status: entry.status || "handoff_pending", progress: 0, queued: !entry.taskId, cancel_requested:Boolean(entry.cancelRequested), options: {transcribe_after_download:entry.transcribe} };
}
async function updateQueuedTask(job) {
  const snapshot = safeTaskSnapshot(job);
  let changed = false;
  for (const entry of submissionQueue.values()) {
    if (entry.taskId !== job.id) continue;
    entry.lastJob = snapshot; entry.status = job.status; changed = true;
    if (isTerminalTask(job.status)) queuedMediaHints.delete(entry.clientId);
  }
  if (changed) await saveSubmissionQueue();
}
async function markTaskNotification(taskId, key) {
  let changed = false;
  for (const entry of submissionQueue.values()) if (entry.taskId === taskId) { entry[key] = true; changed = true; }
  if (changed) await saveSubmissionQueue();
}
async function acceptQueuedJob(clientId, job) {
  if (!job?.id) throw new Error("FoilPage 未返回任务接收凭据");
  const entry = submissionQueue.get(clientId);
  if (!entry) return;
  const alreadyNotified = [...submissionQueue.values()].some(other => other.taskId === job.id && other.submittedNotified);
  entry.taskId = job.id; entry.status = job.status; entry.lastJob = safeTaskSnapshot(job); entry.acceptedAt=Date.now();
  entry.submittedNotified = alreadyNotified;
  await saveSubmissionQueue();
  if (!alreadyNotified && !entry.cancelRequested) {
    // 复用已下载媒体直接转写（先生 2026-10-08）时，任务一落地就是
    // queued——如实告知没重新下载，而不是笼统的「已提交下载」。
    const reusedForTranscribe = String(job?.status || "") === "queued"
      && Boolean(job?.download_complete)
      && Boolean(job?.options?.transcribe_after_download);
    const acknowledged = await notifyTabs(reusedForTranscribe
      ? "已复用已下载的视频，直接开始转写"
      : "已提交下载", false, job, "submitted");
    entry.submittedNotified = acknowledged;
    await saveSubmissionQueue();
  }
}
async function flushSubmissionQueue() {
  if (submissionQueueFlushPromise) return submissionQueueFlushPromise;
  submissionQueueFlushPromise = (async () => {
    await loadSubmissionQueue();
    for (const entry of submissionQueue.values()) {
      // A submission receipt is complete only after a matching page confirms
      // it. Retry an unconfirmed receipt on worker resume; if the download has
      // already completed, report the current terminal success instead of
      // replaying an older submitted state.
      if (entry.taskId && !entry.submittedNotified && !entry.cancelRequested && entry.lastJob?.id) {
        const job = entry.lastJob;
        // An unacknowledged acceptance must never be replayed as the current
        // state after download failure/cancellation. A completed download may
        // still be reported separately when only its transcription failed.
        if (!job.download_complete && ["failed", "cancelled"].includes(job.status)) continue;
        const reusedForTranscribe = Boolean(job.download_complete)
          && Boolean(job.options?.transcribe_after_download)
          && String(job.status || "") === "queued";
        const stage = job.download_complete && !reusedForTranscribe ? "download" : "submitted";
        const message = reusedForTranscribe
          ? "已复用已下载的视频，直接开始转写"
          : stage === "download" ? "下载成功" : "已提交下载";
        const acknowledged = await notifyTabs(message, false, job, stage);
        if (acknowledged) {
          entry.submittedNotified = true;
          if (stage === "download") entry.downloadNotified = true;
          await saveSubmissionQueue();
        }
      }
      if (isTerminalTask(entry.lastJob?.status || entry.status) || entry.submitting) continue;
      if (entry.cancelRequested && !entry.taskId) {
        if (entry.submissionAttempted) {
          try {
            const result = await request("/v1/downloads", {method:"POST",body:JSON.stringify({cancel_request_id:entry.clientId})});
            if (result.job) { entry.taskId=result.job.id; await updateQueuedTask(result.job); }
            else { entry.status="cancelled"; entry.lastJob={id:entry.clientId,url:entry.url,title:entry.title,status:"cancelled",progress:0}; }
          } catch(error) { if (isTemporaryOffline(error)) break; throw error; }
        } else { entry.status="cancelled"; entry.lastJob={id:entry.clientId,url:entry.url,title:entry.title,status:"cancelled",progress:0}; }
        await saveSubmissionQueue();
        if (!entry.taskId) continue;
      }
      if (entry.taskId) {
        if (entry.cancelRequested) {
          try {
            const result = await request(`/v1/downloads/${entry.taskId}/cancel`, {method:"POST", body:"{}"});
            if (result.job) await updateQueuedTask(result.job);
          } catch (error) { if (isTemporaryOffline(error)) break; }
        }
        if (!isTerminalTask(entry.lastJob?.status || entry.status)) watchLocalTask(entry.lastJob || {id:entry.taskId}, entry);
        continue;
      }
      entry.submitting = true;
      try {
        // Read credentials or fresh page candidates only after the desktop is
        // reachable. The source identity stays fixed if another video is active.
        await request("/v1/downloads?limit=1");
        desktopConnectionState = "online";
        let hints = queuedMediaHints.get(entry.clientId) || {};
        // 图集提交自带整组地址，无需向页面要媒体提示（fresh 响应也不含图集）。
        if (entry.videoId && !hints.mediaUrl && !hints.playUrl && !hints.galleryImages) {
          const fresh = await askTabsForFreshMedia(entry.videoId);
          if (String(fresh?.videoId || "") === entry.videoId) hints = { ...hints, ...fresh };
        }
        if (entry.cancelRequested) continue;
        entry.submissionAttempted = true; await saveSubmissionQueue();
        const job = await performPageDownload({ ...hints, url: entry.url, title: entry.title,
          videoId: entry.videoId, transcribe: entry.transcribe, clientRequestId: entry.clientId });
        if (job?.id) { entry.taskId = job.id; entry.lastJob = safeTaskSnapshot(job); entry.status = job.status; }
        if (entry.cancelRequested && entry.taskId) {
          const result = await request(`/v1/downloads/${entry.taskId}/cancel`, {method:"POST", body:"{}"});
          if (result.job) await updateQueuedTask(result.job);
        }
      } catch (error) {
        if (isTemporaryOffline(error)) { desktopConnectionState = "offline"; break; }
        entry.status = "failed";
        const notice = submissionFailureNotice(error);
        entry.lastJob = { id: entry.taskId || entry.clientId, url:entry.url, title:entry.title, status:"failed", progress:0, error:{code:error?.code || "submission_failed",message:safeFailureText(notice,240)} };
        queuedMediaHints.delete(entry.clientId);
        await notifyTabs(notice, true, entry.lastJob, "failure");
      } finally { entry.submitting = false; await saveSubmissionQueue(); }
    }
  })();
  try { return await submissionQueueFlushPromise; }
  finally { submissionQueueFlushPromise = null; }
}
async function resumeBackgroundQueue() {
  await flushSubmissionQueue();
  const owned = await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
  if (Object.keys(owned[DOWNLOAD_JOB_MAP_KEY] || {}).length) await recoverBrowserDownloads();
  await flushPendingImports().catch(() => {});
  await flushPendingFailureReports().catch(() => {});
}
async function submitPageDownload(message) {
  await loadSubmissionQueue();
  if (!await getToken()) { const error = new Error("请先在扩展中连接 FoilPage"); error.code = "local_api_token_missing"; throw error; }
  const url = safeFailurePageUrl(String(message?.url || ""));
  const parsed = new URL(url);
  if (!["douyin.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be"].some(domain => parsed.hostname === domain || parsed.hostname.endsWith(`.${domain}`))) throw new Error("不支持此视频页面");
  const transcribe = Boolean(message?.transcribe);
  const existing = [...submissionQueue.values()].find(entry => entry.url === url && entry.transcribe === transcribe && !isTerminalTask(entry.lastJob?.status || entry.status));
  if (existing) { await flushSubmissionQueue(); return queuedTaskView(existing); }
  if ([...submissionQueue.values()].filter(entry => !isTerminalTask(entry.lastJob?.status || entry.status)).length >= 100) throw new Error("后台任务已满，请先完成或取消部分任务");
  const clientId = globalThis.crypto?.randomUUID?.() || `sj_${Date.now().toString(36)}_${Math.random().toString(36).slice(2,14)}`;
  const entry = { clientId, url, videoId: videoIdFromPageUrl(url), title:String(message?.title || "").trim().slice(0,240),
    transcribe, createdAt:Date.now(), status:"handoff_pending", submittedNotified:false, waitingNotified:false };
  submissionQueue.set(clientId, entry);
  // TASK-8.18：图集整组随队列中转（内存态，不落盘；形状在桌面端再验）。
  const galleryImages = Array.isArray(message?.galleryImages)
    ? message.galleryImages
        .map(row => (Array.isArray(row) ? row.map(v => String(v || "").slice(0, 4096)).filter(Boolean).slice(0, 8) : []))
        .filter(row => row.length).slice(0, 60)
    : null;
  queuedMediaHints.set(clientId, { mediaUrl:String(message?.mediaUrl || "").slice(0,4096),
    mediaCandidates:Array.isArray(message?.mediaCandidates) ? message.mediaCandidates.slice(0,8) : [], playUrl:String(message?.playUrl || "").slice(0,4096), userAgent:message?.userAgent,
    // TASK-8.17：封面地址随提交提示进队列中转（内存态，与签名媒体地址同待遇
    // 不落盘）；漏存这一跳会把 cover_url 在 flush 时丢成空串。
    coverUrl:String(message?.coverUrl || "").slice(0,4096),
    ...(galleryImages ? { galleryImages, kind: "gallery" } : {}) });
  await saveSubmissionQueue(); // The click is durable before any network request.
  await flushSubmissionQueue();
  if (!entry.taskId && !isTerminalTask(entry.status) && !entry.waitingNotified) {
    entry.waitingNotified = true; await saveSubmissionQueue();
    await notifyTabs("已排队，等待 FoilPage", false, {id:clientId,url,title:entry.title}, "waiting");
  }
  return queuedTaskView(entry);
}
async function submitAuthorizedDownload(message) {
  if (!validVideoId(String(message?.videoId || ""))) throw new Error("未识别当前视频");
  return submitPageDownload({ ...message, url:canonicalDetailUrl(String(message.videoId)) });
}
async function cancelOwnedBrowserDownloads(taskId) {
  const saved=await chrome.storage.local.get(DOWNLOAD_JOB_MAP_KEY);
  const owned=new Map([...Object.entries(saved[DOWNLOAD_JOB_MAP_KEY] || {}),...inFlightBrowserDownloads.entries()]);
  for (const [downloadId,meta] of owned) if(meta.taskId===taskId) await Promise.resolve(chrome.downloads.cancel?.(Number(downloadId))).catch(()=>{});
}
async function cancelQueuedTask(taskId) {
  await loadSubmissionQueue();
  const entry = [...submissionQueue.values()].find(item => item.clientId === taskId || item.taskId === taskId);
  if (!entry) {
    const result = await request(`/v1/downloads/${encodeURIComponent(taskId)}/cancel`, {method:"POST",body:"{}"});
    return result.job;
  }
  entry.cancelRequested = true;
  // A never-started offline submission can be cancelled without sending it.
  if (!entry.taskId && !entry.submitting && !entry.submissionAttempted) {
    entry.status = "cancelled"; entry.lastJob = {id:entry.clientId,url:entry.url,title:entry.title,status:"cancelled",progress:0};
    queuedMediaHints.delete(entry.clientId);
  }
  await saveSubmissionQueue();
  if(entry.taskId)await cancelOwnedBrowserDownloads(entry.taskId);
  await flushSubmissionQueue();
  return queuedTaskView(entry);
}
async function checkExistingDownloads(url) {
  // 提交前预检（先生 2026-10-08）：同视频已有任务时页面先确认再提交。
  // 以桌面任务库为准——扩展自己的队列看不到其他入口（桌面粘贴/弹窗/
  // 上次会话）。失败视为无既有任务，服务端去重仍兜底。
  const target = String(url || "").trim();
  if (!target) return { ok: true, active: false, done: false };
  try {
    const result = await request(`/v1/downloads?limit=50&url=${encodeURIComponent(target)}`);
    const jobs = Array.isArray(result?.jobs) ? result.jobs : [];
    const activeJobs = jobs.filter((job) => !["done", "failed", "cancelled"].includes(String(job?.status || "")));
    const active = activeJobs.length > 0;
    // 先生 2026-10-18：重复点「转写」要即时反馈——预检需区分
    // 「进行中且已带转写」（提示后无需再提交）与「进行中未带转写」（照常提交，
    // 服务端就地补置转写旗标）。
    const activeTranscribing = activeJobs.some((job) => Boolean(job?.options?.transcribe_after_download));
    const done = jobs.some((job) => String(job?.status || "") === "done");
    return { ok: true, active, done, activeTranscribing };
  } catch {
    return { ok: true, active: false, done: false };
  }
}

async function taskStateForUrl(url) {
  await loadSubmissionQueue();
  const canonical = safeFailurePageUrl(url);
  const entries = [...submissionQueue.values()].filter(entry => entry.url === canonical).sort((a,b)=>b.createdAt-a.createdAt);
  const job = entries[0] ? queuedTaskView(entries[0]) : [...localTaskWatchers.values()].map(state=>state.lastJob).find(job=>job?.url===canonical);
  return job || null;
}
void resumeBackgroundQueue().catch(() => {});

// 提交失败的页面提示必须带下一步指引（先生 2026-10-14：重载后只见
// “本地任务未能创建”，没有任何反馈或解决办法的引导）。
function submissionFailureNotice(error) {
  const code = String(error?.code || "");
  const detail = safeFailureText(error?.message || "", 160);
  if (code === "login_required") return "请先在 FoilPage 桌面端登录，再回到本页重试下载。";
  if (["unauthorized", "local_api_token_missing", "pairing_closed"].includes(code)) {
    return "浏览器扩展尚未连接或连接已失效。请在 FoilPage「浏览器」页允许连接，再打开扩展重新连接。";
  }
  if (code === "douyin_verification_required") {
    return "抖音校验暂拦了后台解析。请留在当前视频页让视频播放 2 秒，再点一次「下载」；仍失败请到 FoilPage 任务页查看。";
  }
  if (["local_api_unavailable", "local_api_timeout"].includes(code)) {
    return "FoilPage 桌面端未连接。请打开或重启 FoilPage，确认它在运行后回到本页再点「下载」。";
  }
  if (detail) return `下载任务未能创建：${detail}。请到 FoilPage 任务页查看原因后重试。`;
  return "下载任务未能创建。请确认 FoilPage 桌面端正在运行；仍失败请到 FoilPage 任务页查看原因。";
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  void selfUpdateCheck();
  if (message?.type === "shengjian-authorized-download") {
    void withExtensionOperation(() => submitAuthorizedDownload(message))
      .then((job) => sendResponse({ ok: !job?.submission_failed, job, feedback_sent:Boolean(job?.submission_failed), message:job?.submission_failed ? job.error?.message || "下载未能提交" : "" }))
      .catch((error) => sendResponse({ ok: false, message: submissionFailureNotice(error) }));
    return true;
  }
  if (message?.type === "shengjian-page-submit") {
    void withExtensionOperation(() => submitPageDownload(message))
      .then((job) => sendResponse({ ok: !job?.submission_failed, job, feedback_sent:Boolean(job?.submission_failed), message:job?.submission_failed ? job.error?.message || "下载未能提交" : "" }))
      .catch((error) => sendResponse({ ok: false, message: submissionFailureNotice(error) }));
    return true;
  }
  if (message?.type === "shengjian-check-existing") {
    void checkExistingDownloads(String(message.url || ""))
      .then(result => sendResponse(result))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "shengjian-task-state") {
    void taskStateForUrl(message.url).then(job => sendResponse({ok:true,job})).catch(() => sendResponse({ok:false}));
    return true;
  }
  if (message?.type === "shengjian-cancel-task") {
    void withExtensionOperation(() => cancelQueuedTask(String(message.id || "")))
      .then(job => sendResponse({ok:true,job})).catch(error => sendResponse({ok:false,message:error?.message || "暂时无法取消"}));
    return true;
  }
  if (message?.type === "shengjian-capture-diagnostic") {
    void request("/v1/diagnostics/douyin-capture", {
      method: "POST",
      body: JSON.stringify(message.diagnostic || {}),
    })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "shengjian-authorized-status") {
    void status()
      .then((tasks) => sendResponse({ ok: true, tasks, connection: desktopConnectionState }))
      .catch((error) => sendResponse({ ok: false, tasks: [], message: error?.message || "任务状态暂不可用" }));
    return true;
  }
  return undefined;
});

// 测试/诊断钩子：挂在 MV3 service worker 全局上，页面脚本不可达；
// 仅供扩展运行时与 Node 测试夹具动态 import 后调用。
globalThis.__shengjianBackgroundInternals = { safeRelativeFilename, flushPendingImports, restoreWatchedTasks, submissionFailureNotice };
