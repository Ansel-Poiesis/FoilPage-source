// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Only verified server orders and signed licenses can complete a payment. */
'use strict';
window.AlipayPayments = (() => {
  let generation = 0, dialog = null, pollTimer = 0, accountTimer = 0, entryTimer = 0, creation = null;
  let entryRefresh = () => {};
  const key = () => { const s=AppState.settings||{}; return [s.subscription_server_url,s.account_login_token,s.account_user_id].join('\u0000'); };
  const signedIn = () => !!AppState.settings?.account_login_token && window.AccountAccess?.allowed()===true;
  const configured = c => c?.provider==='alipay_page_pay' && c.configured===true
    && Number.isSafeInteger(c.max_order_amount_minor) && c.max_order_amount_minor>0 && c.max_order_amount_minor<=10000000000;
  const states = {pending_payment:'待支付',waiting:'待支付',closed:'已关闭',credited:'已支付',refund_review:'订单复核中'};
  const identifier = s => typeof s==='string' && /^[A-Za-z0-9_-]{12,64}$/.test(s);
  const price = s => '¥'+Number(s.amount).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const validOffer = q => q && ['standard','advanced'].includes(q.plan_id) && ['month','year'].includes(q.period)
    && q.months===(q.period==='month'?1:12) && q.currency==='CNY' && typeof q.amount==='string'
    && /^(?:0|[1-9][0-9]{0,8})\.[0-9]{2}$/.test(q.amount) && Number(q.amount)>0
    && Number.isSafeInteger(q.credits) && q.credits>0;
  function offersFrom(c) {
    const result=[];
    for(const p of Array.isArray(c.catalog)?c.catalog:[]) {
      if(!['standard','advanced'].includes(p?.id) || p.status!=='ready' || p.purchasable!==true)continue;
      for(const o of Array.isArray(p.offers)?p.offers:[]) {
        const q={...o,plan_id:p.id};if(o?.status==='ready' && o.purchasable===true && validOffer(q)
          && Math.round(Number(q.amount)*100)<=c.max_order_amount_minor)result.push(q);
      }
    }
    return result.filter(q=>result.filter(v=>v.plan_id===q.plan_id && v.period===q.period).length===1);
  }
  const sameQuote = (a,b) => !!a && !!b && ['plan_id','period','months','amount','credits','currency'].every(n=>a[n]===b[n]);
  const offer = (c,planId,period) => configured(c) && c.enabled===true
    ? offersFrom(c).find(q=>q.plan_id===planId && q.period===period)||null : null;
  function displayOffer(c,planId,period) {
    const payable=offer(c,planId,period);if(payable)return payable;
    if(!configured(c))return null;
    const plans=(Array.isArray(c.catalog)?c.catalog:[]).filter(p=>p?.id===planId);
    if(plans.length!==1)return null;
    const matches=(Array.isArray(plans[0].offers)?plans[0].offers:[]).filter(q=>q?.period===period);
    if(matches.length!==1)return null;
    const q={...matches[0],plan_id:planId};
    return q.status==='merchant_limit' && q.purchasable===false && validOffer(q)
      && Math.round(Number(q.amount)*100)>c.max_order_amount_minor
      && Math.round(Number(q.amount)*100)<=10000000000 ? q : null;
  }
  function safeOrder(o) {
    if(!o || !identifier(o.order_id) || !Object.hasOwn(states,o.status) || !Number.isSafeInteger(o.revision)
      || o.revision<0 || !validOffer(o.snapshot) || !Number.isSafeInteger(o.snapshot.amount_minor)
      || o.snapshot.amount_minor<=0 || o.snapshot.amount_minor>10000000000
      || Math.round(Number(o.snapshot.amount)*100)!==o.snapshot.amount_minor)return null;
    return o;
  }
  function leave() {
    generation++;clearTimeout(pollTimer);clearTimeout(accountTimer);clearTimeout(entryTimer);
    if(dialog?.isConnected && dialog.querySelector('#alipayPaymentBody'))closeModal();
    dialog=null;
    if(!signedIn() || (creation && creation.account!==key()))creation=null;
    entryRefresh();
  }
  function mount(host,capability,{isCurrent,onSynced,onConfig}={}) {
    leave();host.replaceChildren();
    const mountedAccount=key();
    const canPay=()=>configured(capability) && capability.enabled===true && offersFrom(capability).some(q=>q.period==='month');
    const visible=()=>key()===mountedAccount && signedIn() && capability?.provider==='alipay_page_pay'
      && (canPay() || capability.processing_enabled===true)
      && (!isCurrent || isCurrent());
    host.hidden=!visible();entryRefresh=()=>{};if(!visible())return;
    host.className='manual-payment-entry';
    const label=document.createElement('span');label.textContent='支付宝';
    const entry=document.createElement('button');entry.type='button';entry.className='btn small';entry.id='alipayPaymentOpen';
    entry.textContent=canPay()?'支付宝支付':'查看订单';
    const openOffer=intent=>{if(visible())return open({isCurrent,onSynced,intent,onConfig:config=>{
      capability=config;check();onConfig?.(config);
    }});};
    entry.onclick=()=>openOffer();host.append(label,entry);
    const check=()=>{clearTimeout(entryTimer);if(!host.isConnected)return;host.hidden=!visible();
      entry.textContent=canPay()?'支付宝支付':'查看订单';
      if(visible())entryTimer=setTimeout(check,Math.max(1,Math.min(AppState.settings.account_access.expires_at*1000-Date.now()+1,2147483647)));};check();
    entryRefresh=check;
    return openOffer;
  }
  async function open({isCurrent,onSynced,onConfig,intent}={}) {
    if(!signedIn() || (intent && !validOffer(intent)))return;leave();
    const epoch=generation,account=key();
    const mask=openModal('支付宝','<div id="alipayPaymentBody" class="manual-payment-body"></div>',
      '<button class="btn ghost" id="dlgCancel">关闭</button>');dialog=mask;
    mask.querySelector('.modal').classList.add('manual-payment-dialog');
    const valid=()=>epoch===generation && dialog===mask && mask.isConnected && key()===account && signedIn() && (!isCurrent || isCurrent());
    const checkAccount=()=>{if(dialog!==mask || !mask.isConnected)return;if(!valid()){leave();return;}
      accountTimer=setTimeout(checkAccount,Math.max(1,Math.min(AppState.settings.account_access.expires_at*1000-Date.now()+1,2147483647)));};checkAccount();
    mask.querySelector('#dlgCancel').onclick=leave;mask.querySelector('.modal-x').onclick=leave;
    mask.addEventListener('keydown',e=>{if(e.key==='Escape')leave();});mask.addEventListener('mousedown',e=>{if(e.target===mask)leave();});
    const body=mask.querySelector('#alipayPaymentBody');
    const notice=document.createElement('div');notice.id='alipayPaymentNotice';notice.className='manual-payment-notice';notice.setAttribute('role','status');
    notice.textContent='正在读取订单…';body.append(notice);
    const controls=document.createElement('div');controls.className='manual-payment-controls';
    const content=document.createElement('div');content.className='manual-payment-content';body.append(controls,content);
    const terms=document.createElement('p');terms.className='set-hint';terms.id='alipayTermsNotice';terms.textContent='支付前请确认用户服务协议；退款联系 QQ 2029774968。';PublicTerms.links(terms);body.append(terms);
    let config=null,orders=[],selected=null,busy=false,selection=0,synced='',reviewSynced='',queryUntil=0;
    const tell=(text,error=false)=>{if(valid()){notice.textContent=String(text||'');notice.classList.toggle('is-error',error);}};
    function remember(order) {
      const old=orders.find(o=>o.order_id===order.order_id);
      if(old && ['plan_id','period','months','amount','amount_minor','credits','currency'].some(n=>old.snapshot[n]!==order.snapshot[n])) {
        tell('订单报价已变更，请联系支持',true);return null;
      }
      if(old && (old.revision>order.revision || (old.revision===order.revision && old.status!==order.status))) {selected=old;return old;}
      orders=orders.filter(o=>o.order_id!==order.order_id);orders.unshift(order);selected=order;return order;
    }
    async function syncCredits(id,review=false) {
      const result=await AppApi.refreshSubscription();if(!valid() || selected?.order_id!==id)return;
      if(result?.ok!==true){tell(result?.error||(review?'订单复核状态同步未完成':'支付已确认，积分同步未完成'),true);return;}
      const s=await AppApi.settings();if(!valid() || selected?.order_id!==id)return;
      if(!s || s.account_login_token!==AppState.settings.account_login_token || s.account_user_id!==AppState.settings.account_user_id
        || s.subscription_server_url!==AppState.settings.subscription_server_url || !window.AccountAccess.accepts(s.account_access)
        || s.account_access.allowed!==true || s.account_access.expires_at*1000<=Date.now()) {tell(review?'订单复核状态同步未完成':'支付已确认，积分同步未完成',true);return;}
      for(const n of ['license_token','license_expires_at','subscription_plan','subscription_active','subscription_expires_at','credit_balance']) {
        if(Object.hasOwn(s,n))AppState.settings[n]=s[n];
      }
      window.AccountAccess.apply(s.account_access);
      if(review){reviewSynced=id+':'+selected.revision;synced='';tell('托管已暂停，积分记录保留');}
      else{synced=id;tell('积分已到账');}
      const status=await AppApi.subscriptionStatus();if(valid()){AppState.subscription=status;onSynced?.(status);}
    }
    async function accept(response,id=null) {
      if(!valid())return false;
      const order=safeOrder(response?.order);
      if(response?.ok!==true || !order || (id && order.order_id!==id)){tell(response?.error||'订单响应无效，请重试',true);return false;}
      const current=remember(order);if(!current)return false;
      if(Number.isSafeInteger(response.retry_after) && response.retry_after>0 && response.retry_after<=300)queryUntil=Date.now()+response.retry_after*1000;
      if(current.status==='refund_review'){synced='';tell('托管已暂停，积分记录保留');
        if(reviewSynced!==current.order_id+':'+current.revision)await syncCredits(current.order_id,true);}
      if(current.status==='credited' && synced!==current.order_id)await syncCredits(current.order_id);
      return true;
    }
    function schedule() {
      clearTimeout(pollTimer);
      if(!valid() || !selected || !['pending_payment','waiting'].includes(selected.status) || config?.processing_enabled!==true)return;
      pollTimer=setTimeout(async()=>{if(!valid())return;if(!busy && !document.hidden)await run(async()=>{
        const id=selected.order_id;await accept(await AppApi.alipayOrder(id),id);
      });else schedule();},10000);
    }
    async function run(action) {
      if(busy || !valid())return;busy=true;render();
      try{await action();}catch(_){tell('暂时无法连接支付服务，请重试',true);}
      finally{if(valid()){busy=false;render();schedule();}}
    }
    function button(text,id,action,primary=false) {
      const b=document.createElement('button');b.type='button';b.id=id;b.textContent=text;b.className='btn small '+(primary?'primary':'ghost');
      b.disabled=busy;b.onclick=()=>run(action);return b;
    }
    async function openCashier(id) {
      if(!valid() || selected?.order_id!==id || !['pending_payment','waiting'].includes(selected.status))return;
      const result=await AppApi.openAlipayCashier(id);if(!valid())return;
      tell(result?.ok===true?'已打开收银台':result?.error||'无法打开收银台，请重试',result?.ok!==true);
    }
    function render() {
      if(!valid() || !config)return;
      const focused=document.activeElement?.id;controls.replaceChildren();content.replaceChildren();
      if(orders.length || selected) {
        const picker=document.createElement('select');picker.className='ctrl';picker.id='alipayPaymentHistory';picker.setAttribute('aria-label','支付宝订单');picker.disabled=busy;
        if(configured(config) && config.enabled===true)picker.append(new Option('新订单',''));
        for(const o of orders)picker.append(new Option(`${states[o.status]}（${price(o.snapshot)}）`,o.order_id));
        picker.value=selected?.order_id||'';picker.onchange=()=>{selected=orders.find(o=>o.order_id===picker.value)||null;tell('');render();schedule();};controls.append(picker);
      }
      if(!selected) {
        const offers=offersFrom(config).filter(q=>q.period==='month');
        if(!configured(config) || config.enabled!==true || !offers.length){tell('购买暂未开放');return;}
        const picker=document.createElement('select');picker.className='ctrl';picker.id='alipayPaymentOffer';picker.setAttribute('aria-label','支付宝套餐');picker.disabled=busy;
        offers.forEach((o,i)=>picker.append(new Option(`${o.plan_id==='standard'?'标准版':'专业版'}（${o.period==='year'?'年付':'月付'}），${price(o)}`,String(i))));
        selection=Math.min(selection,offers.length-1);picker.value=String(selection);
        picker.onchange=()=>{selection=Number(picker.value);creation=null;render();};controls.append(picker);
        const quote=document.createElement('div');quote.className='manual-payment-quote';quote.textContent=offers[selection].credits.toLocaleString('zh-CN')+' 积分';controls.append(quote);
        controls.append(button('前往支付','alipayPaymentCreate',async()=>{
          const o=offers[selection],offerKey=o.plan_id+':'+o.period;
          if(!creation || creation.account!==account || creation.offer!==offerKey)creation={account,offer:offerKey,id:crypto.randomUUID()};
          if(!await PublicTerms.ensureAccepted() || !valid())return;
          const response=await AppApi.createAlipayOrder(o.plan_id,o.period,creation.id);if(!valid())return;
          const candidate=safeOrder(response?.order);
          if(candidate && !sameQuote(candidate.snapshot,o)){tell('订单与所选套餐报价不一致，请重新确认',true);return;}
          if(await accept(response)) {creation=null;if(response.cashier_available===true)await openCashier(selected.order_id);}
        },true));
      } else {
        const status=document.createElement('div');status.id='alipayPaymentState';status.className='manual-payment-state';status.setAttribute('role','status');
        status.textContent=selected.status==='credited' && synced===selected.order_id?'已到账':states[selected.status];
        const amount=document.createElement('strong');amount.className='manual-payment-amount';amount.textContent=price(selected.snapshot);
        const quota=document.createElement('div');quota.className='manual-payment-quote';quota.textContent=selected.snapshot.credits.toLocaleString('zh-CN')+' 积分';
        content.append(status,amount,quota);
        const actions=document.createElement('div');actions.className='manual-payment-actions';
        const id=selected.order_id;
        if(config.enabled===true && configured(config) && ['pending_payment','waiting'].includes(selected.status))actions.append(button('继续支付','alipayPaymentResume',async()=>{
          const response=await AppApi.alipayOrder(id,true);if(await accept(response,id) && response.cashier_available===true)await openCashier(id);
          else if(valid() && selected?.status!=='credited' && selected?.status!=='refund_review')tell('收银台暂不可用，请稍后重试',true);
        },true));
        const query=button(selected.status==='credited'?'同步积分':'检查到账','alipayPaymentQuery',async()=>{
          if(selected.status==='credited')await syncCredits(id);
          else {tell('正在检查到账…');const accepted=await accept(await AppApi.queryAlipayOrder(id),id);
            if(accepted && valid() && ['pending_payment','waiting'].includes(selected?.status))tell('暂未到账');}
        });query.disabled=busy || (selected.status!=='credited' && (config.processing_enabled!==true || queryUntil>Date.now()));
        actions.append(query);content.append(actions);
      }
      const target=focused?.startsWith('alipayPayment')?mask.querySelector('#'+focused):null;
      if(target && !target.disabled)target.focus({preventScroll:true});
    }
    try {
      const replies=await Promise.allSettled([AppApi.alipayPaymentConfig(),AppApi.alipayOrders()]);if(!valid())return;
      if(replies[0].status!=='fulfilled' || replies[0].value?.ok!==true){onConfig?.(null);tell(replies[0].value?.error||'支付宝暂不可用',true);return;}
      config=replies[0].value;onConfig?.(config);if(!valid())return;
      if(replies[1].status!=='fulfilled' || replies[1].value?.ok!==true || !Array.isArray(replies[1].value.orders)) {tell('订单读取失败，请重新打开',true);return;}
      orders=replies[1].value.orders.map(safeOrder).filter(Boolean);
      if(intent) {
        const currentOffer=intent.period==='month'?offer(config,intent.plan_id,intent.period):null;
        if(!currentOffer){tell('所选套餐暂不可购买，请返回刷新报价',true);return;}
        if(!sameQuote(currentOffer,intent)){tell('所选套餐报价已变更，请返回重新确认',true);return;}
        selection=offersFrom(config).filter(q=>q.period==='month').findIndex(q=>sameQuote(q,intent));
        const pending=orders.find(o=>['pending_payment','waiting'].includes(o.status)
          && o.snapshot.plan_id===intent.plan_id && o.snapshot.period===intent.period);
        if(pending && !sameQuote(pending.snapshot,intent)){tell('已有订单报价与所选套餐不同，请从订单记录确认',true);return;}
        selected=pending||null;
      } else selected=orders.find(o=>['pending_payment','waiting','refund_review'].includes(o.status))||orders[0]||null;
      tell('');if(selected?.status==='credited')await syncCredits(selected.order_id);
      else if(selected?.status==='refund_review')await syncCredits(selected.order_id,true);
      render();schedule();
    }catch(_){if(valid())onConfig?.(null);tell('暂时无法连接支付服务，请重试',true);}
  }
  return {mount,leave,offer,displayOffer};
})();
