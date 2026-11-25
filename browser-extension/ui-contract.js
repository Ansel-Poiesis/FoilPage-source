// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
(function (global) {
  "use strict";

  const TERMINAL = Object.freeze(["done", "failed", "cancelled"]);
  const STATUS_NAMES = Object.freeze({
    received: "等待下载",
    resolving: "正在解析",
    downloading: "正在下载",
    awaiting_browser: "正在下载",
    handoff_pending: "等待 FoilPage 连接",
    imported: "已导入",
    queued: "转写排队中",
    transcribing: "正在转写",
    running: "正在转写",
    cancelled: "已取消",
    done: "下载成功",
    failed: "失败",
  });
  const FALLBACK_PROGRESS = Object.freeze({
    downloading: 0,
    handoff_pending: 0,
    imported: 0,
    queued: 0,
    done: 1,
  });
  const PILL_HOLD_MS = Object.freeze({ done: 4500 });
  const PAIRING_CODES = Object.freeze([
    "unauthorized", "origin_denied", "local_api_token_missing", "pairing_closed",
  ]);
  const ERROR_CATEGORIES = Object.freeze({
    bridge_unavailable: "connection",
    local_api_unavailable: "connection",
    local_api_timeout: "connection",
    network_timeout: "connection",
    network_error: "connection",
    unauthorized: "pairing",
    origin_denied: "pairing",
    local_api_token_missing: "pairing",
    pairing_closed: "pairing",
    cancelled: "cancelled",
    login_required: "authentication",
    douyin_verification_required: "verification",
    media_forbidden: "permission",
    agent_proxy_unsupported: "configuration",
    local_api_http_error: "http",
  });

  const BRAND = Object.freeze({
    feedback: Object.freeze({
      text: "#f0f4f1", text2: "#d5ddd7", muted: "#b4c0b8", border: "#46524a", focus: "#bee6ca",
    }),
    dark: Object.freeze({
      green: Object.freeze({ primary: "#a6d7b7", hover: "#bee6ca", dim: "#35493c", soft: "#91bea0", onPrimary: "#203027" }),
      ink: Object.freeze({ primary: "#e3e8e3", hover: "#ffffff", dim: "#303631", soft: "#b8c1b8", onPrimary: "#151816" }),
      rose: Object.freeze({ primary: "#ffb0b8", hover: "#ffc9ce", dim: "#43292d", soft: "#e88d98", onPrimary: "#30181b" }),
      indigo: Object.freeze({ primary: "#b7c7ff", hover: "#d0daff", dim: "#29324d", soft: "#94a9ec", onPrimary: "#18213b" }),
    }),
    paper: Object.freeze({
      green: Object.freeze({ primary: "#3f6f54", hover: "#2f5541", dim: "#eef2ef", soft: "#87a794", onPrimary: "#ffffff" }),
      ink: Object.freeze({ primary: "#272c28", hover: "#111512", dim: "#e9ece8", soft: "#7b857d", onPrimary: "#ffffff" }),
      rose: Object.freeze({ primary: "#a94351", hover: "#85333f", dim: "#f8eaec", soft: "#c97781", onPrimary: "#ffffff" }),
      indigo: Object.freeze({ primary: "#415aa8", hover: "#30457f", dim: "#ebeffb", soft: "#8293d1", onPrimary: "#ffffff" }),
    }),
    accents: Object.freeze([
      Object.freeze({ key: "green", label: "松绿", color: "#2c8753" }),
      Object.freeze({ key: "rose", label: "霞红", color: "#e75d74" }),
      Object.freeze({ key: "indigo", label: "靛蓝", color: "#4f68d7" }),
      Object.freeze({ key: "ink", label: "墨色", color: "#2f3437" }),
    ]),
    pill: Object.freeze({
      background: "#26362d", border: "#53695b", text: "#eef5ef",
      doneBackground: "#e3efe8", doneText: "#28563c", doneBorder: "#b8d5c3",
      errorBackground: "#472e36", errorBorder: "#a6616f", errorText: "#ffe5ea",
    }),
    content: Object.freeze({
      cardBackground: "rgba(33,47,39,.96)", cardText: "#eef3ed", cardBorder: "rgba(154,205,174,.28)",
      dot: "#9acdae", dotGlow: "rgba(154,205,174,.5)", hint: "#c0d0c4", onAccent: "#102016",
      buttonBorder: "rgba(154,205,174,.35)", secondaryText: "#c5d0c7", focus: "#cee7d5",
      errorBackground: "#443038", errorBorder: "rgba(240,144,154,.5)",
    }),
    popup: Object.freeze({
      light: Object.freeze({
        paper: "#eef1e9", surface: "#f8faf4", wash: "#e8eee3", line: "#d2dacf",
        ink: "#24362d", muted: "#57695d", accent: "#36664c", primary: "#254b36",
        primaryHover: "#326046", onPrimary: "#f8fbf6", successBg: "#dceade",
        warn: "#7d4e25", warnBg: "#f4eadd", error: "#973746", errorBg: "#f6e8e9",
      }),
      dark: Object.freeze({
        paper: "#222d27", surface: "#2a382f", wash: "#334339", line: "#46574c",
        ink: "#edf3eb", muted: "#b6c5b7", accent: "#b3d9b8", primary: "#c5e2c4",
        primaryHover: "#d9eed8", onPrimary: "#21332a", successBg: "#344b3a",
        warn: "#f1c39b", warnBg: "#44372e", error: "#f6b6be", errorBg: "#4b353b",
      }),
    }),
  });

  function isTerminalTask(status) {
    return TERMINAL.includes(status);
  }

  function progressFor(task) {
    const value = task?.progress == null ? NaN : Number(task.progress);
    if (Number.isFinite(value)) return Math.max(0, Math.min(1, value));
    return FALLBACK_PROGRESS[task?.status] || 0;
  }

  function percent(task) {
    return Math.round(progressFor(task) * 100);
  }

  function progressLabelFor(task) {
    if (task?.cancel_requested && !isTerminalTask(task?.status)) return "等待连接后取消";
    if (task?.status === "awaiting_browser" && task.error?.diagnostic?.fallback_pending) return "正在重试下载";
    if (task?.status === "failed" && task.error?.code === "douyin_verification_required") return "抖音要求网页校验，请在视频页完成验证后重试";
    const name = task?.status === "done" && task?.job_id ? task.transcription_label || "转写成功" : STATUS_NAMES[task?.status] || task?.status || "等待中";
    let label = name;
    if (["downloading", "awaiting_browser", "transcribing", "running"].includes(task?.status)
        && Number.isFinite(Number(task?.progress)) && task.progress != null) {
      label = `${name} ${percent(task)}%`;
    }
    return task?.waiting_connection && !isTerminalTask(task?.status) ? `${label}（连接恢复后继续）` : label;
  }

  function rowStatusText(task) {
    const status = task.status;
    if (status === "downloading") return task.is_gallery ? "正在下载图集" : "正在下载";
    if (["received", "resolving"].includes(status)) return "解析链接";
    if (status === "awaiting_browser") return "等待浏览器下载";
    if (["imported", "queued"].includes(status)) return "等待转写";
    if (["transcribing", "running"].includes(status)) return "正在转写";
    if (status === "done") {
      if (task.error_details?.diagnostic?.transcript_preserved) return "原稿已保留，AI 处理未完成";
      if (task.is_gallery) return "图文已保存";
      const onlyDownloaded = task.kind === "download" && !task.final_path && !task.raw_path;
      return onlyDownloaded ? "仅完成下载" : "已完成";
    }
    if (status === "retryable") return "等待重试";
    if (status === "failed") return "处理失败";
    if (status === "cancelled") return "已取消";
    return task.progress_text || "等待处理";
  }

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
    const value = text.match(/(\d+)%/);
    if (value) return { mode: "percent", percent: Math.max(0, Math.min(100, Number(value[1]))) };
    if (/转写/.test(text)) return { mode: "text", text: "正在转写" };
    if (/解析/.test(text)) return { mode: "text", text: "正在解析" };
    if (/浏览器下载中/.test(text)) return { mode: "text", text: "浏览器下载中" };
    if (/已在处理|已合并/.test(text)) return { mode: "text", text: "已在处理中" };
    if (/改用|切换|兜底/.test(text)) return { mode: "text", text: "已换通道重试" };
    if (/已加入|已提交|已接收/.test(text)) return { mode: "text", text: "已提交下载" };
    if (/提交|读取/.test(text)) return { mode: "text", text: "正在提交" };
    return { mode: "text", text: text.replace(/（[^）]*）/g, "") };
  }

  function pillHoldFor(text, kind) {
    const length = Math.max(1, String(text || "").replace(/（[^）]*）/g, "").length);
    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
    if (kind === "done") return PILL_HOLD_MS.done;
    if (kind === "error") return clamp(2600 + 280 * length, 4000, 8000);
    return clamp(1200 + 220 * length, 2000, 4000);
  }

  function errorInfo(code, status = 0, retryable) {
    const raw = String(code || "");
    const safeCode = /^[A-Za-z0-9_.-]{1,64}$/.test(raw) ? raw : "local_api_error";
    const category = ERROR_CATEGORIES[safeCode] || (Number(status) >= 400 ? "http" : "unknown");
    const inferredRetryable = category === "connection"
      || [408, 425, 429].includes(Number(status)) || Number(status) >= 500;
    return {
      code: safeCode,
      category,
      retryable: typeof retryable === "boolean" ? retryable : inferredRetryable,
    };
  }

  function needsDesktopConnection(code) {
    return PAIRING_CODES.includes(code);
  }

  function isTemporaryOffline(error) {
    return errorInfo(error?.code, error?.status, error?.retryable).retryable;
  }

  global.FoilPageContract = Object.freeze({
    TERMINAL,
    STATUS_NAMES,
    PILL_HOLD_MS,
    PAIRING_CODES,
    BRAND,
    ACCENTS: BRAND.accents,
    isTerminalTask,
    progressFor,
    percent,
    progressLabelFor,
    rowStatusText,
    pillStatusFor,
    pillHoldFor,
    errorInfo,
    needsDesktopConnection,
    isTemporaryOffline,
  });
})(globalThis);
