// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';
window.Tools = (() => {
  const drafts = { url: '', title: '', transcribe: true };
  let linkBusy = false, linkMessage = '', linkError = false, pairingBusy = false;
  let browserStepControl = () => {};
  function paintLinks() {
    const form = document.getElementById('linkForm'); if (!form) return;
    form.setAttribute('aria-busy', String(linkBusy));
    form.querySelectorAll('button,input,textarea').forEach(control => control.disabled = linkBusy);
    const state = form.querySelector('#linkState'); state.textContent = linkMessage;
    state.setAttribute('role', linkError ? 'alert' : 'status');
  }
  let importBusy = false, importMessage = '', importError = false, dropEvents = null;
  function paintImport() {
    const zone = document.getElementById('mediaDropZone');
    if (!zone) return;
    zone.setAttribute('aria-busy', String(importBusy));
    zone.classList.toggle('is-reading', importBusy);
    zone.querySelectorAll('button').forEach(button => button.disabled = importBusy);
    const state = zone.querySelector('#importState');
    state.textContent = importBusy ? '正在读取媒体，加入转写队列…' : importMessage;
    state.setAttribute('role', importError ? 'alert' : 'status');
    zone.querySelector('.import-drop-title').textContent = importBusy ? '正在读取媒体' : '把视频或录音拖到这里';
  }
  function beginImport() {
    if (importBusy) return false;
    importBusy = true; importError = false; paintImport(); return true;
  }
  function finishImport(result) {
    if (!importBusy) return;
    importBusy = false;
    if (result?.cancelled) { paintImport(); return; }
    importError = !result?.ok;
    if (importError) importMessage = result?.error || '导入未完成，请重试';
    else {
      const queued = Array.isArray(result.enqueued) ? result.enqueued.length : 0;
      const count = Number(result.imported) || 0;
      importMessage = queued ? `已加入 ${queued} 项转写任务` : count ? `已读取 ${count} 项媒体，已有转写任务` : '已加入转写队列';
      if (result.skipped) importMessage += `，${result.skipped} 项已存在`;
      if (result.failed) importMessage += `，${result.failed} 项未导入`;
    }
    if (AppState.currentView === 'import') {
      paintImport();
      toast(importMessage, importError ? 'error' : undefined);
    }
  }
  function beginNativeDrop() {
    return AppState.currentView === 'import' && beginImport();
  }
  function finishNativeDrop(result) { finishImport(result); }
  function esc(value) { return String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function needsDouyinBrowserAuth(value) {
    try {
      const text = String(value || '').trim();
      const pastedUrl = text.match(/https?:\/\/[^\s<>"'\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+/i)?.[0] || text;
      const host = new URL(pastedUrl).hostname.toLowerCase();
      return host === 'douyin.com' || host.endsWith('.douyin.com');
    } catch { return false; }
  }
  async function render(key) {
    dropEvents?.abort(); dropEvents = null;
    const host = document.getElementById('page-' + key);
    if (key === 'links') {
      host.innerHTML = `<div class="tool-page"><div class="tool-intro"><span class="tool-symbol">${AppIcons('link')}</span><h2>把链接变成收藏</h2></div>
        <form id="linkForm" class="tool-card"><label for="linkUrl">视频链接</label><textarea id="linkUrl" rows="3" placeholder="粘贴 B 站、YouTube 或抖音链接" required>${esc(drafts.url)}</textarea>
        <details class="tool-details"><summary>自定义标题</summary><input id="linkTitle" aria-label="自定义标题" value="${esc(drafts.title)}" placeholder="留空自动获取标题" maxlength="240"></details>
        <label class="check-row"><input id="linkTranscribe" type="checkbox" ${drafts.transcribe ? 'checked' : ''}>下载后转写</label>
        <div class="tool-footer"><p id="linkState" role="status" aria-live="polite"></p><button class="btn primary" type="submit">开始下载</button></div></form>
        <p class="tool-note">抖音建议从浏览器视频页添加，识别更稳定。</p></div>`;
      const form = host.querySelector('form');
      form.oninput = () => { drafts.url = host.querySelector('#linkUrl').value; drafts.title = host.querySelector('#linkTitle').value; drafts.transcribe = host.querySelector('#linkTranscribe').checked; linkMessage = ''; linkError = false; paintLinks(); };
      paintLinks();
      form.onsubmit = async e => {
        e.preventDefault(); if (linkBusy) return;
        linkBusy = true; linkError = false;
        const request = {...drafts};
        linkMessage = needsDouyinBrowserAuth(request.url)
          ? '正在请求已配对浏览器提供一次性抖音授权…'
          : '正在提交…';
        paintLinks();
        try { const res = await AppApi.downloadFromUrl(request.url.trim(), request.title.trim(), request.transcribe);
          if (!res?.ok) {
            linkError = true; linkMessage = `${res?.error || '提交失败'}${res?.task_id ? `；失败诊断已写入任务列表（${res.task_id}）` : ''}`;
            return;
          }
          linkMessage = '已加入任务';
        } catch (error) { linkError = true; linkMessage = error?.message || '提交失败，请重试'; }
        finally { linkBusy = false; if (AppState.currentView === 'links') paintLinks(); }
      };
    } else if (key === 'import') {
      host.innerHTML = `<div class="tool-page"><div class="tool-intro"><span class="tool-symbol">${AppIcons('import')}</span><h2>听过的，留下文字</h2></div><div id="mediaDropZone" class="tool-card import-card media-drop-zone" aria-label="媒体导入区域" aria-busy="false"><span class="import-art" aria-hidden="true">${AppIcons('import')}</span><h3 class="import-drop-title">把视频或录音拖到这里</h3><p>支持多文件，已导入的内容会自动去重。</p><div class="tool-buttons"><button id="pickFiles" class="btn primary" type="button">选择文件</button><button id="pickFolder" class="btn ghost" type="button">选择文件夹</button></div><p class="import-state" id="importState" role="status" aria-live="polite"></p><span class="import-queue-note">加入队列后，切换页面也会继续转写</span></div></div>`;
      paintImport();
      for (const [id, fn] of [['pickFiles', AppApi.pickImport], ['pickFolder', AppApi.pickFolderImport]]) {
        host.querySelector('#' + id).onclick = async () => {
          if (!beginImport()) return;
          try { finishImport(await fn()); }
          catch (_) { finishImport({ok:false,error:'导入未完成，请检查文件后重试'}); }
        };
      }
      const zone = host.querySelector('#mediaDropZone');
      dropEvents = new AbortController(); const signal = dropEvents.signal;
      let depth = 0;
      zone.addEventListener('dragenter', event => {
        if (!event.dataTransfer?.types.includes('Files')) return;
        event.preventDefault(); ++depth; if (!importBusy) zone.classList.add('is-dragging');
      }, {signal});
      zone.addEventListener('dragleave', event => {
        event.preventDefault(); if (--depth <= 0) { depth = 0; zone.classList.remove('is-dragging'); }
      }, {signal});
      // Prevent navigation anywhere on this page; the persistent native DOM handler imports paths.
      document.addEventListener('dragover', event => {
        if (AppState.currentView !== 'import' || !event.dataTransfer?.types.includes('Files')) return;
        event.preventDefault(); event.dataTransfer.dropEffect = importBusy ? 'none' : 'copy';
      }, {signal});
      document.addEventListener('drop', event => {
        if (AppState.currentView !== 'import' || !event.dataTransfer?.types.includes('Files')) return;
        event.preventDefault(); depth = 0; zone.classList.remove('is-dragging');
        if (!window.pywebview?.api) {
          importMessage = '请在 FoilPage 桌面应用中拖入文件，或点击「选择文件」'; paintImport();
        }
      }, {signal});
    } else if (key === 'browser') {
      host.innerHTML = `<div class="tool-page browser-install-page"><div class="tool-card browser-install-card"><div class="browser-install-head"><h2>连接浏览器</h2><div class="connection-line"><span class="worker-dot" id="browserDot"></span><span id="browserStatus" role="status">正在检查配对…</span></div></div><nav class="browser-install-steps" aria-label="浏览器安装步骤"></nav><div class="browser-install-content"><h3 id="browserStepTitle"></h3><p id="browserStepDescription"></p><button type="button" class="browser-install-figure" id="browserGuideZoom" aria-label="放大当前步骤示意图"><img id="browserStepImage" alt=""><span>${AppIcons('search')}放大示意图</span></button><div class="browser-step-actions" data-browser-step="0"><div class="tool-buttons"><button class="btn primary" id="openExtPage">打开浏览器扩展页</button><button class="btn ghost" id="extensionDir">打开扩展目录</button></div><p class="path-text" id="extensionPath" aria-label="固定扩展目录"></p></div><div class="browser-step-actions" data-browser-step="2" hidden><p id="browserHint"></p><div class="tool-buttons"><button class="btn primary" id="pairBrowser">允许连接</button><button class="icon-btn" id="checkBrowser" title="检查配对" aria-label="检查配对">${AppIcons('retry')}</button></div></div></div><div class="browser-install-footer"><button class="btn ghost small" id="browserPrevious">上一步</button><span id="browserStepCount" aria-live="polite"></span><button class="btn small" id="browserNext">下一步</button></div><details class="tool-details"><summary>首次安装与后续更新</summary><p>首次选择「加载已解压的扩展」，加载下方固定目录；以后保留此路径。</p><p>以后随桌面升级更新扩展文件；任务完成、弹窗关闭后，插件空闲重载。已打开的视频页请刷新。</p><p>若版本仍不一致，请重新加载本页目录并配对。</p></details></div></div>`;
      const steps = [
        {title:'加载扩展', description:'在扩展管理页开启开发者模式，加载 FoilPage 的固定扩展目录。'},
        {title:'打开扩展', description:'点击浏览器工具栏的 FoilPage 图标。未显示时，在扩展菜单中固定 FoilPage。'},
        {title:'完成连接', description:'先允许连接，再在浏览器扩展中点击「一键配置 FoilPage」。'},
      ];
      host.querySelector('.browser-install-head .connection-line').append(host.querySelector('#checkBrowser'));
      const footer = host.querySelector('.browser-install-footer');
      for (const [index,controls] of [[0,host.querySelector('[data-browser-step="0"] .tool-buttons')],[2,host.querySelector('[data-browser-step="2"] .tool-buttons')]]) {
        const actions = document.createElement('div'); actions.className = 'browser-step-actions'; actions.dataset.browserStep = String(index);
        actions.append(controls);footer.insertBefore(actions,host.querySelector('#browserStepCount'));
      }
      let step = 0, stepChosen = false;
      const stepButtons = steps.map((item, i) => {
        const button = document.createElement('button'); button.type = 'button';
        button.innerHTML = `<span class="browser-step-number">${i + 1}</span><span>${item.title}</span>`;
        button.setAttribute('aria-controls', 'browserStepTitle');
        button.onclick = () => {stepChosen = true; step = i; paintStep();};
        host.querySelector('.browser-install-steps').append(button); return button;
      });
      const paintStep = () => {
        host.querySelector('#browserStepTitle').textContent = steps[step].title;
        host.querySelector('#browserStepDescription').textContent = steps[step].description;
        const image = host.querySelector('#browserStepImage');
        image.src = 'assets/browser-pairing-' + (step + 1) + '.svg'; image.alt = steps[step].title + '示意图';
        host.querySelectorAll('[data-browser-step]').forEach(node => {node.hidden = Number(node.dataset.browserStep) !== step;});
        stepButtons.forEach((button, i) => {
          button.classList.toggle('is-active', i === step);
          if (i === step) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
        });
        host.querySelector('#browserPrevious').disabled = step === 0;
        host.querySelector('#browserNext').hidden = step === steps.length - 1;
        host.querySelector('#browserStepCount').textContent = (step + 1) + ' / ' + steps.length;
      };
      host.querySelector('#browserPrevious').onclick = () => {stepChosen = true; step = Math.max(0, step - 1); paintStep();};
      host.querySelector('#browserNext').onclick = () => {stepChosen = true; step = Math.min(steps.length - 1, step + 1); paintStep();};
      host.querySelector('#browserGuideZoom').querySelector('span').innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M7 10h6m-3-3v6"/></svg>放大示意图';
      host.querySelector('#browserGuideZoom').onclick = () => openModal(steps[step].title,
        '<img class="browser-pairing-large" src="assets/browser-pairing-' + (step + 1) + '.svg" alt="' + steps[step].title + '示意图">');
      paintStep();
      browserStepControl = index => {
        if(AppState.currentView!=='browser' || !host.querySelector('.browser-install-card')?.isConnected || !Number.isInteger(index) || index<0 || index>=steps.length)return;
        stepChosen=true;step=index;paintStep();
      };
      let status, checkRevision = 0;
      const panel = host.querySelector('.tool-card');
      host.querySelector('#extensionDir').disabled = true;
      host.querySelector('#pairBrowser').disabled = true;
      const isCurrent = revision => AppState.currentView === 'browser' && panel.isConnected && revision === checkRevision;
      const check = async () => {
        const revision = ++checkRevision;
        try { const result = await AppApi.browserStatus(); if (!isCurrent(revision)) return;
          if (!result?.ok) throw new Error('browser status unavailable');
          status = result;
          if (!stepChosen && status.configured && status.running) {step = 2; paintStep();}
          host.querySelector('#browserStatus').textContent = !status.running ? '本地服务未运行' : status.configured ? '浏览器已配对' : '等待浏览器配对';
          host.querySelector('#browserHint').textContent = !status.running ? '开启本地服务后，再打开浏览器中的 FoilPage 扩展。' : status.configured ? '在视频页提交下载与转写；升级后刷新已打开的视频页。' : '先允许连接，再打开浏览器中的 FoilPage 扩展完成配对。';
          host.querySelector('#browserDot').classList.toggle('on', !!status.running && !!status.configured);
          host.querySelector('#pairBrowser').textContent = status.configured ? '重新配对' : '允许连接';
          host.querySelector('#pairBrowser').dataset.serviceReady = String(!!status.running);
          host.querySelector('#pairBrowser').disabled = pairingBusy || !status.running;
          host.querySelector('#extensionPath').textContent = status.extension_dir || '';
          host.querySelector('#extensionDir').disabled = !status.extension_dir;
        } catch { if (isCurrent(revision)) {
          host.querySelector('#browserStatus').textContent = '无法读取配对状态';
          host.querySelector('#browserHint').textContent = '请确认本地服务正在运行，再检查一次。';
          host.querySelector('#browserDot').classList.remove('on');
          host.querySelector('#pairBrowser').disabled = true;
          host.querySelector('#pairBrowser').dataset.serviceReady = 'false';
        } }
      };
      host.querySelector('#checkBrowser').onclick = check;
      host.querySelector('#extensionDir').onclick = async () => {
        try { if (status?.extension_dir) {const result = await AppApi.openDir(status.extension_dir); if (panel.isConnected && AppState.currentView === 'browser' && !result?.ok) toast(result?.error || '扩展目录无法打开', 'error');} }
        catch (_) {if (panel.isConnected && AppState.currentView === 'browser') toast('扩展目录无法打开，请重试', 'error');}
      };
      host.querySelector('#openExtPage').onclick = async () => {
        try {
          const res = await AppApi.openChromeExtensions();
          if (!res || !res.ok) toast((res && res.error) || '未能打开浏览器扩展页', 'error');
        } catch { toast('未能打开浏览器扩展页', 'error'); }
      };
      host.querySelector('#pairBrowser').onclick = async () => {
        if (pairingBusy) return;
        pairingBusy = true; host.querySelector('#pairBrowser').disabled = true;
        try { const res = await AppApi.beginBrowserPairing(); if (!res?.ok) throw new Error(res?.error || '连接失败');
          if (AppState.currentView !== 'browser' || !panel.isConnected) return;
          host.querySelector('#browserHint').textContent = '两分钟内打开浏览器中的 FoilPage 扩展（工具栏 FoilPage 图标），点击「一键配置 FoilPage」。';
          toast('已允许浏览器连接');
        } catch (e) { if (AppState.currentView === 'browser' && panel.isConnected) toast(e.message, 'error'); }
        finally {pairingBusy = false; if (AppState.currentView === 'browser') {const current = document.getElementById('pairBrowser'); if (current) current.disabled = current.dataset.serviceReady !== 'true';}}
      };
      await check();
    }
  }
  return { render, beginNativeDrop, finishNativeDrop, showBrowserStep:index=>browserStepControl(index) };
})();
