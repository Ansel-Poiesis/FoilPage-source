// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 准备页：首次使用前的环境自检、一键配置与模型档位下载索引。 */
'use strict';

window.Setup = (() => {
  let pollTimer = null;
  let refreshTicket = null;
  let pageEpoch = 0;
  let configRevision = 0;
  let directoryBusy = false;
  let modelSettingBusy = false;
  let fixesBusy = false;
  let downloadName = '';
  let loginOwned = false;
  let checkFailed = false;
  let accountNotice = '';
  let providerCheck = 0;
  let completionBusy = false;
  const modelBusy = new Set();

  const VERDICT_META = {
    ready: { label: '配置满足', cls: 'v-ok' },
    usable: { label: 'CPU 可用', cls: 'v-ok' },
    cpu_slow: { label: '不建议（很慢）', cls: 'v-bad' },
    no_disk: { label: '空间不足', cls: 'v-bad' },
  };

  const MODEL_OPTIONS = [
    { value: 'tiny', label: 'tiny（极速档，约 75 MB）' },
    { value: 'base', label: 'base（轻量档，约 145 MB）' },
    { value: 'small', label: 'small（均衡档，约 488 MB）' },
    { value: 'medium', label: 'medium（进阶档，约 1.5 GB）' },
    { value: 'large-v3', label: 'large-v3（旗舰档，约 3 GB）' },
  ];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function fmtBytes(bytes) {
    if (!bytes || bytes < 0) return '未知';
    const gib = bytes / (1024 * 1024 * 1024);
    if (gib >= 1) return gib.toFixed(1) + ' GB';
    const mib = bytes / (1024 * 1024);
    return mib.toFixed(0) + ' MB';
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  const isCurrent = epoch => epoch === pageEpoch && AppState.currentView === 'setup';
  function closeLogin() { if(loginOwned)window.AccountAuth?.close();loginOwned=false; }
  function leave() { const stage=document.getElementById('setupStage'); if(stage)stepScroll.set(currentStep,stage.scrollTop); closeLogin(); ++pageEpoch; stopPoll(); }

  const STEPS = [
    {key:'login', label:'登录'},
    {key:'environment', label:'环境检测'},
    {key:'models', label:'模型下载'},
  ];
  let currentStep = 'login';
  let latestData = null;
  const stepScroll = new Map();

  function render() {
    closeLogin();
    stopPoll();
    ++pageEpoch;
    const host = document.getElementById('page-setup');
    if (!host) return;
    if (!host.querySelector('.setup-wizard')) {
      host.innerHTML = `<div class="setup-wizard">
        <div class="setup-steps" role="tablist" aria-label="准备步骤">${STEPS.map((step,i) => `<button class="setup-step" id="setupTab-${step.key}" role="tab" data-step="${step.key}" aria-controls="setupPanel-${step.key}"><span class="setup-step-number">${i+1}</span><span>${step.label}</span></button>`).join('')}</div>
        <div class="setup-stage" id="setupStage">
          <section class="setup-slide setup-login" id="setupPanel-login" role="tabpanel" aria-labelledby="setupTab-login" tabindex="-1">
            <div class="setup-account-art" aria-hidden="true"><img src="assets/foilpage.png" alt=""></div>
            <div class="setup-account-copy"><h2 id="setupAccountTitle">登录 FoilPage</h2></div>
            <div class="setup-account-card tool-card" id="setupAccountCard"><span class="setup-account-status" role="status" id="setupAccountStatus"></span><button class="btn primary" id="setupRestoreBtn" hidden>恢复登录</button><button class="btn primary" id="setupWebLoginBtn" hidden>通过网页登录</button><button class="btn" id="setupLoginBtn">邮箱登录</button><button class="btn" id="setupGoogleLoginBtn" hidden>使用 Google 登录</button><button class="btn ghost" id="setupAccountManageBtn" hidden>管理账户</button></div>
          </section>
          <section class="setup-slide" id="setupPanel-environment" role="tabpanel" aria-labelledby="setupTab-environment" tabindex="-1" hidden>
            <div class="setup-heading" id="setupEnvCard"><h3>环境检测</h3><p class="setup-status" role="status" id="setupEnvState">正在检查环境…</p><button class="btn small" id="setupEnvRetryBtn" hidden>重新检查</button></div>
            <div class="setup-env-summary" id="setupEnvSummary"></div>
            <div class="setup-paths tool-card">
              <div class="set-row" id="setupCacheCard"><div class="set-left"><div class="set-label">模型缓存目录</div><div class="set-hint setup-path" id="setupCachePath">正在读取…</div></div><div class="set-ctrl"><button class="btn small" id="setupCacheDirBtn">更改缓存目录</button></div></div>
              <div class="set-row" id="setupDownloadCard"><div class="set-left"><div class="set-label">视频保存目录</div><div class="set-hint setup-path" id="setupDownloadPath"></div></div><div class="set-ctrl"><button class="btn small" id="setupDownloadDirBtn">更改保存目录</button></div></div>
            </div>
            <div class="setup-fix" id="setupFixBox" hidden></div>
            <details class="setup-env-details"><summary>查看完整检测</summary><div id="setupEnvRows"></div></details>
          </section>
          <section class="setup-slide" id="setupPanel-models" role="tabpanel" aria-labelledby="setupTab-models" tabindex="-1" hidden>
            <div class="setup-heading" id="setupModelCard"><h2>模型下载</h2></div>
            <p class="setup-status" role="status" id="setupModelState"></p><button class="btn small" id="setupModelRetryBtn" hidden>重新检查模型</button>
            <div class="set-row setup-default" id="setupDefaultRow"></div>
            <div id="setupRecommended"></div>
            <div class="model-lib" id="setupModelList"><p class="set-hint">正在读取模型状态…</p></div>
          </section>
        </div>
        <div class="setup-footer"><button class="btn ghost setup-guide" id="setupGuideBtn">新手引导</button><div class="setup-paging"><button class="btn ghost" id="setupPreviousBtn" aria-label="上一步">← 上一步</button><button class="btn primary" id="setupNextBtn">下一步 →</button></div><button class="setup-workbench-link" id="setupWorkbenchBtn" hidden>进入任务</button></div>
      </div>`;
      host.querySelector('#setupGuideBtn').onclick = () => window.Onboarding?.show();
      host.querySelector('#setupWorkbenchBtn').onclick = complete;
      host.querySelector('#setupPreviousBtn').onclick = () => showStep(STEPS[Math.max(0, stepIndex()-1)].key, {focus:true});
      host.querySelector('#setupNextBtn').onclick = () => stepIndex() === STEPS.length-1 ? complete() : showStep(STEPS[stepIndex()+1].key, {focus:true});
      host.querySelectorAll('.setup-step').forEach(button => {
        button.onclick = () => showStep(button.dataset.step);
        button.onkeydown = event => {
          let next;
          const i = STEPS.findIndex(step => step.key === button.dataset.step);
          if (event.key === 'ArrowRight') next = (i+1)%STEPS.length;
          if (event.key === 'ArrowLeft') next = (i+STEPS.length-1)%STEPS.length;
          if (event.key === 'Home') next = 0;
          if (event.key === 'End') next = STEPS.length-1;
          if (next !== undefined) {event.preventDefault();if(showStep(STEPS[next].key))host.querySelector('#setupTab-'+STEPS[next].key).focus();}
        };
      });
      host.querySelector('#setupLoginBtn').onclick = () => {
        if (window.Subscription?.openLogin) {
          const epoch=pageEpoch;loginOwned=true;accountNotice='';
          const opened=window.Subscription.openLogin({trigger:host.querySelector('#setupLoginBtn'),isCurrent:()=>isCurrent(epoch),onSuccess:()=>{loginOwned=false;if(isCurrent(epoch)){refreshAccount();refresh();}}});
          if(opened === false) {
            loginOwned=false;
            accountNotice='账户操作暂不可用，请稍后重试或管理账户';refreshAccount();
          }
        }
        else toast('登录暂不可用，请稍后重试', 'error');
      };
      host.querySelector('#setupRestoreBtn').onclick=async()=>{
        const epoch=pageEpoch;const result=await AccountAccess.restore();
        if(result.ok&&isCurrent(epoch)){if(AppState.settings.setup_completed)switchView('workbench');else showStep('environment');}
      };
      host.querySelector('#setupWebLoginBtn').onclick=async()=>{
        const button=host.querySelector('#setupWebLoginBtn');if(button.disabled||button.hidden)return;
        const epoch=pageEpoch;button.disabled=true;
        try{await Subscription.openProviderLogin({method:'web',isCurrent:()=>isCurrent(epoch),onSuccess:()=>{if(isCurrent(epoch)){refreshAccount();if(AppState.settings.setup_completed)switchView('workbench');else refresh();}}});}
        finally{if(isCurrent(epoch))button.disabled=false;}
      };
      host.querySelector('#setupAccountManageBtn').onclick = () => {
        const settings=AppState.settings || {};
        if(!String(settings.subscription_server_url || '').trim() || !String(settings.subscription_license_public_key || '').trim()) window.Subscription.openConnection();
        else switchView('subscription');
      };
      host.querySelector('#setupGoogleLoginBtn').onclick = async () => {
        const button=host.querySelector('#setupGoogleLoginBtn');if(button.disabled || button.hidden)return;
        const epoch=pageEpoch;button.disabled=true;
        try {await Subscription.openProviderLogin({method:'google',isCurrent:()=>isCurrent(epoch),onSuccess:()=>{if(isCurrent(epoch)){refreshAccount();refresh();}}});}
        finally {if(isCurrent(epoch))button.disabled=false;}
      };
      host.querySelector('#setupDownloadDirBtn').onclick = () => pickDirectory('download_dir');
      host.querySelector('#setupCacheDirBtn').onclick = () => pickDirectory('model_dir');
      host.querySelector('#setupEnvRetryBtn').onclick = refresh;
      host.querySelector('#setupModelRetryBtn').onclick = refresh;
    }
    showDirectories(AppState.settings);
    refreshAccount();
    checkProviders();
    showStep(window.AccountAccess.allowed() ? currentStep === 'login' && AppState.settings?.account_login_token ? 'environment' : currentStep : 'login');
    syncProtection();
    if (window.AccountAccess.allowed()) refresh();
  }

  function stepIndex() {return STEPS.findIndex(step => step.key === currentStep);}
  async function complete() {
    if(completionBusy || !window.AccountAccess.allowed())return;
    const epoch=pageEpoch,account=AppState.settings?.account_login_token;
    completionBusy=true;document.getElementById('setupNextBtn').disabled=true;
    try {
      const result=await AppApi.saveSettings({setup_completed:true});
      if(!isCurrent(epoch) || account!==AppState.settings?.account_login_token)return;
      if(result?.ok===false || result?.setup_completed!==true)throw new Error('准备尚未保存，请重试');
      AppState.settings.setup_completed=true;switchView('workbench');
    } catch(_) {if(isCurrent(epoch))toast('准备尚未保存，请重试','error');}
    finally {completionBusy=false;if(isCurrent(epoch))refreshAccount();}
  }
  function showStep(key, {focus=false} = {}) {
    if (!STEPS.some(step => step.key === key)) return false;
    if (key !== 'login' && !window.AccountAccess.allowed()) return false;
    const stage = document.getElementById('setupStage');
    if (!stage) {currentStep=key;return true;}
    if (currentStep !== key) {closeLogin();stepScroll.set(currentStep,stage.scrollTop);}
    currentStep = key;
    STEPS.forEach(step => {
      const selected = step.key === key, tab = document.getElementById('setupTab-'+step.key);
      tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
      document.getElementById('setupPanel-'+step.key).hidden=!selected;
    });
    stage.scrollTop = stepScroll.get(key) || 0;
    document.getElementById('setupPreviousBtn').disabled = stepIndex() === 0;
    document.getElementById('setupNextBtn').textContent = stepIndex() === 2 ? '进入任务 →' : '下一步 →';
    document.getElementById('setupNextBtn').disabled = completionBusy || !window.AccountAccess.allowed();
    document.getElementById('setupWorkbenchBtn').hidden = key === 'models' || !window.AccountAccess.allowed();
    STEPS.forEach(step => {document.getElementById('setupTab-'+step.key).disabled=step.key!=='login'&&!window.AccountAccess.allowed();});
    if (focus) document.getElementById('setupPanel-'+key).focus({preventScroll:true});
    return true;
  }

  function refreshAccount() {
    const title = document.getElementById('setupAccountTitle');
    if (!title) return;
    const settings = AppState.settings || {}, loggedIn = Boolean(settings.account_login_token) && window.AccountAccess.allowed();
    const pending=AppState.accountRestorePending === true && !window.AccountAccess.allowed();
    const configured=!!String(settings.subscription_server_url || '').trim() && !!String(settings.subscription_license_public_key || '').trim();
    if(loggedIn){accountNotice='';AppState.accountRestoreError='';}
    // 先生 2026-10-25：欢迎回来后接逗号与用户名；无名可取时保持原样。
    const displayName = String(settings.account_display_name || '').trim();
    title.textContent = pending ? '正在恢复登录' : loggedIn ? (displayName ? `欢迎回来，${displayName}` : '欢迎回来') : '登录 FoilPage';
    document.getElementById('setupAccountCard').setAttribute('aria-busy',String(pending));
    document.getElementById('setupAccountStatus').textContent = pending ? '正在验证…' : loggedIn ? '' : (accountNotice || AppState.accountRestoreError || (!configured ? '登录服务尚未配置' : ''));
    document.getElementById('setupRestoreBtn').hidden=loggedIn||pending||!settings.account_login_token||!configured;
    document.getElementById('setupRestoreBtn').disabled=pending;
    document.getElementById('setupWebLoginBtn').hidden=loggedIn||pending||document.getElementById('setupWebLoginBtn').dataset.available!=='true';
    document.getElementById('setupLoginBtn').hidden = loggedIn || pending;
    if(loggedIn || pending)document.getElementById('setupGoogleLoginBtn').hidden=true;
    const manage=document.getElementById('setupAccountManageBtn');
    manage.textContent = !configured ? '配置登录服务' : '管理账户';
    manage.hidden = configured && !loggedIn && !accountNotice;
    document.getElementById('setupNextBtn').disabled = completionBusy || !window.AccountAccess.allowed();
    document.getElementById('setupWorkbenchBtn').hidden = currentStep === 'models' || !window.AccountAccess.allowed();
    STEPS.forEach(step => {document.getElementById('setupTab-'+step.key).disabled=step.key!=='login'&&!window.AccountAccess.allowed();});
  }

  async function checkProviders() {
    const revision=++providerCheck, epoch=pageEpoch, server=AppState.settings?.subscription_server_url;
    const button=document.getElementById('setupGoogleLoginBtn'),web=document.getElementById('setupWebLoginBtn');if(!button)return;
    button.hidden=true;if(web)web.hidden=true;
    if(!server || window.AccountAccess.allowed() || AppState.accountRestorePending)return;
    try {
      const contract=await AppApi.checkSubscriptionService();
      if(revision!==providerCheck || !isCurrent(epoch) || server!==AppState.settings?.subscription_server_url)return;
      button.hidden=contract?.login_methods?.google!==true || window.AccountAccess.allowed();
      if(web){web.dataset.available=String(contract?.login_methods?.web===true);web.hidden=contract?.login_methods?.web!==true||window.AccountAccess.allowed();}
    } catch(_) { /* Unconfigured providers remain hidden. */ }
  }

  function modelProtected() {
    return Boolean(checkFailed || modelBusy.size || downloadName || modelSettingBusy || fixesBusy || directoryBusy || AppState.tasks?.some(task => ['transcribing','processing'].includes(task.status) || (task.kind === 'transcription' && task.status === 'running')));
  }
  function syncProtection() {
    const cache = document.getElementById('setupCacheDirBtn'), video = document.getElementById('setupDownloadDirBtn');
    if (cache) {cache.disabled = directoryBusy || !latestData || modelProtected();cache.title = modelProtected() ? '模型正在使用或下载，请完成后再修改缓存目录' : '';}
    if (video) video.disabled = directoryBusy;
    const select=document.querySelector('#setupDefaultRow select');if(select)select.disabled=modelSettingBusy || checkFailed || !Array.from(select.options).some(option=>!option.disabled);
    document.querySelectorAll('#setupModelList button[data-state="idle"], #setupModelList button.danger, #setupDownloadRec').forEach(button=>{button.disabled=modelProtected();});
  }
  function showDirectories(settings, cacheRoot) {
    const video = document.getElementById('setupDownloadPath'), cache = document.getElementById('setupCachePath');
    if (video) {video.textContent=settings?.download_dir || '尚未配置';video.title=video.textContent;}
    if (cache) {cache.textContent=settings?.model_dir || cacheRoot || latestData?.cache_root || '系统默认缓存目录';cache.title=cache.textContent;}
  }
  async function pickDirectory(key) {
    if (directoryBusy || (key === 'model_dir' && (!latestData || modelProtected()))) return;
    const epoch = pageEpoch;
    directoryBusy=true;syncProtection();
    try {
      const result = await AppApi.pickSettingDir(key);
      if (result?.ok && typeof result.settings?.[key] === 'string' && result.settings[key]) {
        AppState.settings={...AppState.settings,[key]:result.settings[key]};++configRevision;
        if (latestData) {latestData.settings={...latestData.settings,[key]:result.settings[key]};if(key==='model_dir')latestData.cache_root=result.settings[key];}
        if (AppState.currentView === 'setup') showDirectories(AppState.settings);
        if (isCurrent(epoch)) toast(key === 'model_dir' ? '模型缓存目录已更新' : '下载保存目录已更新');
      } else if (!result?.cancelled && isCurrent(epoch)) toast(result?.error || '目录保存失败','error');
    } catch(error) {if(isCurrent(epoch))toast('目录保存失败：'+error.message,'error');}
    finally {directoryBusy=false;syncProtection();if(AppState.currentView==='setup')refresh();}
  }

  async function refresh() {
    if (AppState.currentView !== 'setup') return;
    const epoch = pageEpoch, revision = configRevision;
    if (refreshTicket?.epoch === epoch && refreshTicket.revision === revision) return refreshTicket.promise;
    const ticket = {epoch, revision, promise:null};
    refreshTicket = ticket;
    const valid = () => isCurrent(epoch) && revision === configRevision;
    ticket.promise = (async () => {
      try {
        const data = await AppApi.setupCheck();
        if (!valid()) return;
        if (data && data.ok) applyData(data);
        else setEnvState((data && data.error) || '环境检查失败');
      } catch (e) {
        if (valid()) setEnvState('环境检查失败：' + (e && e.message || e));
      } finally {
        if (refreshTicket === ticket) refreshTicket = null;
      }
    })();
    return ticket.promise;
  }

  function setEnvState(message) {
    checkFailed = Boolean(message);
    const node = document.getElementById('setupEnvState');
    if (node) node.textContent = message;
    const model = document.getElementById('setupModelState');
    if (model) model.textContent = message;
    for (const id of ['setupEnvRetryBtn','setupModelRetryBtn']) {
      const button=document.getElementById(id);if(button)button.hidden=!message;
    }
    syncProtection();
  }

  function applyData(data) {
    latestData = data;
    showDirectories(data.settings || AppState.settings, data.cache_root);
    setEnvState('');
    renderEnvRows(data.rows || []);
    renderGpuPackRow(data.gpu_pack);
    renderFixBox(data);
    renderDefaultRow(data);
    const previousDownload = downloadName;
    const activeDownload = data.download?.name || '';
    downloadName = activeDownload;
    if (activeDownload) startPoll();
    else {
      stopPoll();
      if (previousDownload) toast(data.download?.error ? '模型下载失败：' + data.download.error : '模型下载完成', data.download?.error ? 'error' : '');
    }
    renderRecommended(data);
    renderModelList(data);
    syncProtection();
  }

  function renderEnvRows(rows) {
    const box = document.getElementById('setupEnvRows');
    if (!box) return;
    box.textContent = '';
    const summary=document.getElementById('setupEnvSummary');
    if(summary){summary.textContent='';rows.filter(row=>['gpu','disk','default_model'].includes(row.key)).forEach(row=>{const tile=el('div','setup-env-tile');tile.append(el('span','setup-env-label',row.label),el('strong','setup-env-value',row.value));summary.append(tile);});}
    rows.forEach((r) => {
      const rowEl = el('div', 'set-row');
      const left = el('div', 'set-left');
      left.append(el('div', 'set-label', r.label));
      if (r.hint) left.append(el('div', 'set-hint', r.hint));
      const ctrl = el('div', 'set-ctrl');
      const dot = el('span', 'env-dot ' + (r.status || 'info'));
      const value = el('span', 'set-value', r.value);
      value.title = r.value;
      ctrl.append(dot, value);
      rowEl.append(left, ctrl);
      box.append(rowEl);
    });
  }

  // GPU 加速库（先生 2026-10-08 拆包）：安装器只含核心刚需，GPU 用户在此
  // 自选安装配套 zip；解压回 worker _internal/nvidia 由 engine/dll.py 发现。
  function renderGpuPackRow(gpuPack) {
    const box = document.getElementById('setupEnvRows');
    if (!box || !gpuPack) return;
    const installed = Boolean(gpuPack.installed);
    const rowEl = el('div', 'set-row');
    const left = el('div', 'set-left');
    left.append(el('div', 'set-label', 'GPU 加速库'));
    left.append(el('div', 'set-hint', installed
      ? '已安装。CPU 转写同样完整可用；重装可修复损坏的运行库。'
      : '未安装——CPU 转写完整可用，安装后 GPU 转写提速数倍（与安装器配套的 zip 包）。'));
    const ctrl = el('div', 'set-ctrl');
    const dot = el('span', 'env-dot ' + (installed ? 'ok' : 'warn'));
    ctrl.append(dot, el('span', 'set-value', installed ? '已安装' : '未安装'));
    const btn = el('button', 'btn ghost small', installed ? '重新安装…' : '安装 GPU 加速包…');
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const picked = await AppApi.pickGpuPack();
        if (!picked || !picked.ok) {
          if (picked && picked.error) toast(picked.error, 'error');
          return;
        }
        const result = await AppApi.installGpuPack(picked.path);
        if (result && result.ok) {
          toast(result.message || 'GPU 加速库已安装，重启 FoilPage 后生效');
          refresh();
        } else {
          toast((result && result.error) || '安装失败', 'error');
        }
      } finally {
        btn.disabled = false;
      }
    };
    ctrl.append(btn);
    rowEl.append(left, ctrl);
    box.append(rowEl);
  }

  function renderFixBox(data) {
    const box = document.getElementById('setupFixBox');
    if (!box) return;
    box.textContent = '';
    box.hidden = false;
    const fixes = data.fixes || [];
    if (fixes.length) {
      box.append(el('div', 'set-label', '一键配置环境'));
      const details=el('details','setup-fix-details');details.append(el('summary','','查看配置项目'));const list=el('ul');
      fixes.forEach((f) => list.append(el('li', '', f.label)));details.append(list);box.append(details);
      const buttons = el('div', 'tool-buttons');
      const apply = el('button', 'btn primary small', '一键配置环境');
      apply.id = 'setupApplyBtn';
      apply.disabled = checkFailed || fixesBusy || directoryBusy || modelBusy.size > 0 || Boolean(downloadName);
      apply.onclick = async () => {
        if (checkFailed || fixesBusy || directoryBusy || modelBusy.size || downloadName) return;
        fixesBusy = true;
        const epoch = pageEpoch;
        apply.disabled = true;syncProtection();
        try {
          const res = await AppApi.applySetupFixes();
          if (res && res.ok) {
            AppState.settings = {...AppState.settings, ...(res.settings || {})};
            ++configRevision;
            if (isCurrent(epoch)) {
              toast(res.applied && res.applied.length ? '已完成配置：' + res.applied.join('；') : '环境已是最新，无需配置');
              applyData(res);
            }
          } else {
            if (isCurrent(epoch)) toast((res && res.error) || '配置失败', 'error');
          }
        } catch (e) {
          if (isCurrent(epoch)) toast('配置失败：' + (e && e.message || e), 'error');
        } finally {
          fixesBusy = false;syncProtection();
          if (AppState.currentView === 'setup') refresh();
        }
      };
      const recheck = el('button', 'btn ghost small', '重新检查');
      recheck.onclick = () => refresh();
      buttons.append(apply, recheck);
      box.append(buttons);
    } else {
      const warns = (data.rows || []).filter((r) => r.status === 'warn');
      box.append(el('div', 'set-hint', warns.length
        ? '基础配置已就绪，默认模型可在下一步下载。'
        : '环境已就绪，无需额外配置。'));
      const buttons = el('div', 'tool-buttons');
      const recheck = el('button', 'btn ghost small', '重新检查');
      recheck.onclick = () => refresh();
      buttons.append(recheck);
      box.append(buttons);
    }
  }

  function renderDefaultRow(data) {
    const box = document.getElementById('setupDefaultRow');
    if (!box) return;
    const existing=box.querySelector('select');
    if (existing && (modelSettingBusy || box.contains(document.activeElement))) {
      for (const option of existing.options) {
        const model=(data.models||[]).find(item=>item.name===option.value);
        const ready=modelReady(model,data), label=MODEL_OPTIONS.find(item=>item.value===option.value)?.label || option.value;
        option.disabled=!ready;
        option.textContent=label+(ready?'':data.download?.name===option.value?'（下载中）':model?.installed?'（缓存不完整）':'（未下载）');
      }
      syncProtection();return;
    }
    box.textContent = '';
    const left = el('div', 'set-left');
    left.append(el('div', 'set-label', '默认转写模型'));

    const ctrl = el('div', 'set-ctrl');
    const select = el('select', 'ctrl');
    select.dataset.tour = 'setup-default-model';
    const current = (data.settings && data.settings.whisper_model) || 'tiny';
    MODEL_OPTIONS.forEach((opt) => {
      const model = (data.models || []).find(item => item.name === opt.value);
      const ready = modelReady(model, data);
      const suffix = ready ? '' : data.download?.name === opt.value ? '（下载中）' : model?.installed ? '（缓存不完整）' : '（未下载）';
      const option = el('option', '', opt.label + suffix);
      option.value = opt.value;
      option.disabled = !ready;
      option.selected = opt.value === current;
      select.append(option);
    });
    if (!MODEL_OPTIONS.some(option=>option.value===current)) {
      const unavailable=el('option','','当前模型不可用');unavailable.value=current;unavailable.disabled=true;select.prepend(unavailable);
    }
    select.value = current;
    select.disabled = modelSettingBusy || checkFailed || !Array.from(select.options).some(option=>!option.disabled);
    select.onchange = async () => {
      if (modelSettingBusy || checkFailed) return;
      const epoch = pageEpoch, model = select.value;
      if (!modelReady((latestData?.models || []).find(item=>item.name===model), latestData)) {select.value=current;return;}
      modelSettingBusy = true;
      select.disabled = true;syncProtection();
      try {
        const settings = await AppApi.saveSettings({ whisper_model: model });
        if (!settings || settings.ok === false || !MODEL_OPTIONS.some(option => option.value === settings.whisper_model)) throw new Error(settings?.error || '未收到保存确认');
        AppState.settings = {...AppState.settings, whisper_model:settings.whisper_model};
        ++configRevision;
        if (isCurrent(epoch)) toast('默认模型已设为 ' + model);
      } catch (e) {
        if (isCurrent(epoch)) select.value = current;
        if (isCurrent(epoch)) toast('保存失败：' + (e && e.message || e), 'error');
      } finally {
        modelSettingBusy = false;syncProtection();
        if (AppState.currentView === 'setup') {
          const currentSelect = document.querySelector('#setupDefaultRow select'); if (currentSelect) currentSelect.disabled = checkFailed || !Array.from(currentSelect.options).some(option=>!option.disabled);
          refresh();
        }
      }
    };
    ctrl.append(select);
    box.append(left, ctrl);
  }

  function modelReady(model, data = latestData) {
    if (!model || modelBusy.has(model.name) || data?.download?.name === model.name) return false;
    return Object.hasOwn(model,'ready') ? model.ready === true : model.verified === true;
  }

  function renderRecommended(data) {
    const box = document.getElementById('setupRecommended');
    if (!box) return;
    box.textContent = '';
    const tiers = data.tiers || [];
    const rec = tiers.find((t) => t.recommended);
    if (!rec) return;
    const model = (data.models || []).find((m) => m.name === rec.name);
    const verified = modelReady(model, data);
    const busy = modelBusy.has(rec.name) || ((data.download || {}).name === rec.name);
    if (verified) {
      const line = el('div', 'model-guide', '本机推荐档位 ' + rec.name + '（' + rec.tier_label + '）已就绪，可直接开始转写。');
      box.append(line);
      return;
    }
    if (busy || modelBusy.size || data.download?.name) return;
    const line = el('div', 'model-guide', '本机推荐 ' + rec.name + '（' + rec.tier_label + '档，' + (model ? model.size_label : '') + '）');
    const download = el('button', 'btn primary small', '下载推荐模型');
    download.id = 'setupDownloadRec';
    download.onclick = () => (rec.name === 'medium' || rec.name === 'large-v3')
      ? confirmBigDownload(rec.name)
      : startDownload(rec.name);
    const wrap = el('div', 'setup-rec');
    wrap.append(line, download);
    box.append(wrap);
  }

  function badge(className, text) {
    const node = el('span', className, text);
    return node;
  }

  function renderModelList(data) {
    const box = document.getElementById('setupModelList');
    if (!box) return;
    box.textContent = '';
    const dl = data.download || {};
    const models = data.models || [];
    (data.tiers || []).forEach((t) => {
      const m = models.find((x) => x.name === t.name)
        || { name: t.name, size_label: '', description: '', verified: false, installed: false };
      const busy = dl.name === t.name || modelBusy.has(t.name);
      const meta = VERDICT_META[t.verdict] || { label: t.verdict, cls: '' };
      const item = el('div', 'model-item' + (t.recommended ? ' is-recommended' : ''));
      item.dataset.model = t.name;
      const rowEl = el('div', 'set-row');
      const left = el('div', 'set-left');
      const name = el('div', 'set-label', t.name);
      name.append(badge('tier-badge', t.tier_label + '档'));
      name.append(badge('verdict-badge ' + meta.cls, meta.label));
      if (t.recommended) name.append(badge('verdict-badge v-rec', '本机推荐'));
      const hint = el('div', 'set-hint');
      hint.textContent = m.size_label + '（' + m.description + '，' + t.detail + '）'
        + (dl.name===t.name || modelBusy.has(t.name) ? '（下载中）' : modelReady(m,data) ? '（已安装）' : (m.installed ? '（缓存不完整）' : ''));
      left.append(name, hint);
      const act = el('div', 'set-ctrl model-act');
      act.dataset.tour = 'model-' + t.name;
      if (busy) {
        const pill = el('button', 'state-pill state-pill--labeled');
        pill.dataset.state = 'active';
        pill.disabled = true;
        pill.innerHTML = `<span class="thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/></svg></span><span class="label">下载中…</span>`;
        act.append(pill);
      } else if (modelReady(m,data)) {
        const btn = el('button', 'btn danger small', '删除');
        btn.disabled = modelProtected();
        btn.onclick = () => {
          if (modelProtected()) return;
          ConfirmDelete.run({
            message: '确定删除 ' + t.name + ' 模型缓存？删除后可随时重新下载。',
            onConfirm: async () => {
              if(modelProtected())return;
              modelBusy.add(t.name);syncProtection();
              const epoch=pageEpoch;
              try {const res=await AppApi.deleteModel(t.name);if(isCurrent(epoch))toast(res?.ok ? '已删除 '+t.name : (res?.error || '删除失败'),res?.ok ? '' : 'error');}
              catch(error){if(isCurrent(epoch))toast('删除失败：'+error.message,'error');}
              finally{modelBusy.delete(t.name);syncProtection();if(AppState.currentView==='setup')refresh();}
            },
          });
        };
        act.append(btn);
      } else {
        const pill = el('button', 'state-pill state-pill--labeled');
        pill.dataset.state = 'idle';
        pill.disabled = modelProtected();
        pill.innerHTML = `<span class="thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12"/><path d="m6 12 6 6 6-6"/><path d="M5 20h14"/></svg></span><span class="label">下载</span>`;
        pill.onclick = (t.name === 'medium' || t.name === 'large-v3')
          ? () => confirmBigDownload(t.name)
          : () => startDownload(t.name);
        act.append(pill);
      }
      rowEl.append(left, act);
      item.append(rowEl);
      if (busy) {
        const pct = (dl.name === t.name && dl.progress !== null && dl.progress !== undefined)
          ? Math.round(dl.progress * 100) : 0;
        const bar = el('div', 'model-bar');
        const fill = el('div', 'model-bar-fill');
        fill.style.width = pct + '%';
        bar.append(fill);
        item.append(bar);
        item.append(el('div', 'model-msg', (dl.name === t.name && dl.message) || '准备中…'));
      }
      box.append(item);
    });
  }

  function confirmBigDownload(name) {
    ConfirmDelete.run({
      message: name + ' 体积较大，下载可能耗时数分钟到数十分钟。建议确认网络稳定并留有足够磁盘空间；中断后重试会从断点继续。确认开始下载？',
      onConfirm: () => startDownload(name),
    });
  }

  async function startDownload(name) {
    if (modelProtected()) return;
    modelBusy.add(name);syncProtection();
    if(latestData) {renderRecommended(latestData);renderModelList(latestData);}
    const epoch = pageEpoch;
    try {
      const res = await AppApi.downloadModel(name);
      if (res && res.ok) {
        if (isCurrent(epoch)) toast('开始下载模型 ' + name + '，可在下方查看进度');
      } else {
        if (isCurrent(epoch)) toast((res && res.error) || '模型下载失败', 'error');
      }
    } catch (e) {
      if (isCurrent(epoch)) toast('模型下载失败：' + (e && e.message || e), 'error');
    } finally {
      modelBusy.delete(name);syncProtection();
      if (AppState.currentView === 'setup') refresh();
    }
  }

  function startPoll() {
    if (pollTimer || AppState.currentView !== 'setup') return;
    const epoch = pageEpoch;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (isCurrent(epoch)) {
        await refresh();
        if (isCurrent(epoch) && downloadName) startPoll();
      }
    }, 1200);
  }

  function syncState() {syncProtection();refreshAccount();}
  return { render, leave, showStep, refreshAccount, syncState, refreshProviders:checkProviders };
})();
