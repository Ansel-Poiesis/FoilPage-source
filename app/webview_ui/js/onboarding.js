// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Independent reading chapters: real controls stay usable; reading never executes an action. */
'use strict';

window.Onboarding = (() => {
  const CHAPTERS = [
    { key: "environment", title: "环境配置", description: "保存目录与转写模型", icon: "prepare" },
    { key: "browser", title: "浏览器连接", description: "从视频页添加任务", icon: "browser" },
    { key: "application", title: "应用使用", description: "导入、处理与阅读", icon: "pages" },
  ];
  const STEPS = [
    { chapter: "environment", title: "先检查本机环境", lead: "GPU、显存、磁盘与模型状态都在准备页。先看自检结果，再按提示配置。", sub: "黄色提醒可查看「一键配置环境」；环境配置不会自动下载模型。只保存视频时可暂不配置转写。", view: "setup", setupStep: "environment", target: "#setupEnvCard h3", hint: "阅读不会自动修改本机配置" },
    { chapter: "environment", title: "让视频保存在合适的位置", lead: "选择下载文件的本地保存目录，给视频留出足够空间。", sub: "新提交的下载使用新目录，已有文件保留原位置。模型缓存也可在这一页更改，文稿目录可在设置中调整。", view: "setup", setupStep: "environment", target: "#setupDownloadDirBtn", hint: "可点击更改目录，也可先继续阅读" },
    { chapter: "environment", title: "下载一个转写模型", lead: "模型不随安装包分发。按本机推荐与可用空间选择档位；small 约 488 MB，适合日常转写。", sub: "大模型通常更慢、占用更多显存；下载中断后可重试。已有模型时直接核对默认模型。", view: "setup", setupStep: "models", target: "[data-tour='model-small'] button[data-state='idle']", fallback: "[data-tour='setup-default-model']", hint: "由你点击下载；阅读不会启动下载" },
    { chapter: "environment", title: "选定默认模型", lead: "选择已经下载的模型，后续转写任务会使用它。", sub: "没有可用 GPU 时会回退 CPU。更改默认模型后，新提交的任务使用新设置。", view: "setup", setupStep: "models", target: "[data-tour='setup-default-model']", hint: "可在这里选择默认模型" },
    { chapter: "environment", title: "开始留下声音", lead: "在这里下载并选定模型，准备完成后进入任务。", view: "setup", setupStep: "models", kind: "finish" },
    { chapter: "browser", title: "找到随应用安装的扩展", lead: "先打开扩展目录。Chrome、Edge 与其他 Chromium 浏览器可以加载同一目录。", sub: "本地媒体导入不需要扩展；浏览器连接是可选的。", view: "browser", target: "#extensionDir", hint: "点击打开扩展目录" },
    { chapter: "browser", title: "在浏览器中加载扩展", lead: "打开扩展管理页，启用开发者模式，再选择「加载已解压的扩展」，加载刚才的目录。", sub: "首次加载固定目录后，更新 FoilPage 时扩展会在空闲时重载；已打开的视频页请刷新。", view: "browser", target: "#openExtPage", fallback: "#extensionDir", hint: "由你打开浏览器扩展管理页" },
    { chapter: "browser", title: "连接桌面与浏览器", lead: "点击「允许连接」，两分钟内打开浏览器工具栏的 FoilPage 扩展，点击「一键配置 FoilPage」。", sub: "一键配置会开启开机自启与后台服务，可在 设置 → 启动 调整。连接结果显示在此页。", view: "browser", target: "#pairBrowser", hint: "配对完成后，可点击检查连接" },
    { chapter: "browser", title: "在视频旁提交任务", lead: "在抖音、B 站或 YouTube 视频页点「下载」保存媒体，点「转写」继续生成文字。", sub: "切换视频后，已提交任务仍由队列处理；中间尝试其他下载通道不会反复通知。进度与重试在任务页查看。长时间未刷新的抖音页面，早前视频的地址可能过期；可按 F5 刷新视频页，等待画面加载后重试。仍失败时，到任务页查看具体原因。", view: "browser", kind: "flow" },
    { chapter: "application", title: "拖入一段视频，或选择文件", lead: "把本地视频或录音拖到导入区域，也可以选择文件、文件夹，一次添加多段媒体。", sub: "导入后进入转写队列；原媒体保留在原位置。请先在准备页下载并选定转写模型。", view: "import", target: "#mediaDropZone", fallback: ".import-card", compactTarget: "#pickFiles", hint: "可拖入媒体；阅读不会自动创建任务" },
    { chapter: "application", title: "让队列接着处理", lead: "下载与转写进度都在任务页。切换页面、切走浏览器视频后，已提交任务继续处理。", sub: "点击任务查看文件与进度；勾选多项可批量处理，按住 Shift 可连续选择。失败任务可在这里重试。", view: "workbench", target: ".job-open", fallback: "#jobFilters", hint: "选择任务查看详情；空列表时可先继续" },
    { chapter: "application", title: "看看已经保存的视频", lead: "视频页显示封面，点击即可播放。右键打开视频操作，可删除视频或打开所在位置。", sub: "删除下载的视频会保留转写文本；导入的原媒体只移除记录，原文件保留。", view: "videos", target: ".video-menu-trigger", fallback: "#refreshVideos", hint: "右键视频卡片，或使用视频操作按钮" },
    { chapter: "application", title: "在文本库读、改、导出", lead: "搜索文稿，查看原稿与整理后的文本。阅读工具栏可以复制、编辑、导出字幕或打开所在位置。", sub: "原始转写独立保留；整理后仍可随时回看。", view: "library", target: "#libSearch", fallback: "#docList", hint: "可搜索已有文稿；没有文稿时先导入媒体" },
    { chapter: "application", title: "需要整理时，再连接 Agent", lead: "选择自带密钥或 FoilPage 托管。自带密钥需填写服务商 API Key、接口地址与模型。", sub: "本地语音识别无需 API Key。云端整理会发送正文；启用视觉校对还会发送所选字幕帧，费用按所选服务计收。", view: "settings", section: "agent", target: "[data-tour='agent-key']", openDetails: true, hint: "按需配置；阅读不会调用模型" },
    { chapter: "application", title: "正文与标题，分别选择", lead: "正文整理与标题优化分别开启。正文可选择轻度、均衡或深入；标题单独生成简洁准确的表达。", sub: "FoilPage 托管分别消耗额度；自带密钥由服务商计费。标题优化失败会保留原标题，原始转写始终独立保存。", view: "settings", section: "agent", target: "[data-tour='editing-level']", hint: "开启前，请核对服务与费用" },
    { chapter: "application", title: "让任务继续", lead: "任务会在后台处理。文字完成后，到文本库阅读。", view: "workbench", kind: "finish" },
  ];
  const PROGRESS_SCHEMA = 1;
  let active = false, mode = "hub", chapterKey = null, index = 0, overlay, card, ring, arrow, shades = [], observer, frame = 0;
  let scrollTarget = null, returnFocus = null;
  let saveQueue = Promise.resolve(), progressSaveFailed = false;
  let progressCache = null;
  const clamp = (n, min, max) => Math.max(min, Math.min(n, max));

  function el(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(className, text, action) {
    const node = el("button", className, text);
    node.type = "button";
    node.onclick = action;
    return node;
  }
  const chapterIndices = key => STEPS.map((step, i) => step.chapter === key ? i : -1).filter(i => i >= 0);
  function progress() {
    const raw = progressCache || window.AppState?.settings?.onboarding_chapter_progress;
    const chapters = {};
    if (raw?.schema_version === PROGRESS_SCHEMA && raw.chapters && typeof raw.chapters === "object") {
      CHAPTERS.forEach(chapter => {
        const saved = raw.chapters[chapter.key];
        if (saved && Number.isInteger(saved.step)) chapters[chapter.key] = {
          step: clamp(saved.step, 0, chapterIndices(chapter.key).length - 1), completed: saved.completed === true,
        };
      });
    }
    return { schema_version: PROGRESS_SCHEMA, chapters };
  }
  function persist(changes) {
    if (changes.onboarding_chapter_progress) progressCache = changes.onboarding_chapter_progress;
    if (window.AppState) window.AppState.settings = {...window.AppState.settings, ...changes};
    // Preserve ordering when a reader advances faster than the native bridge replies.
    saveQueue = saveQueue.then(async () => {
      try { const saved = await AppApi.saveSettings(changes); if (!saved || saved.ok === false) throw new Error('progress not saved'); progressSaveFailed = false; }
      catch (_) { progressSaveFailed = true; }
      // Other setting controls can return an older full snapshot while these
      // writes are in flight; restore only our own field, never their settings.
      if (progressCache && window.AppState?.settings) window.AppState.settings.onboarding_chapter_progress = progressCache;
      const notice = card?.querySelector(".ob-save-notice");
      if (notice) notice.textContent = progressSaveFailed ? "进度暂存于本次阅读，保存失败时可稍后重试。" : "";
      schedule();
    });
  }
  function remember(completed = false) {
    if (!chapterKey || mode !== "tour") return;
    const next = progress(), old = next.chapters[chapterKey];
    next.chapters[chapterKey] = {step: chapterIndices(chapterKey).indexOf(index), completed: completed || old?.completed === true};
    persist({onboarding_chapter_progress: next});
  }
  function markSeen() {
    persist({onboarding_completed_version: String(window.APP_VERSION || "read")});
  }
  function currentTarget() {
    if (mode !== "tour") return null;
    const step = STEPS[index];
    for (const selector of [step.target, step.fallback]) {
      if (!selector) continue;
      const node = document.querySelector(selector);
      if (!node || node.getClientRects().length === 0) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width && rect.height) {
        // A full drop area cannot share a narrow window with its reading callout.
        // Keep its file button usable instead of covering the whole import surface.
        if (step.compactTarget && (rect.width > innerWidth * .5 || rect.height > innerHeight * .35)) {
          const compact = document.querySelector(step.compactTarget);
          if (compact?.getClientRects().length) return compact;
        }
        return node;
      }
    }
    return null;
  }
  function enterStep(nextIndex) {
    if (!active) return;
    const indices = chapterIndices(chapterKey);
    index = clamp(nextIndex, indices[0], indices[indices.length - 1]);
    const step = STEPS[index];
    scrollTarget = null;
    if (typeof switchView === "function") switchView(step.view);
    if (step.setupStep && window.Setup) window.Setup.showStep(step.setupStep, {focus:false});
    if(step.view==='browser')window.Tools?.showBrowserStep?.(['#extensionDir','#openExtPage'].includes(step.target)?0:2);
    if (step.section && window.Settings) window.Settings.show(step.section);
    if (step.openDetails) {
      const details = document.querySelector("[data-tour='agent-key']")?.closest("details");
      if (details) details.open = true;
    }
    renderCard();
    remember();
    layout();
    schedule();
  }
  function startChapter(key, resume = true) {
    if (!CHAPTERS.some(chapter => chapter.key === key)) return;
    mode = "tour"; chapterKey = key;
    card.removeAttribute("aria-modal");
    const saved = progress().chapters[key];
    const local = resume && saved && !saved.completed ? saved.step : 0;
    enterStep(chapterIndices(key)[local]);
  }
  function showChapters() {
    if (mode === "tour") remember();
    mode = "hub"; chapterKey = null; scrollTarget = null;
    card.setAttribute("aria-modal", "true");
    renderHub(); layout(); schedule();
  }
  function schedule() {
    if (!active || frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (active) layout(); });
  }
  function bounds(node) {
    const rect = node.getBoundingClientRect();
    let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
    let right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
    for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (/(auto|scroll|hidden|clip)/.test(style.overflow + style.overflowY + style.overflowX)) {
        const box = parent.getBoundingClientRect();
        left = Math.max(left, box.left); right = Math.min(right, box.right);
        top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
      }
    }
    return right > left && bottom > top ? { left, top, right, bottom, width: right - left, height: bottom - top } : null;
  }
  function boxStyle(node, left, top, width, height) {
    Object.assign(node.style, { left: left + "px", top: top + "px", width: Math.max(0, width) + "px", height: Math.max(0, height) + "px" });
  }
  function layout() {
    const vw = innerWidth, vh = innerHeight, margin = 12, gap = 16;
    const chrome = document.getElementById("windowChrome");
    const header = chrome && !chrome.hidden ? chrome.closest(".topbar") : null;
    const topMargin = margin + (header ? header.getBoundingClientRect().bottom : 0);
    if (mode === "hub") {
      card.style.width = Math.min(600, vw - margin * 2) + "px";
      card.style.maxHeight = Math.max(180, vh - topMargin - margin) + "px";
      Object.assign(card.style, {left: (vw - card.offsetWidth) / 2 + "px", top: Math.max(topMargin, (vh - card.offsetHeight) / 2) + "px"});
      ring.hidden = arrow.hidden = true;
      boxStyle(shades[0], 0, 0, vw, vh);
      shades.slice(1).forEach(node => boxStyle(node, 0, 0, 0, 0));
      return;
    }
    const target = currentTarget();
    if (target && target !== scrollTarget) {
      scrollTarget = target;
      target.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    }
    const r = target && bounds(target);
    const hint = card.querySelector(".ob-target-hint");
    const fallbackHint = STEPS[index].target ? "暂未找到控件，可继续阅读或稍后重试。" : "点击下一步，或卡片外的空白处继续";
    hint.textContent = r ? (STEPS[index].compactTarget && target?.matches(STEPS[index].compactTarget) ? "可点击选择文件；关闭引导后也可直接拖入媒体。" : STEPS[index].hint) : fallbackHint;
    card.style.width = Math.min(STEPS[index].kind ? 420 : 360, vw - margin * 2) + "px";
    card.style.maxHeight = (vh - topMargin - margin) + "px";
    const cw = card.offsetWidth, ch = card.offsetHeight;
    let position;
    if (r) {
      // Try full-height placement first, then a scrollable callout in the largest free slot.
      const options = [
        { side: "left", x: r.right + gap, y: clamp(r.top, topMargin, vh - ch - margin), width: vw - r.right - gap - margin, height: vh - topMargin - margin },
        { side: "right", x: r.left - gap - cw, y: clamp(r.top, topMargin, vh - ch - margin), width: r.left - gap - margin, height: vh - topMargin - margin },
        { side: "top", x: clamp(r.left + r.width / 2 - cw / 2, margin, vw - cw - margin), y: r.bottom + gap, width: vw - margin * 2, height: vh - r.bottom - gap - margin },
        { side: "bottom", x: clamp(r.left + r.width / 2 - cw / 2, margin, vw - cw - margin), y: topMargin, width: vw - margin * 2, height: r.top - gap - topMargin },
      ].filter(o => o.width >= cw && o.height >= 170);
      position = options.find(o => o.height >= ch) || options.sort((a, b) => b.height - a.height)[0];
      if (position) {
        card.style.maxHeight = Math.min(vh - topMargin - margin, position.height) + "px";
        if (position.side === "bottom") position.y = r.top - gap - card.offsetHeight;
      }
    }
    // Welcome/recap and missing controls use a composed sheet beside the application content.
    if (!position) {
      const main = document.querySelector(".main")?.getBoundingClientRect();
      position = { x: clamp((main?.left || margin) + 24, margin, vw - cw - margin), y: clamp(Math.max(72, topMargin), topMargin, vh - card.offsetHeight - margin) };
    }
    Object.assign(card.style, {
      left: clamp(position.x, margin, vw - card.offsetWidth - margin) + 'px',
      top: clamp(position.y, topMargin, vh - card.offsetHeight - margin) + 'px',
    });
    const spotlight = r && position.side ? r : null;
    ring.hidden = !spotlight;
    arrow.hidden = !spotlight;
    if (spotlight) {
      const left = Math.max(0, r.left - 5), top = Math.max(0, r.top - 5);
      const right = Math.min(vw, r.right + 5), bottom = Math.min(vh, r.bottom + 5);
      boxStyle(ring, left, top, right - left, bottom - top);
      ring.style.pointerEvents = STEPS[index].readOnly ? "auto" : "none";
      boxStyle(shades[0], 0, 0, vw, top);
      boxStyle(shades[1], 0, bottom, vw, vh - bottom);
      boxStyle(shades[2], 0, top, left, bottom - top);
      boxStyle(shades[3], right, top, vw - right, bottom - top);
      const b = card.getBoundingClientRect();
      const vertical = position.side === "left" || position.side === "right";
      const ax = vertical ? (position.side === "left" ? b.left : b.right) : clamp(r.left + r.width / 2, b.left + 24, b.right - 24);
      const ay = vertical ? clamp(r.top + r.height / 2, b.top + 24, b.bottom - 24) : (position.side === "top" ? b.top : b.bottom);
      boxStyle(arrow, ax - 5, ay - 5, 10, 10);
    } else {
      boxStyle(shades[0], 0, 0, vw, vh);
      shades.slice(1).forEach(node => boxStyle(node, 0, 0, 0, 0));
    }
    if (r && !spotlight) hint.textContent = "窗口空间有限，可先继续阅读或放大窗口。";
  }
  function renderHead() {
    const head = el("header", "ob-head");
    if (mode === "tour") head.append(button("ob-chapters", "‹  章节", showChapters));
    else head.append(el("span", "ob-head-spacer"));
    const skip = button("ob-skip", "×", close);
    skip.setAttribute("aria-label", mode === "tour" ? "跳过本轮全部引导" : "关闭新手引导");
    skip.title = mode === "tour" ? "跳过本轮全部引导（Esc）" : "关闭新手引导（Esc）";
    head.append(skip);
    return head;
  }
  function chapterIcon(name) {
    const paths = {
      prepare: '<circle cx="12" cy="12" r="8"/><path d="m8 12 2.5 2.5L16 9"/>',
      browser: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01m0 6-3 2.5 3 2.5m4-5 3 2.5-3 2.5"/>',
      pages: '<path d="M6 3h10l3 3v15H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm10 0v4h3M8 11h7M8 15h7"/>',
    };
    const icon = el("span", "ob-chapter-icon");
    icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + paths[name] + '</svg>';
    return icon;
  }
  function renderHub(completedTitle = "") {
    card.textContent = "";
    card.dataset.kind = "hub";
    card.append(renderHead());
    const body = el("div", "ob-body ob-hub-body");
    const heading = el("div", "ob-hub-heading");
    const title = el("h2", "ob-title", "从这里开始");
    title.id = "onboardingTitle"; title.tabIndex = -1;
    const saved = progress().chapters;
    heading.append(title);
    body.append(heading);
    const list = el("div", "ob-chapter-list");
    CHAPTERS.forEach((chapter, i) => {
      const state = saved[chapter.key];
      const row = el("article", "ob-chapter-choice"); row.dataset.chapter = chapter.key;
      row.dataset.completed = String(state?.completed === true);
      row.append(chapterIcon(chapter.icon));
      const copy = el("div", "ob-chapter-copy");
      const head = el("div", "ob-choice-head");
      head.append(el("span", "ob-choice-number", String(i + 1).padStart(2, "0")), el("h3", "", chapter.title));
      copy.append(head, el("p", "ob-choice-description", chapter.description));
      const tail = el("div", "ob-choice-tail");
      const action = button("btn ob-start" + (!state?.completed ? " primary" : ""), state?.completed ? "重新阅读" : state ? "继续阅读" : "开始阅读", () => startChapter(chapter.key));
      action.setAttribute("aria-label", chapter.title + "：" + action.textContent);
      tail.append(action);
      copy.append(tail); row.append(copy); list.append(row);
    });
    body.append(list);
    const notice = el("p", "ob-save-notice", progressSaveFailed ? "进度暂存于本次阅读，保存失败时可稍后重试。" : "");
    notice.setAttribute("role", "status"); body.append(notice);
    card.append(body);
    title.focus({preventScroll: true});
  }
  function renderCard() {
    card.textContent = "";
    card.dataset.kind = STEPS[index].kind || "point";
    const step = STEPS[index], chapter = CHAPTERS.find(c => c.key === step.chapter);
    card.append(renderHead());
    const body = el("div", "ob-body");
    const indexMenu = el("details", "ob-index");
    const chapterSteps = STEPS.filter(s => s.chapter === step.chapter);
    const local = chapterSteps.indexOf(step) + 1;
    indexMenu.append(el("summary", "ob-index-summary", chapter.title + "：" + local + " / " + chapterSteps.length + "　⌄"));
    const map = el("nav", "ob-map");
    map.setAttribute("aria-label", "引导章节");
    CHAPTERS.forEach((c, i) => {
      const tab = button("ob-chapter-tab" + (c === chapter ? " active" : ""), "", () => startChapter(c.key));
      tab.append(el("span", "ob-chapter-number", String(i + 1).padStart(2, "0")), el("span", "", c.title));
      if (c === chapter) tab.setAttribute("aria-current", "step");
      map.append(tab);
    });
    indexMenu.append(map);
    indexMenu.ontoggle = schedule;
    body.append(indexMenu);
    const title = el("h2", "ob-title", step.title);
    title.id = "onboardingTitle"; title.tabIndex = -1;
    body.append(title, el("p", "ob-lead", step.lead));
    if (step.kind === "welcome") {
      const choices = el("div", "ob-setup-list");
      [["01", "下载模型", "本地转写必需"], ["02", "连接插件", "在视频页使用（选装）"], ["03", "配置 Agent", "进一步整理（选配）"]].forEach(([n, label, note]) => {
        const row = el("div", "ob-setup-row");
        row.append(el("span", "ob-setup-number", n), el("strong", "", label), el("span", "", note));
        choices.append(row);
      });
      body.append(choices);
    } else if (step.kind === "flow") {
      const flow = el("div", "ob-flow");
      ["视频页", "下载 / 转写", "FoilPage 任务"].forEach((text, i) => {
        if (i) flow.append(el("span", "ob-flow-arrow", "→"));
        flow.append(el("span", "ob-flow-item", text));
      });
      body.append(flow);
    }
    if (step.sub) body.append(el("p", "ob-sub", step.sub));
    if (step.chapter === 'browser' && local <= 3) {
      const figure = el('img', 'ob-pairing-figure');
      figure.src = 'assets/browser-pairing-' + local + '.svg';
      figure.style.width = '100%'; figure.style.height = 'auto'; figure.onload = schedule;
      figure.alt = ['浏览器扩展管理页：启用开发者模式，加载固定目录', '浏览器工具栏：打开 FoilPage 扩展', 'FoilPage 扩展：完成连接并提交视频'][local - 1];
      body.append(figure);
    }
    body.append(el("p", "ob-target-hint", ""));
    if (step.action) body.append(button("ob-action", step.action.label + " ↗", () => {
      close();
      switchView(step.action.view);
      if (step.action.section) window.Settings.show(step.action.section);
    }));
    const notice = el("p", "ob-save-notice", progressSaveFailed ? "进度暂存于本次阅读，保存失败时可稍后重试。" : "");
    notice.setAttribute("role", "status"); body.append(notice);
    card.append(body);
    const foot = el("footer", "ob-foot");
    const previous = button("ob-back", "上一步", () => enterStep(index - 1));
    previous.disabled = local === 1;
    foot.append(previous, el("span", "ob-step", String(local).padStart(2, "0") + " / " + chapterSteps.length));
    foot.append(button("btn primary ob-next", local === chapterSteps.length ? "完成本章" : "下一步", advance));
    card.append(foot);
    title.focus({ preventScroll: true });
  }
  function advance() {
    if (mode !== "tour") return;
    const indices = chapterIndices(chapterKey);
    if (index === indices[indices.length - 1]) {
      remember(true);
      close();
      document.getElementById('pageTitle')?.focus({preventScroll:true});
    } else enterStep(index + 1);
  }
  function onTargetClick(event) {
    if (!active || mode !== "tour") return;
    const node = event.target;
    const interactive = node?.closest?.("button, input, select, textarea, a[href], summary, [role='button'], [contenteditable='true']");
    if (card.contains(node)) {
      // Empty sheet space advances; text selection and native controls remain untouched.
      if (!interactive && !node.closest("p, h2, strong, span, svg") && !String(window.getSelection())) advance();
      return;
    }
    if (node === ring && STEPS[index].readOnly) {
      event.preventDefault(); event.stopPropagation(); advance(); return;
    }
    const target = currentTarget();
    if (!target?.contains(node) || interactive) return;
    if (!String(window.getSelection())) {
      event.preventDefault(); event.stopPropagation(); advance();
    }
  }
  function focusable() {
    const target = currentTarget();
    const selectors = "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex='0']";
    const nodes = [...card.querySelectorAll(selectors)];
    if (target && !STEPS[index].readOnly) {
      if (target.matches(selectors)) nodes.push(target);
      nodes.push(...target.querySelectorAll(selectors));
    }
    return nodes.filter(n => n.getClientRects().length && !n.closest("[hidden]"));
  }
  function onKey(event) {
    if (!active) return;
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key === "Tab") {
      const nodes = focusable(), at = nodes.indexOf(document.activeElement);
      if (nodes.length) { event.preventDefault(); nodes[(at + (event.shiftKey ? -1 : 1) + nodes.length) % nodes.length].focus(); }
      return;
    }
    if (event.target?.closest("input, select, textarea, button, summary, [role='button'], [contenteditable='true']")) return;
    if (mode === "tour" && event.key === "ArrowLeft") { event.preventDefault(); enterStep(index - 1); }
    if (mode === "tour" && (event.key === "Enter" || event.key === "ArrowRight")) { event.preventDefault(); advance(); }
  }
  function show(options = {}) {
    if (active) return;
    active = true; mode = "hub"; chapterKey = null; returnFocus = document.activeElement;
    overlay = el("div", "ob-overlay");
    shades = Array.from({ length: 4 }, () => {
      const shade = el("div", "ob-shade");
      shade.onclick = () => mode === 'tour' ? advance() : close(); overlay.append(shade); return shade;
    });
    ring = el("div", "ob-ring"); ring.hidden = true;
    arrow = el("div", "ob-arrow"); arrow.hidden = true;
    card = el("section", "ob-card"); card.setAttribute("role", "dialog"); card.setAttribute("aria-labelledby", "onboardingTitle");
    overlay.append(ring, arrow, card);
    document.getElementById("onboardingRoot").replaceChildren(overlay);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("click", onTargetClick, true);
    document.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    observer = new MutationObserver(schedule);
    const app = document.querySelector(".app");
    if (app) observer.observe(app, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "open", "data-state", "aria-hidden"] });
    if (options.chapter && CHAPTERS.some(chapter => chapter.key === options.chapter)) startChapter(options.chapter, options.resume !== false);
    else showChapters();
  }
  function close() {
    if (!active) return;
    if (mode === "tour") remember();
    active = false;
    cancelAnimationFrame(frame); frame = 0;
    observer?.disconnect();
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("click", onTargetClick, true);
    document.removeEventListener("scroll", schedule, true);
    window.removeEventListener("resize", schedule);
    document.getElementById("onboardingRoot").replaceChildren();
    overlay = card = ring = arrow = null; shades = []; scrollTarget = null;
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
    markSeen();
  }
  function maybeShow() { if (!String(window.AppState?.settings?.onboarding_completed_version || "").trim()) show(); }
  return { show, maybeShow, __shengjianOnboardingInternals: { CHAPTERS, STEPS, state: () => ({active, mode, index, chapter:chapterKey, progress:progress()}), saved: () => saveQueue } };
})();
