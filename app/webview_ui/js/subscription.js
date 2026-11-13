// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 使用与订阅：真实金额目录、账户、积分同步与订单兑换。 */
'use strict';

window.Subscription = (() => {
  const {inputEl, row, valueEl, group} = Settings.controls;
  const emailLoginState = {busy:false, cooldownUntil:0, email:'', code:'', intent:'login', step:'email', timer:0, refresh:() => {}};
  const actionBusy = new Set();
  let viewEpoch = 0;
  let connectionExpanded = false;
  let toolsExpanded = false;
  let featuresExpanded = false;
  const emailCapabilities = {key:'', methods:{}, checking:false, revision:0};
  const toolExpansions = new Map();
  let refreshActions = () => {};
  let retryLoginService = () => {};
  let authRevision = 0;
  let profileRevision = 0;
  let deviceManagementPending = false;
  let betaExpiryTimer = 0, syncState = () => {};
  const buyerState = {key:'', verified:false, busy:false, generation:0};
  let redeemOrderDraft = '';
  const connectionDrafts = new Map();
  const planPeriodSelections = new Map();
  // In-memory capability snapshots are scoped to the exact service and account.
  // Refreshing a known connection must not masquerade as sales being disabled.
  const confirmedContracts = new Map();
  const confirmedAlipay = new Map();
  const trustKey = () => [s().subscription_server_url, s().subscription_license_public_key, s().subscription_checkout_url].join('\u0000');
  const s = () => AppState.settings || {};
  const serviceKey = () => String(s().subscription_server_url || '');
  const accountKey = () => serviceKey() + '\u0000' + String(s().account_login_token || '');
  const betaEnabled = state => {
    const beta = state?.beta_access;
    return !!s().account_login_token && window.AccountAccess.allowed() && beta?.version === 1
      && beta.active === true && beta.unlimited_credits === true && beta.revoked === false
      && (beta.expires_at === null || (typeof beta.expires_at === 'string' && Date.parse(beta.expires_at) > Date.now()));
  };
  function save(changes) {
    if (Object.hasOwn(changes, 'subscription_server_url')) { authRevision++; deviceManagementPending = false; }
    return Settings.save(changes).then(settings => {
      if (settings) {
        Object.entries(changes).forEach(([key, value]) => {
          if (String(connectionDrafts.get(key) ?? '').trim() === value) connectionDrafts.delete(key);
        });
        render();
      }
      return settings;
    });
  }
  function connectionInput(field, label) {
    const input = inputEl(connectionDrafts.has(field) ? connectionDrafts.get(field) : s()[field] || '',
      value => save({[field]: value.trim()}));
    input.id = field;
    input.setAttribute('aria-label', label);
    input.oninput = () => connectionDrafts.set(field, input.value);
    input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); input.onchange(); } };
    return input;
  }
  function leave() {
    viewEpoch++;
    clearTimeout(betaExpiryTimer);syncState=()=>{};
    ManualPayments.leave();
    AlipayPayments.leave();
    document.getElementById('alipayPaymentOpen')?.remove();
    AccountAuth.close();
    AccountProfile.close();
    clearTimeout(emailLoginState.timer);
    emailLoginState.refresh = () => {};
    refreshActions = () => {};
    retryLoginService = () => {};
  }
  async function beginLogin(method, {intent='login',onSuccess,isCurrent} = {}) {
    if (deviceManagementPending || actionBusy.has('login') || actionBusy.has('profile') || emailLoginState.busy || actionBusy.has('logout')) return;
    actionBusy.add('login');
    refreshActions();
    emailLoginState.refresh();
    const revision = authRevision, server = serviceKey();
    let resolved=false,webMask=null,cancelled=false;
    const valid = () => revision === authRevision && server === serviceKey() && (method!=='web'||(!cancelled&&(!webMask||webMask.isConnected)&&(!isCurrent||isCurrent())));
    try {
      const start = await (method === 'web' ? AppApi.webLoginStart() : method === 'wechat' ? AppApi.wechatLoginStart() : AppApi.googleLoginStart(intent));
      if (!valid()) return;
      if (!start?.ok) { toast(start?.error || '登录暂不可用', 'error'); return; }
      if(method==='web'){
        webMask=openModal('通过网页登录','<div class="desktop-login-confirm"><h2>正在通过浏览器登录…</h2></div>','<button class="btn ghost" id="cancelWebLogin">取消</button>');
        webMask.querySelector('#cancelWebLogin').onclick=()=>{cancelled=true;closeModal();};
      }else toast('已打开浏览器，授权后自动登录…');
      const deadline = Date.now() + (method==='web'?600000:120000);
      while (valid() && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        if (!valid()) return;
        let poll;
        try { poll = await (method==='web'?AppApi.webLoginPoll():AppApi.wechatLoginPoll()); } catch (_) { poll = null; }
        if (!valid()) return;
        if (poll?.status === 'ok') {
          if(webMask?.isConnected){webMask=null;closeModal();}
          deviceManagementPending = false;
          await syncLocalAccount();
          if (!valid()) return;
          if (!s().account_login_token || !window.AccountAccess.allowed()) {toast('登录状态尚未验证，请重试','error');return;}
          resolved=true;
          toast(intent==='bind'?'Google 已关联':'登录成功');
          if (!isCurrent || isCurrent()) {if(AppState.currentView==='subscription')render();onSuccess?.();}
          return;
        }
        if (poll?.status === 'device_limit') {
          resolved=true;
          deviceManagementPending = true;
          toast('设备数量已达上限，请解绑旧设备后继续登录'); render(); return;
        }
        if (!poll || ['invalid', 'bad_license','account_link_required','denied','cancelled','expired','error'].includes(poll.status)) {
          toast(poll?.error || '登录未完成，请重试', 'error'); return;
        }
      }
      if (valid()) toast('登录超时，请重试', 'error');
    } catch (e) { if (valid()) toast('登录失败：' + (e?.message || e), 'error'); }
    finally {
      if(method==='web' && !resolved)try{await AppApi.webLoginCancel();}catch(_){}
      if(webMask?.isConnected)closeModal();
      if(method==='google' && !resolved)try{await AppApi.googleLoginCancel();}catch(_){}
      actionBusy.delete('login'); refreshActions(); emailLoginState.refresh();
    }
  }

  async function syncLocalAccount({keepAuth = false} = {}) {
    const key = accountKey(), revision = authRevision, cosmetics = profileRevision;
    try {
      const settings = await AppApi.settings();
      if (key !== accountKey() || revision !== authRevision || cosmetics !== profileRevision) return;
      if (!settings || typeof settings !== 'object') return;
      if (!window.AccountAccess.accepts(settings.account_access)) return;
      for (const key of ['account_login_token', 'account_display_name', 'account_user_id', 'account_avatar']) {
        AppState.settings[key] = settings[key] || '';
      }
      window.AccountAccess.apply(settings.account_access);
      if (!AppState.settings.account_login_token) { if (!keepAuth) AccountAuth.reset(emailLoginState); AccountProfile.close(); }
    } catch (_) { /* Next visit can retry synchronization without changing the account locally. */ }
  }

  function openLogin({intent='login', trigger, onSuccess, onDeviceLimit, isCurrent} = {}) {
    if (deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile')) return false;
    const purpose = intent === 'bind' ? 'bind' : 'login';
    if (purpose === 'bind' && !s().account_login_token) return false;
    const sourceView = AppState.currentView;
    const current = typeof isCurrent === 'function' ? isCurrent : () => AppState.currentView === sourceView;
    const server = serviceKey();
    if (document.getElementById('accountAuthModal')) { AccountAuth.refresh(); return true; }
    // Reopening a failed check retries it; the current entry check remains single-flight.
    if (sourceView === 'subscription') {
      if (!emailCapabilities.checking && (emailCapabilities.key !== server || emailCapabilities.methods.email !== true)) retryLoginService();
    } else {
      const revision = ++emailCapabilities.revision;
      emailCapabilities.key = server; emailCapabilities.methods = {}; emailCapabilities.checking = !!server;
      const request = server ? AppApi.checkSubscriptionService() : AppApi.subscriptionContract();
      request.then(contract => {
        if (revision !== emailCapabilities.revision || server !== serviceKey() || !current()) return;
        PublicTerms.observe(contract);
        emailCapabilities.methods = contract?.login_methods || {}; emailCapabilities.checking = false;
        AccountAuth.refresh();
      }).catch(() => {
        if (revision !== emailCapabilities.revision || server !== serviceKey() || !current()) return;
        emailCapabilities.methods = {}; emailCapabilities.checking = false; AccountAuth.refresh();
      });
    }
    emailLoginState.refresh = () => AccountAuth.refresh();
    AccountAuth.open({
      state:emailLoginState, intent:purpose, trigger,
      cooldownKey:() => serviceKey(),
      isReady:() => !!serviceKey() && emailCapabilities.key === serviceKey() && emailCapabilities.methods.email === true,
      isBlocked:() => deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile'),
      getUnavailableHint:() => emailCapabilities.checking ? '正在检查登录服务，可先填写邮箱' : '邮箱登录暂未开放，请检查服务配置',
      onBusy:() => refreshActions(),
      onSend:async email => {
        const revision = authRevision, key = accountKey();
        const result = await AppApi.emailLoginStart(email);
        return revision === authRevision && key === accountKey() ? result : {stale:true};
      },
      onVerify:async (email, code, requestedPurpose) => {
        const revision = authRevision, key = accountKey(), server = serviceKey();
        const result = await AppApi.emailLoginVerify(email, code, requestedPurpose);
        if (revision !== authRevision || key !== accountKey()) return {stale:true};
        if (result?.ok === true) {
          await syncLocalAccount({keepAuth:true});
          if (revision !== authRevision || server !== serviceKey()) return {stale:true};
          if (!s().account_login_token || (result.login_token && result.login_token !== s().account_login_token)) {
            return {ok:false,error:'登录状态尚未同步，请重试'};
          }
          const completedKey = accountKey();
          deviceManagementPending = false; AccountAuth.close();
          try {
            const status = await AppApi.subscriptionStatus();
            if (revision === authRevision && completedKey === accountKey()) AppState.subscription = status;
          } catch (_) { /* A failed status read cannot grant or replace signed entitlements. */ }
          if (revision !== authRevision || completedKey !== accountKey()) return {stale:true};
          if (current()) {
            toast(requestedPurpose === 'bind' ? '邮箱已绑定' : '登录成功');
            render(); onSuccess?.(result.account);
          }
        } else if (result?.status === 'device_limit') {
          deviceManagementPending = true; AccountAuth.close();
          if (current()) {
            toast('设备数量已达上限，请解绑旧设备后继续登录');
            if (onDeviceLimit) onDeviceLimit();
            else if (sourceView !== 'subscription') switchView('subscription');
            else render();
          }
        }
        return result;
      },
    });
    return true;
  }

  function planCard({ name, audience, periodControl, price, selectedQuota, annual, alternateLabel = '年付', note, current, actionLabel, onAction, disabled, disabledTitle }) {
    const card = document.createElement('div');
    card.className = 'plan-card' + (current ? ' is-current' : '');
    const head = document.createElement('div');
    head.className = 'plan-head';
    const title = document.createElement('span');
    title.className = 'plan-name';
    title.textContent = name;
    head.appendChild(title);
    if (current) {
      const badge = document.createElement('span');
      badge.className = 'plan-current-badge';
      badge.textContent = '当前方案';
      head.appendChild(badge);
    }
    card.appendChild(head);
    if (audience) {
      const who = document.createElement('div');
      who.className = 'plan-audience';
      who.textContent = audience;
      card.appendChild(who);
    }
    if (periodControl) card.appendChild(periodControl);
    const priceEl = document.createElement('div');
    priceEl.className = 'plan-price';
    priceEl.textContent = price;
    card.appendChild(priceEl);
    if (selectedQuota) {
      const quota = document.createElement('div'); quota.className = 'plan-selected-quota';
      quota.textContent = selectedQuota; quota.setAttribute('role', 'status'); card.appendChild(quota);
    }
    if (annual) {
      const annualEl = document.createElement('div');
      annualEl.className = 'plan-annual';
      annualEl.textContent = annual === '长期免费' || !alternateLabel ? annual : alternateLabel + ' ' + annual;
      card.appendChild(annualEl);
    }
    if (note) {
      const noteEl = document.createElement('div');
      noteEl.className = 'plan-quota-note';
      noteEl.textContent = note;
      card.appendChild(noteEl);
    }
    const btn = document.createElement('button');
    btn.className = 'btn ' + (current ? 'small' : 'primary small');
    btn.textContent = actionLabel;
    btn.disabled = !!disabled;
    if (disabledTitle) btn.title = disabledTitle;
    if (onAction) btn.onclick = onAction;
    card.appendChild(btn);
    return card;
  }

  function buildSubscription() {
    const epoch = viewEpoch;
    const currentView = () => epoch === viewEpoch && AppState.currentView === 'subscription';
    const pane = document.createElement('div');
    const accountSummary = document.createElement('div'); accountSummary.className = 'account-summary'; accountSummary.id = 'accountSummary';
    const status = document.createElement('span'); status.textContent = '云端积分';
    const balance = document.createElement('span');
    const expiry = document.createElement('span');
    accountSummary.append(status, balance, expiry);
    // 套餐到期预警横幅（先生 2026-10-14）：距到期不足 3 天或已过期时置顶
    // 显示；软件使用权与云端套餐分别判断，内测不限量时不催促续费。
    const expiryNotice = document.createElement('div');
    expiryNotice.className = 'set-hint subscription-review'; expiryNotice.id = 'planExpiryNotice';
    expiryNotice.setAttribute('role', 'status'); expiryNotice.hidden = true;
    const reviewNotice = document.createElement('div');
    reviewNotice.className = 'set-hint subscription-review'; reviewNotice.id = 'orderReviewNotice';
    reviewNotice.setAttribute('role', 'status'); reviewNotice.hidden = true;
    let accountReviewPaused = false, accountRecordedBalance = null;
    const refreshSummary = () => {
      clearTimeout(betaExpiryTimer);
      const paused = active.order_review?.paused === true || accountReviewPaused;
      reviewNotice.hidden = !paused;
      reviewNotice.textContent = paused ? '订单权益正在复核，托管已暂停，积分记录保留，请联系支持' : '';
      const unlimited = !paused && betaEnabled(active);
      const expiryInfo = (!paused && !unlimited) ? active.plan_expiry_notice : null;
      expiryNotice.hidden = !expiryInfo;
      expiryNotice.textContent = expiryInfo
        ? (expiryInfo.expired
            ? `云端套餐已到期（${expiryInfo.date}），托管整理已暂停；可在下方续购。软件使用权单独计算。`
            : `云端套餐将在${expiryInfo.days === 0 ? ' 24 小时内' : ' ' + expiryInfo.days + ' 天后'}（${expiryInfo.date}）到期，届时托管整理暂停；可在下方续购。软件使用权单独计算。`)
        : '';
      status.textContent = paused ? '积分权益复核中' : active.active && active.plan ? active.plan.name + '（云端积分）' : '云端积分';
      const credits = paused ? accountRecordedBalance ?? active.credit_balance ?? 0 : active.credit_balance ?? active.agent_usage_remaining ?? 0;
      balance.textContent = unlimited ? '余额 ∞ 积分' : s().account_login_token || paused ? (paused ? '保留 ' : '余额 ') + credits.toLocaleString('zh-CN') + ' 积分' : '';
      balance.classList.toggle('is-unlimited', unlimited);
      if(unlimited)balance.setAttribute('aria-label','内测积分不限量');else balance.removeAttribute('aria-label');
      expiry.textContent = unlimited ? '内测已启用' : active.active && active.expires_at ? '至 ' + String(active.expires_at).slice(0, 10) : '';
      refreshBetaCard();
      const expires = Date.parse(active.beta_access?.expires_at || '');
      if (unlimited && Number.isFinite(expires)) betaExpiryTimer=setTimeout(()=>{if(currentView())refreshSummary();},Math.min(expires-Date.now()+1,2147483647));
    };
    const statusAccount = accountKey(), statusAuthRevision = authRevision, statusAccessRevision = s().account_access?.revision;
    AppApi.subscriptionStatus().then((st) => {
      if (!currentView() || statusAccount !== accountKey() || statusAuthRevision !== authRevision || statusAccessRevision !== s().account_access?.revision) return;
      active = st || { active: false, plan_id: 'free' };
      refreshSummary();
      renderCards();
    }).catch(() => { if (currentView()) status.textContent = '暂无法读取'; });

    const betaDetails = document.createElement('details'); betaDetails.id = 'betaCardDetails'; betaDetails.className = 'beta-card-details';
    const betaSummary = document.createElement('summary'); betaSummary.textContent = '兑换码（内测卡）';
    betaDetails.open = !!s().account_login_token;
    const betaForm = document.createElement('form'); betaForm.className = 'beta-card-form';
    const betaInput = document.createElement('input'); betaInput.id = 'betaCardCode'; betaInput.type = 'password';
    betaInput.className = 'ctrl'; betaInput.placeholder = '输入卡密'; betaInput.setAttribute('aria-label','内测卡密');
    betaInput.autocomplete = 'off'; betaInput.spellcheck = false; betaInput.maxLength = 96;
    const betaSubmit = document.createElement('button'); betaSubmit.type = 'submit'; betaSubmit.id = 'redeemBetaCardBtn';
    betaSubmit.className = 'btn small'; betaSubmit.textContent = '兑换';
    const betaMessage = document.createElement('p'); betaMessage.id = 'betaCardMessage'; betaMessage.className = 'set-hint';
    betaMessage.setAttribute('role','status');
    betaInput.setAttribute('aria-describedby','betaCardMessage');
    const betaBalance = document.createElement('p'); betaBalance.id = 'betaRecordedBalance'; betaBalance.className = 'set-hint'; betaBalance.hidden = true;
    const refreshBetaCard = () => {
      const enabled = betaEnabled(active) && active.order_review?.paused !== true && !accountReviewPaused;
      betaSubmit.disabled = !s().account_login_token || !window.AccountAccess.allowed() || actionBusy.has('beta_redeem') || actionBusy.has('logout');
      betaInput.disabled = actionBusy.has('beta_redeem') || actionBusy.has('logout');
      betaSubmit.textContent = actionBusy.has('beta_redeem') ? '正在兑换…' : '兑换';
      betaSummary.textContent = enabled ? '内测已启用' : '兑换码（内测卡）';
      betaBalance.hidden = !enabled;
      betaBalance.textContent = enabled ? '原有余额 ' + (active.credit_balance ?? 0).toLocaleString('zh-CN') + ' 积分' : '';
      if (!s().account_login_token) {betaInput.value='';betaMessage.textContent='请先登录';}
    };
    let betaConsentPending = false;
    betaForm.onsubmit = async event => {
      event.preventDefault();
      if (betaSubmit.disabled || actionBusy.has('beta_redeem') || betaConsentPending) return;
      const card = betaInput.value.trim();
      if (!/^sjbeta_[a-f0-9]{64}$/.test(card)) {betaInput.setAttribute('aria-invalid','true');betaMessage.textContent='请输入完整的内测卡密';return;}
      const requestAccountBeforeConsent = accountKey();
      betaConsentPending = true;
      try {if (!await PublicTerms.ensureAccepted() || requestAccountBeforeConsent !== accountKey() || !currentView() || !window.AccountAccess.allowed()) return;}
      finally {betaConsentPending = false;}
      betaInput.removeAttribute('aria-invalid'); actionBusy.add('beta_redeem'); refreshBetaCard();
      const requestAccount = accountKey(), revision = authRevision;
      const valid = () => currentView() && revision === authRevision && requestAccount === accountKey() && window.AccountAccess.allowed();
      betaMessage.textContent = '正在确认内测权益…';
      try {
        const result = await AppApi.redeemBetaCard(card);
        if (!valid()) return;
        betaInput.value = '';
        if (result?.ok !== true) {betaMessage.textContent = String(result?.error || '兑换未完成，请重试').replace(/sjbeta_[a-z0-9]+/gi,'[卡密]').slice(0,180);return;}
        await syncLocalAccount();
        if (!valid()) return;
        const statusRevision=s().account_access?.revision;
        const verified = await AppApi.subscriptionStatus();
        if (!valid()) return;
        if(statusRevision!==s().account_access?.revision){betaMessage.textContent='权益已更新，请刷新';return;}
        active=verified || active;AppState.subscription=active;refreshSummary();renderCards();
        betaMessage.textContent = betaEnabled(active) ? '内测权益已启用' : '兑换已提交，权益尚未同步，请刷新';
      } catch (_) {if(valid()){betaInput.value='';betaMessage.textContent='暂时无法兑换，请重试';}}
      finally {actionBusy.delete('beta_redeem');refreshActions();}
    };
    betaInput.addEventListener('input',()=>{betaInput.removeAttribute('aria-invalid');betaMessage.textContent='';});
    const betaHint = document.createElement('p');betaHint.className='set-hint';betaHint.textContent='兑换后托管积分不限量，原余额保留；内测可撤销，并发与费用保护仍有效。';
    betaForm.append(betaInput,betaSubmit);betaDetails.append(betaSummary,betaHint,betaForm,betaMessage,betaBalance);pane.append(betaDetails);
    PublicTerms.notice(pane);
    const tiers = group('云端积分');
    tiers.id = 'subscriptionPlansGroup';
    const hint = document.createElement('div');
    hint.className = 'set-hint';
    hint.textContent = '购买后按所选套餐获得积分，不自动续费。';
    const examples = document.createElement('div');
    examples.className = 'model-guide';
    examples.id = 'quotaExamples';
    examples.hidden = true;
    examples.textContent = '';
    const helpGroup = group(''); helpGroup.classList.add('subscription-help');
    const helpDetails = document.createElement('details'); helpDetails.id = 'subscriptionHelp';
    helpDetails.open = toolExpansions.get('subscriptionHelp') === true;
    helpDetails.ontoggle = () => { if (currentView()) toolExpansions.set('subscriptionHelp', helpDetails.open); };
    const helpSummary = document.createElement('summary'); helpSummary.textContent = '订阅帮助';
    const quotaPolicy = document.createElement('p'); quotaPolicy.id = 'quotaPolicy'; quotaPolicy.className = 'plan-quota-note';
    const useHelp = document.createElement('p'); useHelp.className = 'set-hint';
    useHelp.textContent = '本地转写只需登录，不消耗云端积分；云端正文与标题整理按套餐积分分别计量。自带密钥由服务商计费，不消耗 FoilPage 积分。';
    helpDetails.append(helpSummary, hint, quotaPolicy, examples, useHelp); helpGroup.appendChild(helpDetails);
    const grid = document.createElement('div');
    const capabilityNotice = document.createElement('p'); capabilityNotice.id = 'subscriptionServiceStatus';
    capabilityNotice.className = 'subscription-capability-status'; capabilityNotice.setAttribute('role', 'status');
    tiers.appendChild(capabilityNotice);
    grid.className = 'plan-grid plan-comparison-scroll';
    grid.setAttribute('role', 'region');
    grid.setAttribute('aria-label', '横向比较自带服务、标准云端积分和专业云端积分');
    grid.tabIndex = 0;
    tiers.appendChild(grid);
    const featureToggle = document.createElement('button'); featureToggle.type = 'button';
    featureToggle.className = 'plan-feature-toggle'; featureToggle.id = 'subscriptionFeatureToggle';
    featureToggle.setAttribute('aria-controls', 'subscriptionFeatureRows');
    featureToggle.onclick = () => { featuresExpanded = !featuresExpanded; renderCards(); featureToggle.focus({preventScroll:true}); };
    tiers.appendChild(featureToggle);
    pane.appendChild(tiers);
    const manualPaymentEntry = document.createElement('div'); manualPaymentEntry.hidden = true;
    pane.appendChild(manualPaymentEntry);
    const alipayPaymentEntry = document.createElement('div'); alipayPaymentEntry.hidden = true;
    pane.appendChild(alipayPaymentEntry);

    const mountedTrust = trustKey();
    const paymentCacheKey = () => trustKey() + '\u0000' + accountKey() + '\u0000' + String(s().account_user_id || '') + '\u0000' + String(s().account_access?.revision);
    const cachedPayment = confirmedAlipay.get(paymentCacheKey());
    let serviceContract = confirmedContracts.get(mountedTrust) || { checkout_ready: false, available_plans: [], public_catalog: [], login_methods: {} };
    let servicePhase = serviceKey() ? confirmedContracts.has(mountedTrust) ? 'refreshing' : 'loading' : 'unconfigured';
    let paymentPhase = cachedPayment ? 'ready' : s().account_login_token ? 'loading' : 'signedout';
    let alipayConfig = cachedPayment || null, alipayContextCurrent = () => false, openAlipayOffer = null, recheckPrivatePayment = () => {}, pendingPaymentMount = null;
    const mountedPaymentKey = paymentCacheKey();
    alipayContextCurrent = () => currentView() && mountedTrust === trustKey() && mountedPaymentKey === paymentCacheKey()
      && !!s().account_login_token && window.AccountAccess.allowed();
    if (cachedPayment) openAlipayOffer = AlipayPayments.mount(alipayPaymentEntry, cachedPayment, {
      isCurrent:alipayContextCurrent,
      onSynced:status=>{if(alipayContextCurrent()){active=status;AppState.subscription=status;refreshSummary();renderCards();}},
      onConfig:config=>{if(alipayContextCurrent()){alipayConfig=config;confirmedAlipay.set(mountedPaymentKey,config);renderCards();}},
    });
    let refreshBuyerControls = () => {};
    const buyerRequired = () => serviceContract.buyer_verification?.required !== false;
    const buyerVerified = () => buyerState.key === accountKey() && buyerState.verified === true;
    const canRedeem = () => !buyerRequired() || (!!s().account_login_token && buyerVerified()
      && serviceContract.payments?.redeem === true);
    let contractRevision = 0, alipayConfigRevision = 0;
    let serviceChecking = !!s().subscription_server_url;
    ++emailCapabilities.revision;
    emailCapabilities.key = serviceKey(); emailCapabilities.methods = serviceContract.login_methods || {}; emailCapabilities.checking = serviceChecking;
    const refreshEmailControls = () => emailLoginState.refresh();
    const loginMethodReady = method => (serviceContract.login_methods || {})[method] === true;
    const afdianOffer = (planId, months) => {
      if (mountedTrust !== trustKey()) return null;
      const catalog = Array.isArray(serviceContract.public_catalog) ? serviceContract.public_catalog : [];
      const entries = catalog.filter(item => item && item.id === planId);
      if (entries.length !== 1 || entries[0].status !== 'ready' || entries[0].purchasable !== true) return null;
      const offers = Array.isArray(entries[0].offers) ? entries[0].offers : [];
      const matches = offers.filter(offer => offer && offer.months === months);
      if (matches.length !== 1) return null;
      const offer = matches[0];
      const period = months === 1 ? 'month' : 'year';
      if (offer.period !== period || offer.status !== 'ready' || offer.purchasable !== true || offer.currency !== 'CNY'
          || typeof offer.amount !== 'string' || !/^\d+(?:\.\d{1,2})?$/.test(offer.amount)
          || !Number.isFinite(Number(offer.amount)) || Number(offer.amount) <= 0) return null;
      return offer;
    };
    const alipayOffer = (planId, months) => alipayContextCurrent()
      ? AlipayPayments.offer(alipayConfig, planId, months === 1 ? 'month' : 'year') : null;
    const configuredOffer = (planId, months) => months===1 ? alipayOffer(planId, months) || afdianOffer(planId, months) : null;
    const displayOffer = (planId, months) => months===1 ? configuredOffer(planId, months) || (alipayContextCurrent()
      ? AlipayPayments.displayOffer(alipayConfig,planId,'month') : null) : null;
    const limitReason = () => '当前支付宝单笔收款上限 ¥' + (alipayConfig.max_order_amount_minor/100)
      .toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
    const offerPrice = offer => '¥' + Number(offer.amount).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
      + (offer.months === 1 ? '/月' : '/年');
    const draftPrice = value => {
      const match = /^¥(\d+(?:\.\d{1,2})?)\/(月|年)$/.exec(String(value || ''));
      return match ? '¥' + Number(match[1]).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '/' + match[2] : '金额待配置';
    };
    const annualDiscount = (monthly, yearly) => monthly && yearly
      && Math.round(Number(yearly.amount) * 100) === Math.round(Number(monthly.amount) * 100 * 12 * .8)
      ? '（八折）' : '';
    let active = { active: false, plan_id: 'free' };
    let lastPlans = [];

    const renderCards = () => {
      if (!currentView()) return;
      capabilityNotice.dataset.state = servicePhase;
      capabilityNotice.hidden = servicePhase === 'ready' && paymentPhase !== 'loading' && paymentPhase !== 'error';
      capabilityNotice.textContent = servicePhase === 'unconfigured' ? '请先连接服务' : servicePhase === 'error' || paymentPhase === 'error'
        ? '连接暂不可用，请重试' : servicePhase === 'refreshing' ? '正在更新服务状态…' : '正在连接服务…';
      const alipayEntryButton=alipayPaymentEntry.querySelector('button');
      if(alipayEntryButton)alipayEntryButton.disabled=servicePhase==='error';
      const comparisonScroll = grid.scrollLeft;
      grid.textContent = '';
      const table = document.createElement('table');
      table.className = 'plan-comparison';
      const caption = document.createElement('caption'); caption.className = 'sr-only';
      caption.textContent = 'FoilPage 套餐价格、积分与功能对比'; table.appendChild(caption);
      const thead = document.createElement('thead'), header = document.createElement('tr');
      const label = document.createElement('th'); label.scope = 'col'; label.className = 'plan-feature-heading';
      label.textContent = '功能与额度'; header.appendChild(label);
      const appendPlan = (card, id, current) => {
        const cell = document.createElement('th'); cell.scope = 'col'; cell.dataset.planId = id;
        if (current) cell.className = 'is-current';
        cell.appendChild(card); header.appendChild(cell);
      };
      appendPlan(planCard({
        name: '自带服务',
        price: '按服务商计费',
        alternateLabel: '',
        current: !active.active,
        actionLabel: '不消耗 FoilPage 积分',
        disabled: true,
      }), 'free', !active.active);
      lastPlans.forEach((p) => {
        const isCurrent = active.active && active.plan_id === p.id;
        const monthly = configuredOffer(p.id, 1);
        const yearly = configuredOffer(p.id, 12);
        const monthlyDisplay=displayOffer(p.id,1),yearlyDisplay=displayOffer(p.id,12);
        const hasConfirmedOffer = !!(monthly || yearly);
        const hasDisplayPrice=!!(monthlyDisplay || yearlyDisplay);
        const selectionKey = serviceKey() + '\u0000' + p.id;
        if (hasDisplayPrice && !planPeriodSelections.has(selectionKey)) planPeriodSelections.set(selectionKey, monthlyDisplay ? 'month' : 'year');
        const selectedPeriod = 'month';
        planPeriodSelections.set(selectionKey,'month');
        const primaryOffer = selectedPeriod === 'year' ? yearly : monthly;
        const primaryDisplay=selectedPeriod==='year'?yearlyDisplay:monthlyDisplay;
        const confirmedCredits = primaryOffer && Number.isSafeInteger(primaryOffer.credits) && primaryOffer.credits > 0;
        const alipayQuote = alipayOffer(p.id, selectedPeriod === 'year' ? 12 : 1);
        const alipayReady = !!alipayQuote && typeof openAlipayOffer === 'function';
        const checkoutReady = alipayReady || !!primaryOffer && confirmedCredits && serviceContract.checkout_ready === true
          && Array.isArray(serviceContract.available_plans) && serviceContract.available_plans.includes(p.id);
        const unknown = servicePhase === 'loading' || (!checkoutReady && paymentPhase === 'loading' && !['error','unconfigured'].includes(servicePhase));
        const disconnected = servicePhase === 'error' || (!checkoutReady && paymentPhase === 'error');
        let periodControl;
        if (hasConfirmedOffer && AppState.releaseChannel==='test') {
          const select = document.createElement('select'); select.className = 'ctrl plan-period'; select.id = 'planPeriod-' + p.id;
          select.setAttribute('aria-label', p.name + '购买周期');
          for (const [period, label, offer] of [['month', '月付', monthly], ['year', '年付', null]]) {
            const option = document.createElement('option'); option.value = period;
            const ready = !!offer && Number.isSafeInteger(offer.credits) && offer.credits > 0;
            const blocked=displayOffer(p.id,period==='month'?1:12)?.status==='merchant_limit';
            option.disabled = !ready; option.textContent = period==='year' ? '年付（敬请期待）' : label + (ready ? '' : blocked ? '（'+limitReason()+'）' : '（暂未开放）'); select.appendChild(option);
          }
          select.value = selectedPeriod; select.disabled = actionBusy.has('checkout')
            || ![monthly, yearly].some(offer => offer && Number.isSafeInteger(offer.credits) && offer.credits > 0);
          select.onchange = () => {
            const offer = select.value === 'year' ? yearly : select.value === 'month' ? monthly : null;
            if (select.value==='month' && offer && Number.isSafeInteger(offer.credits) && offer.credits > 0) planPeriodSelections.set(selectionKey, select.value);
            renderCards(); document.getElementById(select.id)?.focus({preventScroll:true});
          };
          periodControl = select;
        }
        const alternateOffer = selectedPeriod === 'year' ? monthlyDisplay : yearlyDisplay;
        const alternate = alternateOffer ? offerPrice(alternateOffer)
          + (selectedPeriod === 'month' ? annualDiscount(monthlyDisplay, yearlyDisplay) : '')
          + (Number.isSafeInteger(alternateOffer.credits) && alternateOffer.credits > 0 ? '，' + alternateOffer.credits.toLocaleString('zh-CN') + ' 积分/次' : '，积分额度待确认') : '';
        const blockedReason=primaryDisplay?.status==='merchant_limit'?limitReason():alternateOffer?.status==='merchant_limit'
          ? (selectedPeriod==='year'?'月付':'年付')+'超过'+limitReason():'';
        appendPlan(planCard({
          name: p.name,
          audience: '',
          periodControl,
          price: primaryDisplay ? offerPrice(primaryDisplay) : hasDisplayPrice ? (selectedPeriod === 'year' ? '年付暂未开放' : '月付暂未开放') : draftPrice(p.price),
          selectedQuota: primaryDisplay ? Number.isSafeInteger(primaryDisplay.credits) && primaryDisplay.credits>0 ? '本次 ' + primaryDisplay.credits.toLocaleString('zh-CN') + ' 积分' : '积分额度待确认' : '',
          annual: AppState.releaseChannel==='test' ? '敬请期待' : '',
          alternateLabel: hasDisplayPrice && selectedPeriod === 'year' ? '月付' : '年付',
          note: blockedReason,
          current: isCurrent,
          actionLabel: disconnected ? '连接暂不可用' : alipayReady ? (isCurrent ? '支付宝续购' : '支付宝支付') : checkoutReady ? (isCurrent ? '续购' : '选择') : unknown ? '正在连接…' : servicePhase === 'unconfigured' ? '配置服务' : '暂未开放',
          disabled: !checkoutReady || disconnected || actionBusy.has("checkout"),
          disabledTitle: disconnected ? '请检查连接后重试' : unknown ? '正在确认服务与当前账户的购买能力' : checkoutReady ? '' : primaryDisplay?.status==='merchant_limit' ? blockedReason : (!primaryOffer ? hasConfirmedOffer ? '所选周期尚未开放，请选择服务已确认的周期' : '购买暂未开放'
            : (!confirmedCredits ? '积分额度待确认，暂未开放购买' : '购买暂未开放，请检查服务配置')),
          onAction: !checkoutReady ? null : async () => {
            if (actionBusy.has('checkout')) return;
            if (alipayReady && (!alipayContextCurrent() || !AlipayPayments.offer(alipayConfig,p.id,selectedPeriod))) {renderCards();return;}
            actionBusy.add('checkout'); renderCards();
            try {
              if (alipayReady) {await openAlipayOffer(alipayQuote);return;}
              const consentAccount=accountKey();
              if(!await PublicTerms.ensureAccepted() || consentAccount!==accountKey() || !currentView())return;
              const res = await AppApi.beginSubscription(p.id, selectedPeriod);
              if (res?.checkout_url) window.open(res.checkout_url, '_blank', 'noopener');
              else toast(res?.error || res?.message || '订阅暂不可用', 'error');
            } catch (_) { toast('订阅暂不可用', 'error'); }
            finally { actionBusy.delete('checkout'); refreshActions(); }
          },
        }), p.id, isCurrent);
      });
      thead.appendChild(header); table.appendChild(thead);
      const body = document.createElement('tbody');
      const columns = [{id:'free', entitlements:['transcribe', 'byok_agent', 'local_batch'], max_concurrency:1}, ...lastPlans];
      const creditsFor = (p, months) => {
        if (p.id === 'free') return '—';
        const offer = displayOffer(p.id, months);
        if (offer) return Number.isSafeInteger(offer.credits) && offer.credits > 0 ? offer.credits.toLocaleString('zh-CN') + ' 积分/次' : '积分额度待确认';
        if (configuredOffer(p.id, 1) || configuredOffer(p.id, 12)) return '暂未配置';
        const units = p.monthly_agent_units;
        return Number.isSafeInteger(units) && units > 0 ? (units * months).toLocaleString('zh-CN') + ' 积分/次' : '积分额度待确认';
      };
      const has = (p, entitlement) => Array.isArray(p.entitlements) && p.entitlements.includes(entitlement);
      const coreRows = [
        ['月付积分', p => creditsFor(p, 1)],
        ['转写并发', p => Number.isSafeInteger(p.max_concurrency) && p.max_concurrency > 0 ? p.max_concurrency + ' 路并发转写' : '—'],
        ['本地转写与自带密钥整理', p => has(p, 'transcribe') && has(p, 'byok_agent')],
        ['FoilPage 托管正文与标题', p => has(p, 'hosted_agent')],
        ['视觉校对与批量整理（自带密钥）', p => has(p, 'byok_agent')],
      ];
      const detailRows = [
        ['视频封面、播放与文件位置', () => true],
        ['后台队列与进度恢复', () => true],
        ['本地媒体导入', () => true],
        ['本地模型转写', p => has(p, 'transcribe')],
        ['链接与批量下载', p => has(p, 'local_batch')],
        ['文本编辑与导出', () => true],
        ['自带密钥整理（BYOK）', p => has(p, 'byok_agent')],
        ['正文与标题独立整理', p => has(p, 'byok_agent')],
        ['浏览器插件、配对与随应用更新', () => true],
        ['字幕视觉校对（自带密钥）', p => has(p, 'byok_agent')],
        ['批量整理任务（自带密钥）', p => has(p, 'byok_agent')],
      ];
      const detailBody = document.createElement('tbody'); detailBody.id = 'subscriptionFeatureRows';
      detailBody.hidden = !featuresExpanded; body.className = 'plan-comparison-core';
      featureToggle.textContent = featuresExpanded ? '收起功能明细' : '完整功能明细';
      featureToggle.setAttribute('aria-expanded', String(featuresExpanded));
      for (const [name, value] of [...coreRows, ...detailRows]) {
        const line = document.createElement('tr'), feature = document.createElement('th');
        feature.scope = 'row'; feature.textContent = name; line.appendChild(feature);
        for (const p of columns) {
          const cell = document.createElement('td'); const content = value(p); cell.dataset.planId = p.id;
          if (typeof content === 'boolean' || content === '—') {
            const included = content === true;
            cell.classList.add('plan-feature-value'); cell.dataset.included = String(included);
            const symbol = document.createElement('span'); symbol.className = 'plan-feature-symbol' + (included ? ' is-included' : ''); symbol.setAttribute('aria-hidden', 'true');
            symbol.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (included ? '<path d="m5 12 4 4L19 6"/>' : '<path d="m7 7 10 10M17 7 7 17"/>') + '</svg>';
            const description = document.createElement('span'); description.className = 'sr-only'; description.textContent = included ? '包含' : '不包含';
            cell.append(symbol, description);
          } else cell.textContent = content;
          if ((p.id === 'free' && !active.active) || (active.active && active.plan_id === p.id)) cell.classList.add('is-current');
          const period = name === '月付积分' ? 'month' : name === '年付积分' ? 'year' : '';
          if (period) {
            cell.dataset.period = period;
            if (configuredOffer(p.id, period === 'year' ? 12 : 1) && planPeriodSelections.get(serviceKey() + '\u0000' + p.id) === period) cell.classList.add('is-selected-period');
          }
          line.appendChild(cell);
        }
        (coreRows.some(item => item[0] === name) ? body : detailBody).appendChild(line);
      }
      table.append(body, detailBody); grid.appendChild(table); grid.scrollLeft = comparisonScroll;
    };
    renderCards();

    AppApi.subscriptionPlans().then((items) => {
      if (!currentView()) return;
      lastPlans = items || [];
      const first = lastPlans[0];
      quotaPolicy.textContent = first?.quota_note || '';
      if (first && Array.isArray(first.quota_examples) && first.quota_examples.length) {
        examples.textContent = '额度换算（按实测转写校准）：'
          + first.quota_examples.map((pair) => pair[0] + ' ' + pair[1] + '，' + pair[2]).join('；');
        examples.hidden = false;
      }
      renderCards();
    }).catch(() => {});

    const serviceGroup = group('服务连接');
    serviceGroup.id = 'subscriptionServiceGroup';
    const serviceState = document.createElement('div');
    serviceState.className = 'set-hint';
    serviceState.setAttribute('role', 'status');
    serviceState.textContent = s().subscription_server_url ? '正在检查登录服务…' : '服务尚未配置';
    const connectionState = contract => {
      if (!serviceKey()) return '服务尚未配置';
      if (!contract || (Array.isArray(contract.readiness?.blocking) && contract.readiness.blocking.includes('服务暂时不可达'))) return '暂时无法连接服务，请重试';
      return contract.login_methods?.email === true ? '邮箱登录可用' : '邮箱登录尚未开放';
    };
    const serviceDetails = document.createElement('details');
    serviceDetails.id = 'subscriptionConnectionDetails';
    serviceDetails.className = 'set-advanced';
    serviceDetails.innerHTML = '<summary>连接设置</summary>';
    serviceDetails.appendChild(serviceState);
    serviceDetails.open = connectionExpanded;
    serviceDetails.ontoggle = () => { if (currentView()) connectionExpanded = serviceDetails.open; };
    serviceDetails.appendChild(row('服务地址', '部署后的 HTTPS 地址', connectionInput('subscription_server_url', '服务地址')));
    serviceDetails.appendChild(row('许可证公钥', '服务端生成的 Ed25519 公钥；不填写私钥', connectionInput('subscription_license_public_key', '许可证公钥')));
    serviceDetails.appendChild(row('购买服务地址', '可选；留空使用上方服务地址', connectionInput('subscription_checkout_url', '购买服务地址')));
    serviceGroup.appendChild(serviceDetails);
    const connectionActions = document.createElement('div');
    connectionActions.className = 'set-actions';
    const checkService = document.createElement('button');
    checkService.className = 'btn small';
    checkService.textContent = '检查连接';
    checkService.disabled = !s().subscription_server_url || actionBusy.has('check');
    const applyServiceContract = c => {
      if (!currentView() || mountedTrust !== trustKey()) return;
      serviceChecking = false;
      const unreachable = !c || c.readiness?.blocking?.includes('服务暂时不可达') || (c.ok === false && !c.login_methods);
      servicePhase = unreachable ? 'error' : 'ready';
      if (!unreachable) {PublicTerms.observe(c);serviceContract = c; confirmedContracts.set(mountedTrust, c);}
      ManualPayments.mount(manualPaymentEntry, serviceContract.manual_payments, {isCurrent:currentView,onSynced:status=>{
        active=status;AppState.subscription=status;refreshSummary();renderCards();
      }});
      // Public health cannot authorize an account in a closed sales test.
      recheckPrivatePayment = () => {
      const paymentRevision = ++alipayConfigRevision;
      const paymentAccount = accountKey() + '\u0000' + String(s().account_user_id || '');
      const paymentAuthRevision = authRevision, paymentAccessRevision = s().account_access?.revision;
      const paymentContextCurrent = () => currentView() && paymentRevision === alipayConfigRevision
        && mountedTrust === trustKey()
        && paymentAccount === accountKey() + '\u0000' + String(s().account_user_id || '')
        && paymentAuthRevision === authRevision
        && !!s().account_login_token && window.AccountAccess?.allowed() === true;
      alipayContextCurrent = paymentContextCurrent;
      const cacheKey = paymentCacheKey();
      alipayConfig = confirmedAlipay.get(cacheKey) || null;
      paymentPhase = alipayConfig ? 'ready' : paymentContextCurrent() ? 'loading' : 'signedout';
      const mountAlipay = config => AlipayPayments.mount(alipayPaymentEntry, config, {isCurrent:paymentContextCurrent,onConfig:latest=>{
        if (!paymentContextCurrent()) return;
        alipayConfig=latest;confirmedAlipay.set(cacheKey,latest);renderCards();
      },onSynced:status=>{
        if (!paymentContextCurrent()) return;
        active=status;AppState.subscription=status;refreshSummary();renderCards();
      }});
      openAlipayOffer = mountAlipay(alipayConfig);
      if (paymentContextCurrent()) AppApi.alipayPaymentConfig().then(config => {
        if (!paymentContextCurrent() || paymentAccessRevision !== s().account_access?.revision) return;
        if (config?.ok !== true) {paymentPhase='error';renderCards();return;}
        paymentPhase='ready';confirmedAlipay.set(cacheKey,config);
        alipayConfig=config;
        if(document.getElementById('alipayPaymentBody'))pendingPaymentMount=()=>{if(paymentContextCurrent()){openAlipayOffer=mountAlipay(config);renderCards();}};
        else openAlipayOffer=mountAlipay(config);
        renderCards();
      }).catch(() => {if(paymentContextCurrent()){paymentPhase='error';renderCards();}});
      };
      // A payment's own verified refresh can advance the same session revision.
      // Never remount (and close) its modal while that refresh is completing.
      if(!document.getElementById('alipayPaymentBody'))recheckPrivatePayment();
      else pendingPaymentMount=()=>recheckPrivatePayment();
      emailCapabilities.key = serviceKey(); emailCapabilities.methods = serviceContract.login_methods || {}; emailCapabilities.checking = false;
      const methods = serviceContract.login_methods || {};
      const wb = pane.querySelector('#wechatLoginBtn');
      const gb = pane.querySelector('#googleLoginBtn');
      if (wb) { wb.disabled = methods.wechat !== true; wb.title = wb.disabled ? '微信登录暂未配置' : ''; }
      if (gb) { gb.disabled = methods.google !== true; gb.title = gb.disabled ? 'Google 登录暂未配置' : ''; }
      refreshEmailControls();
      refreshBuyerControls();
      refreshActions();
      renderCards();
    };
    const initialContractRevision = ++contractRevision;
    const initialContractRequest = s().subscription_server_url ? AppApi.checkSubscriptionService() : AppApi.subscriptionContract();
    initialContractRequest.then(contract => {
      if (initialContractRevision === contractRevision) {
        applyServiceContract(contract);
        if (currentView()) serviceState.textContent = connectionState(contract);
      }
    }).catch(() => {
      if (!currentView() || initialContractRevision !== contractRevision) return;
      applyServiceContract(null);
      serviceState.textContent = connectionState(null);
    });
    checkService.onclick = async () => {
      if (actionBusy.has('check') || !s().subscription_server_url) return;
      actionBusy.add('check');
      servicePhase=confirmedContracts.has(mountedTrust)?'refreshing':'loading';serviceChecking=true;
      const revision = ++contractRevision;
      ++emailCapabilities.revision;
      emailCapabilities.key = serviceKey(); emailCapabilities.checking = true;
      refreshEmailControls();
      refreshActions();
      serviceState.textContent = '正在检查登录服务…';
      try {
        const c = await AppApi.checkSubscriptionService();
        if (!currentView() || revision !== contractRevision) return;
        applyServiceContract(c);
        serviceState.textContent = connectionState(c);
      } catch (_) {
        if (!currentView() || revision !== contractRevision) return;
        applyServiceContract(null); serviceState.textContent = connectionState(null);
      }
      finally { actionBusy.delete('check'); refreshActions(); }
    };
    retryLoginService = () => checkService.onclick();
    const syncBalance = document.createElement('button');
    syncBalance.className = 'btn small';
    syncBalance.textContent = '同步积分';
    syncBalance.disabled = !s().account_login_token || actionBusy.has('sync');
    syncBalance.onclick = async () => {
      if (actionBusy.has('sync') || !s().account_login_token) return;
      actionBusy.add('sync');
      refreshActions();
      try {
        const res = await AppApi.refreshSubscription();
        await syncLocalAccount();
        if (res && res.ok) { toast('积分已同步'); render(); }
        else { toast(res?.error || '同步未完成', 'error'); render(); }
      } catch (_) { serviceState.textContent = '暂时无法同步积分'; }
      finally { actionBusy.delete('sync'); refreshActions(); }
    };
    connectionActions.append(checkService);
    serviceGroup.appendChild(connectionActions);
    pane.appendChild(serviceGroup);

    const accountTools = group('账户操作'); accountTools.id = 'accountToolsGroup';
    const accountActions = document.createElement('div'); accountActions.className = 'set-actions'; accountTools.appendChild(accountActions);
    pane.appendChild(accountTools);

    {
      const accountGroup = group('账户');
      accountGroup.id = 'subscriptionAccountGroup';
      accountGroup.classList.add('subscription-account');
      accountGroup.classList.toggle('is-signed-in', !!s().account_login_token);
      accountGroup.querySelector('h3')?.remove();
      accountGroup.setAttribute('role', 'region'); accountGroup.setAttribute('aria-label', '账户');
      const accountRow = document.createElement('div');
      accountRow.className = 'set-row';
      const accountLeft = document.createElement('div');
      accountLeft.className = 'set-left';
      const accountName = document.createElement('div'); accountName.className = 'account-name'; accountName.id = 'accountName';
      accountName.textContent = s().account_login_token ? s().account_display_name || '已登录' : '';
      const accountState = document.createElement('div');
      accountState.className = 'subscription-sr-only'; accountState.setAttribute('role', 'status');
      accountState.id = 'accountState';
      accountState.textContent = s().account_login_token ? '已登录' + (s().account_display_name ? '：' + s().account_display_name : '') : '未登录';
      accountLeft.append(accountName, accountState, accountSummary);
      const avatar = document.createElement('button'); avatar.className = 'account-avatar'; avatar.id = 'accountAvatarBtn'; avatar.type = 'button';
      avatar.setAttribute('aria-label', '编辑头像与昵称');
      const avatarSource = AccountProfile.safeAvatar(s().account_avatar);
      if (s().account_login_token && avatarSource) {
        const image = document.createElement('img'); image.src = avatarSource; image.alt = ''; avatar.appendChild(image);
      } else avatar.innerHTML = AccountProfile.defaultAvatar;
      avatar.disabled = !s().account_login_token || deviceManagementPending;
      const accountCtrl = document.createElement('div');
      accountCtrl.className = 'set-ctrl';
      accountCtrl.style.gap = '10px';
      const emailLogin = document.createElement('button'); emailLogin.className = 'btn primary small';
      emailLogin.id = 'accountEmailLoginBtn'; emailLogin.textContent = s().account_login_token ? '切换账户' : '登录';
      const bindEmail = document.createElement('button'); bindEmail.className = 'btn small';
      bindEmail.id = 'accountBindEmailBtn'; bindEmail.textContent = '绑定邮箱'; bindEmail.hidden = !s().account_login_token;
      accountCtrl.prepend(emailLogin);
      if (s().account_login_token) accountActions.append(syncBalance, bindEmail);
      const profileButton = document.createElement('button'); profileButton.className = 'btn small';
      profileButton.id = 'accountProfileBtn'; profileButton.textContent = '编辑资料'; profileButton.hidden = !s().account_login_token;
      const openProfile = trigger => {
        if (!s().account_login_token || deviceManagementPending || actionBusy.has('profile') || actionBusy.has('logout') || actionBusy.has('login') || emailLoginState.busy) return;
        const key = accountKey(), revision = authRevision;
        AccountProfile.open({
          trigger, name:s().account_display_name || '', avatar:s().account_avatar || '',
          onSave:async (name, image) => {
            if (key !== accountKey() || revision !== authRevision || !s().account_login_token) return {stale:true};
            actionBusy.add('profile'); refreshActions();
            try {
              const result = await AppApi.updateAccountProfile(name, image);
              if (key !== accountKey() || revision !== authRevision) return {stale:true};
              if (result?.status === 'invalid') {
                await syncLocalAccount();
                if (s().account_login_token) return key === accountKey() ? result : {stale:true};
                authRevision++; AccountProfile.close(); render();
                toast(result.error || '登录已失效，请重新登录', 'error');
                return result;
              }
              if (result?.ok === true) {
                const account = result.account;
                if (!account || typeof account.display_name !== 'string' || !account.display_name.trim()
                    || Array.from(account.display_name.trim()).length > 32 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(account.display_name)
                    || typeof account.avatar !== 'string' || (account.avatar && !AccountProfile.safeAvatar(account.avatar))
                    || (account.user_id && s().account_user_id && account.user_id !== s().account_user_id)) {
                  return {ok:false,error:'账户资料响应无效，请重试'};
                }
                profileRevision++;
                AppState.settings.account_display_name = account.display_name;
                AppState.settings.account_avatar = account.avatar;
                render();
              }
              return result;
            } finally { actionBusy.delete('profile'); refreshActions(); }
          },
        });
      };
      profileButton.onclick = () => openProfile(profileButton); avatar.onclick = () => openProfile(avatar);
      if (s().account_login_token) accountCtrl.prepend(profileButton);
      const loginBtn = document.createElement('button');
      loginBtn.className = 'btn primary small';
      loginBtn.id = 'wechatLoginBtn';
      loginBtn.textContent = s().account_login_token ? '切换账号' : '微信扫码登录';
      loginBtn.disabled = !loginMethodReady('wechat');
      loginBtn.onclick = () => { if (loginMethodReady('wechat') && !loginBtn.disabled) beginLogin('wechat'); };
      accountActions.appendChild(loginBtn);
      const googleBtn = document.createElement('button');
      googleBtn.className = 'btn small';
      googleBtn.id = 'googleLoginBtn';
      googleBtn.textContent = 'Google 登录';
      googleBtn.disabled = !loginMethodReady('google');
      googleBtn.onclick = () => { if (loginMethodReady('google') && !googleBtn.disabled) beginLogin('google'); };
      accountActions.appendChild(googleBtn);
      if (s().account_login_token && loginMethodReady('google')) {
        const bindGoogle=document.createElement('button');bindGoogle.className='btn ghost small';bindGoogle.id='accountBindGoogleBtn';bindGoogle.textContent='关联 Google';
        bindGoogle.onclick=()=>beginLogin('google',{intent:'bind'});accountActions.append(bindGoogle);
      }
      if (s().account_login_token) {
        const logoutBtn = document.createElement('button');
        logoutBtn.className = 'btn ghost small';
        logoutBtn.id = 'accountLogoutBtn';
        logoutBtn.textContent = '退出登录';
        logoutBtn.onclick = async () => {
          if (actionBusy.has('logout') || emailLoginState.busy || actionBusy.has('login') || actionBusy.has('profile')) return;
          actionBusy.add('logout'); authRevision++; refreshActions(); refreshEmailControls();
                let logoutCompleted=false;
          try {
            const result = await AppApi.accountLogout();
            if (result?.ok === false) { toast(result.error || '退出失败', 'error'); return; }
            confirmedAlipay.clear();
            AppState.settings.account_login_token = '';
            AppState.settings.account_display_name = '';
            AppState.settings.account_user_id = '';
            AppState.settings.account_avatar = '';
            logoutCompleted=true;
            window.AccountAccess?.lock();
            AccountAuth.reset(emailLoginState);
            AccountProfile.close();
            deviceManagementPending = false;
            toast('已退出账户'); render();
            document.getElementById('accountEmailLoginBtn')?.focus({preventScroll:true});
          } catch (_) { toast('退出失败，请重试', 'error'); }
          finally { actionBusy.delete('logout'); refreshActions(); refreshEmailControls(); if(!logoutCompleted && currentView())render(); }
        };
        accountCtrl.appendChild(logoutBtn);
      }
      accountRow.append(avatar, accountLeft, accountCtrl);
      accountGroup.appendChild(accountRow);
      accountGroup.appendChild(expiryNotice);
      accountGroup.appendChild(reviewNotice);

      emailLogin.onclick = () => { if (!emailLogin.disabled) openLogin({intent:'login', trigger:emailLogin}); };
      bindEmail.onclick = () => { if (!bindEmail.disabled && s().account_login_token) openLogin({intent:'bind', trigger:bindEmail}); };
      emailLoginState.refresh = () => AccountAuth.refresh();
      pane.prepend(accountGroup);

      const buyerGroup = group('爱发电买家身份'); buyerGroup.id = 'buyerVerificationGroup';
      const buyerHint = document.createElement('div'); buyerHint.className = 'set-hint';
      buyerHint.id = 'buyerVerificationHint'; buyerHint.setAttribute('role', 'status');
      const buyerActions = document.createElement('div'); buyerActions.className = 'set-actions';
      const verifyBuyer = document.createElement('button'); verifyBuyer.className = 'btn primary small';
      verifyBuyer.id = 'verifyBuyerBtn'; verifyBuyer.textContent = '验证爱发电账号';
      const stopBuyer = document.createElement('button'); stopBuyer.className = 'btn ghost small';
      stopBuyer.id = 'cancelBuyerVerificationBtn'; stopBuyer.textContent = '取消验证'; stopBuyer.hidden = true;
      buyerActions.append(verifyBuyer, stopBuyer); buyerGroup.append(buyerHint, buyerActions); pane.appendChild(buyerGroup);
      refreshBuyerControls = () => {
        if (!currentView()) return;
        const configured = serviceContract.buyer_verification?.configured === true;
        buyerGroup.hidden = !buyerRequired() && !configured;
        const verified = buyerVerified();
        verifyBuyer.textContent = verified ? '已验证' : buyerState.busy ? '等待浏览器授权…' : '验证爱发电账号';
        verifyBuyer.disabled = verified || buyerState.busy || !configured || !s().account_login_token || deviceManagementPending
          || actionBusy.has('login') || emailLoginState.busy || actionBusy.has('logout');
        stopBuyer.hidden = !buyerState.busy;
        buyerHint.textContent = !s().account_login_token ? '先登录 FoilPage 账户，再验证购买订单的爱发电账号'
          : verified ? serviceContract.payments?.redeem === true
            ? '买家身份已验证，可兑换该账号的订单；FoilPage 账户保持不变'
            : '买家身份已验证，FoilPage 账户保持不变；支付与订单兑换尚未开放'
          : !configured ? '爱发电身份验证暂未配置，订单兑换尚未开放'
          : buyerState.busy ? '请在浏览器授权购买订单的爱发电账号，完成后自动返回确认'
          : '验证购买订单的爱发电账号后才能兑换。仅绑定买家身份，不合并账户或改动积分。';
      };
      verifyBuyer.onclick = async () => {
        if (verifyBuyer.disabled || buyerState.busy) return;
        buyerState.busy = true; actionBusy.add('buyer'); const generation = ++buyerState.generation;
        const revision = authRevision, key = accountKey();
        const valid = () => generation === buyerState.generation && revision === authRevision && key === accountKey();
        refreshBuyerControls(); refreshActions(); refreshEmailControls();
        try {
          const start = await AppApi.buyerVerificationStart();
          if (!valid()) return;
          if (!start?.ok) { toast(start?.error || '买家验证暂不可用', 'error'); return; }
          const deadline = Date.now() + 300000;
          while (valid() && Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, 2000));
            if (!valid()) return;
            const poll = await AppApi.buyerVerificationPoll();
            if (!valid()) return;
            if (poll?.status === 'verified' && poll.buyer_verified === true) {
              buyerState.key = key; buyerState.verified = true; toast('爱发电身份已验证'); render(); return;
            }
            if (!poll?.ok || poll.status !== 'pending') { toast(poll?.error || '买家身份验证未完成', 'error'); return; }
          }
          if (valid()) toast('验证超时，可重新发起', 'error');
        } catch (_) { if (valid()) toast('买家验证暂时无法完成，请重试', 'error'); }
        finally {
          if (generation === buyerState.generation) { buyerState.busy = false; actionBusy.delete('buyer'); }
          refreshBuyerControls(); refreshActions(); refreshEmailControls();
        }
      };
      stopBuyer.onclick = async () => {
        const generation = ++buyerState.generation;
        const revision = authRevision, key = accountKey();
        buyerState.busy = false; actionBusy.delete('buyer'); refreshBuyerControls(); refreshActions(); refreshEmailControls();
        try {
          const result = await AppApi.buyerVerificationStop();
          if (generation !== buyerState.generation || revision !== authRevision || key !== accountKey()) return;
          if (!result?.ok) { toast(result?.error || '取消验证未确认', 'error'); return; }
          if (result.status === 'verified') { buyerState.key = accountKey(); buyerState.verified = true; toast('爱发电身份已完成验证'); render(); }
          else toast('已取消本次验证');
        } catch (_) { toast('取消验证未确认，请在浏览器取消授权', 'error'); }
      };
      refreshBuyerControls();

      if (s().account_login_token || deviceManagementPending) {
        const devices = group('登录设备');
        devices.id = 'accountDevicesGroup';
        const deviceHint = document.createElement('div');
        deviceHint.className = 'set-hint'; deviceHint.setAttribute('role', 'status');
        deviceHint.id = 'accountDevicesHint'; deviceHint.textContent = '正在读取设备…';
        const deviceList = document.createElement('div'); deviceList.id = 'accountDevicesList';
        const deviceActions = document.createElement('div'); deviceActions.className = 'set-actions';
        const reloadDevices = document.createElement('button');
        reloadDevices.className = 'btn small'; reloadDevices.textContent = '刷新设备';
        reloadDevices.id = 'refreshDevicesBtn';
        const completeLogin = document.createElement('button');
        completeLogin.className = 'btn primary small'; completeLogin.textContent = '继续登录';
        completeLogin.id = 'completeAccountLoginBtn'; completeLogin.hidden = !deviceManagementPending;
        const cancelLogin = document.createElement('button');
        cancelLogin.className = 'btn ghost small'; cancelLogin.textContent = '取消本次登录';
        cancelLogin.id = 'cancelAccountLoginBtn'; cancelLogin.hidden = !deviceManagementPending;
        deviceActions.append(reloadDevices, completeLogin, cancelLogin);
        devices.append(deviceHint, deviceList, deviceActions); pane.appendChild(devices);
        let devicesBusy = false, deviceRevision = 0, deviceMax = 2;
        const accountRevision = authRevision, accountServer = serviceKey();
        const accountValid = () => currentView() && accountRevision === authRevision && accountServer === serviceKey();
        const setDevicesBusy = value => {
          devicesBusy = value;
          for (const button of devices.querySelectorAll('button')) button.disabled = value;
        };
        const showDevices = value => {
          if (!accountValid()) return;
          deviceList.replaceChildren();
          if (!value?.ok) {
            deviceHint.textContent = value?.error || '暂时无法读取设备，请重试';
            return;
          }
          const bound = Array.isArray(value.devices) ? value.devices.filter(item => typeof item === 'string' && item) : [];
          const management = value.device_management_only === true;
          if (!management && (!s().account_user_id || !value.user_id || value.user_id === s().account_user_id)) {
            buyerState.key = accountKey(); buyerState.verified = value.buyer_verified === true;
            accountReviewPaused = value.order_review?.paused === true;
            accountRecordedBalance = Number.isSafeInteger(value.credit_balance) && value.credit_balance >= 0 ? value.credit_balance : null;
            refreshSummary();
            refreshBuyerControls(); refreshActions();
          }
          deviceManagementPending = management;
          completeLogin.hidden = cancelLogin.hidden = !management;
          const max = Number.isSafeInteger(value.max_devices) && value.max_devices > 0 ? value.max_devices : 2;
          deviceMax = max;
          deviceHint.textContent = management ? `已验证身份，已绑定 ${bound.length}/${max} 台设备。解绑旧设备后继续登录。`
            : `已绑定 ${bound.length}/${max} 台设备。解绑会使该设备退出账户。`;
          completeLogin.disabled = devicesBusy || bound.length >= max;
          bound.forEach((id, index) => {
            const local = id === value.current_device_id;
            const label = `设备 ${index + 1}（${id.length > 16 ? id.slice(0, 8) + '…' + id.slice(-4) : id}）`;
            const detail = Array.isArray(value.device_details) ? value.device_details.find(item => item.device_id === id) : null;
            const firstSeen = typeof detail?.first_seen === 'string' && /^\d{4}-\d{2}-\d{2}/.test(detail.first_seen) ? detail.first_seen.slice(0, 10) : '';
            const deviceRow = row(label, (local ? '当前设备' : '其他已登录设备') + (firstSeen ? '（首次登录 ' + firstSeen + '）' : ''), (() => {
              const unbind = document.createElement('button');
              unbind.className = 'btn ghost small'; unbind.textContent = '解绑';
              unbind.setAttribute('aria-label', `解绑${label}`);
              unbind.dataset.deviceId = id;
              unbind.onclick = () => {
                if (devicesBusy || !accountValid()) return;
                const mask = openModal('解绑设备？', '<p id="unbindDeviceMessage"></p>',
                  '<button class="btn ghost" id="dlgCancel">保留设备</button><button class="btn danger" id="confirmUnbindDevice">解绑</button>');
                mask.querySelector('#unbindDeviceMessage').textContent = local
                  ? '解绑当前设备后会退出登录，已有任务和本地文件会保留。'
                  : `解绑${label}后，该设备需要重新验证身份才能登录。`;
                mask.querySelector('#dlgCancel').onclick = closeModal;
                mask.querySelector('#confirmUnbindDevice').onclick = async () => {
                  closeModal();
                  if (!accountValid() || devicesBusy) return;
                  setDevicesBusy(true);
                  try {
                    const result = await AppApi.unbindAccountDevice(id);
                    if (!accountValid()) return;
                    if (!result?.ok) { deviceHint.textContent = result?.error || '解绑未完成，请重试'; return; }
                    toast(local && !management ? '当前设备已解绑' : '设备已解绑');
                    if (local && !management) { await syncLocalAccount(); render(); return; }
                    showDevices(result);
                  } catch (_) { if (accountValid()) deviceHint.textContent = '暂时无法解绑设备，请重试'; }
                  finally { if (accountValid()) { setDevicesBusy(false); completeLogin.disabled = completeLogin.hidden || deviceList.children.length >= max; } }
                };
              };
              return unbind;
            })());
            deviceRow.title = id; deviceList.appendChild(deviceRow);
          });
        };
        const loadDevices = async () => {
          if (devicesBusy || !accountValid()) return;
          setDevicesBusy(true); const revision = ++deviceRevision;
          try {
            const value = await AppApi.accountDevices();
            if (value?.status === 'invalid' && accountValid()) {
              deviceManagementPending = false; await syncLocalAccount(); toast('登录已失效，请重新验证身份', 'error'); render(); return;
            }
            if (revision === deviceRevision) showDevices(value);
          } catch (_) { if (accountValid()) deviceHint.textContent = '暂时无法读取设备，请重试'; }
          finally { if (accountValid()) { setDevicesBusy(false); completeLogin.disabled = completeLogin.hidden || deviceList.children.length >= deviceMax; } }
        };
        reloadDevices.onclick = loadDevices;
        completeLogin.onclick = async () => {
          if (devicesBusy || !accountValid()) return;
          setDevicesBusy(true);
          try {
            const result = await AppApi.completeAccountLogin();
            if (!accountValid()) return;
            if (!result?.ok) { deviceHint.textContent = result?.error || '登录未完成，请刷新设备后重试'; return; }
            deviceManagementPending = false; await syncLocalAccount(); toast('登录成功'); render();
          } catch (_) { if (accountValid()) deviceHint.textContent = '暂时无法完成登录，请重试'; }
          finally { if (accountValid()) setDevicesBusy(false); }
        };
        cancelLogin.onclick = async () => {
          if (devicesBusy || !accountValid()) return;
          setDevicesBusy(true);
          try {
            const result = await AppApi.cancelAccountLogin();
            if (!accountValid()) return;
            if (!result?.ok) { deviceHint.textContent = result?.error || '取消未完成'; return; }
            deviceManagementPending = false; authRevision++; toast('已取消本次登录'); render();
          } catch (_) { if (accountValid()) deviceHint.textContent = '取消未完成，请重试'; }
          finally { if (accountValid()) setDevicesBusy(false); }
        };
        loadDevices();
      }

      const redeem = group('兑换订单');
      redeem.id = 'subscriptionRedeemGroup';
      const redeemHint = document.createElement('div');
      redeemHint.className = 'set-hint';
      redeemHint.textContent = '此处仅核销爱发电云端订阅订单。复制「爱发电 → 我的 → 订单」中的订单号粘贴到这里，核验后云端积分到账并按原方案顺延有效期。';
      helpDetails.appendChild(redeemHint);
      const redeemState = document.createElement('div'); redeemState.className = 'set-hint';
      redeemState.id = 'redeemReadiness'; redeemState.setAttribute('role', 'status'); redeem.appendChild(redeemState);
      const redeemRow = document.createElement('div');
      redeemRow.className = 'set-row';
      const redeemLeft = document.createElement('div');
      redeemLeft.className = 'set-left';
      const redeemLabel = document.createElement('div');
      redeemLabel.className = 'set-label';
      redeemLabel.textContent = '订单号';
      redeemLeft.appendChild(redeemLabel);
      const ctrl = document.createElement('div');
      ctrl.className = 'set-ctrl';
      ctrl.style.gap = '10px';
      const input = document.createElement('input');
      input.className = 'ctrl';
      input.id = 'redeemOrderInput';
      input.dataset.tour = 'redeem-order';
      input.placeholder = '粘贴爱发电订单号';
      input.maxLength = 64;
      input.value = redeemOrderDraft;
      input.oninput = () => { redeemOrderDraft = input.value; };
      const btn = document.createElement('button');
      btn.className = 'btn primary small';
      btn.id = 'redeemOrderBtn';
      btn.textContent = '兑换';
      btn.disabled = !s().subscription_server_url || actionBusy.has('redeem') || !canRedeem();
      ctrl.append(input, btn);
      redeemRow.append(redeemLeft, ctrl);
      redeem.appendChild(redeemRow);
      pane.appendChild(redeem);
      btn.onclick = async () => {
        if (actionBusy.has('redeem') || !s().subscription_server_url || !canRedeem()) return;
        const orderNo = input.value.trim();
        if (!orderNo) { toast('请先粘贴订单号', 'error'); return; }
        actionBusy.add('redeem'); refreshActions();
        try {
          const res = await AppApi.redeemOrder(orderNo);
          if (res && res.ok) {
            await syncLocalAccount();
            redeemOrderDraft = '';
            toast(res.repeated ? '该订单已兑换过，状态已同步' : '兑换成功，积分已到账');
            render();
          } else {
            toast((res && res.error) || '兑换失败', 'error');
          }
        } catch (e) {
          toast('兑换失败：' + (e && e.message || e), 'error');
        } finally {
          actionBusy.delete('redeem'); refreshActions();
        }
      };
      input.onkeydown = (e) => { if (e.key === 'Enter') btn.click(); };
    }
    refreshActions = () => {
      refreshBetaCard();
      if (!currentView()) return;
      checkService.disabled = !s().subscription_server_url || actionBusy.has('check');
      syncBalance.disabled = !s().account_login_token || actionBusy.has('sync');
      refreshBuyerControls();
      for (const [id, method] of [['wechatLoginBtn', 'wechat'], ['googleLoginBtn', 'google']]) {
        const button = pane.querySelector('#' + id);
        if (button) {
          button.hidden = !loginMethodReady(method);
          button.disabled = deviceManagementPending || !loginMethodReady(method) || actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile') || emailLoginState.busy;
        }
      }
      const bindGoogle=pane.querySelector('#accountBindGoogleBtn');
      if(bindGoogle)bindGoogle.disabled=!s().account_login_token || deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile') || emailLoginState.busy;
      const logout = pane.querySelector('#accountLogoutBtn');
      if (logout) logout.disabled = actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile') || emailLoginState.busy;
      const emailLogin = pane.querySelector('#accountEmailLoginBtn');
      if (emailLogin) emailLogin.disabled = deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout')
        || actionBusy.has('profile') || (emailLoginState.busy && emailLoginState.intent !== 'login');
      for (const id of ['accountProfileBtn', 'accountAvatarBtn']) {
        const button = pane.querySelector('#' + id);
        if (button) button.disabled = !s().account_login_token || deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout') || actionBusy.has('profile') || emailLoginState.busy;
      }
      const bindEmail = pane.querySelector('#accountBindEmailBtn');
      if (bindEmail) bindEmail.disabled = !s().account_login_token || deviceManagementPending || actionBusy.has('login') || actionBusy.has('logout')
        || actionBusy.has('profile') || (emailLoginState.busy && emailLoginState.intent !== 'bind');
      const redeem = pane.querySelector('#redeemOrderBtn');
      if (redeem) { redeem.disabled = !s().subscription_server_url || actionBusy.has('redeem') || !canRedeem();
        redeem.title = canRedeem() ? '' : s().account_login_token && buyerVerified()
          ? '支付与订单兑换尚未开放' : '先登录 FoilPage 并验证爱发电买家身份'; }
      const redeemState = pane.querySelector('#redeemReadiness');
      if (redeemState) { redeemState.hidden = !buyerRequired(); redeemState.textContent = canRedeem()
        ? '可兑换当前已验证买家的订单' : s().account_login_token && buyerVerified()
          ? '支付与订单兑换尚未开放' : '先登录 FoilPage 并验证爱发电买家身份'; }
      renderCards();
    };
    refreshActions();
    let observedAccess = [trustKey(),accountKey(),s().account_access?.revision,window.AccountAccess.allowed()].join('\u0000');
    syncState = () => {
      if(!currentView())return;
      if(pendingPaymentMount && !document.getElementById('alipayPaymentBody')){
        const mount=pendingPaymentMount;pendingPaymentMount=null;mount();
      }
      const context=()=>[trustKey(),accountKey(),s().account_access?.revision,window.AccountAccess.allowed()].join('\u0000');
      const key=context();if(key===observedAccess)return;observedAccess=key;
      if(mountedTrust!==trustKey())servicePhase='loading';
      if(!document.getElementById('alipayPaymentBody'))recheckPrivatePayment();
      refreshSummary();refreshActions();
      AppApi.subscriptionStatus().then(status=>{
        if(!currentView() || key!==context())return;
        active=status || {active:false,plan_id:'free'};AppState.subscription=active;refreshSummary();renderCards();
      }).catch(()=>{});
    };
    const tools = group(''); tools.classList.add('subscription-tools');
    const toolsDetails = document.createElement('details'); toolsDetails.id = 'subscriptionTools';
    toolsDetails.open = toolsExpanded;
    toolsDetails.ontoggle = () => { if (currentView()) toolsExpanded = toolsDetails.open; };
    const toolsSummary = document.createElement('summary'); toolsSummary.textContent = '管理与帮助'; toolsDetails.appendChild(toolsSummary);
    for (const item of [accountTools, serviceGroup, pane.querySelector('#buyerVerificationGroup'), pane.querySelector('#accountDevicesGroup'), pane.querySelector('#subscriptionRedeemGroup')].filter(Boolean)) {
      const title = item.querySelector('h3')?.textContent || ''; item.querySelector('h3')?.remove(); item.className = 'subscription-tool-content';
      let detail;
      if (item === serviceGroup) {
        detail = serviceDetails; detail.querySelector('summary').textContent = title; detail.appendChild(connectionActions);
      } else {
        detail = document.createElement('details'); detail.className = 'subscription-tool';
        const summary = document.createElement('summary'); summary.textContent = title; detail.append(summary, item);
        detail.open = toolExpansions.get(item.id) === true;
        detail.ontoggle = () => { if (currentView()) toolExpansions.set(item.id, detail.open); };
      }
      if (item === serviceGroup) toolsDetails.appendChild(item); else toolsDetails.appendChild(detail);
      if (item.id === 'accountDevicesGroup' && deviceManagementPending) { toolsDetails.open = true; detail.open = true; }
    }
    tools.appendChild(toolsDetails); pane.appendChild(tools);
    helpGroup.querySelector('h3')?.remove(); helpGroup.className = 'subscription-tool-content'; toolsDetails.appendChild(helpGroup);
    return pane;
  }

  function render() {
    if (AppState.currentView !== 'subscription') return;
    clearTimeout(betaExpiryTimer);
    ManualPayments.leave();
    AlipayPayments.leave();
    const host = document.getElementById('page-subscription');
    const focused = document.activeElement;
    const focusId = host.contains(focused) ? focused.id : '';
    const focusLabel = host.contains(focused) ? focused.getAttribute('aria-label') : '';
    const focusTag = focused.tagName;
    const selection = typeof focused.selectionStart === 'number' ? [focused.selectionStart, focused.selectionEnd] : null;
    viewEpoch++;
    host.replaceChildren();
    const layout = document.createElement('div');
    layout.className = 'settings-layout';
    const pane = document.createElement('div');
    pane.className = 'settings-pane';
    pane.id = 'subscriptionPane';
    pane.appendChild(buildSubscription());
    layout.appendChild(pane);
    host.appendChild(layout);
    const control = focusId ? document.getElementById(focusId)
      : [...pane.querySelectorAll('[aria-label]')].find(node => node.tagName === focusTag && node.getAttribute('aria-label') === focusLabel);
    if (control && !control.disabled) {
      control.focus({preventScroll:true});
      if (selection && typeof control.setSelectionRange === 'function') control.setSelectionRange(...selection);
    }
  }

  function openConnection() {
    connectionExpanded = true;
    toolsExpanded = true;
    switchView('subscription');
    const tools = document.getElementById('subscriptionTools');
    if (tools) tools.open = true;
    const details = document.getElementById('subscriptionConnectionDetails');
    if (details) details.open = true;
    document.getElementById('subscription_server_url')?.focus();
  }
  return {render, leave, syncState:()=>syncState(), openLogin, openConnection, openProviderLogin:options=>beginLogin(options.method||'google',options)};
})();
