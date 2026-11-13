// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Bundled full text works offline; consent is scoped to service and version. */
'use strict';
window.PublicTerms = (() => {
  const metadata = window.PublicTermsMetadata;
  const versions = new Map();
  const service = () => String(AppState.settings?.subscription_server_url || '').trim().replace(/\/+$/, '');
  const version = () => Math.max(metadata.version, versions.get(service()) || 0);
  const supported = () => version() === metadata.version;
  const accepted = () => supported() && AppState.settings?.terms_accepted_service === service()
    && AppState.settings?.terms_accepted_version === version()
    && AppState.settings?.terms_cross_border_accepted_version === version();
  function observe(contract) {
    const v = contract?.terms?.version;
    if(Number.isSafeInteger(v) && v > 0 && v <= 1000000) versions.set(service(), Math.max(v, versions.get(service()) || 0));
  }
  function open(id='user-agreement') {
    if(!metadata.documents.some(d=>d.id===id)) return;
    const previous=document.activeElement;
    const dialog=document.createElement('dialog');dialog.className='public-terms-dialog';dialog.setAttribute('aria-label','条款与用户须知全文');
    const frame=document.createElement('iframe');frame.title='用户条款与须知全文';frame.src='terms.html#'+id;
    frame.setAttribute('sandbox','');frame.referrerPolicy='no-referrer';
    const close=document.createElement('button');close.type='button';close.className='btn small';close.textContent='返回';
    const finish=()=>{dialog.close();dialog.remove();if(previous?.isConnected)previous.focus({preventScroll:true});};
    close.onclick=finish;dialog.addEventListener('cancel',e=>{e.preventDefault();finish();});
    dialog.append(close,frame);document.body.append(dialog);dialog.showModal();close.focus();
  }
  function links(host) {
    for(const d of metadata.documents) {
      const button=document.createElement('button');button.type='button';button.className='terms-link';button.textContent=d.title;
      button.onclick=()=>open(d.id);host.append(button);
    }
  }
  function consent(host,onChange=()=>{}) {
    const wrap=document.createElement('div');wrap.className='terms-consent';
    // 先生 2026-10-31：精简为一次勾选——文案合并条款与境外处理说明；
    // save() 仍同时写 terms_accepted_* 与 terms_cross_border_* 两个字段。
    const label=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.id='termsConsent';
    checkbox.checked=accepted();label.append(checkbox,document.createTextNode('已阅读并同意条款与用户须知（含账户信息经 Cloudflare、Resend 境外处理）'));
    const documents=document.createElement('div');links(documents);
    const hint=document.createElement('p');hint.className='set-hint';hint.textContent=supported()?'':'服务条款已更新，请升级 FoilPage 后阅读并确认新版。';
    hint.hidden=supported();
    const currentService=service(),currentVersion=version();
    const ready=()=>supported() && currentService===service() && currentVersion===version() && checkbox.checked;
    const update=()=>{if(currentVersion!==version() || currentService!==service()){checkbox.checked=false;hint.textContent='条款或服务已变化，请重新打开登录页确认。';hint.hidden=false;}onChange();};
    checkbox.onchange=update;
    wrap.append(label,documents,hint);host.append(wrap);
    return {ready,refresh:update,async save(){
      if(!ready())return false;
      const changes={terms_accepted_version:currentVersion,terms_cross_border_accepted_version:currentVersion,terms_accepted_service:currentService};
      const result=await AppApi.saveSettings(changes);
      if(result?.ok===false || currentVersion!==version() || currentService!==service())return false;
      Object.assign(AppState.settings,changes);return true;
    }};
  }
  async function ensureAccepted() {
    if(accepted())return true;
    return new Promise(resolve=>{
      const previous=document.activeElement,dialog=document.createElement('dialog');dialog.className='terms-confirm-dialog';dialog.setAttribute('aria-label','确认条款');
      const title=document.createElement('h2');title.textContent='请先阅读并确认条款';
      const host=document.createElement('div'),actions=document.createElement('div');actions.className='terms-confirm-actions';
      const cancel=document.createElement('button');cancel.type='button';cancel.className='btn ghost';cancel.textContent='取消';
      const confirm=document.createElement('button');confirm.type='button';confirm.className='btn primary';confirm.textContent='同意并继续';
      let busy=false;const check=consent(host,()=>{confirm.disabled=busy || !check.ready();});
      const finish=value=>{dialog.close();dialog.remove();if(previous?.isConnected)previous.focus({preventScroll:true});resolve(value);};
      cancel.onclick=()=>{if(!busy)finish(false);};dialog.addEventListener('cancel',e=>{e.preventDefault();if(!busy)finish(false);});
      confirm.disabled=!check.ready();confirm.onclick=async()=>{if(busy || !check.ready())return;busy=true;confirm.disabled=true;cancel.disabled=true;
        try{finish(await check.save());}catch(_){finish(false);}};
      actions.append(cancel,confirm);dialog.append(title,host,actions);document.body.append(dialog);dialog.showModal();cancel.focus();
    });
  }
  function notice(host) {
    const block=document.createElement('aside');block.className='terms-notice';block.id='subscriptionTermsNotice';
    const text=document.createElement('p');text.textContent='购买与兑换前请阅读条款；内测免扣托管积分，原余额保留。退款及反馈：QQ '+metadata.contact_qq;
    block.append(text);links(block);host.append(block);
  }
  return {observe,open,links,consent,accepted,ensureAccepted,notice,version};
})();
