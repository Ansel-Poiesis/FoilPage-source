// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 本地两步邮箱验证。请求和账户写入仍由真实 Bridge 合约处理。 */
'use strict';

window.AccountAuth = (() => {
  const modalId = 'accountAuthModal';
  const cooldowns = new Map();
  let active = null;
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  function close() { if (active) MicroModal.close(modalId); }
  function reset(state) {
    close(); clearTimeout(state.timer);
    Object.assign(state, {email:'', code:'', intent:'login', step:'email', error:'', submittedEmail:'', cooldownUntil:0, timer:0});
  }
  function refresh() { active?.refresh(); }
  function open(options) {
    const state = options.state;
    const intent = options.intent === 'bind' ? 'bind' : 'login';
    if (state.busy && state.intent !== intent) return;
    if (active) { active.refresh(); return; }
    if (state.intent !== intent) Object.assign(state, {intent, code:'', step:'email', error:''});
    state.intent = intent;
    state.step = state.step === 'code' ? 'code' : 'email';
    state.error = '';
    if (document.getElementById('modalRoot')?.firstElementChild) closeModal();
    MicroModal.removeModal(modalId);
    const root = make('div', 'account-auth-root'); root.id = modalId; root.setAttribute('aria-hidden', 'true');
    const overlay = make('div', 'auth-backdrop'); overlay.setAttribute('data-micromodal-close', '');
    const dialog = make('section', 'auth-dialog'); dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'accountAuthTitle'); dialog.setAttribute('aria-describedby', 'emailLoginHint');
    const closeButton = make('button', 'auth-close', '×'); closeButton.id = 'accountAuthClose'; closeButton.type = 'button';
    closeButton.setAttribute('aria-label', '关闭登录对话框'); closeButton.setAttribute('data-micromodal-close', '');
    const symbol = make('div', 'auth-symbol'); symbol.setAttribute('aria-hidden', 'true');
    symbol.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/></svg>';
    const title = make('h2', '', intent === 'bind' ? '绑定邮箱' : '登录 FoilPage'); title.id = 'accountAuthTitle';
    const stepText = make('div', 'auth-step-label'); stepText.id = 'accountAuthStep';
    const progress=make('div','auth-progress');progress.setAttribute('aria-hidden','true');progress.innerHTML='<span></span><span></span>';
    const form = make('form', 'auth-form'); form.noValidate = true;
    const emailStep = make('div', 'auth-step'); emailStep.id = 'accountAuthEmailStep';
    const emailLabel = make('label', 'auth-label', '邮箱地址'); emailLabel.htmlFor = 'loginEmailInput';
    const email = make('input', 'ctrl auth-input'); email.id = 'loginEmailInput'; email.type = 'email'; email.autocomplete = 'email';
    email.required = true; email.placeholder = 'you@example.com';
    // 先生 2026-10-31：登录输入记忆——上次成功发送验证码的邮箱自动回填。
    email.value = state.email || (() => { try { return localStorage.getItem('shengjian.loginEmail') || ''; } catch { return ''; } })();
    email.setAttribute('aria-describedby', 'accountAuthError');
    const codeStep = make('div', 'auth-step'); codeStep.id = 'accountAuthCodeStep';
    const destination = make('div', 'auth-destination'); destination.id = 'authEmailDestination';
    const codeLabel = make('label', 'auth-label', '验证码'); codeLabel.htmlFor = 'loginCodeInput';
    const code = make('input', 'ctrl auth-input auth-code'); code.id = 'loginCodeInput'; code.type = 'text';
    code.inputMode = 'numeric'; code.autocomplete = 'one-time-code'; code.maxLength = 6; code.placeholder = '000000';
    code.setAttribute('aria-describedby', 'accountAuthError'); code.value = state.code;
    const error = make('div', 'auth-error'); error.id = 'accountAuthError'; error.setAttribute('role', 'alert'); error.hidden = true;
    const hint = make('div', 'auth-status'); hint.id = 'emailLoginHint'; hint.setAttribute('role', 'status');
    const actions = make('div', 'auth-actions');
    const verify = make('button', 'btn primary auth-submit', intent === 'bind' ? '绑定邮箱' : '登录');
    verify.id = 'verifyCodeBtn'; verify.type = 'button';
    const secondary = make('div', 'auth-secondary');
    const back = make('button', 'btn ghost small', '修改邮箱'); back.id = 'authBackBtn'; back.type = 'button';
    const send = make('button', 'btn primary auth-submit', '获取验证码'); send.id = 'sendCodeBtn'; send.type = 'button';
    emailStep.append(emailLabel, email); codeStep.append(destination, codeLabel, code);
    secondary.append(back, send); actions.append(verify, secondary);
    const termsHost = make('div', 'auth-terms');
    const terms = PublicTerms.consent(termsHost, () => session.refresh());
    // 先生 2026-10-31：登录帮助长文移除——两条信息收进错误提示与 FAQ。
    form.append(emailStep, codeStep, termsHost, error, hint, actions);
    dialog.append(closeButton, symbol, title, stepText, progress, form); overlay.appendChild(dialog); root.appendChild(overlay); document.body.appendChild(root);
    const app = document.querySelector('.app'); const previousInert = app?.inert || false;
    const returnFocus = options.trigger || document.activeElement;
    const session = {root, state, refresh:() => {}};
    active = session;
    const live = () => active === session && root.isConnected;
    const busy = () => state.busy || options.isBlocked?.() === true;
    const available = () => options.isReady?.() === true;
    let shownStep = state.step;
    let wasLocked = busy();
    const cooldownKey = address => String(options.cooldownKey?.() || '') + '\u0000' + address.trim().toLowerCase();
    const rememberCooldown = (address, seconds) => {
      const deadline = Date.now() + seconds * 1000;
      cooldowns.set(cooldownKey(address), deadline);
      if (state.email.trim().toLowerCase() === address.trim().toLowerCase()) state.cooldownUntil = deadline;
    };
    const focusStep = () => {
      if (!live()) return;
      const target = state.step === 'code' ? code : email;
      (target.disabled ? closeButton : target).focus({preventScroll:true});
    };
    const showError = message => { state.error = String(message || '操作未完成，请重试'); if (live()) session.refresh(); };
    const changed = () => { options.onBusy?.(); refresh(); };
    const submitEmail = async () => {
      if (busy() || !available() || !terms.ready() || Date.now() < state.cooldownUntil) return;
      const address = email.value.trim();
      if (!address || !email.checkValidity()) { showError('请输入有效的邮箱地址'); email.focus(); return; }
      state.email = address; state.busy = true; state.error = ''; changed();
      try {
        if (!await terms.save()) {showError('条款确认未保存，请重新确认');return;}
        if (!live() || !terms.ready()) return;
        const result = await options.onSend(address);
        if (result?.stale) return;
        if (result?.ok === true) {
          const seconds = Number(result.resend_after_seconds);
          rememberCooldown(address, Number.isFinite(seconds) && seconds > 0 ? seconds : 60);
          try { localStorage.setItem('shengjian.loginEmail', address); } catch { /* 无痕/受限存储时静默 */ }
          if (state.email.trim() === address && state.intent === intent) {
            state.submittedEmail = address; state.step = 'code'; state.code = live() && result.dev_code ? String(result.dev_code) : '';
          }
        } else {
          const retry = Number(result?.retry_after);
          if (Number.isFinite(retry) && retry > 0) rememberCooldown(address, retry);
          if (live()) showError(result?.error || '验证码提交失败，请重试');
        }
      } catch (_) { if (live()) showError('暂时无法提交验证码，请重试'); }
      finally { state.busy = false; changed(); }
    };
    const submitCode = async () => {
      if (busy() || !available() || !terms.ready()) return;
      const address = state.email.trim(), otp = code.value.trim();
      if (!/^\d{6}$/.test(otp)) { showError('请输入 6 位验证码'); code.focus(); return; }
      state.busy = true; state.error = ''; changed();
      try {
        const result = await options.onVerify(address, otp, intent);
        if (result?.stale) return;
        if (result?.ok !== true && result?.status !== 'device_limit' && live()) showError(result?.error || '验证未通过，请重试');
      } catch (_) { if (live()) showError('暂时无法验证，请重试'); }
      finally { state.busy = false; changed(); }
    };
    email.oninput = () => { state.email = email.value; state.error = ''; session.refresh(); };
    code.oninput = () => {
      const normalized = code.value.replace(/\s+/g, '').slice(0, 6);
      if (code.value !== normalized) code.value = normalized;
      state.code = code.value; state.error = ''; session.refresh();
    };
    code.onpaste = event => {
      const value = event.clipboardData?.getData('text');
      if (typeof value !== 'string' || !/\s/.test(value)) return;
      event.preventDefault();
      const start = code.selectionStart || 0, end = code.selectionEnd || start;
      const remaining = Math.max(0, 6 - code.value.length + end - start);
      code.setRangeText(value.replace(/\s+/g, '').slice(0, remaining), start, end, 'end');
      code.dispatchEvent(new Event('input', {bubbles:true}));
    };
    send.onclick = submitEmail; verify.onclick = submitCode;
    email.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); submitEmail(); } };
    code.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); submitCode(); } };
    form.onsubmit = event => { event.preventDefault(); if (state.step === 'email') submitEmail(); else submitCode(); };
    back.onclick = () => { if (busy()) return; state.step = 'email'; state.code = ''; state.error = ''; session.refresh(); focusStep(); };
    session.refresh = () => {
      if (!live()) return;
      clearTimeout(state.timer);
      const codeMode = state.step === 'code';
      emailStep.hidden = codeMode; codeStep.hidden = !codeMode;
      stepText.textContent = codeMode ? '2 / 2（验证邮箱）' : '1 / 2（输入邮箱）';
      progress.dataset.step=codeMode?'2':'1';
      destination.textContent = state.submittedEmail || state.email;
      if (email.value !== state.email) email.value = state.email;
      if (code.value !== state.code) code.value = state.code;
      const locked = busy(), ready = available();
      const focusBefore = document.activeElement;
      const moveFocus = shownStep !== state.step || (wasLocked && !locked);
      shownStep = state.step; wasLocked = locked;
      state.cooldownUntil = cooldowns.get(cooldownKey(state.email)) || 0;
      const remaining = Math.max(0, Math.ceil((state.cooldownUntil - Date.now()) / 1000));
      email.disabled = code.disabled = locked; back.disabled = locked;
      send.disabled = !ready || locked || !terms.ready() || remaining > 0;
      send.title = terms.ready() ? '' : '请先阅读并同意条款与数据说明';
      verify.disabled = !ready || locked || !terms.ready();
      verify.hidden = !codeMode; back.hidden = !codeMode;
      send.className = codeMode ? 'btn ghost small' : 'btn primary auth-submit';
      send.textContent = state.busy && !codeMode ? '提交中…' : remaining > 0 ? `${remaining} 秒后重发` : codeMode ? '重发验证码' : '获取验证码';
      verify.textContent = state.busy ? '验证中…' : intent === 'bind' ? '绑定邮箱' : '登录';
      hint.textContent = !ready ? options.getUnavailableHint?.() || '邮箱登录暂未开放，请检查服务配置'
        : codeMode ? '验证码已提交，请查收邮箱' : '';
      hint.hidden = !hint.textContent;
      error.textContent = state.error || ''; error.hidden = !state.error;
      email.setAttribute('aria-invalid', state.error && !codeMode ? 'true' : 'false');
      code.setAttribute('aria-invalid', state.error && codeMode ? 'true' : 'false');
      if (moveFocus && (!dialog.contains(focusBefore) || [closeButton, email, code, send, verify].includes(focusBefore))) focusStep();
      if (remaining > 0) state.timer = setTimeout(refresh, 1000);
    };
    MicroModal.show(modalId, {disableFocus:true, disableScroll:true, awaitOpenAnimation:false, awaitCloseAnimation:false,
      onShow:() => { if (app) app.inert = true; session.refresh(); focusStep(); },
      onClose:() => {
        if (active === session) active = null;
        clearTimeout(state.timer); state.code = ''; state.error = ''; code.value = '';
        if (app) app.inert = previousInert;
        root.classList.remove('is-open');
        MicroModal.removeModal(modalId); root.remove();
        const target = returnFocus?.isConnected ? returnFocus : document.getElementById(options.returnFocusId || 'accountEmailLoginBtn');
        if (target && !target.disabled) target.focus({preventScroll:true});
      },
    });
  }
  return {open, close, refresh, reset};
})();

