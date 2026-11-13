// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 应用壳：导航、路由、状态流、Toast、Modal、删除确认、顶栏操作 */
'use strict';

window.APP_VERSION = "0.1.0";
window.BASE_FONT_SIZE = 15;

window.AppState = {
  settings: null,
  tasks: [],
  summary: { total: 0, active: 0, terminal: 0, completed: 0, progress: 0, text: "转写进度：空闲" },
  worker: { running: false, label: "Worker 已停止", meta: "" },
  currentView: "workbench",
  releaseChannel: 'formal',
  launchContext: {mode:"manual", sequence:0},
  selected: new Set(),
  anchorId: null,
  filter: "全部",
};

const PAGES = [
  { key: 'setup', label: '准备', title: '准备', icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.2 2.4 2.4 4.6-5"/></svg>` },
  { key: "workbench", label: "任务", title: "任务", icon: AppIcons('tasks') },
  { key: "videos", label: "视频", title: "视频", icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4Z"/></svg>` },
  { key: "library", label: "文本库", title: "文本库", icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5a2 2 0 0 1 2-2h11a3 3 0 0 1 3 3v15H6a2 2 0 0 0-2 2Z"/><path d="M4 5v16a2 2 0 0 1 2-2h14M8 7h8M8 11h8"/></svg>` },
];

/* 使用与订阅、设置固定在侧边栏底部。 */
/* 底部固定组（先生 2026-10-19）：常见问题置于订阅上方，与设置一起固定在侧边栏底部。 */
const FAQ_PAGE = { key: "faq", label: "常见问题", title: "常见问题", icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 17v.01"/><path d="M12 13.5c0-2 2.2-2.2 2.2-4.2a2.2 2.2 0 1 0-4.4 0"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2.4-2.5 4.5v.5"/></svg>` };
const SUBSCRIPTION_PAGE = { key: "subscription", label: "订阅", title: "使用与订阅", icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h3"/></svg>` };
const SETTINGS_PAGE = { key: "settings", label: "设置", title: "设置", icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.1 2.1-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.04 1.56v.1h-3v-.1A1.7 1.7 0 0 0 10.68 18.6a1.7 1.7 0 0 0-1.88.34l-.06.06-2.1-2.1.06-.06A1.7 1.7 0 0 0 7.04 15 1.7 1.7 0 0 0 5.48 14H5.4v-3h.08A1.7 1.7 0 0 0 7.04 9.96 1.7 1.7 0 0 0 6.7 8.08l-.06-.06 2.1-2.1.06.06a1.7 1.7 0 0 0 1.88.34A1.7 1.7 0 0 0 11.72 4.8v-.1h3v.1a1.7 1.7 0 0 0 1.04 1.52 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.1 2.1-.06.06a1.7 1.7 0 0 0-.34 1.88A1.7 1.7 0 0 0 20.96 11h.08v3h-.08A1.7 1.7 0 0 0 19.4 15Z"/></svg>` };
const BOTTOM_PAGES = [FAQ_PAGE, SUBSCRIPTION_PAGE, SETTINGS_PAGE];

// Acquisition tools have their own stable place in the sidebar.
PAGES.push(
  { key: 'browser', label: '浏览器', title: '浏览器', icon: AppIcons('browser') },
  { key: 'links', label: '链接下载', title: '链接下载', icon: AppIcons('link') },
  { key: 'import', label: '导入媒体', title: '导入媒体', icon: AppIcons('import') },
);
// 先生 2026-10-19：新手引导不再常驻左侧导航（再入口：设置 → 关于 →
// 「重新阅读新手引导」；首用引导流程不受影响）。

function toast(message, kind) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = "toast" + (kind === "error" ? " err" : "");
  // 可访问性（00-待办 #9）：常规 toast 以 status 语义礼貌播报，错误用
  // alert 立即播报；与扩展端信息窗（role=status aria-live=polite）对齐。
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 6000);
}

let modalReturnFocus = null;
// WebView can focus the first link at startup and report :focus-visible.
// Show this navigation shortcut only after an actual Tab key, not autofocus.
document.addEventListener('keydown', event => {
  if (!event.isTrusted || event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
  const firstTab = document.body.dataset.keyboardNavigation !== 'true';
  document.body.dataset.keyboardNavigation = 'true';
  if (firstTab && document.activeElement?.classList.contains('skip-link')) event.preventDefault();
}, true);
window.addEventListener('blur', () => { document.body.dataset.keyboardNavigation = 'false'; });
document.addEventListener('pointerdown', (event) => {
  document.body.dataset.keyboardNavigation = 'false';
  const notice = document.getElementById('toast');
  if (notice && !notice.hidden && !notice.contains(event.target)) {
    notice.hidden = true; clearTimeout(notice._timer);
  }
});
function openModal(title, bodyHtml, footHtml) {
  if (document.getElementById("modalRoot").firstElementChild) closeModal();
  const root = document.getElementById("modalRoot");
  modalReturnFocus = document.activeElement;
  root.innerHTML = `<div class="modal-mask"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle" tabindex="-1">
    <div class="modal-head"><span class="t" id="modalTitle"></span><button class="modal-x" type="button" aria-label="关闭对话框">×</button></div>
    <div class="modal-body"></div><div class="modal-foot"></div></div></div>`;
  const mask = root.firstElementChild;
  const dialog = mask.querySelector(".modal");
  mask.querySelector(".t").textContent = title;
  mask.querySelector(".modal-body").innerHTML = bodyHtml;
  if (footHtml) mask.querySelector(".modal-foot").innerHTML = footHtml;
  mask.querySelector(".modal-x").onclick = closeModal;
  mask.addEventListener("mousedown", (e) => { if (e.target === mask) closeModal(); });
  const app = document.querySelector(".app");
  if (app) app.inert = true;
  dialog.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
    if (e.key !== "Tab") return;
    const focusable = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')]
      .filter(el => !el.hidden && el.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (!first) { e.preventDefault(); dialog.focus(); }
    else if (document.activeElement === dialog) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  const initial = dialog.querySelector("#dlgCancel, [autofocus], .modal-foot button:not(:disabled)") || dialog;
  initial.focus({ preventScroll:true });
  return mask;
}
function closeModal() {
  document.getElementById("modalRoot").querySelectorAll("video").forEach(video => {
    video.pause(); video.removeAttribute("src"); video.load();
  });
  document.getElementById("modalRoot").innerHTML = "";
  const app = document.querySelector(".app");
  if (app) app.inert = false;
  if (modalReturnFocus && modalReturnFocus.isConnected) modalReturnFocus.focus({ preventScroll:true });
  modalReturnFocus = null;
}

function copyText(text, tip) {
  const done = () => toast(tip || "已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const returnFocus = document.activeElement;
  let ta;
  try {
    ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    if (document.execCommand("copy") !== true) throw new Error("copy denied");
    done();
  } catch (e) { toast("复制失败", "error"); }
  finally {
    ta?.remove();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll:true });
  }
}

window.ConfirmDiscard = {
  run({ onDiscard, message }) {
    const mask = openModal(
      "放弃未保存的修改？",
      '<p id="discardMessage"></p>',
      '<button class="btn ghost" id="dlgCancel">继续编辑</button><button class="btn danger" id="discardChanges">放弃修改</button>'
    );
    mask.querySelector('#discardMessage').textContent = message || '离开后，这次未保存的修改将丢失。';
    mask.querySelector('#dlgCancel').onclick = closeModal;
    mask.querySelector('#discardChanges').onclick = () => { closeModal(); onDiscard(); };
  },
};

/* 删除确认：受“设置-删除前确认”开关控制；弹窗内可勾选“不再提醒” */
window.ConfirmDelete = {
  run(opts) {
    const s = AppState.settings || {};
    if (s.confirm_delete === false) {
      opts.onConfirm();
      return;
    }
    const mask = openModal(
      "确认删除",
      `<div class="dlg-delete">
         <div class="dlg-text" id="dlgText"></div>
         <label class="dlg-check"><input type="checkbox" id="dlgNoAsk"><span>不再提醒，下次直接删除</span></label>
       </div>`,
      `<button class="btn ghost" id="dlgCancel">取消</button>
       <button class="btn danger" id="dlgOk">删除</button>`
    );
    mask.querySelector("#dlgText").textContent =
      opts.message || "确定要删除选中的内容吗？相关文件将一并移除，删除后不可恢复。";
    mask.querySelector("#dlgCancel").onclick = closeModal;
    mask.querySelector("#dlgOk").onclick = () => {
      const noAsk = mask.querySelector("#dlgNoAsk").checked;
      if (noAsk) {
        AppApi.saveSettings({ confirm_delete: false }).then((settings) => {
          if (!settings || settings.ok === false || typeof settings.confirm_delete !== 'boolean') throw new Error('settings not saved');
          AppState.settings = { ...AppState.settings, confirm_delete:settings.confirm_delete };
        }).catch(() => toast('未能保存删除确认设置', 'error'));
      }
      closeModal();
      opts.onConfirm();
    };
  },
};

/* 浅色主题集合：原生标题栏随之下发 light，其余为 dark */
const LIGHT_THEMES = new Set(["paper", "latte", "nord-light"]);
const KNOWN_THEMES = new Set([
  "dark", "paper", "dracula", "tokyo-night", "nord", "mocha",
  "gruvbox", "rose-pine", "latte", "nord-light",
]);

function applyTheme() {
  window.I18n?.setLanguage(AppState.settings?.ui_language || "zh-CN");
  const s = AppState.settings || {};
  let theme = s.theme || "dark";
  // Old installs can briefly expose a legacy value before settings migration.
  // Keep that transition deterministic and never render the retired light UI.
  if (theme === "light") theme = "paper";
  if (theme === "system") theme = "dark";
  if (!KNOWN_THEMES.has(theme)) theme = "dark";
  document.body.dataset.theme = theme;
  document.body.dataset.accent = s.accent || "green";
  document.body.dataset.backgroundStyle = ['plain','glow','texture'].includes(s.background_style) ? s.background_style : 'glow';
  /* 通知原生标题栏随主题变色（浏览器 mock 下静默忽略） */
  try {
    if (window.AppApi && window.AppApi.inWebview) {
      AppApi.setNativeTheme(LIGHT_THEMES.has(theme) ? "light" : "dark").catch(() => {});
    }
  } catch (e) { /* ignore */ }
}

function applyFontScale() {
  const s = AppState.settings || {};
  const scale = parseFloat(s.font_scale) || 1.15;
  document.body.style.fontSize = (window.BASE_FONT_SIZE * scale).toFixed(2) + "px";
}

function applyMotion() {
  const s = AppState.settings || {};
  document.body.dataset.animations = s.animations === false ? "off" : "on";
}

function buildNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = "";
  const all = PAGES.concat(BOTTOM_PAGES);
  all.forEach((p) => {
    if (p.key === 'browser') {
      const label = document.createElement('div'); label.className = 'nav-section'; label.textContent = '工具'; nav.append(label);
    }
    const row = document.createElement("button");
    row.type = 'button';
    row.className = "nav-row" + (p.key === 'faq' ? ' pinned' : '');
    row.dataset.page = p.key;
    row.title = p.label;
    row.setAttribute('aria-label', p.label);
    row.innerHTML = `<span class="ind"></span><span class="nav-icon">${p.icon}</span><span class="nav-label"></span>`;
    row.querySelector(".nav-label").textContent = p.label;
    row.onclick = () => switchView(p.key);
    nav.appendChild(row);
  });
}

function switchView(key) {
  document.body.dataset.keyboardNavigation = 'false';
  if (!window.AccountAccess.allowed() && !['setup','subscription','settings','faq'].includes(key)) {
    key = 'setup';
    window.Setup.showStep('login');
  }
  if (key === 'guide') { if (window.Onboarding) window.Onboarding.show(); return; }
  const page = PAGES.concat(BOTTOM_PAGES).find((p) => p.key === key);
  if (!page) return;
  if (AppState.currentView === 'library' && key === 'library') { window.Library.refreshLight(); return; }
  // Account revocation hides the reader immediately. Its existing editor DOM
  // remains intact and Library.render restores it after successful login.
  if (!AccountAccess.allowed() && AppState.currentView === 'library' && document.getElementById('discardChanges')) closeModal();
  if (AccountAccess.allowed() && AppState.currentView === 'library' && window.Library?.canLeave
      && !window.Library.canLeave(() => switchView(key))) return;
  if (AppState.currentView === "subscription" && key !== "subscription") window.Subscription.leave();
  if (AppState.currentView === "setup" && key !== "setup") window.Setup.leave();
  AppState.currentView = key;
  document.querySelectorAll(".page").forEach((el) => el.classList.remove("active"));
  const target = document.getElementById("page-" + key);
  target.classList.add("active");
  document.getElementById("pageTitle").textContent = page.title;
  document.querySelectorAll(".nav-row").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === key);
    if (el.dataset.page === key) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  if (key === "workbench") window.Workbench.render();
  else if (key === "videos") window.Videos.render();
  else if (key === "library") window.Library.render();
  else if (key === "settings") window.Settings.render();
  else if (key === "subscription") window.Subscription.render();
  else if (key === "faq") window.FaqPage.render();
  else if (key === "setup") window.Setup.render();
  else window.Tools.render(key);
  window.AccountAccess.sync();
}

function renderWorkerCard() {
  /* 侧边栏 worker 卡已按先生要求移除；Worker 状态仍在设置-模型页展示 */
}

/* Desktop delivers notifications independently of WebView visibility. */
const NOTIFY_ACTIVE = ["queued", "running", "retryable", "received", "resolving", "awaiting_browser", "downloading", "imported", "transcribing"];
function renderWindowChrome(state) {
  const enabled = state?.enabled === true;
  AppState.windowChrome = enabled ? {...AppState.windowChrome, ...state} : {enabled:false};
  document.body.dataset.windowChrome = String(enabled);
  document.getElementById("windowChrome").hidden = !enabled;
  const max = document.getElementById("windowMaximize");
  const maximized = enabled && AppState.windowChrome.maximized === true;
  max.setAttribute("aria-label", maximized ? "还原窗口" : "最大化");
  max.title = maximized ? "还原窗口" : "最大化";
  max.setAttribute("aria-pressed", String(maximized));
  max.innerHTML = maximized
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6V4h11v11h-2"/><rect x="4" y="9" width="11" height="11" rx="1"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>';
  syncWindowChromeGeometry();
  syncWindowDragRegion();
}
function syncWindowDragRegion() {
  const regions = document.querySelectorAll('.window-drag-region');
  const active = AppState.windowChrome?.enabled === true && !document.querySelector(".app")?.inert;
  const native = active && AppState.windowChrome.native_drag === true;
  // WebView2 handles caption movement directly. The fallback class enables
  // pywebview's mousemove bridge only on runtimes without native support.
  regions.forEach(region => {
    region.classList.toggle('window-drag-fallback', active && !native);
    for (const property of ['app-region', '-webkit-app-region']) {
      region.style.setProperty(property, native ? 'drag' : 'no-drag');
      region.querySelectorAll('button,a,input,select,textarea,[role=button],[contenteditable],.window-chrome')
        .forEach(node => node.style.setProperty(property, 'no-drag'));
    }
  });
}
function syncWindowChromeGeometry() {
  const chrome = document.getElementById("windowChrome");
  if (chrome && !chrome.hidden) document.body.style.setProperty("--window-controls-bottom", chrome.getBoundingClientRect().bottom + "px");
  else document.body.style.removeProperty("--window-controls-bottom");
}
function bindTopbar() {
  const run = async (button, action) => {
    if (!AppState.windowChrome?.enabled || button.disabled) return;
    button.disabled = true;
    try {
      const result = await action();
      if (result?.ok === false) toast(result.error || "窗口操作未完成", "error");
      else if (typeof result?.maximized === "boolean") renderWindowChrome({...AppState.windowChrome, maximized:result.maximized});
    } catch (_) { toast("窗口操作未完成", "error"); }
    finally { button.disabled = false; }
  };
  const minimize = document.getElementById("windowMinimize");
  const maximize = document.getElementById("windowMaximize");
  const hide = document.getElementById("windowHide");
  minimize.onclick = () => run(minimize, AppApi.windowMinimize);
  maximize.onclick = () => run(maximize, AppApi.windowToggleMaximize);
  hide.onclick = () => run(hide, AppApi.windowHide);
  document.querySelectorAll('.window-drag-region').forEach(dragRegion => {
    dragRegion.ondblclick = event => {
      if (!event.target.closest('button,a,input,select,textarea,[role=button],[contenteditable]')
          && AppState.windowChrome?.native_drag !== true && !document.querySelector('.app').inert) {
        event.stopPropagation(); run(maximize, AppApi.windowToggleMaximize);
      }
    };
  });
  document.getElementById('toast').onclick = () => { document.getElementById('toast').hidden = true; };
  new MutationObserver(syncWindowDragRegion).observe(document.querySelector(".app"), {attributes:true, attributeFilter:["inert"]});
  syncWindowDragRegion();
  new ResizeObserver(syncWindowChromeGeometry).observe(document.querySelector(".topbar"));
}

window.__onState = (payload) => {
  if (!payload) return;
  if (payload.account_access) AccountAccess.apply(payload.account_access);
  if (payload.agent_settings_revision) window.Settings?.observeRevision?.(payload.agent_settings_revision);
  if (payload.product_notifications) window.dispatchEvent(new CustomEvent('shengjian:product-notifications', {detail:payload.product_notifications}));
  for (const event of payload.notifications || []) {
    const isFail = event.title && String(event.title).includes("失败");
    toast(event.title + "：" + event.message, isFail ? "error" : "");
  }
  if (payload.launch_context) AppState.launchContext = payload.launch_context;
  if (payload.tasks) AppState.tasks = payload.tasks;
  if (payload.window_chrome) renderWindowChrome(payload.window_chrome);
  if (payload.summary) AppState.summary = payload.summary;
  if (payload.worker) AppState.worker = payload.worker;
  if (AppState.currentView === "workbench") window.Workbench.render();
  else if (AppState.currentView === "videos") window.Videos.refreshLight();
  else if (AppState.currentView === "library") window.Library.refreshLight();
  else if (AppState.currentView === "setup") window.Setup?.syncState?.();
  else if (AppState.currentView === "subscription") window.Subscription?.syncState?.();
};

/* pywebview 注入 api 可能在 DOMContentLoaded 之后才完成：先轮询等待，
   避免 init 在桥接未就绪时调用 undefined 方法导致 bootstrap 失败。 */
function waitForBridge(timeout) {
  return new Promise((resolve) => {
    if (window.__MOCK__) { resolve(false); return; }  // 浏览器 mock 调试不等待
    const limit = timeout || 8000;
    const t0 = Date.now();
    (function poll() {
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_bootstrap === "function") {
        resolve(true);
        return;
      }
      if (Date.now() - t0 > limit) { resolve(false); return; }
      setTimeout(poll, 40);
    })();
  });
}

