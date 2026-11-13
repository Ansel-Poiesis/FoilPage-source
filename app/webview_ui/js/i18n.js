// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Interface localization only. User titles, tags, prompts, inputs and documents
 * are never translated. Detailed legal/source documents retain their own language. */
'use strict';
window.I18n = (() => {
  const en = new Map(Object.entries({
    '准备':'Setup','任务':'Tasks','视频':'Videos','文本库':'Library','常见问题':'Help',
    '通过网页登录':'Continue on the web','通过网页登录':'Sign in on the web','恢复登录':'Restore sign-in',
    '正在通过浏览器登录…':'Signing in through your browser…','正在验证…':'Verifying…',
    '登录已失效，请重新登录':'Your session has expired. Sign in again.',
    '暂时无法恢复登录，请重试':'Unable to restore sign-in. Try again.',
    '订阅':'Subscription','使用与订阅':'Usage & subscription','设置':'Settings','工具':'Tools',
    '浏览器':'Browser','链接下载':'Download link','导入媒体':'Import media','界面':'Appearance',
    '识别':'Transcription','文本整理':'Text processing','Agent 控制':'Agent control','模型':'Models',
    '数据':'Data','启动':'Startup','关于':'About','外观':'Appearance','主题':'Theme',
    '界面语言':'Interface language','与转写识别语言独立':'Independent of the transcription language',
    '深色':'Dark','白岩':'Light (White Rock)','纸白':'Light (White Rock)',
    '暗夜德古拉 Dracula':'Dracula','东京夜 Tokyo Night':'Tokyo Night','诺德极夜 Nord':'Nord',
    '奶咖摩卡 Catppuccin Mocha':'Catppuccin Mocha','黄土复古 Gruvbox':'Gruvbox',
    '玫瑰松林 Rosé Pine':'Rosé Pine','拿铁 Catppuccin Latte':'Catppuccin Latte','诺德雪 Nord Light':'Nord Light',
    '背景':'Background','素面':'Plain','柔光':'Soft glow','细纹':'Texture','界面强调色':'Accent color',
    '绿色':'Green','玫瑰':'Rose','靛蓝':'Indigo','墨色':'Ink','界面字号':'Interface text size',
    '界面动效':'Animations','删除前确认':'Confirm before deleting','任务排序':'Task order',
    '最新在前':'Newest first','最早在前':'Oldest first','已保存':'Saved','设置已保存':'Settings saved',
    '保存':'Save','保存修改':'Save changes','取消':'Cancel','关闭':'Close','确定':'Confirm',
    '打开':'Open','更改':'Change','选择':'Select','重试':'Retry','刷新':'Refresh','复制':'Copy',
    '复制成功':'Copied','已复制':'Copied','返回':'Back','删除':'Delete','移除':'Remove',
    '全选':'Select all','清空':'Clear','搜索':'Search','全部':'All','全部任务':'All tasks',
    '进行中':'In progress','已完成':'Completed','失败':'Failed','已失败':'Failed','等待中':'Queued',
    '等待':'Waiting','暂停':'Pause','继续':'Resume','开始':'Start','处理中':'Processing',
    '暂无任务':'No tasks yet','暂无文本':'No documents yet','暂无文稿':'No documents yet',
    '搜索文本':'Search documents','搜索任务':'Search tasks','搜索视频':'Search videos',
    '全部标签':'All tags','文本列表':'Documents','清空文本筛选':'Clear document filters',
    '拖动调整宽度':'Drag to resize','返回文本列表':'Back to documents','整理稿':'Edited draft',
    '原稿':'Transcript','源文件':'Source file','标签':'Tags','标签：':'Tags: ',
    '添加标签':'Add tag','编辑标签':'Edit tags','复制全文':'Copy all text','复制文本':'Copy text',
    '复制选中文字':'Copy selection','编辑':'Edit','编辑正文':'Edit document','导出':'Export',
    '导出文本':'Export text','打开文件位置':'Open file location','打开文件夹':'Open folder',
    '删除文稿':'Delete document','删除文本':'Delete document','删除此文本':'Delete document',
    '保存文稿':'Save document','放弃修改':'Discard changes','撤销':'Undo','重做':'Redo',
    '选择一篇文本开始阅读':'Select a document to start reading','未找到匹配的文本':'No matching documents',
    '下载':'Download','下载中':'Downloading','正在下载':'Downloading','下载完成':'Downloaded',
    '转写':'Transcribe','转写中':'Transcribing','正在转写':'Transcribing','整理':'Refine',
    '整理中':'Refining','优化标题':'Improve title','已归档':'Archived','归档':'Archive',
    '取消归档':'Unarchive','重新转写':'Transcribe again','重新整理':'Refine again',
    '本地转写':'Local transcription','链接':'Link','文件':'File','本地文件':'Local file',
    '已加入任务':'Added to tasks','粘贴视频链接':'Paste a video link','视频链接':'Video link',
    '开始下载':'Start download','下载并转写':'Download & transcribe','添加任务':'Add task',
    '导入文件':'Import files','选择文件':'Choose files','选择文件夹':'Choose folder',
    '拖入文件或点击选择':'Drop files here or click to choose','批量导入':'Import multiple files',
    '扫描目录':'Scan folder','扫描':'Scan','停止扫描':'Stop scan','自动扫描':'Automatic scan',
    '已下载视频':'Downloaded videos','播放':'Play','播放视频':'Play video','重新下载':'Download again',
    '取消扫描':'Cancel scan','正在取消…':'Cancelling…','正在取消视频扫描':'Cancelling the video scan',
    '取消视频扫描':'Cancel video scan','正在取消扫描':'Cancelling video scan',
    '扫描已取消，已登记的视频保留':'Scan cancelled; indexed videos were kept',
    '正在停止扫描，已登记的视频会保留':'Stopping the scan; indexed videos will be kept',
    '无法取消扫描':'Unable to cancel the scan','无法取消扫描，请重试':'Unable to cancel the scan. Try again.',
    '移除记录':'Remove entry','删除视频':'Delete video','视频已删除':'Video deleted',
    '登录':'Sign in','退出登录':'Sign out','邮箱':'Email','邮箱地址':'Email address',
    '验证码':'Verification code','发送验证码':'Send code','验证并登录':'Verify & sign in',
    '验证':'Verify','账户':'Account','账户信息':'Account','昵称':'Display name','头像':'Avatar',
    '登录状态':'Sign-in status','已登录':'Signed in','未登录':'Not signed in',
    '环境检测':'Environment','模型下载':'Download models','检查环境':'Check environment',
    '重新检查':'Check again','一键配置环境':'Configure environment','下载模型':'Download model',
    '默认模型':'Default model','识别模型':'Transcription model','识别语言':'Transcription language',
    '运行设备':'Compute device','设备':'Device','自动':'Auto','自动检测':'Auto-detect',
    '中文':'Chinese','英语':'English','日语':'Japanese','韩语':'Korean','已安装':'Installed',
    '未安装':'Not installed','正在下载模型':'Downloading model','安装 GPU 加速包':'Install GPU pack',
    '模型目录':'Model folder','模型缓存目录':'Model cache folder','下载目录':'Download folder',
    '视频保存目录':'Video folder','输出目录':'Output folder','保存目录':'Save folder',
    '数据目录':'Data folder','数据库':'Database','数据库路径':'Database path','浏览器下载目录':'Browser download folder',
    '打开数据目录':'Open data folder','清理缓存':'Clear cache','备份':'Backup','恢复':'Restore',
    '启动时运行':'Run at startup','开机启动':'Launch at sign-in','关闭时驻留托盘':'Close to tray',
    '任务通知':'Task notifications','转写完成通知':'Notify when transcription completes',
    'Worker 运行中':'Worker running','Worker 已停止':'Worker stopped','启动 Worker':'Start worker',
    '停止 Worker':'Stop worker','重启 Worker':'Restart worker','空闲':'Idle',
    '正文整理':'Draft refinement','标题优化':'Title refinement','启用正文整理':'Enable draft refinement',
    '启用标题优化':'Enable title refinement','自带 API':'Bring your own API','托管云端':'Hosted cloud',
    '服务商':'Provider','接口地址':'API endpoint','模型名称':'Model name','连接测试':'Test connection',
    '测试连接':'Test connection','连接成功':'Connected','连接失败':'Connection failed',
    '整理力度':'Editing level','轻度':'Light','均衡':'Balanced','结构化':'Structured',
    '提示词':'Prompt','自定义提示词':'Custom prompt','提示词配置':'Prompt profiles',
    '转写提示词':'Transcription prompts','初始提示词':'Initial prompt','热词':'Hotwords',
    '服务连接':'Service connection','管理与帮助':'Manage & help','积分':'Credits','余额':'Balance',
    '购买积分':'Buy credits','内测卡':'Beta card','兑换':'Redeem','订单':'Orders','设备管理':'Devices',
    '标准版':'Standard','专业版':'Pro','月付':'30 days','年付':'360 days','本地使用':'Local processing',
    '安装浏览器扩展':'Install browser extension','浏览器扩展':'Browser extension',
    '打开扩展目录':'Open extension folder','允许连接':'Allow connection','已配对':'Paired',
    '未配对':'Not paired','连接状态':'Connection status','扩展状态':'Extension status',
    '一键配置':'Quick pairing','完整配置':'Full access','只读配置':'Read-only access',
    '复制配置':'Copy configuration','配置与修复指南':'Setup & repair guide','阅读指南':'Read guide',
    '版本':'Version','当前版本':'Current version','检查更新':'Check for updates',
    '通知与更新':'Notifications & updates','正式版':'Stable','测试版':'Preview','更新记录':'Release notes',
    '打开 GitHub':'Open GitHub','问题反馈':'Report an issue','使用指南':'User guide',
    '复制诊断信息':'Copy diagnostics','最小化':'Minimize','最大化':'Maximize','还原':'Restore',
    '关闭窗口':'Close window','FoilPage 标志':'FoilPage logo','切换侧栏':'Toggle sidebar',
    '文本版本':'Document version','译文视图':'Translation view','复制 Markdown':'Copy Markdown',
    '编辑 Markdown':'Edit Markdown','导出 Markdown':'Export Markdown','原文件':'Source file',
    '删除当前文本':'Delete this document','打开 Markdown 文件位置':'Open document folder',
    '展开侧栏':'Expand sidebar','收起侧栏':'Collapse sidebar','帮助':'Help','下一步':'Next',
    '上一步':'Previous','完成':'Finish','跳过':'Skip','重新开始':'Start over',
    '保存失败':'Save failed','加载失败':'Could not load','请重试':'Please try again',
    '正在加载…':'Loading…','加载中…':'Loading…','未配置':'Not configured','已配置':'Configured',
    '启用':'Enable','禁用':'Disable','开启':'On','关闭自动扫描':'Disable automatic scan',
  }));
  en.set('跳到主要内容','Skip to main content');
  for (const [source, value] of Object.entries({
    '整理力度':'Editing level','智能（按内容自选）':'Smart (choose for the content)',
    '轻整理（原话排版）':'Light (faithful formatting)','适度整理（通顺易读）':'Balanced (readable prose)',
    '结构整理（清晰成篇）':'Structured (clear article)','精炼改写（自由发挥）':'Rewrite (flexible editing)',
    '轻整理（保真）':'Light (faithful)','适度整理（平衡）':'Balanced',
    '结构整理（成篇）':'Structured','精炼改写（自由）':'Rewrite (flexible)',
    '沿用当前设置':'Use current settings','转写模式':'Transcription mode',
    '按当前设置（整理跟随全局开关）':'Use current settings (follow the editing switch)',
    '仅原稿（本次不跑 AI 整理，速度快）':'Transcript only (skip AI editing)',
    '原稿＋AI 整理稿（本次强制整理，消耗 AI 额度）':'Transcript and AI article (uses AI allowance)',
    '不再每次提醒，下次直接按本次选择执行':'Remember these choices and skip this dialog next time',
    '记住重新转写选项':'Remember retranscription options',
    '开启后按上次选择执行，关闭后每次确认':'Use the previous choices when on; turn this off to confirm every time',
    '提示音':'Notification sound','系统提示音':'System sound','无声':'Silent',
    '清脆（短促双音）':'Crisp (short double tone)','柔和（慢包络）':'Soft (gentle tone)',
    '低沉（厚重）':'Deep (low tone)','自定义音频（WAV）':'Custom audio (WAV)',
    '成功提示音':'Success sound','失败提示音':'Failure sound','试听':'Preview',
    '任务完成时播放的 WAV 文件':'WAV file played when a task completes',
    '任务失败时播放的 WAV 文件':'WAV file played when a task fails',
    '任务终态时播放；预设组或自定义 WAV 文件':'Choose a preset or custom WAV for task notifications',
    '原稿已保留，AI 处理未完成':'Transcript saved; AI processing is incomplete',
    'AI 服务鉴权失败，请到设置检查 API Key':'AI authentication failed; check the API key in Settings',
    'AI 模型不可用，请检查模型名称和接口地址':'AI model unavailable; check the model name and endpoint',
    'AI 服务限流，请稍后重试整理':'AI service rate limited; retry editing later',
    'AI 整理未完成，原稿已保留，请检查服务后重试':'AI editing is incomplete; the transcript is saved. Check the service and retry',
  })) en.set(source, value);
  for(const [source,value] of Object.entries({
    '需处理':'Needs attention','完成度':'Progress','导入':'Import','未开始':'Not started',
    '未选择':'Nothing selected','查看任务详情':'View task details',
    '选择一项查看进度与文件；勾选多项，批量处理。':'Select a task to see progress and files, or select several for batch actions.',
    '把链接变成收藏':'Keep a video from its link','自定义标题':'Custom title','下载后转写':'Transcribe after download',
    '抖音建议从浏览器视频页添加，识别更稳定。':'For Douyin, submit the video from the paired browser extension for more reliable identification.',
    '听过的，留下文字':'Keep what you heard as text','把视频或录音拖到这里':'Drop video or audio files here',
    '支持多文件，已导入的内容会自动去重。':'Import multiple files; previously imported files are detected automatically.',
    '加入队列后，切换页面也会继续转写':'Transcription continues in the queue when you switch pages.',
    '连接浏览器':'Connect your browser','浏览器已配对':'Browser paired','加载扩展':'Load extension',
    '打开扩展':'Open extension','完成连接':'Finish pairing','重新配对':'Pair again',
    '先允许连接，再在浏览器扩展中点击「一键配置 FoilPage」。':'Allow a connection, then choose Quick pairing in the browser extension.',
    '放大示意图':'Enlarge illustration','在视频页提交下载与转写；升级后刷新已打开的视频页。':'Submit downloads and transcription from a video page. Refresh open video pages after an update.',
    '首次安装与后续更新':'Installation & updates',
    '首次选择「加载已解压的扩展」，加载下方固定目录；以后保留此路径。':'Choose Load unpacked and select the folder below. Keep this folder for future updates.',
    '以后随桌面升级更新扩展文件；任务完成、弹窗关闭后，插件空闲重载。已打开的视频页请刷新。':'Desktop updates replace the extension files. The extension reloads when idle, after tasks finish and its popup closes. Refresh open video pages.',
    '若版本仍不一致，请重新加载本页目录并配对。':'If the versions still differ, reload the extension from this folder and pair again.',
  }))en.set(source,value);
  const skip = 'script,style,[contenteditable],.reader-body,.doc-title,.doc-tags,.job-title,.video-title,.video-card h3,.video-delete-title,#libTagFilter option:not([value=""]),[data-i18n-skip]';
  const textState = new WeakMap(), attrState = new WeakMap();
  const attributes = ['title','placeholder','aria-label','alt'];
  let language='zh-CN', observer=null;
  function t(text) {
    if (language !== 'en' || typeof text !== 'string') return text;
    const trimmed=text.trim();
    if (en.has(trimmed)) return text.replace(trimmed,en.get(trimmed));
    const scanProgress=trimmed.match(/^正在(扫描|取消扫描)（已读取 (\d+) 个视频）$/);
    if(scanProgress)return scanProgress[1]==='扫描'
      ? `Scanning videos (${scanProgress[2]} visited)`
      : `Cancelling video scan (${scanProgress[2]} visited)`;
    const tags=trimmed.match(/^标签（(\d+)）$/);
    return tags ? 'Tags: '+tags[1] : text;
  }
  function translate(root=document.body) {
    if (!root) return;
    observer?.disconnect();
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()) {
      const node=walker.currentNode;
      if(node.parentElement?.closest(skip+',textarea,input'))continue;
      let state=textState.get(node);
      if(!state || node.nodeValue!==state.rendered)state={source:node.nodeValue};
      state.rendered=t(state.source);
      if(node.nodeValue!==state.rendered)node.nodeValue=state.rendered;
      textState.set(node,state);
    }
    for(const el of [root,...root.querySelectorAll('[title],[placeholder],[aria-label],[alt]')]) {
      if(el.closest(skip))continue;
      const states=attrState.get(el)||{};
      for(const attr of attributes) {
        if(!el.hasAttribute(attr))continue;
        const value=el.getAttribute(attr);
        let state=states[attr];
        if(!state || value!==state.rendered)state={source:value};
        state.rendered=t(state.source);
        if(value!==state.rendered)el.setAttribute(attr,state.rendered);
        states[attr]=state;
      }
      attrState.set(el,states);
    }
    observer?.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:attributes});
  }
  function setLanguage(value) {
    language=value==='en'?'en':'zh-CN';
    document.documentElement.lang=language;
    translate();
  }
  document.addEventListener('DOMContentLoaded',()=>{
    observer=new MutationObserver(()=>translate());
    translate();
  });
  return {t,setLanguage,translate,get language(){return language;}};
})();
