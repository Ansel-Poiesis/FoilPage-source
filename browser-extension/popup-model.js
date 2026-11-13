// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
export const statusNames = Object.freeze({
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

const fallbackProgress = Object.freeze({
  downloading: 0,
  handoff_pending: 0,
  imported: 0,
  queued: 0,
  done: 1,
});

export function progressFor(task) {
  const value = task?.progress == null ? NaN : Number(task.progress);
  if (Number.isFinite(value)) return Math.max(0, Math.min(1, value));
  return fallbackProgress[task?.status] || 0;
}

export function progressLabelFor(task) {
  if (task?.cancel_requested && !["done","failed","cancelled"].includes(task?.status)) return "等待连接后取消";
  const name = task?.status === "done" && task?.job_id ? task.transcription_label || "转写成功" : statusNames[task?.status] || task?.status || "等待中";
  let label = name;
  if (['downloading', 'awaiting_browser', 'transcribing', 'running'].includes(task?.status) && Number.isFinite(Number(task?.progress)) && task.progress != null) {
    label = `${name} ${Math.round(progressFor(task) * 100)}%`;
  }
  return task?.waiting_connection && !["done","failed","cancelled"].includes(task?.status) ? `${label}（连接恢复后继续）` : label;
}
