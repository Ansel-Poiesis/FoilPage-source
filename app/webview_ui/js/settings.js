// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 设置页：界面 / 识别 / 整理与转译 / 模型 / 数据 / 启动 / 关于 */
'use strict';

window.Settings = (() => {
  const SECTIONS = [
    { key: "ui", label: "界面", icon: "monitor" },
    { key: "recognition", label: "识别", icon: "wave" },
    { key: "agent", label: "文本整理", icon: "spark" },
    { key: "control", label: "Agent 控制", icon: "power" },
    { key: "model", label: "模型", icon: "box" },
    { key: "data", label: "数据", icon: "database" },
    { key: "startup", label: "启动", icon: "power" },
    { key: "about", label: "关于", icon: "info" },
  ];
  const SECTION_ICONS = {
    monitor: `<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>`,
    wave: `<svg viewBox="0 0 24 24"><path d="M4 12h2l2-6 4 12 3-8 2 4h3"/></svg>`,
    spark: `<svg viewBox="0 0 24 24"><path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7Z"/></svg>`,
    box: `<svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></svg>`,
    database: `<svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v7c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 12v7c0 1.7 3.1 3 7 3s7-1.3 7-3v-7"/></svg>`,
    power: `<svg viewBox="0 0 24 24"><path d="M12 2v10M6.3 5.3a8 8 0 1 0 11.4 0"/></svg>`,
    credit: `<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h3"/></svg>`,
    info: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>`,
  };
  let section = "ui";
  const modelBusy = new Set();
  let modelTimer = null;
  let saveQueue = Promise.resolve(), saveRevision = 0, rebuilding = false;
  const fieldRevisions = new Map(), pendingControls = new Set(), editingDrafts = new Map();
  const sectionLayout = new Map();
  const customProfileEdits = new Map();
  const customProfileEnabledRevisions = new Map();
  let externalSettingsTicket = null, externalSettingsSequence = 0;
  let productNotificationSnapshot = null, productNotificationRevision = 0;
  let refreshNotificationInbox = null;
  const notificationContext=()=>JSON.stringify([s().account_login_token||'',s().subscription_server_url||'',s().subscription_license_public_key||'',AppState.releaseChannel||'formal']);
  window.addEventListener('shengjian:product-notifications',event=>{
    productNotificationRevision++;
    productNotificationSnapshot={context:notificationContext(),value:event.detail};
    refreshNotificationInbox?.();
  });
  const inSection = key => AppState.currentView === "settings" && section === key;
  function controlKey(node) {
    const label = node.getAttribute("aria-label") || node.closest(".set-row")?.querySelector(".set-label")?.textContent || node.type || "control";
    return `${node.tagName}:${node.type || ""}:${label}:${node.closest("[data-profile-id]")?.dataset.profileId || ""}`;
  }
  function runControl(node, action, redraw = false) {
    if (rebuilding || !node.isConnected || node.disabled) return;
    const owner = section, key = controlKey(node), pendingKey = owner + ":" + key, value = node.value;
    if (pendingControls.has(pendingKey)) return;
    let result;
    try { result = action(); } catch (_) { result = undefined; }
    if (!result?.then) { if (result === false) node.setAttribute("aria-invalid", "true"); return result; }
    pendingControls.add(pendingKey); node.setAttribute("aria-busy", "true");
    const editable = node.matches('input:not([type="range"]),textarea');
    if (!editable) node.disabled = true;
    return result.then(saved => {
      if (!saved) { node.setAttribute("aria-invalid", "true"); if (node.tagName === "SELECT") {node.value = node.dataset.initialValue; editingDrafts.get(owner)?.delete(key);} return; }
      node.removeAttribute("aria-invalid");
      if (node.value === value) node.dataset.initialValue = value;
      const draft = editingDrafts.get(owner)?.get(key);
      if (draft?.value === value) editingDrafts.get(owner).delete(key);
      return saved;
    }).catch(() => { node.setAttribute("aria-invalid", "true"); })
      .then(saved => {
        pendingControls.delete(pendingKey); node.removeAttribute("aria-busy"); if (!editable) node.disabled = false;
        if (inSection(owner)) document.getElementById("settingsPane")?.querySelectorAll('[aria-busy="true"],[data-pending-control],[data-model-select]').forEach(control => {
          if (controlKey(control) === key) {
            if (control.hasAttribute("data-pending-control")) {control.disabled = control.dataset.pendingControl === "true"; delete control.dataset.pendingControl;}
            control.removeAttribute("aria-busy");
            if (control.hasAttribute("data-model-select")) control.disabled = control.dataset.modelAvailable !== "true";
            if (control.readPersistedValue) {
              const confirmed = control.readPersistedValue();
              if (control.getAttribute("role") === "switch" && typeof confirmed === "boolean") {
                control.setAttribute("aria-checked", String(confirmed)); control.classList.toggle("on", confirmed);
              } else if (control.tagName === "SELECT" && confirmed != null) {
                control.value = String(confirmed); control.dataset.initialValue = control.value;
              }
            }
          }
        });
        if (!saved && inSection(owner)) document.getElementById("settingsPane")?.querySelectorAll('select').forEach(control => {if (controlKey(control) === key) {control.value = control.dataset.initialValue; control.setAttribute("aria-invalid", "true");}});
        if (saved && redraw && inSection(owner) && node.isConnected) render({key, section:owner});
        return saved;
      });
  }

  const ACCENTS = [
    { key: "green", label: "松绿", color: "#2c8753" },
    { key: "rose", label: "霞红", color: "#e75d74" },
    { key: "indigo", label: "靛蓝", color: "#4f68d7" },
    { key: "ink", label: "墨色", color: "#2f3437" },
  ];

  /* 字号：滑动调整（0.9-1.3 连续档），原字档按钮已退役 */
  const MODELS = [
    { value: "tiny", label: "tiny（约 75 MB，快速验证）" },
    { value: "base", label: "base（约 145 MB，日常轻量）" },
    { value: "small", label: "small（约 488 MB，质量速度平衡）" },
    { value: "medium", label: "medium（约 1.5 GB，更高中文质量）" },
    { value: "large-v3", label: "large-v3（约 3 GB，最高质量）" },
  ];

  const PROFILES = [
    { value: "auto", label: "自动分类" },
    { value: "emotional_copy", label: "情感文案" },
    { value: "consulting", label: "咨询" },
    { value: "knowledge", label: "知识科普" },
  ];

  const CUSTOM_PROFILE_TEMPLATE = {
    id: "",
    name: "新的整理策略",
    system_prompt: "你是 FoilPage 的原文保真校订编辑。保持原文顺序、语气、例子和限定条件，只修复明确的 ASR 错字、同音字、标点、断句和明显赘词；不确定时保留原词并标记待确认，禁止摘要、重排、扩写或编造。只输出 Markdown，第一行是一级标题。",
    enabled: true,
    model: "",
    temperature: null,
  };

  function profileOptions() {
    const options = PROFILES.slice();
    (s().agent_custom_profiles || []).forEach((p) => {
      if (p && p.id && p.name && p.enabled !== false) {
        options.push({ value: "custom:" + p.id, label: p.name + "（自定义）" });
      }
    });
    return options;
  }

  function s() { return AppState.settings || {}; }

  function save(changes, feedback) {
    const revision = ++saveRevision, patch = {...changes}, origin = AppState.currentView, owner = section;
    Object.keys(patch).forEach(key => fieldRevisions.set(key, revision));
    const visible = () => AppState.currentView === origin && (origin !== "settings" || section === owner);
    const request = saveQueue.then(async () => {
      try {
        const context = () => JSON.stringify([s().account_login_token || "", String(s().subscription_server_url || "").trim().replace(/\/+$/, ""), String(s().subscription_license_public_key || "").trim()]);
        const accountContext = context(), access = s().account_access;
        const previousServer = s().subscription_server_url, previousKey = s().subscription_license_public_key;
        const settings = await AppApi.saveSettings(patch);
        if (!settings || settings.ok === false || Object.keys(patch).some(key => !Object.hasOwn(settings, key))) throw new Error(settings?.error || "未收到保存确认，请重试");
        const confirmed = {};
        Object.keys(patch).forEach(key => { if (fieldRevisions.get(key) === revision) confirmed[key] = settings[key]; });
        const sameAccount = context() === accountContext && s().account_access === access;
        AppState.settings = {...AppState.settings, ...confirmed};
        if(typeof settings.agent_settings_revision==='string' && /^[a-f0-9]{64}$/.test(settings.agent_settings_revision))AppState.settings.agent_settings_revision=settings.agent_settings_revision;
        const changedServer = Object.hasOwn(confirmed, "subscription_server_url") && String(previousServer || "").trim().replace(/\/+$/, "") !== String(confirmed.subscription_server_url || "").trim().replace(/\/+$/, "");
        const changedKey = Object.hasOwn(confirmed, "subscription_license_public_key") && String(previousKey || "").trim() !== String(confirmed.subscription_license_public_key || "").trim();
        if (changedServer || changedKey) window.AccountAccess?.lock();
        else if (sameAccount && settings.account_access && typeof settings.account_access === "object") window.AccountAccess?.apply(settings.account_access);
        window.AccountAccess?.sync();
        if (confirmed.agent_custom_profiles) customProfileEdits.clear();
        applyTheme();
        if (typeof applyFontScale === "function") applyFontScale();
        if (typeof applyMotion === "function") applyMotion();
        if (feedback !== false && visible()) toast("设置已保存");
        return AppState.settings;
      } catch (e) {
        if (visible()) toast("保存失败：" + (e?.message || "请重试"), "error");
        applyTheme(); if (typeof applyFontScale === "function") applyFontScale();
        return undefined;
      }
    });
    saveQueue = request.then(() => {});
    return request;
  }

  function toggleEl(on, onClick, persistedValue) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "toggle" + (on ? " on" : "");
    el.setAttribute("role", "switch");
    el.setAttribute("aria-checked", on ? "true" : "false");
    if (persistedValue) el.readPersistedValue = typeof persistedValue === "function" ? persistedValue : () => s()[persistedValue];
    el.tabIndex = 0;
    el.onclick = () => runControl(el, onClick, true);
    return el;
  }

  function externalSettingsNotice() {
    const pane=document.getElementById('settingsPane');if(!pane || AppState.currentView!=='settings' || pane.querySelector('#externalSettingsNotice'))return;
    const notice=document.createElement('div');notice.className='settings-external-notice';notice.id='externalSettingsNotice';notice.setAttribute('role','status');
    const text=document.createElement('span');text.textContent='设置已更新，当前输入已保留。';
    const refresh=document.createElement('button');refresh.type='button';refresh.className='btn ghost small';refresh.textContent='更新显示';
    refresh.onclick=()=>render();notice.append(text,refresh);pane.prepend(notice);
  }
  async function observeRevision(revision) {
    if(typeof revision!=='string' || !/^[a-f0-9]{64}$/.test(revision) || revision===s().agent_settings_revision || externalSettingsTicket?.revision===revision)return;
    const context=()=>JSON.stringify([s().account_login_token||'',s().account_user_id||'',s().subscription_server_url||'',s().subscription_license_public_key||'']);
    const account=context(),fields=new Map(fieldRevisions),ticket={revision,sequence:++externalSettingsSequence};externalSettingsTicket=ticket;
    try{
      const incoming=await AppApi.settings();
      if(externalSettingsTicket!==ticket || context()!==account || !incoming || incoming.ok===false || incoming.agent_settings_revision!==revision)return;
      const confirmed={};
      for(const [key,value] of Object.entries(incoming)){
        if(key==='agent_settings_revision' || key==='data_dir' || key==='credit_balance' || key.startsWith('account_') || key.startsWith('license_') || key.startsWith('agent_usage_') || ['subscription_active','subscription_plan','subscription_expires_at','subscription_last_order_id'].includes(key))continue;
        if(fieldRevisions.get(key)===fields.get(key))confirmed[key]=value;
      }
      const changedServer=Object.hasOwn(confirmed,'subscription_server_url') && confirmed.subscription_server_url!==s().subscription_server_url;
      const changedKey=Object.hasOwn(confirmed,'subscription_license_public_key') && confirmed.subscription_license_public_key!==s().subscription_license_public_key;
      AppState.settings={...s(),...confirmed,agent_settings_revision:revision};
      if(changedServer || changedKey){for(const key of ['account_login_token','account_user_id','account_display_name','account_avatar','license_token','license_expires_at'])if(Object.hasOwn(incoming,key))AppState.settings[key]=incoming[key];window.AccountAccess?.lock();}
      applyTheme();if(typeof applyFontScale==='function')applyFontScale();if(typeof applyMotion==='function')applyMotion();
      if(AppState.currentView==='settings'){
        const pane=document.getElementById('settingsPane'),focused=document.activeElement;
        const editing=focused?.closest?.('#settingsPane') && focused.matches('input,textarea,select');
        const drafts=pane && [...pane.querySelectorAll('input,textarea,select')].some(control=>control.dataset.initialValue!==undefined && control.value!==control.dataset.initialValue);
        if(editing || drafts || pendingControls.size || customProfileEdits.size || document.querySelector('.modal,[aria-modal="true"]'))externalSettingsNotice();
        else render();
      }else if(AppState.currentView==='setup'){window.Setup?.refreshAccount?.();window.Setup?.syncState?.();}
    }catch(_){ /* The next changed-revision push can retry without touching drafts. */ }
    finally{if(externalSettingsTicket===ticket)externalSettingsTicket=null;}
  }

  function selectEl(value, options, onChange, persistedValue) {
    const el = document.createElement("select");
    el.className = "ctrl";
    options.forEach((opt) => {
      const op = document.createElement("option");
      op.value = opt.value;
      op.textContent = opt.label;
      el.appendChild(op);
    });
    el.value = value;
    el.dataset.initialValue = value;
    if (persistedValue) el.readPersistedValue = typeof persistedValue === "function" ? persistedValue : () => s()[persistedValue];
    el.onchange = () => runControl(el, () => onChange(el.value));
    return el;
  }

  function inputEl(value, onChange, opts) {
    const el = document.createElement("input");
    el.className = "ctrl";
    el.type = (opts && opts.password) ? "password" : (opts && opts.number ? "number" : "text");
    el.value = value;
    el.dataset.initialValue = value;
    el.spellcheck = false;
    el.onchange = () => {
      if (rebuilding || !el.isConnected) return;
      if (!el.closest("#settingsPane")) return onChange(el.value);
      if (el.value === el.dataset.initialValue && !el.hasAttribute("aria-invalid")) return;
      return runControl(el, () => onChange(el.value));
    };
    el.onkeydown = event => { if (event.key === "Enter" && el.closest("#settingsPane")) {event.preventDefault(); el.onchange();} };
    return el;
  }

  function row(label, hint, ctrl) {
    const r = document.createElement("div");
    r.className = "set-row";
    const left = document.createElement("div");
    left.className = "set-left";
    const lab = document.createElement("div");
    lab.className = "set-label";
    lab.textContent = label;
    left.appendChild(lab);
    if (hint) {
      const h = document.createElement("div");
      h.className = "set-hint";
      h.textContent = hint;
      left.appendChild(h);
    }
    const c = document.createElement("div");
    c.className = "set-ctrl";
    if (ctrl.matches('input, select, textarea, [role="switch"]') && !ctrl.hasAttribute('aria-label')) ctrl.setAttribute('aria-label', label);
    c.appendChild(ctrl);
    r.appendChild(left);
    r.appendChild(c);
    return r;
  }

  function valueEl(text) {
    const el = document.createElement("span");
    el.className = "set-value";
    el.textContent = text;
    return el;
  }

  function group(title) {
    const g = document.createElement("div");
    g.className = "set-group";
    const h = document.createElement("h3");
    h.textContent = title;
    g.appendChild(h);
    return g;
  }

  function dirCtrl(pathValue, pickKey, openValue) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;align-items:center;gap:8px;flex:1;min-width:0;justify-content:flex-end;";
    const box = document.createElement("div");
    box.className = "path-box";
    const txt = document.createElement("span");
    txt.className = "path-text" + (pathValue ? "" : " is-empty");
    txt.textContent = pathValue || "未设置";
    txt.title = pathValue || "";
    box.appendChild(txt);
    wrap.appendChild(box);
    if (pickKey) {
      const b = document.createElement("button");
      b.className = "btn ghost small";
      b.textContent = "选择";
      b.onclick = () => runControl(b, async () => {
        const owner = section;
        try {
          const res = await AppApi.pickSettingDir(pickKey);
          if (res?.cancelled) return;
          if (!res?.ok || typeof res.settings?.[pickKey] !== "string" || !res.settings[pickKey]) throw new Error(res?.error || "目录保存失败");
          AppState.settings = {...AppState.settings, [pickKey]:res.settings[pickKey]};
          window.AccountAccess?.sync();
          if (inSection(owner) && wrap.isConnected) toast("目录已更新");
          return AppState.settings;
        } catch (error) {if (inSection(owner) && wrap.isConnected) toast(error.message || "目录保存失败", "error");}
      }, true);
      wrap.appendChild(b);
    }
    if (openValue) {
      const b = document.createElement("button");
      b.className = "btn ghost small";
      b.textContent = "打开";
      b.onclick = () => runControl(b, async () => {
        const owner = section;
        try {const res = await AppApi.openDir(openValue); if (!res?.ok) throw new Error(res?.error || "无法打开"); return res;}
        catch (error) {if (inSection(owner) && wrap.isConnected) toast(error.message || "无法打开", "error");}
      });
      wrap.appendChild(b);
    }
    return wrap;
  }

  /* 当前主题值：未知/遗留值收束到深色 */
  function themeValue() {
    const known = new Set([
      "dark", "paper", "dracula", "tokyo-night", "nord", "mocha",
      "gruvbox", "rose-pine", "latte", "nord-light",
    ]);
    const t = s().theme || "dark";
    return known.has(t) ? t : "dark";
  }

  /* 字号滑动调整：0.9-1.3 连续档，实时显示像素值 */
  function fontScaleCtrl() {
    const wrap = document.createElement("div");
    wrap.className = "fs-slider-row";
    const slider = document.createElement("input");
    slider.type = "range";
    slider.className = "fs-slider";
    slider.setAttribute("aria-label", "界面字号");
    slider.min = "0.9";
    slider.max = "1.3";
    slider.step = "0.05";
    slider.value = String(parseFloat(s().font_scale) || 1.15);
    const value = document.createElement("span");
    value.className = "fs-value";
    const showValue = () => {
      value.textContent = Math.round(window.BASE_FONT_SIZE * parseFloat(slider.value)) + "px";
      slider.setAttribute("aria-valuetext", value.textContent);
    };
    showValue();
    slider.oninput = () => {
      showValue();
      const scale = parseFloat(slider.value);
      document.body.style.fontSize = (window.BASE_FONT_SIZE * scale).toFixed(2) + "px";
    };
    slider.onchange = () => save({ font_scale: parseFloat(slider.value) }, false);
    wrap.append(slider, value);
    return wrap;
  }

  /* ---------------- 界面 ---------------- */
  function buildUi() {
    const pane = document.createElement("div");
    const g = group("外观");
    g.appendChild(row("界面语言", "与转写识别语言独立", selectEl(s().ui_language || "zh-CN", [
      {value:"zh-CN",label:"简体中文"}, {value:"en",label:"English"},
    ], value => save({ui_language:value}), "ui_language")));
    const savedTheme = themeValue();
    const theme = selectEl(
      savedTheme,
      [
        { value: "dark", label: "深色" },
        { value: "paper", label: "白岩" },
        { value: "dracula", label: "暗夜德古拉 Dracula" },
        { value: "tokyo-night", label: "东京夜 Tokyo Night" },
        { value: "nord", label: "诺德极夜 Nord" },
        { value: "mocha", label: "奶咖摩卡 Catppuccin Mocha" },
        { value: "gruvbox", label: "黄土复古 Gruvbox" },
        { value: "rose-pine", label: "玫瑰松林 Rosé Pine" },
        { value: "latte", label: "拿铁 Catppuccin Latte" },
        { value: "nord-light", label: "诺德雪 Nord Light" },
      ],
      (v) => save({ theme: v }), "theme"
    );
    g.appendChild(row("主题", "", theme));
    g.appendChild(row("背景", "", selectEl(s().background_style || "glow", [
      {value:"plain",label:"素面"},{value:"glow",label:"柔光"},{value:"texture",label:"细纹"},
    ], value=>save({background_style:value}), "background_style")));

    const accentWrap = document.createElement("div");
    accentWrap.className = "accent-row";
    ACCENTS.forEach((a) => {
      const sw = document.createElement("button");
      sw.type = "button";
      sw.setAttribute("aria-label", a.label);
      sw.setAttribute("aria-pressed", s().accent === a.key ? "true" : "false");
      sw.className = "accent-swatch" + (s().accent === a.key ? " active" : "");
      sw.style.background = a.color;
      sw.title = a.label;
      sw.onclick = () => runControl(sw, () => save({ accent: a.key }), true);
      accentWrap.appendChild(sw);
    });
    g.appendChild(row("界面强调色", "", accentWrap));

    g.appendChild(row("界面字号", "", fontScaleCtrl()));

    const animToggle = toggleEl(!!s().animations, () => save({ animations: !s().animations }), "animations");
    g.appendChild(row("界面动效", "", animToggle));

    const confirmToggle = toggleEl(!!s().confirm_delete, () => save({ confirm_delete: !s().confirm_delete }), "confirm_delete");
    g.appendChild(row("删除前确认", "", confirmToggle));

    const sortOrder = selectEl(
      s().task_sort_order || "newest",
      [
        { value: "newest", label: "最新在前" },
        { value: "oldest", label: "最早在前" },
      ],
      (v) => save({ task_sort_order: v }), "task_sort_order"
    );
    g.appendChild(row("任务排序", "", sortOrder));
    pane.appendChild(g);
    return pane;
  }

  /* ---------------- 识别 ---------------- */
  function buildRecognition() {
    const pane = document.createElement("div");
    const g = group("识别");
    const device = selectEl(
      s().device || "cuda",
      [{ value: "cuda", label: "GPU（CUDA）" }, { value: "cpu", label: "CPU" }],
      (v) => save({ device: v }), "device"
    );
    g.appendChild(row("运行设备",
      "建议：NVIDIA 独显且显存充足（≥ 4 GB）选 GPU；无独显或显存不足时选 CPU。",
      device));
    const language = selectEl(
      s().language || "zh",
      [{ value: "zh", label: "中文" }, { value: "en", label: "英文" }, { value: "auto", label: "自动检测" }],
      (v) => save({ language: v }), "language"
    );
    g.appendChild(row("识别语言", "", language));
    const attempts = inputEl(String(s().max_attempts || 3), (v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 20) { toast("尝试次数需为 1-20", "error"); return false; }
      return save({ max_attempts: n });
    }, { number: true });
    g.appendChild(row("最大尝试次数", "", attempts));
    const autoTranscribe = toggleEl(!!s().transcribe_after_download, () => save({ transcribe_after_download: !s().transcribe_after_download }), "transcribe_after_download");
    g.appendChild(row("下载后自动转写", "默认只下载不转写；纯视觉无文本的视频建议保持关闭，需要时可对单个任务手动转写", autoTranscribe));
    const delVideo = toggleEl(!!s().delete_video_after_transcribe, () => save({ delete_video_after_transcribe: !s().delete_video_after_transcribe }), "delete_video_after_transcribe");
    g.appendChild(row("转写完成后删除视频", "转写成功后自动删除 FoilPage 下载的视频文件；手动导入的本地媒体不受影响。删除后无法再对原视频重新转写", delVideo));
    const delProc = toggleEl(!!s().delete_process_files_after_transcribe, () => save({ delete_process_files_after_transcribe: !s().delete_process_files_after_transcribe }), "delete_process_files_after_transcribe");
    g.appendChild(row("转写完成后删除过程文件", "转写成功后删除提取的中间音频与原始转写稿，只保留最终文本与元数据", delProc));
    pane.appendChild(g);
    return pane;
  }

  /* ---------------- 整理与转译（Agent） ---------------- */
  async function copyAgentGuide(markdown) {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(markdown);
        copied = true;
      }
    } catch (_) { /* WebView may deny clipboard access; try local selection. */ }
    if (!copied) {
      const previous = document.activeElement;
      const text = document.createElement("textarea");
      text.value = markdown; text.setAttribute("aria-label", "待复制的配置指南");
      text.style.cssText = "position:fixed;left:-10000px;top:0;";
      document.body.appendChild(text);
      try { text.select(); copied = document.execCommand("copy") === true; }
      catch (_) { copied = false; }
      finally { text.remove(); if (previous?.isConnected) previous.focus({preventScroll:true}); }
    }
    toast(copied ? "配置指南已复制" : "复制未完成，请在指南中选择复制", copied ? "" : "error");
    return copied;
  }

  function agentGuideControls() {
    const controls = document.createElement("div");
    controls.style.cssText = "display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;";
    const read = document.createElement("button"), copy = document.createElement("button");
    for (const button of [read,copy]) { button.type = "button"; button.className = "btn ghost small"; }
    read.textContent = "阅读指南"; read.setAttribute("aria-label", "阅读配置指南");
    copy.textContent = "复制给 Agent"; copy.setAttribute("aria-label", "复制配置指南给 Agent");
    let busy = false;
    const load = async (show) => {
      if (busy) return;
      busy = true; read.disabled = true; copy.disabled = true;
      try {
        const result = await AppApi.agentSetupGuide();
        if (!controls.isConnected || AppState.currentView !== "settings" || section !== "agent") return;
        if (result?.ok !== true || typeof result.markdown !== "string" || !result.markdown.trim()) {
          toast("配置指南暂时无法读取，请重试", "error"); return;
        }
        if (!show) { await copyAgentGuide(result.markdown); return; }
        // Disabling the invoking button during the read releases focus in Chromium.
        // Restore it before the dialog captures its return-focus target.
        read.disabled = false; read.focus({preventScroll:true});
        const modal = openModal("Agent 配置与修复指南", "", '<button id="agentGuideCopy" class="btn primary" type="button" aria-label="复制这份指南给 Agent">复制给 Agent</button><button id="agentGuideClose" class="btn ghost" type="button">关闭</button>');
        const article = document.createElement("article");
        article.className = "reader-body";
        article.style.cssText = "padding:0;max-width:none;font-family:var(--font-ui);font-size:14px;line-height:1.8;";
        article.innerHTML = Markdown.render(result.markdown);
        modal.querySelector(".modal-body").appendChild(article);
        modal.querySelector("#agentGuideClose").onclick = closeModal;
        const modalCopy = modal.querySelector("#agentGuideCopy");
        modalCopy.onclick = async () => {
          if (modalCopy.disabled) return;
          modalCopy.disabled = true;
          try { await copyAgentGuide(result.markdown); }
          finally { modalCopy.disabled = false; }
        };
      } catch (_) {
        if (controls.isConnected && AppState.currentView === "settings" && section === "agent") toast("配置指南暂时无法读取，请重试", "error");
      } finally {
        busy = false; read.disabled = false; copy.disabled = false;
        if (controls.isConnected && AppState.currentView === "settings" && section === "agent" && document.activeElement === document.body) (show ? read : copy).focus({preventScroll:true});
      }
    };
    read.onclick = () => load(true); copy.onclick = () => load(false);
    controls.append(read,copy);
    return controls;
  }

  function buildAgent() {
    const pane = document.createElement("div");
    const g = group("文本整理");
    let relayReady = false;
    const hostedMode = s().agent_billing_mode === "hosted";
    const agentToggle = toggleEl(!!s().agent_enabled, () => {
      if (hostedMode && !relayReady && !s().agent_enabled) return;
      return save({ agent_enabled: !s().agent_enabled });
    }, "agent_enabled");
    g.appendChild(row("AI 整理正文", "", agentToggle));
    const titleToggle = toggleEl(!!s().title_optimization_enabled, () => {
      if (hostedMode && !relayReady && !s().title_optimization_enabled) return;
      return save({ title_optimization_enabled: !s().title_optimization_enabled });
    }, "title_optimization_enabled");
    g.appendChild(row("AI 优化标题", "", titleToggle));
    const billingMode = selectEl(s().agent_billing_mode || "byok", [
      {value:"byok",label:"自带密钥（BYOK）"}, {value:"hosted",label:"FoilPage 托管（积分）"},
    ], value => save({agent_billing_mode:value}).then(settings => {if (settings && AppState.currentView === "settings" && section === "agent") render(); return settings;}), "agent_billing_mode");
    g.appendChild(row("AI 调用方式", "", billingMode));
    g.appendChild(row("配置与修复指南", "", agentGuideControls()));
    if (hostedMode) {
      const controls = document.createElement("div"); controls.className = "hosted-capability";
      const state = valueEl("正在检查托管服务…"); state.id = "hostedAgentState"; state.setAttribute("role","status");
      const check = document.createElement("button"); check.type = "button"; check.className = "btn ghost small"; check.textContent = "检查服务"; check.setAttribute("aria-label","检查托管服务");
      // 托管模型选择（1.3.12）：选项来自服务端 /v1/health 的已复核目录；
      // 目录不足两个模型或服务未就绪时保持禁用，避免选中不可用模型。
      const HOSTED_MODEL_LABELS = { "qwen-plus": "qwen-plus（忠实整理，基础积分）", "deepseek-flash": "deepseek-flash（润色优化，3 倍积分）" };
      const hostedModelSelect = selectEl(String(s().agent_hosted_model || ""), [
        { value: "", label: "默认（服务端推荐）" },
      ], (v) => save({ agent_hosted_model: v }), "agent_hosted_model");
      hostedModelSelect.disabled = true;
      const applyHostedModels = (models) => {
        const catalog = Array.isArray(models) ? models.map(item => item && item.model).filter(Boolean) : [];
        const options = [{ value: "", label: catalog.length ? "默认（服务端推荐）" : "默认（服务端仅此模型）" }];
        catalog.forEach(id => options.push({ value: id, label: HOSTED_MODEL_LABELS[id] || id }));
        hostedModelSelect.replaceChildren(...options.map(opt => {
          const op = document.createElement("option"); op.value = opt.value; op.textContent = opt.label; return op;
        }));
        const current = String(s().agent_hosted_model || "");
        hostedModelSelect.value = catalog.includes(current) ? current : "";
        hostedModelSelect.dataset.initialValue = hostedModelSelect.value;
        hostedModelSelect.disabled = catalog.length < 2 || !relayReady;
      };
      applyHostedModels([]);
      const refresh = async (probe) => {
        check.disabled = true;
        try {
          if (probe) await AppApi.checkSubscriptionService();
          const capability = await AppApi.subscriptionStatus();
          if (!controls.isConnected || !inSection("agent")) return;
          relayReady = capability?.hosted_agent_ready === true;
          state.textContent = relayReady ? "托管已就绪" : capability?.hosted_agent_reason || "托管暂未开放，请检查服务配置";
          applyHostedModels(capability?.hosted_agent_models);
        } catch (_) { if (!controls.isConnected || !inSection("agent")) return; relayReady = false; state.textContent = "暂时无法连接托管服务"; applyHostedModels([]); }
        finally {
          agentToggle.disabled = !relayReady && !s().agent_enabled;
          titleToggle.disabled = !relayReady && !s().title_optimization_enabled;
          check.disabled = false;
        }
      };
      agentToggle.disabled = !s().agent_enabled; titleToggle.disabled = !s().title_optimization_enabled;
      check.onclick = () => refresh(true); controls.append(state,check);
      g.appendChild(row("托管服务", "",controls));
      g.appendChild(row("托管模型", "润色档每次整理消耗 3 倍积分（更重模型）", hostedModelSelect));
      refresh(false);
    }
    const level = selectEl(s().agent_editing_level || "auto", [
      { value: "auto", label: "智能（按内容自选）" },
      { value: "light", label: "轻整理（原话排版）" },
      { value: "balanced", label: "适度整理（通顺易读）" },
      { value: "structured", label: "结构整理（清晰成篇）" },
      { value: "rewrite", label: "精炼改写（自由发挥）" },
    ], (v) => save({ agent_editing_level: v }), "agent_editing_level");
    level.dataset.tour = "editing-level";
    g.appendChild(row("整理力度", "", level));
    g.appendChild(row("记住重新转写选项", "开启后按上次选择执行，关闭后每次确认", toggleEl(
      !!s().reorganize_no_remind, () => save({ reorganize_no_remind: !s().reorganize_no_remind }), "reorganize_no_remind")));
    const provider = selectEl(
      s().agent_provider || "mimo",
      [
        { value: "mimo", label: "MiMo 视觉模型（推荐）" },
        { value: "deepseek", label: "DeepSeek" },
        { value: "openai", label: "OpenAI 兼容" },
      ],
      (v) => save({ agent_provider: v }), "agent_provider"
    );
    g.appendChild(row("服务商", "MiMo 可读取视频帧字幕，辅助纠正同音字和专名", provider));
    const baseUrl = inputEl(s().agent_base_url || "https://api.xiaomimimo.com/v1", (v) => save({ agent_base_url: v }));
    g.appendChild(row("接口地址", "", baseUrl));
    const agentModel = inputEl(s().agent_model || "mimo-v2.5", (v) => save({ agent_model: v }));
    g.appendChild(row("模型", "", agentModel));
    const apiKey = inputEl(s().agent_api_key || "", (v) => save({ agent_api_key: v }), { password: true });
    apiKey.dataset.tour = "agent-key";
    g.appendChild(row("API Key", "", apiKey));
    const profile = selectEl(s().agent_profile || "auto", profileOptions(), (v) => save({ agent_profile: v }), "agent_profile");
    g.appendChild(row("默认策略", "", profile));
    const temp = inputEl(String(s().agent_temperature ?? 0.6), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 0 || n > 2) { toast("创造性需在 0-2 之间", "error"); return false; }
      return save({ agent_temperature: n });
    }, { number: true });
    g.appendChild(row("模型温度", "高级参数，与整理力度无关", temp));
    const timeout = inputEl(String(s().agent_timeout_seconds ?? 90), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 5) { toast("超时需至少 5 秒", "error"); return false; }
      return save({ agent_timeout_seconds: n });
    }, { number: true });
    g.appendChild(row("超时（秒）", "", timeout));
    const visionToggle = toggleEl(!!s().agent_vision_enabled, () => save({ agent_vision_enabled: !s().agent_vision_enabled }), "agent_vision_enabled");
    visionToggle.disabled = hostedMode;
    g.appendChild(row("启用字幕视觉校对", hostedMode ? "托管目前仅支持文本；视觉核对需切换到自带密钥" : "发现视频帧字幕时，优先用画面核对含糊词；无法确认时保留原词并标记待确认", visionToggle));
    const visionModel = inputEl(s().agent_vision_model || "mimo-v2.5", (v) => save({ agent_vision_model: v }));
    g.appendChild(row("视觉模型", "MiMo 当前视觉模型建议使用 mimo-v2.5", visionModel));
    const visionFrames = inputEl(String(s().agent_vision_frame_count || 6), (v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 12) { toast("视觉帧数需为 1-12", "error"); return false; }
      return save({ agent_vision_frame_count: n });
    }, { number: true });
    g.appendChild(row("视觉帧数", "每条任务最多发送的字幕候选帧，越高越容易覆盖字幕变化", visionFrames));
    const delSrc = toggleEl(!!s().delete_source_after_translation, () => save({ delete_source_after_translation: !s().delete_source_after_translation }), "delete_source_after_translation");
    g.appendChild(row("整理完成后删除视频", "", delSrc));
    const titleRules = document.createElement('details');
    titleRules.className = 'set-advanced';
    titleRules.innerHTML = '<summary>标题模型与规则</summary>';
    const titleModel = inputEl(s().title_model || "", v => save({ title_model: v }));
    titleModel.disabled = hostedMode;
    titleRules.appendChild(row("标题模型", hostedMode ? "托管使用服务端模型；此项仅对自带密钥生效" : "留空使用整理模型；每条转写额外请求一次", titleModel));
    const titlePrompt = document.createElement('textarea');
    titlePrompt.className = 'set-input';
    titlePrompt.rows = 4;
    titlePrompt.maxLength = 4000;
    titlePrompt.value = s().title_skill_prompt || '';
    titlePrompt.dataset.initialValue = titlePrompt.value;
    titlePrompt.placeholder = '可追加命名要求，如保留关键专名、优先使用对象与核心问题';
    titlePrompt.setAttribute('aria-label', '标题附加规则');
    titlePrompt.onchange = () => runControl(titlePrompt, () => save({ title_skill_prompt: titlePrompt.value }));
    titleRules.appendChild(row("标题 skill", "内置 concise-accurate-title：准确、简洁、无引流措辞，可追加要求", titlePrompt));
    g.appendChild(titleRules);
    const updTitle = toggleEl(!!s().update_title_after_transcribe, () => save({ update_title_after_transcribe: !s().update_title_after_transcribe }), "update_title_after_transcribe");
    titleRules.appendChild(row("同步到任务标题", "将优化后的标题同步到任务列表；关闭标题生成时保留原标题", updTitle));
    const advanced = document.createElement('details');
    advanced.className = 'set-advanced';
    advanced.innerHTML = '<summary>模型与连接设置</summary>';
    const advancedLabels = new Set(['服务商', '接口地址', '模型', 'API Key', '模型温度', '超时（秒）', '视觉模型', '视觉帧数']);
    Array.from(g.querySelectorAll('.set-row')).forEach(r => {
      if (advancedLabels.has(r.querySelector('.set-label')?.textContent)) advanced.appendChild(r);
    });
    // Configuration must be immediately discoverable on a fresh installation.
    advanced.open = !hostedMode && !s().agent_api_key;
    if (hostedMode) {
      [provider, baseUrl, agentModel, apiKey, temp, visionModel, visionFrames].forEach(control => { control.disabled = true; });
      advanced.querySelector("summary").textContent = "自带密钥模型与连接设置";
    }
    g.appendChild(advanced);
    pane.appendChild(g);
    const customGroup = group("自定义 Agent");
    const customList = document.createElement("div");
    customGroup.appendChild(customList);
    const addBtn = document.createElement("button");
    addBtn.className = "btn small";
    addBtn.type = "button";addBtn.classList.add("custom-agent-add");
    addBtn.textContent = "＋ 添加自定义 Agent";
    addBtn.onclick = () => {
      const profiles = (s().agent_custom_profiles || []).slice();
      const id = "custom-" + Date.now().toString(36);
      profiles.push(Object.assign({}, CUSTOM_PROFILE_TEMPLATE, { id, name: "新的整理策略" }));
      runControl(addBtn, () => save({ agent_custom_profiles: profiles }), true);
    };
    customGroup.appendChild(addBtn);
    renderCustomAgents(customList);
    pane.appendChild(customGroup);
    return pane;
  }

  function renderCustomAgents(container) {
    container.innerHTML = "";
    const profiles = (s().agent_custom_profiles || []).slice();
    profiles.forEach((p, index) => {
      const card = document.createElement("div");
      card.className = "set-group";
      card.classList.add("custom-agent-card");
      card.dataset.profileId = p.id || "";

      const head = document.createElement("div");
      head.className = "custom-agent-head";
      const name = inputEl(p.name || "", (v) => updateCustomProfile(container, index, { name: v }));
      name.setAttribute("aria-label", "自定义 Agent 名称");
      name.style.flex = "1";
      head.appendChild(name);
      const enableToggle = toggleEl(p.enabled !== false, () => {
        const enabled = !card.querySelector(".toggle").classList.contains("on");
        return updateCustomProfile(container, index, { enabled });
      }, () => {const profile = s().agent_custom_profiles?.find(item => item.id === p.id); return profile ? profile.enabled !== false : undefined;});
      head.appendChild(enableToggle);
      enableToggle.setAttribute("aria-label", "启用自定义 Agent");
      const del = document.createElement("button");
      del.className = "btn danger small";
      del.textContent = "删除";
      del.onclick = () => {
        const list = collectCustomProfiles(container);
        list.splice(index, 1);
        runControl(del, () => save({ agent_custom_profiles: list }), true);
      };
      head.appendChild(del);
      card.appendChild(head);

      const promptLabel = document.createElement("div");
      promptLabel.className = "set-label";
      promptLabel.style.cssText = "margin:10px 0 4px;";
      promptLabel.textContent = "系统提示词";
      card.appendChild(promptLabel);
      const prompt = document.createElement("textarea");
      prompt.className = "ctrl";
      prompt.classList.add("set-input");
      prompt.value = p.system_prompt || "";
      prompt.dataset.initialValue = prompt.value;
      prompt.setAttribute("aria-label", "自定义 Agent 系统提示词");
      prompt.placeholder = "定义这个 Agent 的行为与输出要求";
      prompt.onchange = () => runControl(prompt, () => updateCustomProfile(container, index, { system_prompt: prompt.value }));
      card.appendChild(prompt);

      const meta = document.createElement("div");
      meta.className = "custom-agent-meta";
      const model = inputEl(p.model || "", (v) => updateCustomProfile(container, index, { model: v }));
      model.setAttribute("aria-label", "自定义 Agent 模型");
      model.disabled = s().agent_billing_mode === "hosted";
      model.placeholder = "模型（留空跟随默认）";
      model.style.flex = "1";
      meta.appendChild(model);
      const temperature = inputEl(p.temperature == null ? "" : String(p.temperature), (v) => {
        const n = v.trim() === "" ? null : parseFloat(v);
        if (n != null && (isNaN(n) || n < 0 || n > 2)) { toast("创造性需在 0-2 之间", "error"); return false; }
        return updateCustomProfile(container, index, { temperature: n });
      }, { number: true });
      temperature.placeholder = "创造性（留空跟随默认）";
      temperature.setAttribute("aria-label", "自定义 Agent 创造性");
      temperature.classList.add("custom-agent-temperature");
      meta.appendChild(temperature);
      card.appendChild(meta);
      container.appendChild(card);
    });
  }

  function updateCustomProfile(container, index, changes) {
    const list = collectCustomProfiles(container);
    if (!list[index]) return;
    const id = list[index].id;
    const enabledRevision = Object.hasOwn(changes, "enabled") ? (customProfileEnabledRevisions.get(id) || 0) + 1 : null;
    if (enabledRevision != null) customProfileEnabledRevisions.set(id, enabledRevision);
    customProfileEdits.set(id, {...customProfileEdits.get(id), ...changes});
    list[index] = Object.assign({}, list[index], changes);
    return save({ agent_custom_profiles: list }, false).then(settings => {
      if (!settings && enabledRevision != null && customProfileEnabledRevisions.get(id) === enabledRevision) {
        const draft = customProfileEdits.get(id);
        if (draft) {
          const {enabled, ...remaining} = draft;
          if (Object.keys(remaining).length) customProfileEdits.set(id, remaining); else customProfileEdits.delete(id);
        }
      }
      return settings;
    });
  }

  function collectCustomProfiles(container) {
    const cards = Array.from(container.querySelectorAll(":scope > .set-group"));
    return cards.map((card) => {
      const inputs = card.querySelectorAll("input");
      const prompt = card.querySelector("textarea");
      return {...{
        id: card.dataset.profileId || "",
        name: inputs[0] ? inputs[0].value : "",
        system_prompt: prompt ? prompt.value : "",
        enabled: card.querySelector(".toggle") ? card.querySelector(".toggle").classList.contains("on") : true,
        model: inputs[1] ? inputs[1].value : "",
        temperature: inputs[2] && inputs[2].value.trim() !== "" ? parseFloat(inputs[2].value) : null,
      }, ...customProfileEdits.get(card.dataset.profileId)};
    });
  }

  /* ---------------- 模型 ---------------- */
  function modelAvailability(item, data) {
    const busy = !!item && (modelBusy.has(item.name) || data?.download?.name === item.name);
    const confirmed = !!item && (Object.hasOwn(item, "ready") ? item.ready === true : item.verified === true);
    const ready = confirmed && !busy;
    const reason = ready ? "" : busy ? "下载中" : item?.reason || (item?.installed === true ? "缓存不完整，请重新下载" : "未下载");
    return {ready, reason};
  }

  function syncDefaultModel(box, data, message) {
    const select = box.defaultModelSelect;
    if (!select || !select.isConnected) return;
    const readable = data?.ok === true && Array.isArray(data.models), selected = select.value;
    let available = false;
    MODELS.forEach(model => {
      const option = [...select.options].find(item => item.value === model.value);
      if (!option) return;
      const item = readable ? data.models.find(item => item.name === model.value) : null;
      const state = readable ? modelAvailability(item, data) : {ready:false, reason:message || "正在检查"};
      option.disabled = !state.ready;
      option.textContent = `${model.value}（${(model.label.split("（")[1] || "").split("，")[0]}，${state.ready ? "已就绪" : state.reason}）`;
      option.title = `${model.label}（${state.ready ? "已就绪" : state.reason}）`;
      available ||= state.ready;
    });
    select.value = selected;
    select.title = select.selectedOptions[0]?.title || select.selectedOptions[0]?.textContent || "";
    select.dataset.modelAvailable = String(available);
    select.disabled = !available || pendingControls.has("model:" + controlKey(select));
  }

  function buildModel() {
    const pane = document.createElement("div");
    const w = AppState.worker || {};
    let g = group("推理引擎");
    const wc = document.createElement("div");
    wc.className = "row-controls";
    const dot = document.createElement("span");
    dot.className = "worker-dot" + (w.running ? " on" : "");
    dot.style.cssText = "width:11px;height:11px;border-radius:50%;background:" + (w.running ? "var(--primary)" : "var(--faint)");
    const label = document.createElement("span");
    label.style.cssText = "font-size:13px;color:var(--text2);font-weight:600";
    label.textContent = w.label || "Worker 已停止";
    const meta = document.createElement("span");
    meta.style.cssText = "font-size:11px;color:var(--muted)";
    meta.textContent = w.meta || "";
    const btn = document.createElement("button");
    btn.className = "btn " + (w.running ? "danger small" : "primary small");
    btn.textContent = w.running ? "停止 Worker" : "启动 Worker";
    btn.onclick = () => runControl(btn, async () => {
      try {
        const state = await AppApi.workerToggle();
        if (!state || state.ok === false || typeof state.running !== "boolean") throw new Error(state?.error || "未收到操作确认");
        AppState.worker = state; renderWorkerCard();
        if (inSection("model") && wc.isConnected) toast(state.running ? "Worker 已启动" : "Worker 已停止");
        return state;
      } catch (error) {if (inSection("model") && wc.isConnected) toast("操作失败：" + error.message, "error");}
    }, true);
    wc.appendChild(dot);
    wc.appendChild(label);
    wc.appendChild(meta);
    wc.appendChild(btn);
    g.appendChild(row("运行状态", "", wc));
    pane.appendChild(g);

    g = group("转写模型");
    const model = selectEl(s().whisper_model || "tiny", MODELS, (v) => {
      if (model.disabled || !model.selectedOptions[0] || model.selectedOptions[0].disabled) {model.value = s().whisper_model || "tiny"; return false;}
      return save({ whisper_model: v });
    }, "whisper_model");
    model.dataset.tour = "default-model";
    model.dataset.modelSelect = "true"; model.dataset.modelAvailable = "false"; model.disabled = true;
    [...model.options].forEach(option => {option.disabled = true; option.textContent = option.value + "（正在检查）";});
    if (![...model.options].some(option => option.value === s().whisper_model) && s().whisper_model) {
      const missing = document.createElement("option"); missing.value = s().whisper_model; missing.textContent = s().whisper_model + "（不可用）"; missing.disabled = true; model.appendChild(missing); model.value = missing.value;
    }
    g.appendChild(row("默认模型", "", model));
    const modelDir = s().model_dir || "";
    g.appendChild(row("模型缓存", "", dirCtrl(modelDir, "model_dir", modelDir)));
    const endpoint = inputEl(s().hf_endpoint || "", (v) => save({ hf_endpoint: v }));
    endpoint.dataset.tour = "model-mirror";
    g.appendChild(row("Hugging Face 镜像", "", endpoint));
    pane.appendChild(g);

    const lib = group("模型库");
    lib.dataset.tour = "model-library";
    const libHint = document.createElement("div");
    libHint.className = "set-hint";
    libHint.textContent = "下载完成后即可选择模型。";
    lib.appendChild(libHint);
    const guide = document.createElement("div");
    guide.className = "model-guide";
    guide.textContent = "下载建议：small（488 MB）日常够用；medium/large-v3 体积大、耗时久，建议网络稳定时下载，并优先在「Hugging Face 镜像」填入 hf-mirror.com 加速。下载中断后重试会从断点继续。";
    const guideDetails=document.createElement("details");guideDetails.className="set-advanced";
    const guideSummary=document.createElement("summary");guideSummary.textContent="下载建议";guideDetails.append(guideSummary,guide);lib.appendChild(guideDetails);
    const libBox = document.createElement("div");
    libBox.className = "model-lib";
    libBox.defaultModelSelect = model;
    lib.appendChild(libBox);
    renderModelLibrary(libBox);
    pane.appendChild(lib);
    return pane;
  }

  const modelReadTickets = new WeakMap();
  function modelReadError(box, message) {
    box.dataset.loading = "false"; box.setAttribute("aria-busy", "false");
    syncDefaultModel(box, null, "检查失败");
    box.textContent = message;
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "btn ghost small"; retry.textContent = "重试读取模型";
    retry.onclick = () => renderModelLibrary(box); box.appendChild(retry);
  }
  async function renderModelLibrary(box) {
    if (!inSection("model")) return;
    const ticket = (modelReadTickets.get(box) || 0) + 1; modelReadTickets.set(box, ticket);
    const valid = () => box.isConnected && inSection("model") && modelReadTickets.get(box) === ticket;
    box.textContent = "正在读取模型状态…"; box.dataset.loading = "true"; box.setAttribute("aria-busy", "true");
    syncDefaultModel(box, null);
    let data;
    try {
      data = await AppApi.models();
    } catch (e) {
      if (valid()) modelReadError(box, "无法读取模型状态，请重试");
      return;
    }
    if (!valid()) return;
    if (data?.ok !== true || !Array.isArray(data.models)) {
      modelReadError(box, (data && data.error) || "无法读取模型状态");
      return;
    }
    box.dataset.loading = "false"; box.setAttribute("aria-busy", "false");
    syncDefaultModel(box, data);
    box.textContent = "";
    if (!data.models || !data.models.length) {
      const empty=document.createElement("div");empty.className="model-empty";
      empty.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></svg><strong>尚无可用模型</strong><span>检查缓存目录，或在准备页下载模型</span>';
      box.appendChild(empty);
      return;
    }
    /* 缓存空间与模型条目都统一为 set-row 形态，与其他设置行一致 */
    const freeRow = document.createElement("div");
    freeRow.className = "set-row";
    const fl = document.createElement("div");
    fl.className = "set-left";
    const flab = document.createElement("div");
    flab.className = "set-label";
    flab.textContent = "缓存盘剩余空间";
    fl.appendChild(flab);
    const fctrl = document.createElement("div");
    fctrl.className = "set-ctrl";
    const fval = document.createElement("span");
    fval.className = "set-value";
    fval.textContent = fmtBytes(data.free_bytes || 0);
    fctrl.appendChild(fval);
    freeRow.append(fl, fctrl);
    box.appendChild(freeRow);
    const dl = data.download || {};
    data.models.forEach((m) => {
      const availability = modelAvailability(m, data);
      const item = document.createElement("div");
      item.className = "model-item";
      item.dataset.model = m.name;
      const rowEl = document.createElement("div");
      rowEl.className = "set-row";
      const left = document.createElement("div");
      left.className = "set-left";
      const name = document.createElement("div");
      name.className = "set-label";
      name.textContent = m.name + (availability.ready ? "（已就绪）" : "");
      const desc = document.createElement("div");
      desc.className = "set-hint model-desc";
      desc.textContent = m.size_label + "，" + m.description
        + (!availability.ready && !modelBusy.has(m.name) && dl.name !== m.name ? "（" + availability.reason + "）" : "");
      left.appendChild(name);
      left.appendChild(desc);
      const act = document.createElement("div");
      act.className = "set-ctrl model-act";
      act.dataset.tour = "model-" + m.name;
      const busy = modelBusy.has(m.name) || dl.name === m.name;
      if (busy) {
        const pill = document.createElement("button");
        pill.className = "state-pill state-pill--labeled";
        pill.dataset.state = "active";
        pill.disabled = true;
        pill.innerHTML = `<span class="thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/></svg></span><span class="label">下载中…</span>`;
        act.appendChild(pill);
      } else if (availability.ready) {
        const btn = document.createElement("button");
        btn.className = "btn danger small";
        btn.textContent = "删除";
        btn.onclick = () => {
          ConfirmDelete.run({
            message: "确定删除 " + m.name + " 模型缓存？删除后可随时重新下载。",
            onConfirm: () => AppApi.deleteModel(m.name).then((res) => {
              if (res && res.ok) {
                toast("已删除 " + m.name);
                renderModelLibrary(box);
              } else {
                toast((res && res.error) || "删除失败", "error");
              }
            }),
          });
        };
        act.appendChild(btn);
      } else {
        const pill = document.createElement("button");
        pill.className = "state-pill state-pill--labeled";
        pill.dataset.state = "idle";
        pill.innerHTML = `<span class="thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12"/><path d="m6 12 6 6 6-6"/><path d="M5 20h14"/></svg></span><span class="label">${m.installed === true ? "重新下载" : "下载"}</span>`;
        const isBig = m.name === "medium" || m.name === "large-v3";
        pill.onclick = isBig
          ? () => confirmBigDownload(m, box)
          : () => downloadModelByName(m.name, box);
        act.appendChild(pill);
      }
      rowEl.appendChild(left);
      rowEl.appendChild(act);
      item.appendChild(rowEl);
      if (busy) {
        const pct = (dl.name === m.name && dl.progress !== null && dl.progress !== undefined)
          ? Math.round(dl.progress * 100) : 0;
        const bar = document.createElement("div");
        bar.className = "model-bar";
        const fill = document.createElement("div");
        fill.className = "model-bar-fill";
        fill.style.width = pct + "%";
        bar.appendChild(fill);
        item.appendChild(bar);
        const msg = document.createElement("div");
        msg.className = "model-msg";
        msg.textContent = (dl.name === m.name && dl.message) || "准备中…";
        item.appendChild(msg);
      }
      box.appendChild(item);
    });
    if (modelBusy.size || dl.name) startModelProgress(box);
  }

  function confirmBigDownload(m, box) {
    ConfirmDelete.run({
      message: m.name + "（" + m.size_label + "）下载耗时取决于网速，可能数分钟到数十分钟。"
        + "建议先确认「Hugging Face 镜像」已配置（如 hf-mirror.com）并留有足够磁盘空间；"
        + "中断后重试会从断点继续。确认开始下载？",
      onConfirm: () => downloadModelByName(m.name, box),
    });
  }

  async function downloadModelByName(name, box) {
    if (modelBusy.has(name)) return;
    modelBusy.add(name);
    renderModelLibrary(box);
    try {
      const res = await AppApi.downloadModel(name);
      if (res && res.ok) {
        if (box.isConnected && inSection("model")) toast("开始下载模型 " + name + "，可在下方查看进度");
      } else {
        modelBusy.delete(name);
        if (box.isConnected && inSection("model")) toast((res && res.error) || "模型下载失败", "error");
      }
    } catch (e) {
      modelBusy.delete(name);
      if (box.isConnected && inSection("model")) toast("模型下载失败：" + (e && e.message || e), "error");
    } finally {
      modelBusy.delete(name); if (box.isConnected && inSection("model")) renderModelLibrary(box);
    }
  }

  function startModelProgress(box) {
    if (modelTimer) return;
    let reading = false;
    modelTimer = setInterval(async () => {
      if (!box.isConnected || !inSection("model")) {stopModelProgress(); return;}
      if (reading) return;
      reading = true;
      const ticket = modelReadTickets.get(box);
      const valid = () => box.isConnected && inSection("model") && modelReadTickets.get(box) === ticket;
      let data;
      try {
        data = await AppApi.models();
      } catch (e) {
        if (valid()) syncDefaultModel(box, null, "检查失败");
        return;
      } finally {
        reading = false;
      }
      if (!valid()) return;
      if (data?.ok !== true || !Array.isArray(data.models)) {syncDefaultModel(box, null, "检查失败"); return;}
      syncDefaultModel(box, data);
      if (!data.download?.name && !modelBusy.size) {
        const failure = data && data.download && data.download.error;
        stopModelProgress();
        if (failure) toast("模型下载失败：" + failure, "error");
        renderModelLibrary(box);
        return;
      }
      if (!data.download?.name) return;
      const dl = data.download;
      const item = box.querySelector('.model-item[data-model="' + dl.name + '"]');
      if (!item) return;
      const pct = (dl.progress === null || dl.progress === undefined)
        ? null : Math.round(dl.progress * 100);
      const label = item.querySelector(".model-act .state-pill .label");
      if (label) label.textContent = pct === null ? "下载中…" : "下载中 " + pct + "%";
      const fill = item.querySelector(".model-bar-fill");
      if (fill) fill.style.width = (pct || 0) + "%";
      const msg = item.querySelector(".model-msg");
      if (msg) msg.textContent = dl.message || "";
    }, 1200);
  }

  function stopModelProgress() {
    if (modelTimer) {
      clearInterval(modelTimer);
      modelTimer = null;
    }
  }

  function fmtBytes(bytes) {
    if (!bytes || bytes < 0) return "未知";
    const gib = bytes / (1024 * 1024 * 1024);
    if (gib >= 1) return gib.toFixed(1) + " GB";
    const mib = bytes / (1024 * 1024);
    return mib.toFixed(0) + " MB";
  }

  /* ---------------- 数据 ---------------- */
  function renderReconcile(box, res) {
    if (!box.isConnected || !inSection("data")) return;
    box.innerHTML = "";
    if (!res?.ok) {box.textContent = res?.error || "数据自检失败，请重试"; return;}
    box.style.maxHeight = "220px";
    box.style.overflowY = "auto";
    const add = (text, cls) => {
      const line = document.createElement("div");
      line.className = cls || "reconcile-line";
      line.textContent = text;
      box.appendChild(line);
    };
    const fixed = (res.stale_fixed || []).length;
    const orphansOut = res.orphan_outputs || [];
    const orphansDl = res.orphan_downloads || [];
    add("悬空任务已收敛：" + fixed + " 条", fixed ? "reconcile-ok" : "reconcile-muted");
    if (!orphansOut.length && !orphansDl.length) {
      add("未发现孤儿产物。", "reconcile-muted");
      return;
    }
    add("孤儿产物（无任务记录，可安全清理）：", "");
    [...orphansOut, ...orphansDl].forEach((item) => {
      const wrap = document.createElement("div");
      wrap.className = "reconcile-line";
      const name = document.createElement("span");
      name.textContent = (item.name || item.path || "?") + "  ";
      const clean = document.createElement("button");
      clean.className = "btn ghost tiny";
      clean.textContent = "清理";
      clean.onclick = () => {
        if (clean.disabled) return;
        ConfirmDelete.run({
          message: "确定清理孤儿产物？\n" + (item.path || ""),
          onConfirm: async () => {
            const isOut = item.path && orphansOut.includes(item);
            const payload = isOut ? { outputs: [item.path], downloads: [] } : { outputs: [], downloads: [item.path] };
            clean.disabled = true;
            try {
              const result = await AppApi.cleanupOrphans(payload.outputs, payload.downloads);
              if (!result?.ok) throw new Error(result?.error || "清理失败，请重试");
              const removed = [...(result.removed_outputs || []), ...(result.removed_downloads || [])];
              if (!removed.includes(item.path)) throw new Error("文件未被清理，请重新自检后重试");
              if (!box.isConnected || !inSection("data")) return;
              toast("已清理", "success");
              renderReconcile(box, await AppApi.dataReconcile());
            } catch (e) {if (box.isConnected && inSection("data")) toast(e?.message || "清理失败，请重试", "error");}
            finally {clean.disabled = false;}
          },
        });
      };
      wrap.appendChild(name);
      wrap.appendChild(clean);
      box.appendChild(wrap);
    });
  }

  /* ---------------- 缓存清理 ---------------- */
  async function renderCacheBox(box) {
    box.textContent = "正在读取缓存状态…";
    let data;
    try {
      data = await AppApi.cacheStatus();
    } catch (e) {
      if (!box.isConnected || !inSection("data")) return;
      box.textContent = "无法读取缓存状态：" + (e && e.message || e);
      return;
    }
    if (!box.isConnected || !inSection("data")) return;
    if (!data || !data.ok) {
      box.textContent = (data && data.error) || "无法读取缓存状态";
      return;
    }
    box.textContent = "";
    const row = (label, bytes, hint, btnText, onClick) => {
      const r = document.createElement("div");
      r.className = "set-row";
      const left = document.createElement("div");
      left.className = "set-left";
      const lab = document.createElement("div");
      lab.className = "set-label";
      lab.textContent = label;
      const h = document.createElement("div");
      h.className = "set-hint";
      h.textContent = hint + "：" + (bytes > 0 ? fmtBytes(bytes) : "无");
      left.appendChild(lab);
      left.appendChild(h);
      const c = document.createElement("div");
      c.className = "set-ctrl";
      const b = document.createElement("button");
      b.className = "btn small" + (bytes > 0 ? "" : " ghost");
      b.textContent = btnText;
      b.disabled = bytes <= 0;
      b.onclick = onClick;
      c.appendChild(b);
      r.appendChild(left);
      r.appendChild(c);
      box.appendChild(r);
    };
    row("临时文件与日志", data.cleanable_bytes || 0,
      "转写/下载中断遗留与日志轮转备份",
      "清理", () => {
        ConfirmDelete.run({
          message: "确定清理临时缓存与轮转日志？不影响任何任务记录与文本产物。",
          onConfirm: () => AppApi.cleanupCache().then((res) => {
            if (res && res.ok) {
              toast(res.freed_bytes > 0 ? "已释放 " + fmtBytes(res.freed_bytes) : "没有可清理的缓存", "success");
              renderCacheBox(box);
            } else {
              toast((res && res.error) || "清理失败", "error");
            }
          }).catch((e) => toast("清理失败：" + (e && e.message || e), "error")),
        });
      });
    row("模型缓存", data.model_bytes || 0,
      "已下载的转写模型，删除后需重新下载",
      "全部删除", () => {
        ConfirmDelete.run({
          message: "确定删除全部模型缓存？将释放 " + fmtBytes(data.model_bytes || 0) + "，删除后需重新下载才能转写。",
          onConfirm: () => AppApi.deleteAllModels().then((res) => {
            if (res && res.ok) {
              toast("已删除 " + (res.removed || 0) + " 个模型缓存", "success");
              renderCacheBox(box);
            } else {
              toast((res && res.error) || "删除失败", "error");
            }
          }).catch((e) => toast("删除失败：" + (e && e.message || e), "error")),
        });
      });
  }

  function buildAgentControl() {
    const pane=document.createElement("div");
    const g = group("连接 Agent");
    const apiToggle = toggleEl(!!s().local_api_enabled, () => {
      return save({ local_api_enabled: !s().local_api_enabled }, false).then(settings => {
        if (settings && inSection("control")) toast("本地 API 设置已保存，重启后生效");
        return settings;
      });
    }, "local_api_enabled");
    g.appendChild(row("启用本地 API", "", apiToggle));
    const port = valueEl(String(s().local_api_port || 18765));
      g.appendChild(row("监听端口", "", port));
      const agentControls = document.createElement("div");
      agentControls.className = "row-controls";
      for (const [label, allowWrite] of [["完整 MCP", true], ["只读 MCP", false]]) {
        const button = document.createElement("button");
        button.className = "btn small"; button.textContent = label;
        button.onclick = async () => {
          button.disabled = true;
          try {
            const result = await AppApi.agentControlConfig(allowWrite);
            if (!button.isConnected || !inSection("control")) return;
            if (!result?.ok) { toast("配置读取失败", "error"); return; }
            button.disabled = false; button.focus({preventScroll:true});
            const modal = openModal("连接您的 Agent", "", '<button id="copyAgentMcp" class="btn primary" type="button">复制 MCP 配置</button><button id="closeAgentMcp" class="btn ghost" type="button">关闭</button>');
            const note = document.createElement("p");
            note.textContent = result.available ? (allowWrite ? "可管理内容、模型与转写提示词，并控制任务" : "仅查询任务、文稿与配置") : "请先安装包含 Agent 控制程序的新版 FoilPage";
            modal.querySelector(".modal-body").appendChild(note);
            const code = document.createElement("pre");
            code.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.6;user-select:text;";
            code.textContent = JSON.stringify(result.configuration, null, 2); modal.querySelector(".modal-body").appendChild(code);
            const copy = document.getElementById("copyAgentMcp"); copy.disabled = !result.available;
            copy.onclick = async () => {
              try { await navigator.clipboard.writeText(code.textContent); toast("MCP 配置已复制"); }
              catch (_) { toast("复制失败，请手动选择配置", "error"); }
            };
            document.getElementById("closeAgentMcp").onclick = closeModal;
          } catch (_) { if (button.isConnected) toast("配置读取失败", "error"); }
          finally { button.disabled = false; }
        };
        agentControls.appendChild(button);
      }
      g.appendChild(row("Agent 控制", "", agentControls));
      pane.appendChild(g);

    const activity=group("控制状态");
    activity.id="agentActivity";
    const state=valueEl("正在读取…");state.id="agentActivityState";state.setAttribute("role","status");
    activity.appendChild(row("Agent", "", state));
    const saved=valueEl("—");saved.id="agentLastSaved";activity.appendChild(row("最近保存", "", saved));
    const details=document.createElement("details");details.className="set-advanced";
    const summary=document.createElement("summary");summary.textContent="最近活动";
    const list=document.createElement("ul");list.className="agent-activity-list";list.id="agentActivityRecords";
    details.append(summary,list);activity.appendChild(details);pane.appendChild(activity);
    const stamp=value=>{const date=new Date(typeof value==="number"?value*1000:value);return Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';};
    const refresh=async()=>{
      try{
        const result=await AppApi.agentActivity();
        if(!activity.isConnected || !inSection("control"))return;
        if(result?.ok!==true){state.textContent="暂时无法读取";return;}
        const processingEnabled=typeof result.processing_enabled==='boolean'?result.processing_enabled:result.enabled===true;
        state.textContent=processingEnabled?"Agent 模型已启用":"Agent 模型未启用";
        state.classList.toggle("is-enabled",processingEnabled);
        saved.textContent=result.last_saved_at?stamp(result.last_saved_at):"尚无保存记录";
        if(typeof result.last_revision==='string' && /^[a-f0-9]{64}$/.test(result.last_revision))saved.title="版本 "+result.last_revision.slice(0,12);
        list.replaceChildren();
        const records=Array.isArray(result.records)?result.records.slice(0,8):[];
        for(const record of records){
          if(typeof record.operation!=="string" || typeof record.ok!=="boolean")continue;
          const line=document.createElement('li'),label=document.createElement('span'),time=document.createElement('time'),resultText=document.createElement('span');
          label.textContent=record.operation.slice(0,80);time.textContent=stamp(record.at);resultText.textContent=record.ok?'完成':'未完成';resultText.className=record.ok?'':'is-error';
          line.append(label,time,resultText);list.appendChild(line);
        }
        if(!list.childElementCount){const empty=document.createElement('li');empty.className='is-empty';empty.textContent='尚无 Agent 活动';list.appendChild(empty);}
      }catch(_){if(activity.isConnected && inSection("control"))state.textContent="暂时无法读取";}
    };
    refresh();return pane;
  }

  function buildData() {
    const pane = document.createElement("div");
    let g = group("目录");
    const dataDir = s().data_dir || "";
    g.appendChild(row("数据目录", "", dirCtrl(dataDir, null, dataDir)));
    const outDir = s().output_dir || "";
    g.appendChild(row("输出目录", "", dirCtrl(outDir, "output_dir", outDir)));
    const dlDir = s().download_dir || "";
    g.appendChild(row("下载目录", "", dirCtrl(dlDir, "download_dir", dlDir)));
    const browserDl = s().browser_download_dir || "";
    g.appendChild(row("Chrome 下载目录", "", dirCtrl(browserDl, "browser_download_dir", browserDl)));
    pane.appendChild(g);

    const fold=(title,content)=>{
      const details=document.createElement('details');details.className='set-advanced settings-section-fold';
      const summary=document.createElement('summary');summary.textContent=title;content.querySelector('h3')?.remove();details.append(summary,content);return details;
    };
    g=group('视频读取');
    g.appendChild(row('默认目录','',dirCtrl(dlDir,null,dlDir)));
    g.appendChild(row('读取子目录','',toggleEl(s().video_scan_recursive===true,()=>save({video_scan_recursive:s().video_scan_recursive!==true}), 'video_scan_recursive')));
    const addDirectory=document.createElement('button');addDirectory.type='button';addDirectory.className='btn ghost small';addDirectory.textContent='添加目录';addDirectory.id='addVideoScanDirectory';
    const pathKey=value=>String(value).replace(/[\\/]+$/,'').replace(/\//g,'\\').toLocaleLowerCase('en-US');
    addDirectory.onclick=()=>runControl(addDirectory,async()=>{
      const accountContext=JSON.stringify([s().account_login_token,s().subscription_server_url,s().subscription_license_public_key]);
      try{
        const picked=await AppApi.pickVideoDirectory();
        if(picked?.cancelled || !inSection('data') || !addDirectory.isConnected)return;
        if(accountContext!==JSON.stringify([s().account_login_token,s().subscription_server_url,s().subscription_license_public_key]))return;
        const value=typeof picked?.path==='string'?picked.path.trim():'';
        if(picked?.ok!==true || value.length>4096 || !(/^[a-z]:[\\/]/i.test(value) || /^\/(?!\/)/.test(value)) || /^[\\/]{2}/.test(value) || value.includes('://') || /[\x00-\x1f]/.test(value))throw new Error('请选择本机绝对路径');
        const directories=Array.isArray(s().video_scan_directories)?s().video_scan_directories:[];
        if([dlDir,...directories].some(path=>path && pathKey(path)===pathKey(value))){toast('此目录已在读取范围内');return;}
        if(directories.length>=20)throw new Error('最多添加 20 个目录');
        return await save({video_scan_directories:[...directories,value]});
      }catch(error){if(addDirectory.isConnected && inSection('data'))toast(error.message||'目录添加失败','error');}
    },true);
    g.appendChild(row('额外目录','',addDirectory));
    const directoryList=document.createElement('ul');directoryList.className='scan-directory-list';directoryList.id='videoScanDirectories';
    for(const value of Array.isArray(s().video_scan_directories)?s().video_scan_directories:[]){
      const item=document.createElement('li'),path=document.createElement('span'),remove=document.createElement('button');
      path.className='path-text';path.textContent=value;path.title=value;
      remove.type='button';remove.className='btn ghost small';remove.textContent='移除';remove.setAttribute('aria-label','移除目录 '+value);
      remove.onclick=()=>runControl(remove,()=>save({video_scan_directories:(s().video_scan_directories||[]).filter(path=>path!==value)}),true);
      item.append(path,remove);directoryList.appendChild(item);
    }
    if(!directoryList.childElementCount){const empty=document.createElement('li');empty.className='scan-directory-empty';empty.textContent='仅读取默认下载目录';directoryList.appendChild(empty);}
    g.appendChild(directoryList);pane.appendChild(fold('视频读取',g));

    g=group('失败记录清理');
    const retentionDays=inputEl(String(s().failed_task_retention_days??0),value=>{
      const number=Number(value);if(!value.trim() || !Number.isSafeInteger(number) || number<0 || number>3650){toast('保留天数需为 0–3650 的整数','error');return false;}
      return save({failed_task_retention_days:number});
    },{number:true});retentionDays.min='0';retentionDays.max='3650';retentionDays.step='1';
    const cleanupHours=inputEl(String(s().failed_task_cleanup_interval_hours??24),value=>{
      const number=Number(value);if(!value.trim() || !Number.isSafeInteger(number) || number<1 || number>720){toast('检查间隔需为 1–720 的整数小时','error');return false;}
      return save({failed_task_cleanup_interval_hours:number});
    },{number:true});cleanupHours.min='1';cleanupHours.max='720';cleanupHours.step='1';
    g.appendChild(row('保留天数','0 为关闭；只清理无正文、无文件的空失败记录',retentionDays));
    g.appendChild(row('检查间隔（小时）','',cleanupHours));pane.appendChild(fold('失败记录清理',g));

    g = group("下载");
    const dlCovers = toggleEl(s().download_covers !== false, () => save({ download_covers: s().download_covers === false }), "download_covers");
    g.appendChild(row("下载封面", "下载视频时同步保存平台封面，任务列表与详情显示缩略图；封面获取失败不影响下载", dlCovers));
    const rate = inputEl(String(s().download_rate_limit_kib ?? 0), (v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 0) { toast("限速需为非负整数", "error"); return false; }
      return save({ download_rate_limit_kib: n });
    }, { number: true });
    g.appendChild(row("下载限速（KiB/s）", "", rate));
    const pendingTimeout = inputEl(String(s().download_pending_timeout_minutes ?? 120), (v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 5 || n > 1440) { toast("悬挂时限需为 5–1440 的整数分钟", "error"); return false; }
      return save({ download_pending_timeout_minutes: n });
    }, { number: true });
    pendingTimeout.min = "5"; pendingTimeout.max = "1440"; pendingTimeout.step = "1";
    g.appendChild(row("悬挂任务判失败（分钟）", "等待浏览器授权等悬挂状态超过此时限自动判失败并提示收藏→刷新→重新下载；默认 120 分钟，与视频页下载地址的有效期相当", pendingTimeout));
    const free = inputEl(String(s().download_min_free_gib ?? 5), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 0) { toast("数值无效", "error"); return false; }
      return save({ download_min_free_gib: n });
    }, { number: true });
    g.appendChild(row("最低空闲空间（GiB）", "", free));
    const proxyPolicy = selectEl(s().download_proxy_policy || "auto", [
      { value: "auto", label: "自动（国内直连，境外走系统代理）" },
      { value: "direct", label: "全部直连（忽略系统代理）" },
      { value: "system", label: "全部走系统代理" },
      { value: "custom", label: "自定义代理（全部走指定地址）" },
    ], (value) => save({ download_proxy_policy: value }).then((settings) => {
      if (settings && inSection("data")) render();
      return settings;
    }), "download_proxy_policy");
    g.appendChild(row("网络策略", "B 站/抖音直连最稳（VPN 用户常见 412/无法解析的根因）；YouTube 等境外内容需要代理时可切换", proxyPolicy));
    const proxyUrl = inputEl(String(s().download_proxy_url ?? ""), (v) => {
      const value = v.trim();
      if (!value) return save({ download_proxy_url: "" });
      if (value.length > 512 || !/^(?:http|https|socks4|socks5|socks5h):\/\/.+/i.test(value)) {
        toast("代理地址需形如 http://127.0.0.1:7890 或 socks5://host:1080", "error");
        return false;
      }
      return save({ download_proxy_url: value });
    });
    proxyUrl.placeholder = "http://127.0.0.1:7890（仅「自定义代理」模式使用）";
    g.appendChild(row("代理地址", "SOCKS 仅对 yt-dlp 通道生效；抖音 CDN 直提通道会自动直连", proxyUrl));
    pane.appendChild(g);

    g = group("队列节奏");
    const poll = inputEl(String(s().poll_seconds ?? 2), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 0.5 || n > 60) { toast("轮询间隔需在 0.5-60 秒", "error"); return false; }
      return save({ poll_seconds: n });
    }, { number: true });
    g.appendChild(row("轮询间隔（秒）", "", poll));
    const heartbeat = inputEl(String(s().heartbeat_seconds ?? 5), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 1) { toast("心跳间隔需至少 1 秒", "error"); return false; }
      return save({ heartbeat_seconds: n });
    }, { number: true });
    g.appendChild(row("心跳间隔（秒）", "", heartbeat));
    const lease = inputEl(String(s().lease_timeout_seconds ?? 300), (v) => {
      const n = parseFloat(v);
      if (isNaN(n) || n < 10) { toast("租约超时需至少 10 秒", "error"); return false; }
      return save({ lease_timeout_seconds: n });
    }, { number: true });
    g.appendChild(row("租约超时（秒）", "", lease));
    pane.appendChild(g);

      g = group("数据自检与备份");
      const statusEl = document.createElement("div");
      statusEl.className = "hint";
      statusEl.textContent = "自检会收敛超时悬空任务，并列出没有任务记录的孤儿产物。";
      g.appendChild(statusEl);
      const resultBox = document.createElement("div");
      resultBox.className = "reconcile-box";
      const checkBtn = document.createElement("button");
      checkBtn.className = "btn small";
      checkBtn.textContent = "运行数据自检";
      checkBtn.onclick = () => {
        checkBtn.disabled = true;
        checkBtn.textContent = "自检中…";
        AppApi.dataReconcile().then((res) => {
          if (!resultBox.isConnected || !inSection("data")) return;
          renderReconcile(resultBox, res || {});
          toast(res && res.ok ? "自检完成" : "自检失败", res && res.ok ? "info" : "error");
        }).catch((e) => {if (resultBox.isConnected && inSection("data")) {resultBox.textContent = "数据自检失败，请重试"; toast("自检失败：" + (e && e.message || e), "error");}})
          .finally(() => { checkBtn.disabled = false; checkBtn.textContent = "运行数据自检"; });
      };
      const btnWrap = document.createElement("div");
      btnWrap.style.display = "flex";
      btnWrap.style.gap = "8px";
      btnWrap.style.alignItems = "center";
      btnWrap.appendChild(checkBtn);
      btnWrap.appendChild(resultBox);
      g.appendChild(btnWrap);

      const backupBtn = document.createElement("button");
      backupBtn.className = "btn small";
      backupBtn.textContent = "导出备份";
      backupBtn.onclick = () => {
        const stamp = new Date().toISOString().slice(0, 10);
        AppApi.pickSaveFile("FoilPage-Backup-" + stamp + ".zip").then((r) => {
          if (!r || !r.ok || r.cancelled) return;
          return AppApi.exportBackup(r.path);
        }).then((r) => {
          if (!r) return;
          toast(r.ok ? "备份已导出：" + r.path : r.error || "导出失败", r.ok ? "success" : "error");
        }).catch((e) => toast("导出失败：" + (e && e.message || e), "error"));
      };

      const importBtn = document.createElement("button");
      importBtn.className = "btn small";
      importBtn.textContent = "导入备份";
      importBtn.onclick = () => {
        AppApi.pickOpenFile().then((r) => {
          if (!r || !r.ok || r.cancelled) return;
          ConfirmDelete.run({
            message: "导入备份会覆盖当前设置与任务库，导入前会自动备份现状。确定继续？",
            onConfirm: () => AppApi.importBackup(r.path).then((res) => {
              if (res && res.ok) toast(res.message || "导入完成，请重启 FoilPage", "success");
              else toast(res && res.error || "导入失败", "error");
            }),
          });
        }).catch((e) => toast("导入失败：" + (e && e.message || e), "error"));
      };

      const diagBtn = document.createElement("button");
      diagBtn.className = "btn small";
      diagBtn.textContent = "导出诊断包";
      diagBtn.type="button";
      diagBtn.onclick = async () => {
        if(diagBtn.disabled)return;
        diagBtn.disabled=true;diagBtn.textContent="正在导出…";
        const owner=section;
        try {
          const stamp=new Date().toISOString().slice(0,10);
          const picked=await AppApi.pickSaveFile("FoilPage-Diagnostics-"+stamp+".zip");
          if(!picked?.ok || picked.cancelled)return;
          const result=await AppApi.exportDiagnostics(picked.path);
          if(inSection(owner) && diagBtn.isConnected)toast(result?.ok?"诊断包已导出":result?.error||"导出失败",result?.ok?"success":"error");
        }catch(_){if(inSection(owner) && diagBtn.isConnected)toast("诊断包导出失败，请重试","error");}
        finally {diagBtn.disabled=false;diagBtn.textContent="导出诊断包";}
      };

      const actions = document.createElement("div");
      actions.className = "settings-data-actions";
      actions.appendChild(backupBtn);
      actions.appendChild(importBtn);
      actions.appendChild(diagBtn);
      g.appendChild(actions);
      pane.appendChild(g);

      g = group("缓存清理");
      const cacheHint = document.createElement("div");
      cacheHint.className = "set-hint";
      cacheHint.textContent = "清理转写/下载中断遗留的临时文件与轮转日志（正常使用可随时清理）；模型缓存为已下载的完整模型，删除后需重新下载。";
      g.appendChild(cacheHint);
      const cacheBox = document.createElement("div");
      cacheBox.className = "cache-box";
      g.appendChild(cacheBox);
      pane.appendChild(g);
      renderCacheBox(cacheBox);
      [...pane.querySelectorAll(':scope > .set-group')].forEach(section=>{
        const title=section.querySelector('h3');
        if(!['队列节奏','缓存清理'].includes(title?.textContent))return;
        const details=document.createElement('details');details.className='set-advanced settings-section-fold';
        const summary=document.createElement('summary');summary.textContent=title.textContent;title.remove();
        section.before(details);details.append(summary,section);
      });
      return pane;
    }

  /* ---------------- 启动 ---------------- */
  function buildStartup() {
    const pane = document.createElement("div");
    const g = group("启动与托盘");
    const launchToggle = toggleEl(!!s().launch_at_startup, () => {
      const next = !s().launch_at_startup;
      return AppApi.setLaunchAtStartup(next).then((settings) => {
        if (!settings || settings.ok === false || typeof settings.launch_at_startup !== "boolean") throw new Error(settings?.error || "未收到保存确认");
        AppState.settings = {...AppState.settings, launch_at_startup:settings.launch_at_startup};
        window.AccountAccess?.sync();
        if (inSection("startup") && pane.isConnected) toast(settings.launch_at_startup ? "已注册开机自启" : "已取消开机自启");
        return settings;
      }).catch((e) => {if (inSection("startup") && pane.isConnected) toast("操作失败：" + (e && e.message || e), "error");});
    }, "launch_at_startup");
    g.appendChild(row("开机静默启动", "", launchToggle));
    const ciWakeToggle = toggleEl(!!s().ci_runner_wakeup, () => save({ ci_runner_wakeup: !s().ci_runner_wakeup }), "ci_runner_wakeup");
    g.appendChild(row("启动时唤起工程 CI 服务", "本机装有 CI Runner 服务时自动拉起（开发机功能）", ciWakeToggle));
    const trayToggle = toggleEl(!!s().close_to_tray, () => save({ close_to_tray: !s().close_to_tray }), "close_to_tray");
    g.appendChild(row("关闭窗口后驻留托盘", "", trayToggle));
    const notifyToggle = toggleEl(s().task_notifications !== false, () => save({ task_notifications: s().task_notifications === false }), "task_notifications");
    g.appendChild(row("任务完成通知", "下载、转写完成或失败时提醒", notifyToggle));
    /* 提示音（1.3.12）：预设音频组 + 自定义 WAV */
    const SOUND_PACKS = [
      { value: "system", label: "系统提示音" },
      { value: "silent", label: "无声" },
      { value: "crisp", label: "清脆（短促双音）" },
      { value: "soft", label: "柔和（慢包络）" },
      { value: "deep", label: "低沉（厚重）" },
      { value: "custom", label: "自定义音频（WAV）" },
    ];
    const knownPack = (value) => SOUND_PACKS.some((option) => option.value === value);
    const currentPack = () => (knownPack(s().notification_sound_pack) ? s().notification_sound_pack : "system");
    const customSoundRows = [];
    const refreshCustomSoundRows = () => {
      const show = currentPack() === "custom";
      customSoundRows.forEach((r) => { r.style.display = show ? "" : "none"; });
    };
    const soundPathCtrl = (kind, hint) => {
      const key = "notification_sound_" + kind + "_path";
      const wrap = document.createElement("div");
      wrap.style.cssText = "display:flex;align-items:center;gap:8px;flex:1;min-width:0;justify-content:flex-end;";
      const box = document.createElement("div");
      box.className = "path-box";
      const txt = document.createElement("span");
      const showPath = () => {
        const value = String(s()[key] || "");
        txt.className = "path-text" + (value ? "" : " is-empty");
        txt.textContent = value || "未选择";
        txt.title = value;
      };
      showPath();
      box.appendChild(txt);
      wrap.appendChild(box);
      const pickBtn = document.createElement("button");
      pickBtn.className = "btn ghost small";
      pickBtn.textContent = "选择";
      pickBtn.onclick = () => runControl(pickBtn, async () => {
        const owner = section;
        try {
          const res = await AppApi.pickNotificationSound(kind);
          if (res?.cancelled) return;
          if (!res?.ok || typeof res.settings?.[key] !== "string" || !res.settings[key]) throw new Error(res?.error || "选择失败");
          AppState.settings = {...AppState.settings, [key]: res.settings[key]};
          showPath();
          if (inSection(owner) && wrap.isConnected) toast("提示音已更新");
          await AppApi.previewNotificationSound(kind, "custom", res.settings[key]);
          return res;
        } catch (error) { if (inSection(owner) && wrap.isConnected) toast(error.message || "选择失败", "error"); }
      }, true);
      wrap.appendChild(pickBtn);
      const playBtn = document.createElement("button");
      playBtn.className = "btn ghost small";
      playBtn.textContent = "试听";
      playBtn.onclick = () => runControl(playBtn, () => AppApi.previewNotificationSound(kind, "custom", String(s()[key] || "")));
      wrap.appendChild(playBtn);
      return row(kind === "success" ? "成功提示音" : "失败提示音", hint, wrap);
    };
    customSoundRows.push(soundPathCtrl("success", "任务完成时播放的 WAV 文件"));
    customSoundRows.push(soundPathCtrl("error", "任务失败时播放的 WAV 文件"));
    const packSelect = selectEl(currentPack(), SOUND_PACKS, async (v) => {
      await save({ notification_sound_pack: v }, false);
      refreshCustomSoundRows();
      if (v !== "custom") await AppApi.previewNotificationSound("success", v, "");
    }, "notification_sound_pack");
    g.appendChild(row("提示音", "任务终态时播放；预设组或自定义 WAV 文件", packSelect));
    customSoundRows.forEach((r) => g.appendChild(r));
    refreshCustomSoundRows();
    const notifyTest = document.createElement("button");
    notifyTest.className = "btn ghost small";
    notifyTest.textContent = "测试通知";
    notifyTest.onclick = () => runControl(notifyTest, async () => {
      try {const res = await AppApi.systemNotify("FoilPage（通知测试）", "完成下载或转写时，会在这里提醒你。");
        if (inSection("startup") && pane.isConnected) toast(res?.ok ? "已发送，若未显示请检查 Windows 通知设置" : "系统通知暂不可用", res?.ok ? "" : "error"); return res?.ok ? res : undefined;
      } catch (_) {if (inSection("startup") && pane.isConnected) toast("系统通知暂不可用", "error");}
    });
    g.appendChild(row("通知检查", "Windows 免打扰可能隐藏系统提醒", notifyTest));
    const autoStart = toggleEl(!!s().worker_auto_start, () => save({ worker_auto_start: !s().worker_auto_start }), "worker_auto_start");
    g.appendChild(row("启动应用时运行 Worker", "", autoStart));
    const autoRestart = toggleEl(!!s().worker_auto_restart, () => save({ worker_auto_restart: !s().worker_auto_restart }), "worker_auto_restart");
    g.appendChild(row("自动恢复转写服务", "", autoRestart));
    pane.appendChild(g);
    return pane;
  }

  /* ---------------- 关于 ---------------- */
  function buildNotificationInbox() {
    const details=document.createElement('details');details.className='set-advanced settings-section-fold product-inbox';details.id='productNotificationInbox';
    const summary=document.createElement('summary');summary.textContent='通知与更新';
    const count=document.createElement('span');count.className='product-inbox-count';summary.appendChild(count);
    const body=document.createElement('div');body.className='set-group product-inbox-body';
    const tools=document.createElement('div');tools.className='product-inbox-tools';
    const status=document.createElement('span');status.className='hint';status.setAttribute('role','status');
    const refresh=document.createElement('button');refresh.type='button';refresh.className='btn ghost small';refresh.textContent='刷新';refresh.setAttribute('aria-label','刷新通知');
    const list=document.createElement('ul');list.className='product-inbox-list';list.id='productNotificationsList';tools.append(status,refresh);body.append(tools,list);details.append(summary,body);
    const stamp=value=>typeof value==='number' ? value<1e12?value*1000:value : Date.parse(value);
    const context=notificationContext();let pending=false,renderedSignature=null;
    const visible=()=>details.isConnected && inSection('about') && notificationContext()===context;
    const safeUrl=value=>{try{const url=new URL(value);return typeof value==='string' && value.length<=2048 && url.protocol==='https:' && url.host==='github.com' && url.pathname.startsWith('/Ansel-Poiesis/FoilPage-source/releases/') && !url.search && !url.hash && !url.username && !url.password && !/[\x00-\x1f]/.test(value)?url.href:null;}catch(_){return null;}};
    const render=()=>{
      if(!visible())return;
      const snapshot=productNotificationSnapshot?.context===context?productNotificationSnapshot.value:null;
      if(snapshot?.ok!==true){renderedSignature=null;count.textContent='';status.textContent=snapshot?'暂时无法读取':'正在读取…';list.replaceChildren();return;}
      const seen=new Set(),now=Date.now(),items=[];
      for(const item of [...(Array.isArray(snapshot.updates)?snapshot.updates:[]),...(Array.isArray(snapshot.events)?snapshot.events:[])]){
        if(!item || typeof item.event_id!=='string' || !item.event_id || item.event_id.length>200 || seen.has(item.event_id) || typeof item.title!=='string' || !item.title.trim())continue;
        const expires=item.expires_at==null?null:stamp(item.expires_at);
        if(expires!==null && (!Number.isFinite(expires) || expires<=now))continue;
        if(item.kind==='software_update' && item.channel!==(AppState.releaseChannel==='test'?'test':'stable'))continue;
        seen.add(item.event_id);items.push(item);
      }
      items.sort((a,b)=>(stamp(b.created_at)||0)-(stamp(a.created_at)||0));
      const signature=JSON.stringify(items.slice(0,30).map(item=>[item.event_id,item.kind,item.title,item.message,item.created_at,item.url]));
      if(signature===renderedSignature){status.textContent='';return;}
      renderedSignature=signature;
      count.textContent=items.length?String(items.length):'';status.textContent='';list.replaceChildren();
      for(const item of items.slice(0,30)){
        const li=document.createElement('li'),head=document.createElement('div'),title=document.createElement('strong'),time=document.createElement('time');head.className='product-inbox-head';
        title.textContent=item.title.trim().slice(0,140);const created=stamp(item.created_at);time.textContent=Number.isFinite(created)?new Date(created).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'}):'';
        head.append(title,time);li.appendChild(head);
        if(typeof item.message==='string' && item.message.trim()){const message=document.createElement('p');message.textContent=item.message.slice(0,1600);li.appendChild(message);}
        const url=item.kind==='software_update'?safeUrl(item.url):null;
        if(url){const open=document.createElement('button');open.type='button';open.className='btn ghost small';open.textContent='查看更新';open.onclick=async()=>{
          if(open.disabled || !visible())return;open.disabled=true;
          try{const result=await AppApi.openUrl(url);if(result?.ok!==true && visible())toast('暂时无法打开更新页面','error');}
          catch(_){if(visible())toast('暂时无法打开更新页面','error');}finally{open.disabled=false;}
        };li.appendChild(open);}
        list.appendChild(li);
      }
      if(!list.childElementCount){const empty=document.createElement('li');empty.className='product-inbox-empty';empty.textContent='暂无新通知';list.appendChild(empty);}
    };
    const load=async()=>{
      if(pending)return;pending=true;refresh.disabled=true;const revision=productNotificationRevision;
      try{const value=await AppApi.productNotifications();if(!visible() || revision!==productNotificationRevision)return;
        productNotificationSnapshot={context,value};render();
      }catch(_){if(visible() && revision===productNotificationRevision)status.textContent='暂时无法读取';}
      finally{pending=false;refresh.disabled=false;}
    };
    refresh.onclick=load;refreshNotificationInbox=render;
    queueMicrotask(()=>{if(productNotificationSnapshot?.context===context)render();else load();});return details;
  }

  function buildAbout() {
    const pane = document.createElement("div");
      let g = group("FoilPage");
      const version = valueEl("v" + (window.APP_VERSION || "0.1.1"));
      g.appendChild(row("版本", "", version));
      g.appendChild(row("产品", "", valueEl("FoilPage")));
      const updateBtn = document.createElement("button");
      updateBtn.className = "btn small";
      updateBtn.textContent = "检查更新";
      const updateStatus = document.createElement("span");
      updateStatus.className = "hint";
      updateStatus.style.marginLeft = "10px";
      const updateWrap = document.createElement("div");
      updateWrap.className = "settings-update-actions";
      updateBtn.onclick = () => {
        updateBtn.disabled = true;
        updateBtn.textContent = "检查中…";
        AppApi.checkUpdate().then((r) => {
          if (!r || !r.ok) {
            updateStatus.textContent = (r && r.error) || "无法连接更新服务";
            return;
          }
          if (r.is_latest) {
            updateStatus.textContent = "已是最新版本（" + r.latest + "）";
          } else {
            updateStatus.textContent = "发现新版本 " + r.latest;
            const go = document.createElement("a");
            go.href = r.url || "#";
            go.target = "_blank";
            go.textContent = "前往下载";
            updateStatus.appendChild(document.createTextNode(" "));
            updateStatus.appendChild(go);
          }
        }).catch((e) => { updateStatus.textContent = "检查失败：" + (e && e.message || e); })
          .finally(() => { updateBtn.disabled = false; updateBtn.textContent = "检查更新"; });
      };
      updateWrap.appendChild(updateBtn);
      updateWrap.appendChild(updateStatus);
      g.appendChild(row("更新", "", updateWrap));
      pane.appendChild(g);
      pane.appendChild(buildNotificationInbox());

    g = group("帮助");
    const guideBtn = document.createElement("button");
    guideBtn.className = "btn small";
    guideBtn.textContent = "重新阅读新手引导";
    guideBtn.onclick = () => { if (window.Onboarding) window.Onboarding.show(); };
    g.appendChild(row("新手引导", "", guideBtn));
    pane.appendChild(g);

    g = group("反馈与诊断");
    const feedbackChannel = document.createElement("div");
    feedbackChannel.className = "set-ctrl";
    feedbackChannel.style.gap = "8px";
    const mailCopy = document.createElement("button");
    mailCopy.className = "btn ghost small";
    mailCopy.textContent = "复制邮箱";
    mailCopy.onclick = () => copyText("aachuan@foxmail.com", "已复制邮箱 aachuan@foxmail.com");
    const qqCopy = document.createElement("button");
    qqCopy.className = "btn ghost small";
    qqCopy.textContent = "复制 QQ";
    qqCopy.onclick = () => copyText("2029774968", "已复制 QQ 2029774968");
    feedbackChannel.append(mailCopy, qqCopy);
    g.appendChild(row("反馈渠道", "邮箱 aachuan@foxmail.com，QQ 2029774968；遇到问题或建议都欢迎", feedbackChannel));
    const mailBtn = document.createElement("a");
    mailBtn.className = "btn primary small";
    mailBtn.style.textDecoration = "none";
    mailBtn.textContent = "邮件反馈";
    mailBtn.href = "mailto:aachuan@foxmail.com?subject="
      + encodeURIComponent("FoilPage 反馈 v" + (window.APP_VERSION || ""))
      + "&body=" + encodeURIComponent("描述遇到的问题（做了什么、预期什么、实际发生什么）：\n\n（可选）在 设置 → 数据 导出诊断包，作为附件一起发送。");
    g.appendChild(row("邮件反馈", "", mailBtn));
    const diagnostics=document.createElement("button");diagnostics.type="button";diagnostics.className="btn ghost small";diagnostics.textContent="前往导出";diagnostics.onclick=()=>show("data");
    g.appendChild(row("诊断包", "", diagnostics));
    pane.appendChild(g);

    g = group("运行时摘要");
    const sum = AppState.summary || {};
    const rows = [
      ["任务总数", String(sum.total || 0)],
      ["进行中", String(sum.active || 0)],
      ["已完成", String(sum.completed ?? sum.terminal ?? 0)],
      ["完成度", Math.round((sum.progress || 0) * 100) + "%"],
    ];
    rows.forEach(([k, v]) => {
      g.appendChild(row(k, "", valueEl(v)));
    });
    const runtimeDetails=document.createElement("details");runtimeDetails.className="set-advanced settings-section-fold";
    const runtimeSummary=document.createElement("summary");runtimeSummary.textContent="运行时摘要";g.querySelector("h3").remove();runtimeDetails.append(runtimeSummary,g);pane.appendChild(runtimeDetails);

    g = group("应用");
    const quitBtn = document.createElement("button");
    quitBtn.className = "btn danger small";
    quitBtn.textContent = "退出 FoilPage";
    quitBtn.onclick = () => AppApi.quit();
    g.appendChild(row("退出", "", quitBtn));
    pane.appendChild(g);
    const credit=document.createElement("p");credit.className="about-credit";credit.textContent="@玩家川行出品";pane.appendChild(credit);
    return pane;
  }

  const BUILDERS = {
    ui: buildUi,
    recognition: buildRecognition,
    agent: buildAgent,
    control: buildAgentControl,
    model: buildModel,
    data: buildData,
    startup: buildStartup,
    about: buildAbout,
  };

  function buildLayout() {
    const host = document.getElementById("page-settings");
    host.innerHTML = `
      <div class="settings-layout">
        <div class="settings-nav" id="settingsNav"></div>
        <div class="settings-pane" id="settingsPane"></div>
      </div>`;
    const nav = document.getElementById("settingsNav");
    SECTIONS.forEach((sec) => {
      const item = document.createElement("button");
      item.type = "button";
      item.setAttribute("aria-pressed", sec.key === section ? "true" : "false");
      item.className = "sitem" + (sec.key === section ? " active" : "");
      item.dataset.section = sec.key;
      item.innerHTML = `<span class="setting-icon" aria-hidden="true">${SECTION_ICONS[sec.icon]}</span><span></span>`;
      item.querySelector("span:last-child").textContent = sec.label;
      item.onclick = () => { section = sec.key; render(); document.querySelector(`.sitem[data-section="${sec.key}"]`)?.focus({preventScroll:true}); };
      nav.appendChild(item);
    });
  }

  function render(returnFocus) {
    stopModelProgress();
    const previousPane = document.getElementById("settingsPane"), previousSection = previousPane?.dataset.section;
    const focused = document.activeElement;
    const focusKey = returnFocus?.section === section ? returnFocus.key : previousSection === section && focused?.closest?.("#settingsPane") ? controlKey(focused) : null;
    const selection = focused?.matches?.('input[type="text"],input[type="password"],textarea') ? [focused.selectionStart,focused.selectionEnd] : null;
    if (previousPane && previousSection) {
      const drafts = editingDrafts.get(previousSection) || new Map();
      previousPane.querySelectorAll('input,textarea,select').forEach(control => {
        if (control.dataset.initialValue !== undefined && control.value !== control.dataset.initialValue) drafts.set(controlKey(control), {value:control.value,invalid:control.getAttribute("aria-invalid")});
      });
      editingDrafts.set(previousSection, drafts);
      sectionLayout.set(previousSection, {scroll:previousPane.scrollTop, details:[...previousPane.querySelectorAll("details")].map(node => node.open)});
    }
    rebuilding = true;
    buildLayout();
    const pane = document.getElementById("settingsPane");
    pane.dataset.section = section;
    pane.innerHTML = "";
    pane.appendChild(BUILDERS[section]());
    pane.querySelectorAll('input,textarea,select,button').forEach(control => {
      const key = controlKey(control), draft = editingDrafts.get(section)?.get(key);
      if (draft && 'value' in control) {control.value = draft.value; if (draft.invalid) control.setAttribute("aria-invalid", draft.invalid);}
      if (pendingControls.has(section + ":" + key)) {
        control.setAttribute("aria-busy", "true");
        if (!control.matches('input:not([type="range"]),textarea')) {control.dataset.pendingControl = String(control.disabled); control.disabled = true;}
      }
    });
    const layout = sectionLayout.get(section);
    if (layout) {pane.querySelectorAll("details").forEach((node,index) => {node.open = layout.details[index] === true;}); pane.scrollTop = layout.scroll;}
    if (focusKey) {
      const control = [...pane.querySelectorAll("[aria-label],input,textarea,select,button")].find(node => controlKey(node) === focusKey);
      (control && !control.disabled ? control : document.querySelector(".sitem.active"))?.focus({preventScroll:true});
      if (control && selection && control.matches('input[type="text"],input[type="password"],textarea')) control.setSelectionRange(...selection);
    }
    rebuilding = false;
  }

  /* 供新手引导等外部入口直达指定分区（无效分区保持当前选择） */
  function show(sectionKey) {
    if (sectionKey === "subscription") { switchView("subscription"); return; }
    if (BUILDERS[sectionKey]) section = sectionKey;
    render();
  }

  return { render, show, save, observeRevision, controls: { inputEl, row, valueEl, group } };
})();
