// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Application entry follows the bridge's verified account, never a local flag. */
'use strict';
window.AccountAccess = (() => {
  let expiryTimer = 0;
  let restoration = null;
  function allowed() {
    const access = AppState.settings?.account_access;
    return access?.allowed === true && Number.isFinite(access.expires_at)
      && access.expires_at * 1000 > Date.now();
  }
  function sync() {
    clearTimeout(expiryTimer);
    document.body.dataset.accountAccess = allowed() ? 'verified' : 'required';
    document.querySelectorAll('.nav-row').forEach(button => {
      const locked = !allowed() && !['setup','subscription','settings','faq'].includes(button.dataset.page);
      button.disabled = locked;
      if(locked){button.setAttribute('aria-disabled','true');button.setAttribute('aria-description','请先登录');}
      else {button.removeAttribute('aria-disabled');button.removeAttribute('aria-description');}
      button.classList.toggle('account-locked', locked);
    });
    if (allowed()) {
      const remaining = AppState.settings.account_access.expires_at * 1000 - Date.now();
      expiryTimer = setTimeout(sync, Math.min(remaining + 1, 2147483647));
    } else if (window.__appReady && AppState.currentView === 'setup') {
      window.Setup?.showStep('login');
    } else if (window.__appReady && !['subscription','settings','faq'].includes(AppState.currentView)) {
      switchView('setup');
      window.Setup?.showStep('login');
    }
    window.Setup?.refreshAccount?.();
    window.Subscription?.syncState?.();
  }
  function lock() {
    AppState.settings.account_access = {required:true, allowed:false, expires_at:null, reason:'login_required',revision:(AppState.settings.account_access?.revision||0)+1};
    sync();
  }
  function accepts(access) {
    return !!access && access.required === true && typeof access.allowed === 'boolean'
      && Number.isInteger(access.revision) && access.revision >= (AppState.settings.account_access?.revision||0);
  }
  function apply(access) {
    if (!accepts(access)) return false;
    AppState.settings.account_access = {...access};sync();return true;
  }
  async function restore() {
    if(restoration)return restoration;
    const context=()=>JSON.stringify([AppState.settings?.account_login_token||'',String(AppState.settings?.subscription_server_url||'').trim().replace(/\/+$/,''),String(AppState.settings?.subscription_license_public_key||'').trim()]);
    const captured=context(),name=AppState.settings.account_display_name,avatar=AppState.settings.account_avatar;
    AppState.accountRestorePending=true;AppState.accountRestoreError='';window.Setup?.refreshAccount();
    restoration=(async()=>{
      let refresh;
      try{
        try{refresh=await AppApi.refreshSubscription();}catch{}
        const settings=await AppApi.settings();
        if(context()===captured&&accepts(settings?.account_access)){
          const confirmed={};
          for(const key of ['account_login_token','account_access_token','account_user_id','license_token','license_expires_at','subscription_plan','subscription_active','subscription_expires_at','credit_balance','subscription_last_order_id'])if(Object.hasOwn(settings,key))confirmed[key]=settings[key];
          if(AppState.settings.account_display_name===name&&Object.hasOwn(settings,'account_display_name'))confirmed.account_display_name=settings.account_display_name;
          if(AppState.settings.account_avatar===avatar&&Object.hasOwn(settings,'account_avatar'))confirmed.account_avatar=settings.account_avatar;
          AppState.settings={...AppState.settings,...confirmed};apply(settings.account_access);
        }
      }catch{}
      finally{
        AppState.accountRestorePending=false;
        if(!allowed())AppState.accountRestoreError=AppState.settings.account_login_token?(refresh?.error||'暂时无法恢复登录，请重试'):'登录已失效，请重新登录';
        window.Setup?.refreshAccount();window.Setup?.refreshProviders?.();restoration=null;
      }
      return{ok:allowed()};
    })();
    return restoration;
  }
  return {allowed, sync, lock, accepts, apply, restore};
})();
