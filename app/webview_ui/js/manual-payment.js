// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Account-bound payment requests. Receipt submission never grants credits. */
'use strict';
window.ManualPayments = (() => {
  let generation = 0;
  let activeDialog = null;
  let dialogExpiry = 0;
  let paymentExpiry = 0;
  let dialogCleanup = () => {};
  let entryExpiry = 0;
  let creation = null;
  const key = () => {
    const value = AppState.settings || {};
    return String(value.subscription_server_url || '') + '\u0000' + String(value.account_login_token || '')
      + '\u0000' + String(value.account_user_id || '');
  };
  const signedIn = () => !!AppState.settings?.account_login_token && window.AccountAccess?.allowed() === true;
  const ready = value => value?.configured === true && value.provider === 'alipay_personal';
  const statuses = {pending_payment:'待支付',pending_review:'待核验',credited:'已核验',cancelled:'已撤销',rejected:'未通过',expired:'已过期'};
  const requestId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{12,64}$/.test(value);
  const price = offer => '¥' + Number(offer.amount).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  function validOffer(value) {
    return value && ['standard','advanced'].includes(value.plan_id) && ['month','year'].includes(value.period)
      && value.months === (value.period === 'month' ? 1 : 12) && value.currency === 'CNY'
      && typeof value.amount === 'string' && /^\d{1,12}(?:\.\d{1,2})?$/.test(value.amount)
      && Number(value.amount) > 0 && Number.isSafeInteger(value.credits) && value.credits > 0;
  }
  function offersFrom(config) {
    const offers = [];
    for (const plan of Array.isArray(config.catalog) ? config.catalog : []) {
      if (!['standard','advanced'].includes(plan?.id) || plan.purchasable !== true || plan.status !== 'ready') continue;
      for (const source of Array.isArray(plan.offers) ? plan.offers : []) {
        const offer = {...source,plan_id:plan.id};
        if (source?.status === 'ready' && source.purchasable === true && validOffer(offer)) offers.push(offer);
      }
    }
    return offers.filter(offer => offers.filter(item => item.plan_id === offer.plan_id && item.period === offer.period).length === 1);
  }
  function safeRequest(value) {
    if (!value || !requestId(value.request_id) || !Object.hasOwn(statuses,value.status)
      || !validOffer(value.snapshot) || !Number.isSafeInteger(value.revision) || value.revision < 0
      || typeof value.association_code !== 'string' || !/^[A-Za-z0-9_-]{4,64}$/.test(value.association_code)
      || typeof value.expires_at !== 'string' || !Number.isFinite(Date.parse(value.expires_at))) return null;
    return value;
  }
  function leave() {
    generation++;
    clearTimeout(dialogExpiry);
    clearTimeout(paymentExpiry);dialogCleanup();dialogCleanup=()=>{};
    if (activeDialog?.isConnected && activeDialog.querySelector('#manualPaymentBody')) closeModal();
    activeDialog = null;
  }
  function mount(host, contract, {isCurrent,onSynced} = {}) {
    clearTimeout(entryExpiry);
    host.replaceChildren(); host.hidden = !ready(contract);
    if (!ready(contract)) return;
    if (creation && creation.key !== key()) creation = null;
    const label = document.createElement('span'); label.textContent = '支付宝（人工确认）';
    const button = document.createElement('button'); button.type = 'button'; button.id = 'manualPaymentOpen';
    button.className = 'btn small'; button.textContent = contract.enabled === true ? '支付申请' : '查看申请';
    button.disabled = !signedIn(); button.title = button.disabled ? '请先登录 FoilPage' : '';
    button.onclick = () => { if (signedIn() && (!isCurrent || isCurrent())) open({onSynced}); };
    host.className = 'manual-payment-entry'; host.append(label,button);
    const refreshEntryExpiry=()=>{
      if(!host.isConnected || (isCurrent && !isCurrent()))return;
      button.disabled=!signedIn();button.title=button.disabled?'请先登录 FoilPage':'';
      if(signedIn())entryExpiry=setTimeout(refreshEntryExpiry,Math.min(AppState.settings.account_access.expires_at*1000-Date.now()+1,2147483647));
    };
    refreshEntryExpiry();
  }
  async function open({onSynced} = {}) {
    if (!signedIn()) return;
    leave();
    const epoch = generation, account = key();
    const mask = openModal('支付宝（人工确认）', '<div id="manualPaymentBody" class="manual-payment-body"></div>',
      '<button class="btn ghost" id="dlgCancel">关闭</button>');
    activeDialog = mask;
    const refreshDialogExpiry=()=>{
      if(activeDialog!==mask || !mask.isConnected)return;
      if(!signedIn() || key()!==account){leave();return;}
      dialogExpiry=setTimeout(refreshDialogExpiry,Math.min(AppState.settings.account_access.expires_at*1000-Date.now()+1,2147483647));
    };
    refreshDialogExpiry();
    const dialog = mask.querySelector('.modal'); dialog.classList.add('manual-payment-dialog');
    const body = mask.querySelector('#manualPaymentBody');
    const valid = () => epoch === generation && mask.isConnected && activeDialog === mask && key() === account && signedIn();
    const visible=()=>{if(!document.hidden && valid())render();};
    document.addEventListener('visibilitychange',visible);
    dialogCleanup=()=>document.removeEventListener('visibilitychange',visible);
    mask.addEventListener('keydown',event=>{if(event.key==='Escape')leave();});
    mask.addEventListener('mousedown',event=>{if(event.target===mask)leave();});
    mask.querySelector('#dlgCancel').onclick = leave;
    mask.querySelector('.modal-x').onclick = leave;
    const notice = document.createElement('div'); notice.id = 'manualPaymentNotice'; notice.className = 'manual-payment-notice';
    notice.setAttribute('role','status'); notice.textContent = '正在读取支付申请…'; body.appendChild(notice);
    let config, requests = [], selected = null, busy = false, syncedRequest = '', receiptDraft = '', offerSelection = '', focusTarget = '', textSelection = null;
    const fail = message => { if(valid()) {notice.textContent=String(message || '支付申请暂不可用');notice.classList.add('is-error');} };
    const setNotice = message => {notice.classList.remove('is-error');notice.textContent=message;};
    const responseRequest = response => safeRequest(response?.request || response?.payment_request);
    const controls = document.createElement('div'); controls.className = 'manual-payment-controls';
    const content = document.createElement('div'); content.className = 'manual-payment-content';
    body.append(controls,content);
    async function run(action) {
      if (busy || !valid()) return;
      busy=true; render();
      try {await action();}
      catch (_) {fail('暂时无法连接支付服务，请重试');}
      finally {if(valid()){busy=false;render();}}
    }
    function remember(value) {
      const previous = requests.find(item=>item.request_id===value.request_id);
      if(previous) {
        const sameQuote=['plan_id','period','months','credits','currency'].every(name=>previous.snapshot[name]===value.snapshot[name])
          && Number(previous.snapshot.amount)===Number(value.snapshot.amount);
        if(!sameQuote){fail('支付申请报价已变更，请联系支持');return null;}
        if(previous.revision>value.revision){selected=previous;return previous;}
      }
      requests=requests.filter(item=>item.request_id!==value.request_id);requests.unshift(value);selected=value;
      return value;
    }
    async function syncCredits() {
      const result = await AppApi.refreshSubscription();
      if (!valid()) return;
      if (result?.ok !== true) {fail(result?.error || '核验已通过，积分同步未完成');return;}
      // refreshSubscription applies only a verified account/device license.
      const settings = await AppApi.settings();
      if (!valid()) return;
      if (!settings || settings.account_login_token !== AppState.settings.account_login_token
          || settings.account_user_id !== AppState.settings.account_user_id || !window.AccountAccess.accepts(settings.account_access)
          || settings.account_access.allowed !== true || !Number.isFinite(settings.account_access.expires_at)
          || settings.account_access.expires_at * 1000 <= Date.now()) {
        fail('核验已通过，积分同步未完成');return;
      }
      for(const name of ['license_token','license_expires_at','subscription_plan','subscription_active','subscription_expires_at','credit_balance']) {
        if(Object.hasOwn(settings,name))AppState.settings[name]=settings[name];
      }
      window.AccountAccess.apply(settings.account_access);
      syncedRequest=selected.request_id;setNotice('积分已到账');
      const status=await AppApi.subscriptionStatus();
      if(valid()){AppState.subscription=status;onSynced?.(status);}
    }
    function button(label,id,action,primary=false) {
      const value=document.createElement('button');value.type='button';value.id=id;value.textContent=label;
      value.className='btn small'+(primary?' primary':' ghost');value.disabled=busy;value.onclick=()=>run(action);return value;
    }
    function render() {
      if(!valid() || !config)return;
      clearTimeout(paymentExpiry);
      const paymentDeadline=selected?Date.parse(selected.expires_at):NaN;
      const effectiveStatus=selected?.status==='pending_payment' && paymentDeadline<=Date.now()?'expired':selected?.status;
      if(selected?.status==='pending_payment' && paymentDeadline>Date.now()) {
        paymentExpiry=setTimeout(()=>{if(valid())render();},Math.min(paymentDeadline-Date.now()+1,2147483647));
      }
      const focused=document.activeElement;
      if(focused?.id?.startsWith('manualPayment') && dialog.contains(focused)) {
        focusTarget=focused.id;
        textSelection=typeof focused.selectionStart==='number'?[focused.selectionStart,focused.selectionEnd]:null;
      }
      controls.replaceChildren();content.replaceChildren();
      const available=offersFrom(config);
      if(requests.length>1) {
        const label=document.createElement('label');label.className='manual-payment-label';label.textContent='支付申请';label.htmlFor='manualPaymentHistory';
        const history=document.createElement('select');history.className='ctrl';history.id='manualPaymentHistory';history.disabled=busy;
        for(const request of requests) {
          const option=document.createElement('option');option.value=request.request_id;
          option.textContent=statuses[request.status]+'，'+price(request.snapshot)+'，'+request.association_code;history.appendChild(option);
        }
        history.value=selected?.request_id || requests[0].request_id;
        history.onchange=()=>{selected=requests.find(item=>item.request_id===history.value);receiptDraft='';setNotice('');render();};
        controls.append(label,history);
      }
      const canCreate=config.enabled===true && available.length>0;
      if(!selected || ['cancelled','rejected','expired','credited'].includes(effectiveStatus)) {
        if(canCreate) {
          const label=document.createElement('label');label.className='manual-payment-label';label.htmlFor='manualPaymentOffer';label.textContent='套餐';
          const picker=document.createElement('select');picker.className='ctrl';picker.id='manualPaymentOffer';picker.disabled=busy;
          available.forEach((offer,index)=>{const option=document.createElement('option');option.value=String(index);
            option.textContent=(offer.plan_id==='standard'?'标准版':'专业版')+'（'+(offer.period==='month'?'月付':'年付')+'），'+price(offer);picker.appendChild(option);});
          const selectedOffer=available.findIndex(offer=>offer.plan_id+'-'+offer.period===offerSelection);
          if(selectedOffer>=0)picker.value=String(selectedOffer);
          if(creation?.key===account){const index=available.findIndex(offer=>offer.plan_id===creation.plan_id&&offer.period===creation.period);if(index>=0)picker.value=String(index);}
          const quota=document.createElement('div');quota.className='manual-payment-quote';quota.id='manualPaymentQuote';
          const refreshQuote=()=>{const offer=available[Number(picker.value)];quota.textContent=offer.credits.toLocaleString('zh-CN')+' 积分（'+(offer.period==='month'?'30 天':'360 天')+'）';};
          picker.onchange=()=>{const offer=available[Number(picker.value)];offerSelection=offer.plan_id+'-'+offer.period;creation=null;refreshQuote();};refreshQuote();
          const create=button('生成支付申请','manualPaymentCreate',async()=>{
            const offer=available[Number(picker.value)];
            if(!creation || creation.key!==account || creation.plan_id!==offer.plan_id || creation.period!==offer.period) {
              const id=globalThis.crypto?.randomUUID?.();
              if(!id){fail('无法生成支付申请编号，请重启应用后重试');return;}
              creation={key:account,plan_id:offer.plan_id,period:offer.period,id};
            }
            const response=await AppApi.createManualPayment(offer.plan_id,offer.period,creation.id);
            if(!valid())return;
            const request=responseRequest(response);
            if(response?.ok!==true || !request || request.snapshot.plan_id!==offer.plan_id || request.snapshot.period!==offer.period) {
              fail(response?.error || '支付申请响应无效，请重试');return;
            }
            creation=null;if(remember(request))setNotice('支付申请已生成');
          },true);
          controls.append(label,picker,quota,create);
        } else if(!selected)setNotice('暂未开放新的支付申请');
      }
      if(!selected){restoreFocus();return;}
      const state=document.createElement('div');state.className='manual-payment-state';state.id='manualPaymentState';state.setAttribute('role','status');
      state.textContent=selected.status==='credited' && syncedRequest===selected.request_id?'已到账':statuses[effectiveStatus];
      const amount=document.createElement('div');amount.className='manual-payment-amount';amount.textContent=price(selected.snapshot);
      const code=document.createElement('div');code.className='manual-payment-reference';
      const caption=document.createElement('span');caption.textContent='关联码';const value=document.createElement('strong');value.id='manualPaymentCode';value.textContent=selected.association_code;code.append(caption,value);
      const quote=document.createElement('div');quote.className='manual-payment-quote';quote.textContent=(selected.snapshot.plan_id==='standard'?'标准版':'专业版')+'（'+(selected.snapshot.period==='month'?'月付':'年付')+'），'+selected.snapshot.credits.toLocaleString('zh-CN')+' 积分';
      content.append(state,amount,quote,code);
      if(['pending_payment','expired'].includes(effectiveStatus)) {
        if(effectiveStatus==='pending_payment') {
          const deadline=document.createElement('div');deadline.className='manual-payment-quote';deadline.id='manualPaymentDeadline';
          deadline.textContent='付款截止 '+new Date(paymentDeadline).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});content.appendChild(deadline);
        }
        if(effectiveStatus==='pending_payment' && typeof config.qr_data_url==='string' && config.qr_data_url.length<=131072 && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(config.qr_data_url)) {
          const qr=document.createElement('img');qr.id='manualPaymentQr';qr.className='manual-payment-qr';qr.alt='支付宝收款二维码';qr.src=config.qr_data_url;content.appendChild(qr);
        }
        if(effectiveStatus==='pending_payment') {
          if(config.recipient_label){const recipient=document.createElement('div');recipient.className='manual-payment-recipient';recipient.textContent='收款方：'+String(config.recipient_label).slice(0,100);content.appendChild(recipient);}
          if(config.instructions){const instructions=document.createElement('p');instructions.className='manual-payment-instructions';instructions.textContent=String(config.instructions).slice(0,2000);content.appendChild(instructions);}
        } else {
          const overdue=document.createElement('p');overdue.className='manual-payment-instructions';overdue.textContent='付款时限已过；此前已付款可提交核验';content.appendChild(overdue);
        }
        const form=document.createElement('form');form.className='manual-payment-receipt';
        const label=document.createElement('label');label.className='manual-payment-label';label.htmlFor='manualPaymentTransaction';label.textContent='支付宝交易号';
        const input=document.createElement('input');input.id='manualPaymentTransaction';input.className='ctrl';input.autocomplete='off';input.spellcheck=false;input.maxLength=64;input.inputMode='numeric';input.placeholder='账单详情中的完整交易号';input.value=receiptDraft;input.disabled=busy;
        input.oninput=()=>{receiptDraft=input.value;};
        const submit=button('提交核验','manualPaymentSubmit',async()=>{
          const receipt=receiptDraft.trim();if(!/^[0-9]{16,64}$/.test(receipt)){fail('请填写支付宝账单中的完整交易号');return;}
          const id=selected.request_id,response=await AppApi.submitManualPayment(id,receipt);
          if(!valid())return;const request=responseRequest(response);
          if(response?.ok!==true || !request || request.request_id!==id){fail(response?.error || '提交未完成，请重试');return;}
          const updated=remember(request);if(!updated)return;receiptDraft='';
          if(updated.status==='credited'){setNotice('');await syncCredits();}
          else setNotice(updated.status==='pending_review'?'已提交，等待人工核验':statuses[updated.status]);
        },true);
        form.onsubmit=event=>{event.preventDefault();submit.click();};form.append(label,input,submit);content.appendChild(form);
      } else if(effectiveStatus==='pending_review') {
        const pending=document.createElement('p');pending.className='manual-payment-instructions';pending.textContent='核验通过后发放积分。';content.appendChild(pending);
      }
      const actions=document.createElement('div');actions.className='manual-payment-actions';
      actions.appendChild(button(selected.status==='credited'?'同步积分':'刷新状态','manualPaymentRefresh',async()=>{
        const id=selected.request_id,response=await AppApi.manualPaymentStatus(id);if(!valid())return;
        const request=responseRequest(response);
        if(response?.ok!==true || !request || request.request_id!==id){fail(response?.error || '状态读取失败，请重试');return;}
        const updated=remember(request);if(!updated)return;setNotice('');if(updated.status==='credited')await syncCredits();
      }));
      if(effectiveStatus==='pending_payment') {
        actions.appendChild(button('撤销申请','manualPaymentCancel',async()=>{
          const id=selected.request_id,response=await AppApi.cancelManualPayment(id);if(!valid())return;
          const request=responseRequest(response);
          if(response?.ok!==true || !request || request.request_id!==id){fail(response?.error || '撤销未完成，请刷新状态');return;}
          if(remember(request)){receiptDraft='';setNotice('申请已撤销');}
        }));
      }
      content.appendChild(actions);
      restoreFocus();
    }
    function restoreFocus() {
      if(!focusTarget)return;
      const target=dialog.querySelector('#'+focusTarget)
        || dialog.querySelector('#manualPaymentTransaction, #manualPaymentRefresh, #manualPaymentOffer');
      if(target && !target.disabled) {
        target.focus({preventScroll:true});
        if(textSelection && typeof target.setSelectionRange==='function')target.setSelectionRange(...textSelection);
      } else dialog.focus({preventScroll:true});
    }
    try {
      const responses=await Promise.allSettled([AppApi.manualPaymentConfig(),AppApi.manualPaymentRequests()]);
      if(!valid())return;
      const configured=responses[0].status==='fulfilled'?responses[0].value:null;
      if(configured?.ok!==true || !ready(configured)){fail(configured?.error || '支付通道尚未配置');return;}
      config=configured;
      const listing=responses[1].status==='fulfilled'?responses[1].value:null;
      if(listing?.ok!==true){fail(listing?.error || '暂时无法读取已有申请，请重试');return;}
      requests=(Array.isArray(listing.requests)?listing.requests:[]).map(safeRequest).filter(Boolean);
      selected=requests.find(item=>['pending_payment','pending_review'].includes(item.status)) || requests[0] || null;
      if(creation?.key===account && selected?.snapshot.plan_id===creation.plan_id && selected?.snapshot.period===creation.period)creation=null;
      setNotice('');render();
      if(selected?.status==='credited')await run(syncCredits);
    }catch(_){fail('暂时无法连接支付服务，请重试');}
  }
  return {mount,leave};
})();
