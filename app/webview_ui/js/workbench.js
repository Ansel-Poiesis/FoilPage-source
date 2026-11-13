// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 工作台页：导入、批量选择、筛选、任务列表、详情 inspector */
'use strict';

window.Workbench = (() => {
  const FILTERS = ["全部", "处理中", "需处理", "已完成"];
  const ACTIVE = ["queued", "running", "retryable", "received", "resolving", "awaiting_browser", "downloading", "imported", "transcribing"];
  const NEED = ["failed", "retryable", "awaiting_browser", "received", "resolving"];
  const TERMINAL = ["done", "failed", "cancelled"];
  let rangeBaseline = null, rangeAnchor = null, visibleSelectionKey = "", tasksRequest = 0;

  /* TASK-8.17 平台封面：会话级缓存（本地预览服务器的 token 地址），按任务
     懒取；cover_path 变化（重试后重下）会重新取。 */
  const coverCache = new Map();
  function coverUrlFor(task) {
    if (!task.cover_path) return "";
    const hit = coverCache.get(task.task_id);
    return hit && hit.path === task.cover_path ? hit.url : "";
  }
  function ensureCover(task) {
    if (!task.cover_path) return;
    const hit = coverCache.get(task.task_id);
    if (hit && hit.path === task.cover_path) return;
    coverCache.set(task.task_id, { path: task.cover_path, url: "" });
    AppApi.taskCover(task.task_id).then((res) => {
      if (!res || !res.ok || !res.url) return;
      const entry = coverCache.get(task.task_id);
      if (!entry || entry.path !== task.cover_path) return;
      entry.url = res.url;
      const thumb = document.querySelector(`.job[data-task-id="${CSS.escape(task.task_id)}"] .job-cover`);
      if (thumb) thumb.style.backgroundImage = `url("${res.url}")`;
      const big = document.querySelector(".inspector-cover");
      if (big && big.dataset.coverTask === task.task_id) big.style.backgroundImage = `url("${res.url}")`;
    }).catch(() => {});
  }

  function statusColor(status) {
    if (status === "done") return "var(--primary)";
    if (status === "failed") return "var(--danger)";
    if (status === "cancelled") return "var(--faint)";
    if (ACTIVE.includes(status)) return "var(--amber)";
    return "var(--muted)";
  }

  function percent(task) {
    const value = Number(task.progress);
    return Number.isFinite(value) ? Math.round(Math.max(0, Math.min(1, value)) * 100) : 0;
  }

  /* TASK-8.18 图集：检查器网格懒取（token URL 会话缓存，目录变更重取）。 */
  const galleryCache = new Map();
  function ensureGallery(task, host) {
    if (!task.is_gallery || task.status !== "done") return;
    const key = `${task.task_id}@${task.downloaded_path || ""}`;
    if (galleryCache.has(key)) {
      if (galleryCache.get(key).length) renderGallery(host, galleryCache.get(key));
      return;
    }
    galleryCache.set(key, []);
    host.dataset.galleryTask = task.task_id;
    AppApi.taskGallery(task.task_id).then((res) => {
      if (!res || !res.ok || !Array.isArray(res.urls) || !res.urls.length) return;
      galleryCache.set(key, res.urls);
      // 异步回来时检查器可能已切到别的任务：按任务令牌校验后再渲染。
      if (host.dataset.galleryTask === task.task_id) renderGallery(host, res.urls);
    }).catch(() => {});
  }

  function renderGallery(host, urls) {
    let grid = host.querySelector(".inspector-grid");
    if (!grid) {
      grid = document.createElement("div");
      grid.className = "inspector-grid";
      host.querySelector(".inspector")?.prepend(grid);
    }
    grid.innerHTML = "";
    urls.slice(0, 60).forEach((url) => {
      const cell = document.createElement("span");
      cell.className = "inspector-grid-cell";
      cell.setAttribute("role", "img");
      cell.setAttribute("aria-label", "图集图片");
      cell.style.backgroundImage = `url("${url}")`;
      grid.appendChild(cell);
    });
  }

  function pipeline(task) {
    return stageLines(task).map(line => ({
      label: line.label, detail: line.text,
      state: { on: "done", cur: "active", idle: "pending", fail: "failed" }[line.state],
    }));
  }

  function sorted(tasks) {
    const order = (AppState.settings || {}).task_sort_order || "newest";
    return [...tasks].sort((a, b) => {
      const ta = String(a.updated_at || "");
      const tb = String(b.updated_at || "");
      if (!ta && !tb) return 0;
      if (!ta) return 1;
      if (!tb) return -1;
      return order === "oldest" ? ta.localeCompare(tb) : tb.localeCompare(ta);
    });
  }

  function filtered(tasks) {
    const f = AppState.filter;
    let rows;
    if (f === "全部") rows = tasks;
    else if (f === "处理中") rows = tasks.filter((t) => ACTIVE.includes(t.status));
    else if (f === "需处理") rows = tasks.filter((t) => NEED.includes(t.status));
    else if (f === "已完成") rows = tasks.filter((t) => t.status === "done");
    else rows = tasks;
    return sorted(rows);
  }

  function countFor(filter) {
    const tasks = AppState.tasks || [];
    if (filter === "全部") return tasks.length;
    if (filter === "处理中") return tasks.filter((t) => ACTIVE.includes(t.status)).length;
    if (filter === "需处理") return tasks.filter((t) => NEED.includes(t.status)).length;
    if (filter === "已完成") return tasks.filter((t) => t.status === "done").length;
    return 0;
  }

  function buildLayout() {
    const host = document.getElementById("page-workbench");
    host.innerHTML = `
      <div class="wb-split">
        <div class="wb-left">
          <div class="filters" id="jobFilters"></div>
          <div class="job-list" id="jobList" role="list" aria-label="任务列表"></div>
        </div>
        <aside class="wb-right" id="inspector" aria-label="任务详情"></aside>
      </div>`;
  }

  function renderFilters() {
    const wrap = document.getElementById("jobFilters");
    if (!wrap) return;
    wrap.innerHTML = "";
    FILTERS.forEach((f) => {
      const el = document.createElement("button");
      el.type = 'button';
      el.className = "filter" + (f === AppState.filter ? " active" : "");
      el.setAttribute("aria-pressed", String(f === AppState.filter));
      el.dataset.filter = f;
      el.innerHTML = `${f}<span class="filter-count">${countFor(f)}</span>`;
      el.onclick = () => { AppState.filter = f; render(); };
      wrap.appendChild(el);
    });
    const summary = AppState.summary || {};
    if (summary.total) {
      const progress = document.createElement("span");
      progress.className = "filters-progress";
      progress.title = `${summary.completed ?? summary.terminal ?? 0}/${summary.total} 项已完成`;
      const label = document.createElement("span");
      label.className = "fp-label";
      label.textContent = "完成度 ";
      const value = document.createElement("b");
      value.textContent = Math.round((summary.progress || 0) * 100) + "%";
      label.appendChild(value);
      progress.append(label);
      wrap.appendChild(progress);
    }
    const clearBtn = document.createElement("button");
    clearBtn.className = "icon-btn clear-completed";
    clearBtn.innerHTML = AppIcons('archive');
    clearBtn.setAttribute('aria-label', '归档已完成转写');
    clearBtn.title = "把已完成的转写记录从工作台清除，文本库结果保留";
    clearBtn.onclick = clearCompleted;
    wrap.appendChild(clearBtn);
  }

  function clearCompleted() {
    const doneCount = (AppState.tasks || []).filter((t) => t.status === "done").length;
    if (doneCount === 0) { toast("没有可清除的完成记录"); return; }
    ConfirmDelete.run({
      message: `把 ${doneCount} 条已完成记录从工作台清除？文本库结果保留，视频与文件不删除。`,
      onConfirm: () => {
        AppApi.clearCompleted().then((res) => {
          const n = res && Array.isArray(res.removed) ? res.removed.length : 0;
          if (res?.ok === false) { toast(res.error || '清除失败，请重试', 'error'); return; }
          refreshAll();
          toast(n > 0 ? `已清除 ${n} 条完成记录` : '没有可清除的完成记录', n > 0 ? '' : 'error');
        }).catch(() => toast('清除失败，请重试', 'error'));
      },
    });
  }

  function selectedTasks() {
    const tasks = AppState.tasks || [];
    return tasks.filter((t) => AppState.selected.has(t.task_id));
  }

  function deleteTask(task) {
    ConfirmDelete.run({
      message: `确定要删除「${task.display_title || "该任务"}」吗？相关文件将一并移除，删除后不可恢复。`,
      onConfirm: () => AppApi.deleteTasks([task.task_id]).then((res) => {
        const n = res && Array.isArray(res.removed) ? res.removed.length : 0;
        if (!n) { toast(res?.error || '删除失败', 'error'); return; }
        refreshAll(); toast('已删除');
      }).catch(() => toast('删除失败，请重试', 'error')),
    });
  }

  /* 多选时的右栏批量面板（原左栏批量条已按先生裁决移除并入此处：
     多选时右栏原本空白，正好承载批量操作）。 */
  function renderMultiInspector(host) {
    const sel = selectedTasks();
    const count = AppState.selected.size;
    const activeN = sel.filter((t) => ACTIVE.includes(t.status)).length;
    const doneN = sel.filter((t) => t.status === "done").length;
    const failedN = sel.filter((t) => ["failed", "cancelled"].includes(t.status)).length;
    const canRetry = sel.some((t) => TERMINAL.includes(t.status));
    const canCancel = activeN > 0;
    const canAgent = doneN > 0;
    host.innerHTML = `
      <div class="inspector">
        <div class="t">已选 ${count} 项</div>
        <div class="meta-line"><span>进行中</span><span>${activeN}</span></div>
        <div class="meta-line"><span>已完成</span><span>${doneN}</span></div>
        <div class="meta-line"><span>失败/取消</span><span>${failedN}</span></div>
        <div class="actions"></div>
      </div>`;
    const actions = host.querySelector(".actions");
    const addBtn = (label, css, disabled, fn) => {
      const b = document.createElement("button");
      b.className = "btn " + css;
      b.textContent = label;
      b.disabled = disabled;
      b.onclick = () => fn();
      actions.appendChild(b);
    };
    addBtn("重试/重新转写", "ghost small", !canRetry, () => {
      const ids = [...AppState.selected];
      const downs = ids.filter((id) => id.startsWith("dl:"));
      const jobs = ids.filter((id) => id.startsWith("job:"));
      const steps = [];
      if (downs.length) {
        steps.push(AppApi.retryDownloads(downs).then((res) => {
          const n = res && res.retried ? res.retried.length : 0;
          const need = res && res.needs_page ? res.needs_page.length : 0;
          if (n) toast(`已重新提交 ${n} 个下载`);
          if (need) toast(`${need} 个抖音任务需回视频页重新点击下载（地址有时效，建议先收藏）`, "error");
        }).catch(() => toast("下载重试失败", "error")));
      }
      if (jobs.length) {
        steps.push(AppApi.retranscribe(jobs).then((res) => {
          const n = res && res.created ? res.created.length : 0;
          if (n) toast(`已重建 ${n} 个转写任务`);
        }));
      }
      Promise.all(steps).then(() => { AppState.selected.clear(); refreshAll(); });
    });
    addBtn("批量整理", "ghost small", !canAgent, () => {
      pickEditingLevel((level) => {
      AppApi.batchAgentTranslate([...AppState.selected], level).then((res) => {
        if (!res || !res.ok) { toast((res && res.error) || "批量整理启动失败", "error"); return; }
        toast(`已开始批量整理 ${res.count} 项${level ? "（" + levelLabel(level) + "）" : ""}`);
        pollBatchAgent();
      }).catch((e) => toast("批量整理失败：" + (e && e.message || e), "error"));
      });
    });
    addBtn("取消", "ghost small", !canCancel, () => {
      AppApi.cancel([...AppState.selected]).then(() => { AppState.selected.clear(); refreshAll(); });
    });
    addBtn("删除", "danger small", false, () => {
      const ids = [...AppState.selected];
      ConfirmDelete.run({
        message: `确定要删除选中的 ${ids.length} 个任务吗？相关文件将一并移除，删除后不可恢复。`,
        onConfirm: () => {
          AppApi.deleteTasks(ids).then((res) => {
            const n = res && Array.isArray(res.removed) ? res.removed.length : 0;
            if (!n) { toast(res?.error || '删除失败', 'error'); return; }
            refreshAll(); toast(`已删除 ${n} 项`);
          }).catch(() => toast('删除失败，请重试', 'error'));
        },
      });
    });
  }

  function pollBatchAgent() {
    const timer = setInterval(() => {
      AppApi.batchAgentStatus().then((st) => {
        if (!st) return;
        if (st.running) {
          toast(st.message || "批量整理进行中…");
          return;
        }
        clearInterval(timer);
        AppState.selected.clear();
        refreshAll();
        const msg = (st.errors && st.errors.length)
          ? st.message + "，例如 " + st.errors[0]
          : st.message;
        toast(msg, st.failed > 0 ? "error" : "");
      }).catch(() => clearInterval(timer));
    }, 2500);
  }

  function reconcileSelection(rows) {
    const ids = rows.map(task => task.task_id);
    const visible = new Set(ids);
    AppState.selected = new Set([...AppState.selected].filter(id => visible.has(id)));
    if (!visible.has(AppState.anchorId)) AppState.anchorId = null;
    const key = ids.join("|");
    if (key !== visibleSelectionKey) {
      visibleSelectionKey = key;
      rangeBaseline = null;
      rangeAnchor = null;
    }
  }

  function selectTask(rows, currentId, event, checkbox) {
    const cur = rows.findIndex(task => task.task_id === currentId);
    if (cur < 0) return;
    const anchor = rows.findIndex(task => task.task_id === AppState.anchorId);
    if (event.shiftKey && anchor >= 0) {
      if (rangeAnchor !== AppState.anchorId || !rangeBaseline) {
        rangeAnchor = AppState.anchorId;
        rangeBaseline = new Set(AppState.selected);
      }
      const ids = rows.slice(Math.min(anchor, cur), Math.max(anchor, cur) + 1).map(task => task.task_id);
      const additive = checkbox || event.ctrlKey || event.metaKey;
      AppState.selected = additive ? new Set(rangeBaseline) : new Set();
      const selecting = !checkbox || rangeBaseline.has(AppState.anchorId);
      ids.forEach(id => selecting ? AppState.selected.add(id) : AppState.selected.delete(id));
    } else {
      if (checkbox || event.ctrlKey || event.metaKey) {
        AppState.selected.has(currentId) ? AppState.selected.delete(currentId) : AppState.selected.add(currentId);
      } else {
        AppState.selected = new Set([currentId]);
      }
      AppState.anchorId = currentId;
      rangeBaseline = null;
      rangeAnchor = null;
    }
    render();
  }

  /* 单个任务右侧的双阶段进度：每个百分比只属于当前阶段，完成后显示语义化状态。 */
  function stageLines(task) {
    const st = task.status, isDownload = task.kind === "download";
    const error = ["failed", "retryable"].includes(st), cancelled = st === "cancelled";
    const downloaded = !isDownload || ["imported", "queued", "transcribing", "running", "done"].includes(st) || !!task.downloaded_path;
    let download = { label: isDownload ? "下载" : "导入", state: "idle", text: "未开始" };
    if (downloaded) download = { ...download, state: "on", text: "完成" };
    else if (error) download = { ...download, state: "fail", text: st === "retryable" ? "待重试" : "失败" };
    else if (cancelled) download.text = "已取消";
    else if (st === "downloading") download = { ...download, state: "cur", text: percent(task) + "%" };
    else if (["received", "resolving", "awaiting_browser"].includes(st)) {
      download = { ...download, state: "cur", text: { received: "已接收", resolving: "解析中", awaiting_browser: "等待下载" }[st] };
    }
    const onlyDownloaded = isDownload && st === "done" && !task.final_path && !task.raw_path;
    let transcribe = { label: "转写", state: "idle", text: "未开始" };
    if (onlyDownloaded) transcribe.text = "未选择";
    else if (st === "done") transcribe = { ...transcribe, state: "on", text: "完成" };
    else if (error && downloaded) transcribe = { ...transcribe, state: "fail", text: st === "retryable" ? "待重试" : "失败" };
    else if (cancelled && downloaded) transcribe.text = "已取消";
    else if (["queued", "imported"].includes(st)) transcribe = { ...transcribe, state: "cur", text: "排队中" };
    else if (["transcribing", "running"].includes(st)) transcribe = { ...transcribe, state: "cur", text: percent(task) + "%" };
    return [download, transcribe];
  }

  /* 任务行的副标题只说明当前阶段；具体百分比放在右侧阶段列，避免同一个数字出现两次。 */
  function rowStatusText(task) {
    const st = task.status;
    if (st === "downloading") return task.is_gallery ? "正在下载图集" : "正在下载";
    if (["received", "resolving"].includes(st)) return "解析链接";
    if (st === "awaiting_browser") return "等待浏览器下载";
    if (["imported", "queued"].includes(st)) return "等待转写";
    if (["transcribing", "running"].includes(st)) return "正在转写";
    if (st === "done") {
        if (task.error_details?.diagnostic?.transcript_preserved) return "原稿已保留，AI 处理未完成";
      if (task.is_gallery) return "图文已保存";
      const onlyDownloaded = task.kind === "download" && !task.final_path && !task.raw_path;
      return onlyDownloaded ? "仅完成下载" : "已完成";
    }
    if (st === "retryable") return "等待重试";
    if (st === "failed") return "处理失败";
    if (st === "cancelled") return "已取消";
    return task.progress_text || "等待处理";
  }

  function renderList() {
    const list = document.getElementById("jobList");
    if (!list) return;
    const rows = filtered(AppState.tasks || []);
    if (!rows.length) {
      const filteredEmpty = (AppState.tasks || []).length > 0;
      list.innerHTML = `<div class="empty"><span class="empty-symbol" aria-hidden="true">${AppIcons('tasks')}</span><div class="big">${filteredEmpty ? '这里暂时没有任务' : '留下第一段声音'}</div><div class="sub">${filteredEmpty ? '切换筛选，查看其他任务。' : '导入本地媒体，或粘贴视频链接。'}</div>${filteredEmpty ? '' : '<div class="empty-actions"><button class="btn primary small" data-goto="import">导入媒体</button><button class="btn ghost small" data-goto="links">粘贴链接</button></div>'}</div>`;
      list.querySelectorAll("[data-goto]").forEach((b) => { b.onclick = () => switchView(b.dataset.goto); });
      return;
    }
    list.innerHTML = "";
    rows.forEach((task) => {
      const el = document.createElement("div");
      const isActive = ACTIVE.includes(task.status);
      el.className = "job" + (AppState.selected.has(task.task_id) ? " selected" : "") + (isActive ? " active" : "");
      el.dataset.status = task.status;
      el.dataset.taskId = task.task_id;
      el.setAttribute("role", "listitem");
      el.innerHTML = `
        <label class="job-check"><input class="job-check-input" type="checkbox" ${AppState.selected.has(task.task_id) ? "checked" : ""} aria-label="选择任务"></label>
        <button class="job-body job-open" type="button" aria-pressed="${AppState.selected.has(task.task_id)}">
          <span class="job-title"></span>
        </button>
        <div class="job-stages"></div>`;
      if (task.cover_path) {
        const cover = document.createElement("span");
        cover.className = "job-cover";
        cover.setAttribute("aria-hidden", "true");
        const coverUrl = coverUrlFor(task);
        if (coverUrl) cover.style.backgroundImage = `url("${coverUrl}")`;
        el.insertBefore(cover, el.querySelector(".job-body"));
        ensureCover(task);
      }
      el.querySelector(".job-title").textContent = task.display_title || "未命名";
      if (task.is_gallery) {
        const badge = document.createElement("span");
        badge.className = "job-kind-badge";
        badge.textContent = "图文";
        el.querySelector(".job-body").insertBefore(badge, el.querySelector(".job-title"));
      }
      el.querySelector(".job-title").title = task.display_title || '';
      const stagesBox = el.querySelector(".job-stages");
      stageLines(task).forEach((line) => {
        const row = document.createElement("div");
        row.className = "job-stage " + line.state;
        const dot = document.createElement("span");
        dot.className = "st-dot";
        const label = document.createElement("span");
        label.textContent = line.label;
        const val = document.createElement("span");
        val.textContent = line.text;
        row.append(dot, label, val);
        stagesBox.appendChild(row);
      });
      const del = document.createElement("button");
      del.className = "job-delete";
      del.setAttribute("aria-label", "删除任务 " + (task.display_title || "未命名"));
      del.title = "删除";
      del.innerHTML = `<span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></span><span class="lbl">删除</span>`;
      del.onclick = (e) => {
        e.stopPropagation();
        deleteTask(task);
      };
      el.appendChild(del);
      const checkbox = el.querySelector(".job-check input");
      checkbox.setAttribute("aria-label", "选择任务 " + (task.display_title || "未命名"));
      checkbox.onclick = (e) => {
        e.stopPropagation();
        selectTask(rows, task.task_id, e, true);
      };
      el.onclick = (e) => {
        // A label forwards its own click to the checkbox; do not select twice.
        if (e.target.closest(".job-check")) return;
        selectTask(rows, task.task_id, e, false);
      };
      list.appendChild(el);
    });
  }

  function renderInspector() {
    const host = document.getElementById("inspector");
    if (!host) return;
    if (AppState.selected.size > 1) {
      renderMultiInspector(host);
      return;
    }
    const tasks = AppState.tasks || [];
    const selectedId = AppState.selected.size === 1 ? [...AppState.selected][0] : null;
    const task = tasks.find((t) => t.task_id === selectedId) || null;
    if (!task) {
      host.innerHTML = `<div class="empty"><span class="empty-symbol" aria-hidden="true">${AppIcons('tasks')}</span><div class="big">查看任务详情</div><div class="sub">选择一项查看进度与文件；勾选多项，批量处理。</div></div>`;
      return;
    }
    const steps = pipeline(task);
    const err = task.error || "";
    const errorDetails = task.error_details || null;
    const errorCode = String(errorDetails?.code || "unknown").replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 64);
    host.innerHTML = `
      <div class="inspector">
        ${task.cover_path ? `<div class="inspector-cover" data-cover-task="${task.task_id}" role="img" aria-label="任务封面"></div>` : ""}
        <div class="t"></div>
        <div class="pct"></div>
        <div class="prog-track"><i></i></div>
        <div class="pipeline"></div>
        <div class="meta-line"><span>状态</span><span class="ml-status"></span></div>
        <div class="meta-line"><span>分类</span><span class="ml-cat"></span></div>
        <div class="meta-line"><span>更新时间</span><span class="ml-time"></span></div>
        ${err ? `<div class="err"></div>` : ""}
        ${err && errorDetails ? `<details class="task-error-details"><summary>诊断详情（${errorCode}）</summary><div class="task-error-tools"></div><pre class="task-error-json"></pre></details>` : ""}
        <div class="actions"></div>
      </div>`;
    if (task.cover_path) {
      const bigCover = host.querySelector(".inspector-cover");
      const coverUrl = coverUrlFor(task);
      if (coverUrl) bigCover.style.backgroundImage = `url("${coverUrl}")`;
      ensureCover(task);
    }
    host.querySelector(".t").setAttribute('data-i18n-skip','');
    host.querySelector(".t").textContent = task.display_title;
    const pctEl = host.querySelector(".pct");
    const measurable = ["downloading", "running", "transcribing"].includes(task.status);
    pctEl.textContent = measurable ? String(percent(task)) : rowStatusText(task);
    const pctSmall = document.createElement("small");
    pctSmall.textContent = "%";
    if (measurable) pctEl.appendChild(pctSmall);
    host.querySelector('.prog-track').hidden = !measurable;
    host.querySelector('.pipeline').hidden = task.status === 'done';
    host.querySelector(".prog-track i").style.width = percent(task) + "%";
    const mlStatus = host.querySelector(".ml-status");
    mlStatus.style.color = statusColor(task.status);
    mlStatus.textContent = task.progress_text || "-";
    host.querySelector(".ml-cat").textContent = task.result_category || "未分类";
    host.querySelector(".ml-time").textContent = task.updated_at || "-";
    const pipe = host.querySelector(".pipeline");
    steps.forEach((s) => {
      const row = document.createElement("div");
      row.className = "pstep " + s.state;
      row.innerHTML = `<span class="dot"></span><span>${s.label}</span><span style="margin-left:auto">${s.detail}</span>`;
      pipe.appendChild(row);
    });
    if (err) host.querySelector(".err").textContent = err;
    ensureGallery(task, host);
    if (err && errorDetails) {
      const errorJson = JSON.stringify(errorDetails, null, 2);
      host.querySelector(".task-error-json").textContent = errorJson;
      const copyButton = document.createElement("button");
      copyButton.type = "button";
      copyButton.className = "btn ghost small";
      copyButton.textContent = "复制诊断";
      copyButton.onclick = async () => {
        try {
          await navigator.clipboard.writeText(errorJson);
          toast("已复制失败诊断");
        } catch {
          toast("无法访问剪贴板，可选中下方诊断文本复制", "error");
        }
      };
      host.querySelector(".task-error-tools").appendChild(copyButton);
    }

    const actions = host.querySelector(".actions");
    function addBtn(label, css, fn) {
      const b = document.createElement("button");
      b.className = "btn " + css;
      b.textContent = label;
      b.onclick = () => fn();
      actions.appendChild(b);
    }
    addBtn("播放", "ghost small", () => AppApi.playMedia(task));
    addBtn("打开位置", "ghost small", () => AppApi.openPath(task.source_path || task.downloaded_path));
    const resultPath = task.final_path || task.raw_path;
    if (resultPath) addBtn("打开文本", "ghost small", () => AppApi.openPath(resultPath));
    if (task.kind === "download" && task.status === "done") {
      addBtn("转写", "ghost small", () => {
        AppApi.transcribeDownload(task.task_id).then((res) => {
          if (res && res.ok) toast("已加入转写队列");
          else if (res && res.already) toast(res.error || "该任务已在转写队列中，无需重复提交");
          else toast((res && res.error) || "无法开始转写", "error");
          refreshAll();
        });
      });
    } else if (task.kind === "download" && ["failed", "retryable", "cancelled"].includes(task.status)) {
      // 下载行的重试必须走下载轨：抖音失败行的签名/播放地址已失，
      // 桌面端无法自愈——引导回视频页重下；其余平台本地重试可成。
      if (task.platform === "douyin") {
        addBtn("回视频页重下", "ghost small", () => {
          AppApi.openVideoPage(task.task_id).then((res) => {
            if (res && res.ok) toast("已打开视频页：请点页面上的下载按钮（视频地址有时效，建议先收藏）");
            else toast((res && res.error) || "无法打开视频页", "error");
          }).catch(() => toast("无法打开视频页", "error"));
        });
      } else {
        addBtn("重试下载", "ghost small", () => {
          AppApi.retryDownloads([task.task_id]).then((res) => {
            const n = res && res.retried ? res.retried.length : 0;
            const need = res && res.needs_page ? res.needs_page.length : 0;
            if (n) toast(`已重新提交 ${n} 个下载`);
            if (need) toast("该任务需回视频页重新点击下载（地址有时效）", "error");
            if (!n && !need) toast((res && res.error) || "该任务当前不可重试", "error");
            refreshAll();
          }).catch(() => toast("重试失败", "error"));
        });
      }
    } else if (["failed", "retryable", "cancelled", "done"].includes(task.status)) {
      addBtn(task.status === "done" ? "重新转写" : "重试转写", "ghost small", () => {
        AppApi.retranscribe([task.task_id]).then((res) => {
          const n = res && res.created ? res.created.length : 0;
          if (n) toast(`已重建 ${n} 个转写任务`);
          else toast("没有可重建的转写任务", "error");
          refreshAll();
        });
      });
    }
    if (ACTIVE.includes(task.status)) {
      addBtn("取消", "ghost small", () => AppApi.cancel([task.task_id]).then(refreshAll));
    }
    addBtn("删除", "danger small", () => deleteTask(task));
  }

  function refreshAll() {
    const serial = ++tasksRequest, before = AppState.tasks;
    return AppApi.tasks().then((tasks) => {
      if (serial !== tasksRequest || before !== AppState.tasks) return;
      if (!Array.isArray(tasks)) throw new Error('Invalid task list');
      AppState.tasks = tasks;
      const valid = new Set(tasks.map(t => t.task_id));
      AppState.selected = new Set([...AppState.selected].filter(id => valid.has(id)));
      render();
    }).catch(() => { if (serial === tasksRequest && AppState.currentView === 'workbench') toast('刷新任务失败，请重试', 'error'); });
  }

  /* 渲染门控：桌面端每 2s 推送一次全量状态。数据指纹（不含选中集）没变
     就跳过列表重建——~250 行任务列表每 2 秒推倒重来会把 WebView2 的 JS
     线程周期性打满，按钮点击在渲染窗口内排队，体感即「点了没反应」。
     选中集单独走 syncSelection（类名/勾选框就地更新），点选一行不再触发
     全量重建；检查器/批量条只随「选中或数据」变化重绘。进度取整到百分
     点：下载中的任务仍按推送节奏刷新，静止任务零重绘。 */
  function dataKey(state) {
    const s = state || AppState;
    const tasks = s.tasks || [];
    const parts = tasks.map((t) => [
      t.task_id, t.status, Math.round((Number(t.progress) || 0) * 100), t.error || "",
      t.display_title || "", t.result_category || "", t.updated_at || "",
      t.final_path || "", t.raw_path || "", t.downloaded_path || "",
      t.cover_path || "", t.is_gallery ? "gallery" : "",
    ].join("|"));
    return [
      s.currentView, s.filter, (s.settings || {}).task_sort_order || "newest",
      tasks.length, parts.join(";"),
      (s.summary && s.summary.text) || "",
      s.worker && s.worker.running ? 1 : 0,
    ].join("#");
  }

  function syncSelection() {
    const list = document.getElementById("jobList");
    if (!list) return;
    list.querySelectorAll(".job").forEach((el) => {
      const on = AppState.selected.has(el.dataset.taskId);
      el.classList.toggle("selected", on);
      el.querySelector(".job-open")?.setAttribute("aria-pressed", String(on));
      const cb = el.querySelector(".job-check-input");
      if (cb && cb.checked !== on) cb.checked = on;
    });
  }

  function render(force) {
    const focused = document.activeElement;
    const filterFocus = focused?.closest?.('#jobFilters .filter')?.dataset.filter;
    const clearFocus = focused?.closest?.('#jobFilters .clear-completed');
    const inspectorFocus = focused?.closest?.('#inspector button')?.textContent;
    const detailFocus = focused?.matches?.('#inspector summary');
    const detailOpen = document.querySelector('#inspector .task-error-details')?.open;
    reconcileSelection(filtered(AppState.tasks || []));
    const key = dataKey();
    const structural = Boolean(force) || key !== render._lastKey;
    if (structural) {
      render._lastKey = key;
      const list = document.getElementById("jobList");
      const scrollTop = list ? list.scrollTop : 0;
      const focused = document.activeElement;
      const focusedRow = focused?.closest?.(".job");
      const focusTaskId = focusedRow?.dataset.taskId;
      const focusControl = focused?.classList?.contains("job-open") ? ".job-open"
        : focused?.classList?.contains("job-delete") ? ".job-delete"
        : focused?.classList?.contains("job-check-input") ? ".job-check-input" : null;
      buildLayout();
      renderFilters();
      renderList();
      const fresh = document.getElementById("jobList");
      if (fresh && scrollTop > 0) fresh.scrollTop = scrollTop;
      if (focusTaskId && focusControl && fresh) {
        [...fresh.querySelectorAll(".job")].find(row => row.dataset.taskId === focusTaskId)
          ?.querySelector(focusControl)?.focus({ preventScroll:true });
      }
    } else {
      syncSelection();
    }
    const selectionKey = [...AppState.selected].sort().join(",") + "@" + key;
    if (structural || selectionKey !== render._lastSelectionKey) {
      render._lastSelectionKey = selectionKey;
      renderInspector();
    }
    if (filterFocus) [...document.querySelectorAll('#jobFilters .filter')].find(button => button.dataset.filter === filterFocus)?.focus({preventScroll:true});
    else if (clearFocus) document.querySelector('#jobFilters .clear-completed')?.focus({preventScroll:true});
    else if (inspectorFocus) [...document.querySelectorAll('#inspector button')].find(button => button.textContent === inspectorFocus)?.focus({preventScroll:true});
    const details = document.querySelector('#inspector .task-error-details');
    if (details && detailOpen) details.open = true;
    if (details && detailFocus) details.querySelector('summary')?.focus({preventScroll:true});
  }

  const EDITING_LEVELS = [
    { value: "", label: "沿用当前设置" },
    { value: "auto", label: "智能（按内容自选）" },
    { value: "light", label: "轻整理（保真）" },
    { value: "balanced", label: "适度整理（平衡）" },
    { value: "structured", label: "结构整理（成篇）" },
    { value: "rewrite", label: "精炼改写（自由）" },
  ];
  function levelLabel(value) {
    return (EDITING_LEVELS.find((item) => item.value === value) || {}).label || value;
  }
  function pickEditingLevel(onPick) {
    let overlay = document.getElementById("editing-level-picker");
    if (overlay) overlay.remove();
    overlay = document.createElement("div");
    overlay.id = "editing-level-picker";
    overlay.style.cssText = "position:fixed;inset:0;z-index:90;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.38)";
    const panel = document.createElement("div");
    panel.style.cssText = "min-width:260px;padding:18px;border-radius:14px;background:var(--surface,var(--bg,#fff));box-shadow:0 18px 50px rgba(0,0,0,.35);display:flex;flex-direction:column;gap:8px";
    const title = document.createElement("div");
    title.textContent = "选择整理力度";
    title.style.cssText = "font-weight:700;margin-bottom:4px";
    panel.appendChild(title);
    const close = () => overlay.remove();
    EDITING_LEVELS.forEach((item) => {
      const btn = document.createElement("button");
      btn.className = "btn ghost small";
      btn.textContent = item.label;
      btn.onclick = () => { close(); onPick(item.value); };
      panel.appendChild(btn);
    });
    const cancel = document.createElement("button");
    cancel.className = "btn ghost small";
    cancel.textContent = "取消";
    cancel.onclick = close;
    panel.appendChild(cancel);
    overlay.onclick = (event) => { if (event.target === overlay) close(); };
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
  }
  return { render, refreshAll, __progressInternals: { stageLines, pipeline, percent, dataKey } };
})();