async function init() {
  buildNav();
  await waitForBridge();
  try {
    const boot = await AppApi.bootstrap();
    if (boot) {
      AppState.launchContext = boot.launch_context || {mode:"manual", sequence:0};
      AppState.releaseChannel = boot.release_channel === 'test' ? 'test' : 'formal';
      AppState.settings = boot.settings || {};
      AppState.tasks = boot.tasks || [];
      AppState.summary = boot.summary || AppState.summary;
      AppState.worker = boot.worker || AppState.worker;
      renderWindowChrome(boot.window_chrome);
    }
  } catch (e) {
    console.warn("bootstrap failed", e);
  }
  applyTheme();
  applyFontScale();
  applyMotion();
  document.getElementById("brandVer").textContent = "v" + window.APP_VERSION;
  document.querySelector('.brand').title = 'FoilPage ' + window.APP_VERSION;
  bindTopbar();
  let restoring = false;
  if (!AccountAccess.allowed() && AppState.settings?.account_login_token) {
    restoring = true;
    AppState.accountRestorePending = true; AppState.accountRestoreError = '';
    window.__appReady = true;
    switchView('setup');
    await AccountAccess.restore();
  }
  AccountAccess.sync();
  if (!AppState.windowChrome?.enabled) AppApi.windowChrome().then(renderWindowChrome).catch(() => {});
  // Authentication is separate from the setup milestone. A returning configured
  // user opens the task list; switching accounts never resets local content.
  if(!restoring || AppState.currentView === 'setup') switchView(initialView());
  window.__appReady = true;
  const pendingView = window.__pendingNativeView;
  delete window.__pendingNativeView;
  if (pendingView !== 'guide' && PAGES.concat(BOTTOM_PAGES).some(page => page.key === pendingView)) switchView(pendingView);
}

function initialView() {
  if (!AccountAccess.allowed()) return 'setup';
  return AppState.settings?.setup_completed === true || AppState.launchContext.mode === 'background' ? 'workbench' : 'setup';
}

document.addEventListener("DOMContentLoaded", init);
