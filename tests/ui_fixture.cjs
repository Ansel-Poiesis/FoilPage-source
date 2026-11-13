// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Serialized into a fresh browser page; no user data or provider calls. */
module.exports = function installMock(options = {}) {
      const title = '从声音到文字：如何在保留原话、例子和限定条件的前提下，让一篇很长的访谈记录变得清晰易读——完整标题不应被截断';
      const docs = Array.from({length:9}, (_,i) => ({task_id:'job:'+i,title:i ? ['一次关于创作的谈话','把课堂笔记留给未来的自己','声音里的细节与表达'][i%3] : title,status:'done',has_final:true,has_raw:true,source_path:'demo.mp4',updated_at:'2026-09-19 12:00:00',tags:['阅读','创作']}));
      let settings = {theme:'dark',font_scale:1,accent:'green',agent_enabled:true,agent_editing_level:'balanced',agent_profile:'auto',agent_custom_profiles:[],close_to_tray:true,task_notifications:true,onboarding_completed_version:'fixture',download_dir:'C:\\fixture\\downloads'};
      settings.background_style = 'glow'; settings.setup_completed = false;
      // Existing suites model prior explicit consent; consent tests opt out.
      settings.terms_accepted_version = options.termsAccepted === false ? 0 : options.termsVersion ?? 2;
      settings.terms_cross_border_accepted_version = settings.terms_accepted_version;
      settings.terms_accepted_service = options.paymentServer || '';
      // Most suites isolate non-auth product behavior. Auth gate regressions
      // explicitly request denied/expired access instead of skipping the gate.
      settings.account_access={required:true,allowed:options.accountAccess!==false,expires_at:options.accountExpiry ?? Math.floor(Date.now()/1000)+3600,reason:options.accountAccess===false?'login_required':'verified',revision:0};
      const tasks = docs.map((d,i) => ({...d,display_title:d.title,kind:i%2?'download':'transcription',progress:i===1?.46:1,progress_text:i===1?'正在下载':'已完成',status:i===1?'downloading':'done',source:'本地转写',result_category:'阅读',raw_path:i%2?'':'raw.md',final_path:i%2?'':'final.md'}));
      // TASK-8.17：封面展示 opt-in 注入（下载中的任务带封面，其余不带）。
      if (options.taskCover) tasks[1].cover_path = 'C:\\fixture\\downloads\\cover-demo.jpg';
      // TASK-8.18：图集任务 opt-in 注入（该任务变为已完成的图文下载）。
      if (options.taskGallery) Object.assign(tasks[1], {
        is_gallery: true, status: 'done', progress: 1, progress_text: '图文已保存',
        kind: 'download', downloaded_path: 'C:\\fixture\\downloads\\gallery-demo',
      });
      window.__fixtureTitle = title;
      const windowChrome = {enabled:options.nativeChrome === true,maximized:false};
      window.__fixtureWindowChrome = windowChrome;
      window.__fixtureLaunchContext = {mode:options.launchMode || 'manual', sequence:options.launchSequence ?? 1};
      if (options.pendingNativeView) window.__pendingNativeView = options.pendingNativeView;
      const videoItems = options.videoItems || [
        {id:'download:one',title:'一次关于创作的谈话',source:'抖音',downloaded_at:'2026-10-04',duration_seconds:184,available:true,deletable:true,delete_label:'删除视频',deletion_note:'删除下载的视频文件，保留转写文本',
         thumbnail_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADwAAABpCAAAAABD1B/qAAAAf0lEQVR4nGNgYOHgERSVlFNS09Y3trBxcPX0DQqLik9OzykoqaxtbO3qmzR99vwlK9Zs3Lpz36Fjp89fvnHnwdOXbz99+/WfYVTzqOZRzaOaRzWPah7VPKp5VPOo5lHNo5pHNY9qHtU8qnlU86jmUc2jmkc1j2oe1TyqeZBqBgBmATaCT64IVgAAAABJRU5ErkJggg=='},
        {id:'download:two',title:'声音里的细节与表达',source:'B站',downloaded_at:'2026-10-03',duration_seconds:3661,available:true,deletable:true,delete_label:'删除视频',deletion_note:'删除下载的视频文件，保留转写文本',
         thumbnail_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAKAAAABaCAAAAAAarQKOAAABKElEQVR4nO3O4SpDAQCA0SvSRIsmTZOIRLSsSUSLSEQiWSSytEUiEk1rskS0iEQkIpGIRCSaRJZoWhOJSDSJSDStSbyGH995giMIXt4+viI//wBxYJAkOEQaKgsLj4iMio6JjYtPkCcqlEnJKalp6aqMzKzsnNy8/ILCouKSUnVZeUVlVbWmRqurratvaGxqbmlt07cbjB2dpq7unt4+c//A4NDwyOjY+MTk1PTM7Nz8wuLS8srq2vrG5tb2jmV3b//g0Hp0fGI7tTvOzi8ur65vbu/uH5yPT88vr2/vH5+uL7fn++dXIEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAkSJEiQIEGCBAn+n+AfV/rqCpmW7FgAAAAASUVORK5CYII='},
        {id:'download:missing',title:'已清理的视频',source:'YouTube',downloaded_at:'2026-10-02',available:false,deletable:true,delete_label:'移除记录',deletion_note:'移除登记，保留转写文本'},
      ];
      let importRequest = 0;
      const importReply = async () => {
        const reply = options.importReplies?.[importRequest++];
        const delay = reply?.delay ?? options.importDelay;
        if (delay) await new Promise(resolve => setTimeout(resolve, delay));
        if (reply?.throws) throw new Error('fixture import rejected');
        return reply?.value ?? options.importResult ?? {ok:true,imported:1,enqueued:['fixture-job'],skipped:0,failed:0};
      };
      window.__MOCK__ = {
        get_product_notifications:()=>options.productNotifications || {ok:true,events:[],updates:[]},
        pick_video_scan_directory:()=>({ok:true,path:options.videoDirectory || 'D:\\Videos'}),
        open_url:(url)=>({ok:typeof url === 'string' && url.startsWith('https://github.com/Ansel-Poiesis/FoilPage-releases/releases/')}),
        get_bootstrap:()=>({tasks,release_channel:options.releaseChannel || 'formal',settings:{...settings},launch_context:{...window.__fixtureLaunchContext},window_chrome:windowChrome,worker:{running:true,label:'Worker 运行中',meta:'large-v3（GPU）'},summary:{total:9,active:1,terminal:8,completed:8,progress:8/9}}),
        get_settings:()=>({...settings}),
        get_agent_activity:()=>options.agentActivity || {ok:true,enabled:true,last_saved_at:null,last_revision:'fixture',records:[]},
        get_video_scan_status:()=>({ok:true,scan:options.videoScan || {running:false,state:'ready',revision:1}}),
        get_failed_task_retention:()=>({ok:true,enabled:false,retention_days:0,jobs:[],downloads:[],files_deleted:0}),
        get_tasks:()=>tasks,
        get_task_gallery:async id=>{
          if (!options.taskGallery || !String(id).endsWith('1')) return {ok:false,error:'该任务不是图集'};
          return {ok:true,count:2,urls:['http://127.0.0.1:18765/v1/taskcover/fixture.jpg?key=f&i=0','http://127.0.0.1:18765/v1/taskcover/fixture.jpg?key=f&i=1']};
        },
        get_task_cover:async id=>{
          if (options.taskCoverFail) return {ok:false,error:'该任务没有封面'};
          if (!options.taskCover || !String(id).endsWith('1')) return {ok:false,error:'该任务没有封面'};
          return {ok:true,url:'http://127.0.0.1:18765/v1/taskcover/fixturecover.jpg?key=fixture'};
        },
        get_window_chrome:()=>windowChrome,
        minimize_window:()=>({ok:true}),
        toggle_maximize_window:()=>{windowChrome.maximized=!windowChrome.maximized;return {ok:true,maximized:windowChrome.maximized};},
        hide_window:()=>({ok:true}),
        get_downloaded_videos:async()=>{
          if (options.videoDelay) await new Promise(resolve=>setTimeout(resolve,options.videoDelay));
          return options.videosFail ? {ok:false,error:'读取视频失败'} : {ok:true,items:videoItems.slice(),total:videoItems.length};
        },
        get_agent_setup_guide:()=>({ok:true,markdown:'# 当前 FoilPage 实例\n\n版本与当前目录，基础配置与修复指南。',file_name:'ShengJian-Agent-Setup.md'}),
        open_video_location:()=>options.openVideoFail ? {ok:false,error:'视频目录暂时无法打开'} : {ok:true},
        delete_video:async id=>{
          if(options.deleteVideoDelay) await new Promise(resolve=>setTimeout(resolve,options.deleteVideoDelay));
          if(options.deleteVideoFail) return {ok:false,error:'视频删除失败，请重试'};
          const index=videoItems.findIndex(item=>item.id===id);
          if(index>=0 && videoItems[index].deletable===false) return {ok:false,error:'视频仍被活动任务使用'};
          const item=index>=0 ? videoItems.splice(index,1)[0] : null;
          return {ok:true,message:item?.delete_label==='移除记录'?'记录已移除':'视频已删除',items:videoItems.slice()};
        },
        get_video_preview:async id=>{
          if(options.previewDelay) await new Promise(resolve=>setTimeout(resolve,options.previewDelay));
          if(options.previewFail) return {ok:false,error:'视频文件已移除'};
          return {ok:true,title:videoItems.find(item=>item.id===id)?.title || '视频',preview_url:options.previewUrl || 'http://127.0.0.1:18765/v1/media/fixture/video.mp4',mime_type:'video/mp4'};
        },
        get_documents:()=>docs,
        get_tag_index:()=>[{tag:'阅读',count:9}],
        get_reader:()=>({ok:true,tab:'final',path:'C:\\fixture\\docs\\final.md',text:'# 从声音到文字\n\n## 先留下原话\n\n每一个例子都值得留下。**不改变原意**，也是整理的起点。\n\n## 给文字呼吸的空间\n\n分段、小标题与适当的重点，让长文更容易阅读。'}),
        play_media:()=>({ok:true}),
        save_settings:changes=>{Object.assign(settings,changes);return {...settings};},
        browser_status:()=>({ok:true,running:true,configured:true,extension_dir:'C:\\Programs\\ShengJian\\browser-extension'}),
        begin_browser_pairing:()=>({ok:true,seconds:120}),
        pick_and_import:importReply, pick_folder_and_import:importReply,
        import_files:importReply,
        download_from_url:()=>({ok:true}),
        system_notify:()=>({ok:true}),
        get_models:()=>[],
        cache_status:()=>({}),
        get_subscription_status:()=>({hosted_agent_ready:options.orderReview ? false : window.__fixtureHostedReady ?? options.hostedReady === true,hosted_agent_reason:'托管模型尚未配置',active:false,plan_id:'free',plan:null,monthly_agent_units:0,max_concurrency:1,credit_balance:options.orderReview ? 500 : 0,agent_usage_remaining:0,order_review:{paused:options.orderReview===true,paused_orders:options.orderReview?1:0},entitlements:['transcribe','byok_agent','local_batch']}),
        get_trial_state:()=>({ok:true,active:true,days_left:5,started_at:'2026-09-21T00:00:00+00:00',expires_at:'2026-10-06T00:00:00+00:00',tampered:false}),
        get_software_access:()=>options.softwareAccess || {ok:true,status:'trial',allowed:true,trial_days:15,days_left:5,trial_expires_at:'2026-10-06T00:00:00+00:00',unlocked:false,price:'9.90',currency:'CNY',source:'signed_license'},
        create_software_unlock_order:()=>({ok:false,error:'测试不会创建真实解锁订单'}),
        get_subscription_plans:()=>[
          {id:'standard',name:'标准版',tier:1,audience:'个人高频用户',price:'¥19.8/月',annual_price:'¥190.08/年',monthly_agent_units:500,max_concurrency:2,unit_label:'积分',quota_note:'1 积分 ≈ 2,000 字整理，更重模型 ×3 消耗',quota_examples:[['一分钟口播','≈400 字','1 积分'],['十分钟视频','≈3,500 字','2 积分'],['一小时长视频','≈14,000 字','8 积分']],description:'托管 Agent 500 积分/次，视觉校对与批量整理',features:['本地转写与自带密钥整理','500 积分/次，可重复购买','字幕视觉校对','批量整理任务','2 路并发转写'],entitlements:['transcribe','byok_agent','local_batch','hosted_agent','visual_proofread','batch_organize']},
          {id:'advanced',name:'专业版',tier:2,audience:'内容生产者',price:'¥49.8/月',annual_price:'¥766.08/年',monthly_agent_units:2000,max_concurrency:4,unit_label:'积分',quota_note:'1 积分 ≈ 2,000 字整理，更重模型 ×3 消耗',quota_examples:[['一分钟口播','≈400 字','1 积分'],['十分钟视频','≈3,500 字','2 积分'],['一小时长视频','≈14,000 字','8 积分']],description:'托管 Agent 2,000 积分/次，支持4路并发转写',features:['包含标准版全部权益','2,000 积分/次，可重复购买','4 路并发转写'],entitlements:['transcribe','byok_agent','local_batch','hosted_agent','visual_proofread','batch_organize','export_enhanced','priority_support']},
        ],
      };
  if (options.settings) Object.assign(settings,options.settings);
  if (options.firstRun) settings.onboarding_completed_version = '';
  if (options.theme) settings.theme = options.theme;
  if (options.scale) settings.font_scale = options.scale;
  if (options.checkoutUrl) settings.subscription_checkout_url = options.checkoutUrl;
  if (options.paymentServer) settings.subscription_server_url = options.paymentServer;
  if (options.loggedIn) { settings.account_login_token = 'login-fixture'; settings.account_display_name = options.displayName || options.settings?.account_display_name || '微信用户'; settings.account_user_id = 'u-fixture'; }
  window.__fixtureCalls = [];
  window.__fixtureCallArgs = [];
  const setupModels = () => options.modelItems ? options.modelItems.map(item => ({...item})) : [
    { name:'tiny', size_label:'75 MB', description:'快速试用', verified:false, installed:false, ready:false, reason:'未下载', bytes_on_disk:0 },
    { name:'small', size_label:'488 MB', description:'日常转写', verified:!!options.installed, installed:!!options.installed, ready:!!options.installed, reason:options.installed?'':'未下载', bytes_on_disk:0 },
  ];
  const setupCheckPayload = () => ({ ok: true,
    cuda: {available:true, device_count:1, compute_types:['float16']},
    vram_mb: 8192, free_bytes: 20e9, cache_root: 'C:\\fixture\\hf',
    settings: { whisper_model: settings.whisper_model || (options.installed ? 'small' : 'tiny'), device:'cuda', hf_endpoint:'https://hf-mirror.com', model_dir:'', download_dir:settings.download_dir },
    rows: [
      {key:'gpu',label:'GPU / CUDA',status:'ok',value:'NVIDIA CUDA（1 台）',hint:''},
      {key:'vram',label:'显存',status:'info',value:'8 GB',hint:''},
      {key:'disk',label:'磁盘剩余',status:'ok',value:'18.6 GB',hint:''},
      {key:'mirror',label:'下载镜像',status:'ok',value:'https://hf-mirror.com',hint:''},
      {key:'cache_dir',label:'缓存目录',status:'ok',value:'C:\\fixture\\hf',hint:''},
      {key:'default_model',label:'默认模型',status:options.installed?'ok':'warn',value:options.installed?'small（已安装）':'tiny（未下载）',hint:options.installed?'':'转写前需先下载默认模型，或切到已安装的档位'},
    ],
    models: setupModels(),
    tiers: [
      {name:'tiny',tier:1,tier_label:'极速',verdict:'ready',detail:'GPU 可直接运行',recommended:false},
      {name:'small',tier:3,tier_label:'均衡',verdict:'ready',detail:'GPU 可直接运行',recommended:true},
    ],
    fixes: options.pendingFixes ? [{key:'mirror',label:'补默认下载镜像 https://hf-mirror.com',action:'set_endpoint'}] : [],
    download: {name:'',message:'',progress:null,error:''},
  });
  let contractRequest = 0;
  let setupRequest = 0;
  let managedLogin = false;
  let buyerVerified = options.buyerVerified === true;
  let buyerStopped = false;
  let buyerPollCount = 0;
  let accountDevices = options.deviceIds ? [...options.deviceIds] : ['device-fixture', 'device-old'];
  const deviceView = () => ({ok:true, user_id:settings.account_user_id || 'u-fixture',buyer_verified:buyerVerified,
    credit_balance:options.orderReview ? 500 : 0,order_review:{paused:options.orderReview===true,paused_orders:options.orderReview?1:0},
    devices:[...accountDevices], max_devices:2,
    current_device_id:managedLogin ? 'device-new' : 'device-fixture', device_management_only:managedLogin,
    device_details:accountDevices.map(id=>({device_id:id,first_seen:'2026-10-01T00:00:00+00:00'}))});
  Object.assign(window.__MOCK__, {
    get_models: async () => {
      if (options.modelDelay) await new Promise(resolve => setTimeout(resolve, options.modelDelay));
      return { ok: true, free_bytes: 20e9, models: setupModels() };
    },
    get_setup_check: async () => {
      const reply = options.setupReplies?.[setupRequest++];
      const snapshot = setupCheckPayload();
      const delay = reply?.delay ?? options.modelDelay;
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      return reply?.value ? {...snapshot, ...reply.value} : snapshot;
    },
    apply_setup_fixes: async () => Object.assign(setupCheckPayload(), {applied: []}),
    download_model: async () => {
      if(options.downloadDelay) await new Promise(resolve=>setTimeout(resolve,options.downloadDelay));
      return {ok:false,error:'测试不会下载模型'};
    },
    pick_setting_dir: async () => {
      if(options.pickerDelay) await new Promise(resolve=>setTimeout(resolve,options.pickerDelay));
      if(options.pickerCancel) return {ok:false,cancelled:true};
      if(options.pickerFail) return {ok:false,error:'目录保存失败'};
      settings.download_dir=options.pickDirectory || 'C:\\fixture\\downloads-new';
      return {ok:true,settings:{...settings}};
    },
    delete_model: () => ({ok:false}),
    open_dir: () => ({ok:true}),
    batch_agent_translate: () => ({ok:false,error:'测试不会调用模型'}),
    get_subscription_contract: async () => {
      const reply = options.contractReplies?.[contractRequest++];
      if (reply) {
        if (reply.delay) await new Promise(resolve => setTimeout(resolve, reply.delay));
        return reply.value;
      }
      if (options.contractDelay) await new Promise(resolve => setTimeout(resolve, options.contractDelay));
      if (options.contractThrows) throw new Error('fixture service unavailable');
      if (window.__fixtureContractOverride) return window.__fixtureContractOverride;
      const defaultCatalog = ['standard', 'advanced'].map((id, index) => ({
        id, status:'ready', purchasable:true,
        // Deliberately differ from legacy client prices to expose accidental fallback.
        offers:[
          {period:'month',months:1,credits:index ? 5000 : 500,amount:index ? '47.50' : '23.80',currency:'CNY',price:index ? '¥47.5/月' : '¥23.8/月',status:'ready',purchasable:true},
          {period:'year',months:12,credits:index ? 60000 : 6000,amount:index ? '510.00' : '259.00',currency:'CNY',price:index ? '¥510/年' : '¥259/年',status:'ready',purchasable:true},
        ],
      }));
      return {
        checkout_ready: !!(options.checkoutUrl || options.serviceReady) && options.serviceBlocked !== true && !options.serviceOffline,
        available_plans: options.availablePlans || ['standard', 'advanced'],
        configuration_ready: !!options.serviceReady && !options.serviceOffline,
        public_catalog: options.serviceOffline ? [] : (options.publicCatalog === undefined
          ? ((options.checkoutUrl || options.serviceReady) ? defaultCatalog : []) : options.publicCatalog),
        login_methods: options.serviceOffline ? {} : (options.loginMethods || {
          wechat: !!options.paymentServer && !options.loginUnavailable,
          google: !!options.paymentServer && !options.loginUnavailable,
          email: !!options.paymentServer && !options.emailUnavailable,
        }),
        buyer_verification: {required:options.buyerRequired === true,configured:options.buyerConfigured === true,provider:'afdian'},
        payments: {redeem:options.paymentsEnabled !== false},
        manual_payments: options.manualPayments || {configured:false,enabled:false,provider:'alipay_personal',catalog:[]},
        alipay_payments: options.alipayPayments || {configured:false,enabled:false,processing_enabled:false,provider:'alipay_page_pay',catalog:[]},
        gates: options.serviceBlocked ? ['爱发电方案未配置'] : ['真实登录和订单验收'],
      };
    },
    check_subscription_service: () => window.__MOCK__.get_subscription_contract(),
    get_alipay_payment_config: () => options.alipayPaymentConfig || {ok:true,...(options.alipayPayments || {
        configured:false,enabled:false,processing_enabled:false,provider:'alipay_page_pay',catalog:[],
      })},
    get_manual_payment_config: () => options.manualPaymentConfig || {ok:false,error:'人工确认尚未配置'},
    list_manual_payment_requests: () => ({ok:true,requests:options.manualRequests || []}),
    create_manual_payment_request: () => ({ok:false,error:'测试不会创建真实付款'}),
    submit_manual_payment_request: () => ({ok:false,error:'测试不会提交真实交易'}),
    get_manual_payment_request: () => ({ok:false,error:'测试不会读取真实支付申请'}),
    cancel_manual_payment_request: () => ({ok:false,error:'测试不会撤销真实支付申请'}),
    refresh_subscription: () => {
      if(options.refreshInvalid) {
        settings.account_login_token=''; settings.account_display_name=''; settings.account_user_id='';
        settings.account_access={required:true,allowed:false,expires_at:null,reason:'login_required',revision:(settings.account_access?.revision||0)+1};
        return {ok:false,error:'登录已失效，请重新登录'};
      }
      return {ok:true};
    },
    begin_subscription: async () => {
      if (options.checkoutDelay) await new Promise(resolve => setTimeout(resolve, options.checkoutDelay));
      return {ok:false};
    },
    wechat_login_start: () => options.loginUnavailable
      ? {ok:false, error:'微信登录未配置（需 WECHAT_APP_ID/WECHAT_APP_SECRET/服务端公网地址）'}
      : {ok:true, login_token:'login-fixture', auth_url:'https://open.weixin.qq.com/connect/qrconnect?appid=x'},
    email_login_start: async () => {
      if (options.emailDelay) await new Promise(resolve => setTimeout(resolve, options.emailDelay));
      return options.emailFail ? {ok:false, error:'验证码暂时无法发送',retry_after:options.emailRetryAfter}
        : {ok:true, dev_code:'123456', hint:'DEV 模式',resend_after_seconds:options.emailResendAfter ?? 60};
    },
    email_login_verify: async () => {
      if (options.emailDelay) await new Promise(resolve => setTimeout(resolve, options.emailDelay));
      if (options.deviceLimit) {managedLogin=true;return {ok:false,status:'device_limit',account:deviceView()};}
      if (!options.emailFail) {settings.account_login_token='login-email-fixture';settings.account_display_name='fixture用户';settings.account_user_id='u-email';settings.account_access={required:true,allowed:true,expires_at:Math.floor(Date.now()/1000)+3600,reason:'verified',revision:(settings.account_access?.revision||0)+1};}
      return options.emailFail ? {ok:false, error:'验证码不正确'}
        : {ok:true, login_token:'login-email-fixture', license_token:'', account:{user_id:'u-email', display_name:'fixture用户', active:true, plan_id:'standard', credit_balance:500, expires_at:'2026-10-26T00:00:00+00:00', identities:['email'], devices:['device-fixture'], trial:{active:false, days_left:0}}};
    },
    google_login_start: () => options.loginUnavailable
      ? {ok:false, error:'Google 登录未配置'}
      : {ok:true, login_token:'login-google-fixture', auth_url:'https://accounts.google.com/o/oauth2/v2/auth?client_id=x'},
    google_login_cancel:()=>({ok:true,status:'cancelled'}),
    web_login_start:()=>({ok:true,status:'pending',expires_in_seconds:600}),
    web_login_cancel:()=>({ok:true}),
    web_login_poll:()=>{
      if(options.webLoginPending)return{ok:true,status:'pending'};
      settings.account_login_token='web-authorized-fixture';settings.account_user_id='u-fixture';settings.account_display_name='网页账户';
      settings.account_access={required:true,allowed:true,expires_at:Math.floor(Date.now()/1000)+86400,reason:'verified',revision:(settings.account_access?.revision||0)+1};
      return{ok:true,status:'ok'};
    },
    wechat_login_poll: () => {
      if(options.loginUnavailable)return {ok:false,status:'invalid'};
      settings.account_login_token='verified-provider-session';settings.account_display_name='微信用户';settings.account_user_id='u-fixture';
      settings.account_access={required:true,allowed:true,expires_at:Math.floor(Date.now()/1000)+3600,reason:'verified',revision:(settings.account_access?.revision||0)+1};
      return {ok:true,status:'ok',login_token:settings.account_login_token,license_token:'',account:{user_id:'u-fixture',display_name:'微信用户',active:true,plan_id:'standard',credit_balance:500,expires_at:'2026-10-26T00:00:00+00:00',identities:['wechat'],devices:['device-fixture'],trial:{active:false,days_left:0}}};
    },
    account_logout: () => {
      if (options.logoutFail) return {ok:false,error:'退出暂时失败'};
      settings.account_login_token = '';
      settings.account_display_name = '';
      settings.account_access={required:true,allowed:false,expires_at:null,reason:'login_required',revision:(settings.account_access?.revision||0)+1};
      return {ok:true};
    },
    update_account_profile: async (displayName, avatar = null) => {
      if (options.profileDelay) await new Promise(resolve=>setTimeout(resolve, options.profileDelay));
      if (options.profileFail) return {ok:false,error:'资料保存失败'};
      if (!settings.account_login_token) return {ok:false,error:'请先登录'};
      settings.account_display_name = displayName;
      if (avatar !== null) settings.account_avatar = avatar;
      return {ok:true,account:{display_name:settings.account_display_name,avatar:settings.account_avatar || ''}};
    },
    get_account_devices: async () => {
      if (options.devicesDelay) await new Promise(resolve=>setTimeout(resolve,options.devicesDelay));
      return options.devicesFail ? {ok:false,error:'设备服务暂时不可达'} : deviceView();
    },
    unbind_account_device: async id => {
      if (options.devicesDelay) await new Promise(resolve=>setTimeout(resolve,options.devicesDelay));
      if (options.unbindFail) return {ok:false,error:'设备解绑失败'};
      accountDevices=accountDevices.filter(value=>value!==id);
      const local=id==='device-fixture' && !managedLogin;
      if (local) {settings.account_login_token='';settings.account_display_name='';settings.account_user_id='';}
      return {...deviceView(),current_device_unbound:local};
    },
    complete_account_login: () => {
      if (accountDevices.length>=2) return {ok:false,error:'设备数量已达上限'};
      managedLogin=false;accountDevices.push('device-new');
      settings.account_login_token='login-device-new';settings.account_display_name='设备用户';settings.account_user_id='u-new';
      return {ok:true,login_token:'login-device-new',account:{display_name:'设备用户',user_id:'u-new'}};
    },
    cancel_account_login: () => {managedLogin=false;return {ok:true};},
    buyer_verification_start: () => {buyerStopped=false;buyerPollCount=0;return options.buyerStartFail
      ? {ok:false,error:'买家验证未配置'} : {ok:true,status:'pending'};},
    buyer_verification_poll: async () => {
      if(options.buyerPollDelay) await new Promise(resolve=>setTimeout(resolve,options.buyerPollDelay));
      if(options.buyerPollFail) return {ok:false,status:'error',error:'买家验证未完成'};
      if(buyerStopped || buyerPollCount++ < (options.buyerPendingPolls || 0)) return {ok:true,status:'pending'};
      buyerVerified=true;return {ok:true,status:'verified',buyer_verified:true,account:deviceView()};
    },
    buyer_verification_stop: () => {buyerStopped=true;return {ok:true,status:'cancelled'};},
    redeem_subscription_order: async () => {
      if (options.redeemDelay) await new Promise(resolve=>setTimeout(resolve,options.redeemDelay));
      if (options.redeemAccount && !options.redeemFail) {
        settings.account_login_token='redeem-session-fixture';settings.account_display_name='兑换用户';settings.account_user_id='u-redeem';
      }
      return options.redeemFail
      ? {ok:false, error:'该订单已绑定其他设备'}
      : {ok:true, repeated:false, active:true, plan_id:'standard', plan:null, expires_at:'2026-10-26T00:00:00+00:00', credit_balance:500, monthly_agent_units:500, agent_usage_remaining:500, agent_usage_period:'2026-09', agent_usage_used:0, entitlements:['transcribe','byok_agent','local_batch','hosted_agent']};
    },
  });
  for (const key of Object.keys(window.__MOCK__)) {
    const fn = window.__MOCK__[key];
    window.__MOCK__[key] = (...args) => { window.__fixtureCalls.push(key); window.__fixtureCallArgs.push([key, ...args]); return fn(...args); };
  }
};