/* 资料只在明确保存后发送；头像在本机裁为小图，不读取远程地址。 */
window.AccountProfile = (() => {
  const modalId = 'accountProfileModal';
  const defaultAvatar = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>';
  let active = null;
  const make = (tag, className, text) => {
    const node = document.createElement(tag); if (className) node.className = className; if (text) node.textContent = text; return node;
  };
  function safeAvatar(value) {
    return typeof value === 'string' && value.length <= 65536 && /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) ? value : '';
  }
  function close() { if (active) MicroModal.close(modalId); }
  async function thumbnail(file) {
    if (!file || file.size > 8 * 1024 * 1024) throw new Error('头像文件不能超过 8 MB');
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('请选择 JPG、PNG 或 WebP 图片');
    const bytes = new Uint8Array(await file.slice(0, 16).arrayBuffer());
    const actual = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? 'image/jpeg'
      : [137,80,78,71,13,10,26,10].every((byte, index) => bytes[index] === byte) ? 'image/png'
      : String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF' && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP' ? 'image/webp' : '';
    if (actual !== file.type) throw new Error('图片格式与文件内容不一致');
    const source = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('无法读取头像图片')); reader.readAsDataURL(file);
    });
    const image = new Image(); image.src = source;
    try { await image.decode(); } catch (_) { throw new Error('无法读取这张图片，请换一张'); }
    if (!image.naturalWidth || !image.naturalHeight) throw new Error('图片尺寸无效');
    const crop = Math.min(image.naturalWidth, image.naturalHeight), canvas = document.createElement('canvas');
    let size = Math.min(256, crop);
    let firstAttempt = true;
    while (firstAttempt || size >= 64) {
      firstAttempt = false;
      canvas.width = canvas.height = size;
      canvas.getContext('2d').drawImage(image, (image.naturalWidth - crop) / 2, (image.naturalHeight - crop) / 2, crop, crop, 0, 0, size, size);
      const lossless = canvas.toDataURL('image/png'); if (safeAvatar(lossless)) return lossless;
      for (const quality of [.9, .75, .6, .45]) {
        const compact = canvas.toDataURL('image/webp', quality); if (safeAvatar(compact)) return compact;
      }
      size = Math.floor(size * .75);
    }
    throw new Error('无法将头像缩小到可保存的大小，请换一张');
  }
  function open(options) {
    if (active) return;
    AccountAuth.close();
    if (document.getElementById('modalRoot')?.firstElementChild) closeModal();
    MicroModal.removeModal(modalId);
    const root = make('div', 'account-auth-root'); root.id = modalId; root.setAttribute('aria-hidden', 'true');
    const overlay = make('div', 'auth-backdrop'); overlay.setAttribute('data-micromodal-close', '');
    const dialog = make('section', 'auth-dialog profile-dialog'); dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-labelledby', 'accountProfileTitle');
    const closeButton = make('button', 'auth-close', '×'); closeButton.id = 'accountProfileClose'; closeButton.type = 'button'; closeButton.setAttribute('aria-label', '取消编辑资料'); closeButton.setAttribute('data-micromodal-close', '');
    const title = make('h2', '', '编辑资料'); title.id = 'accountProfileTitle';
    const form = make('form', 'profile-form'); form.noValidate = true;
    const avatarGroup = make('div', 'profile-avatar-group');
    const preview = make('div', 'account-avatar profile-avatar-preview'); preview.id = 'profileAvatarPreview'; preview.setAttribute('role', 'img'); preview.setAttribute('aria-label', '头像预览');
    const controls = make('div', 'profile-avatar-controls');
    const upload = make('button', 'btn small', '选择头像'); upload.type = 'button'; upload.id = 'profileChooseAvatarBtn';
    const restore = make('button', 'btn ghost small', '恢复默认'); restore.type = 'button'; restore.id = 'profileRestoreAvatarBtn';
    const input = make('input'); input.type = 'file'; input.id = 'profileAvatarInput'; input.accept = 'image/jpeg,image/png,image/webp'; input.hidden = true; input.setAttribute('aria-label', '选择头像图片');
    const format = make('div', 'profile-file-hint', 'JPG、PNG、WebP（最大 8 MB）');
    const label = make('label', 'auth-label', '昵称'); label.htmlFor = 'profileNameInput';
    const name = make('input', 'ctrl auth-input'); name.id = 'profileNameInput'; name.autocomplete = 'nickname'; name.maxLength = 64; name.value = options.name || ''; name.placeholder = '你的昵称'; name.required = true; name.setAttribute('aria-describedby', 'accountProfileError');
    const error = make('div', 'auth-error'); error.id = 'accountProfileError'; error.setAttribute('role', 'alert'); error.hidden = true;
    const status = make('div', 'auth-status'); status.id = 'profileAvatarStatus'; status.setAttribute('role', 'status'); status.hidden = true;
    const actions = make('div', 'profile-actions');
    const cancel = make('button', 'btn ghost', '取消'); cancel.type = 'button'; cancel.setAttribute('data-micromodal-close', '');
    const save = make('button', 'btn primary', '保存'); save.type = 'submit'; save.id = 'profileSaveBtn';
    controls.append(upload, restore, input, format); avatarGroup.append(preview, controls); actions.append(cancel, save);
    form.append(avatarGroup, label, name, error, status, actions); dialog.append(closeButton, title, form); overlay.appendChild(dialog); root.appendChild(overlay); document.body.appendChild(root);
    const app = document.querySelector('.app'), previousInert = app?.inert || false, trigger = options.trigger || document.activeElement;
    const session = {root, busy:false, loading:false, generation:0}; active = session;
    let avatar = safeAvatar(options.avatar), avatarPatch = null;
    const live = () => active === session && root.isConnected;
    const showError = message => { if (live()) { error.textContent = String(message || '保存未完成，请重试'); error.hidden = false; } };
    const refresh = () => {
      if (!live()) return;
      preview.replaceChildren();
      if (avatar) { const image = make('img'); image.src = avatar; image.alt = ''; preview.appendChild(image); } else preview.innerHTML = defaultAvatar;
      name.disabled = session.busy; input.disabled = upload.disabled = restore.disabled = session.busy || session.loading;
      save.disabled = session.busy || session.loading; save.textContent = session.busy ? '保存中…' : '保存';
      status.textContent = session.loading ? '正在处理头像…' : ''; status.hidden = !status.textContent;
    };
    name.oninput = () => { error.hidden = true; name.setAttribute('aria-invalid', 'false'); };
    upload.onclick = () => input.click();
    restore.onclick = () => { if (session.busy || session.loading) return; avatar = ''; avatarPatch = ''; input.value = ''; error.hidden = true; refresh(); };
    input.onchange = async () => {
      if (session.busy) return;
      const file = input.files?.[0]; if (!file) return;
      session.loading = true; error.hidden = true; const generation = ++session.generation; refresh();
      try {
        const image = await thumbnail(file);
        if (live() && generation === session.generation) { avatar = image; avatarPatch = image; }
      } catch (failure) { if (live() && generation === session.generation) showError(failure.message); }
      finally { if (live() && generation === session.generation) { session.loading = false; input.value = ''; refresh(); } }
    };
    form.onsubmit = async event => {
      event.preventDefault(); if (session.busy || session.loading || !live()) return;
      const nickname = name.value.trim();
      if (!nickname || Array.from(nickname).length > 32 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(nickname)) {
        showError('昵称需为 1–32 个字符，不能包含控制字符'); name.setAttribute('aria-invalid', 'true'); name.focus(); return;
      }
      session.busy = true; error.hidden = true; refresh();
      try {
        const result = await options.onSave(nickname, avatarPatch);
        if (result?.ok === true) { if (live()) close(); }
        else if (live()) showError(result?.stale ? '账户已变化，请重新打开资料编辑' : result?.error || '保存未完成，请重试');
      } catch (_) { showError('暂时无法保存资料，请重试'); }
      finally { session.busy = false; refresh(); }
    };
    MicroModal.show(modalId, {disableFocus:true, disableScroll:true, awaitOpenAnimation:false, awaitCloseAnimation:false,
      onShow:() => { if (app) app.inert = true; refresh(); name.focus({preventScroll:true}); name.select(); },
      onClose:() => {
        if (active === session) active = null; session.generation++;
        if (app) app.inert = previousInert;
        root.classList.remove('is-open'); MicroModal.removeModal(modalId); root.remove();
        const target = trigger?.isConnected ? trigger : document.getElementById(trigger?.id || 'accountProfileBtn');
        if (target && !target.disabled) target.focus({preventScroll:true});
      },
    });
  }
  return {open, close, safeAvatar, defaultAvatar};
})();
