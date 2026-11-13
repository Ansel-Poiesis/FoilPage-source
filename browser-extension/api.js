// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
const API_BASE = "http://127.0.0.1:18765";
const REQUEST_TIMEOUT_MS = 30000;

export class LocalApiError extends Error {
  constructor(message, { code = "local_api_error", status = 0, detail = "" } = {}) {
    super(message);
    this.name = "LocalApiError";
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new LocalApiError("本地服务响应超时，请确认 FoilPage 正在运行后重试", {
        code: "local_api_timeout", detail: error?.name || "AbortError",
      });
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function getToken() {
  const { localApiToken = "" } = await chrome.storage.local.get("localApiToken");
  return localApiToken.trim();
}

export async function setToken(value) {
  await chrome.storage.local.set({ localApiToken: String(value || "").trim() });
}

export async function health() {
  let response;
  try {
    response = await fetchWithTimeout(`${API_BASE}/health`, { cache: "no-store" });
  } catch {
    throw new Error("请先打开 FoilPage 桌面端");
  }
  if (!response.ok) throw new Error("本地服务不可用");
  const data = await response.json().catch(() => ({}));
  if (data.service !== "VideoToArticle" || data.api_version !== "1") {
    throw new Error("FoilPage 本地服务未启动");
  }
  return data;
}

export async function pair() {
  let response;
  try {
    response = await fetchWithTimeout(`${API_BASE}/v1/pair`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
  } catch {
    throw new Error("请先打开 FoilPage 桌面端");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error?.message || `连接失败 ${response.status}`);
  if (typeof data.token !== "string" || data.token.length < 32) {
    throw new Error("FoilPage 返回了无效连接凭据");
  }
  await setToken(data.token);
  return data;
}

export async function setup() {
  let response;
  try {
    response = await fetchWithTimeout(`${API_BASE}/v1/setup`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        launch_at_startup: true,
        background_mode: true,
        worker_auto_start: true
      })
    });
  } catch {
    throw new Error("请先启动一次 FoilPage 桌面端");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error?.message || `配置失败 ${response.status}`);
  if (typeof data.token !== "string" || data.token.length < 32) {
    throw new Error("FoilPage 返回了无效连接凭据");
  }
  await setToken(data.token);
  return data;
}

export async function request(path, options = {}) {
  const token = await getToken();
  if (!token) throw new LocalApiError("请先填写连接令牌", { code: "local_api_token_missing" });
  let response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, {
      ...options,
      cache: "no-store",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
        ...(options.headers || {})
      }
    });
  } catch (error) {
    if (error instanceof LocalApiError) throw error;
    throw new LocalApiError("FoilPage 连接已中断，请确认桌面端正在运行", {
      code: "local_api_unavailable",
      detail: error?.message || error?.name || "NetworkError",
    });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new LocalApiError(
    data?.error?.message || `本地服务请求失败（HTTP ${response.status}）`,
    {
      code: data?.error?.code || "local_api_http_error",
      status: response.status,
      detail: data?.error?.message || "",
    },
  );
  return data;
}

export function supportedUrl(value) {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return ["bilibili.com", "b23.tv", "douyin.com", "youtube.com", "youtu.be"]
      .some((domain) => host === domain || host.endsWith(`.${domain}`));
  } catch {
    return false;
  }
}
