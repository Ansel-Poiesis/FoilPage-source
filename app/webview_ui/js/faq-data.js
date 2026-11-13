// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 内置常见问题（设置页「帮助」分区数据源）。
   维护纪律：与 docs/常见问题-QA.md 同步更新（后者为完整版含自诊命令）。 */
'use strict';

window.FAQ_GROUPS = [
  {
    group: "下载与任务",
    items: [
      { q: "下载失败了，需要马上做什么吗？", a: "不必惊慌。首次出错后 FoilPage 会自动换其他下载通道重试，页面会提示「别急，正在尝试其他下载办法」，此时无需操作；最终成功或失败都会再通知一次。失败不会损坏已下载的文件或其他任务。" },
      { q: "重新下载同一个视频会怎样？", a: "有三层防护：进行中的任务会合并（提示「正在处理中」）；已完成的下载会先弹确认卡（重新下载将新增一份文件，需要文稿请点「转写」）；点「转写」则直接复用已下载的视频，不会重复下载。" },
      { q: "下载到一半退出 FoilPage 或关机，任务会丢吗？", a: "不会。已提交的任务保留在队列里，重新打开 FoilPage 后继续处理；视频页面也可以随时关闭或切换，任务由 FoilPage 后台继续。桌面端没开时在页面点下载，会提示先打开 FoilPage。" },
      { q: "某个平台（如 B 站）一直下载失败，但浏览器能正常看？", a: "多为 VPN／代理工具在 DNS 层拦截了该平台（浏览器用加密 DNS 绕过，FoilPage 走系统解析被拦）。FoilPage 已内置 DNS 自动回退；若仍报 412/403 类错误，是平台风控代理的境外出口——把该平台加入 VPN 的拆分隧道（Split Tunneling）走直连，或下载前临时断开代理。" },
      { q: "抖音下载失败，提示「校验暂拦了后台解析」？", a: "平台对后台解析有网页校验。留在当前视频页让视频真实播放 2 秒，再点一次「下载」；仍失败到任务页查看具体原因。这是平台侧限制，不是账号问题。" },
      { q: "视频页开了很久，下载开始失败或任务一直悬挂？", a: "页面采集窗口约 2 小时，超龄页面的下载地址会过期。解决方法：把该视频加入收藏，按 F5 刷新视频页，等画面加载出来后重新下载——收藏能让扩展重新捕获该视频，刷新则换到新鲜地址（页面停留超过约 100 分钟时 FoilPage 会主动提醒）。悬挂任务超过设定时限（默认 120 分钟）会自动判失败并给出指引，时限可在 设置 → 下载 中调整。" },
      { q: "重载了浏览器扩展，页面上按钮没反应？", a: "重载扩展只对新加载的页面生效——已打开的页面仍是旧脚本，按 F5 刷新视频页即可。" },
      { q: "视频页没有下载／转写按钮（比如合集、长视频页）？", a: "先按 F5 刷新页面再看：部分合集、长视频页面结构特殊，旧版本扩展识别失败时会撤掉按钮，新版会以浮动按钮兜底显示。刷新后仍没有，说明浏览器里还在跑旧版扩展——打开 chrome://extensions 找到 FoilPage 扩展点一次「重新加载」（历史版本的自更新可能被滞留任务冻住，1.0.25 起已自愈），再 F5 视频页即可。" },
      { q: "下载成功后又弹出失败提示？", a: "不会发生：迟到的失败提示永远不会覆盖已成功的下载；终态失败也只通知一次，不会反复打扰。" },
    ],
  },
  {
    group: "转写与文本",
    items: [
      { q: "转写进行中退出 FoilPage，进度会丢吗？", a: "不会。任务在后台队列中保留进度，重新打开并登录后继续；任务页可随时查看状态。" },
      { q: "云端积分用完了怎么办？", a: "到订阅页充值后，对已完成转写的任务重试整理即可（转写结果不丢）。本地转写不受任何影响，始终可用。" },
      { q: "正文能直接复制吗？怎么导出？", a: "能。阅读页正文直接拖选后 Ctrl+C（或右键「复制选中文字」），无需进入编辑模式；工具栏可复制全部 Markdown；文本库单篇右键可复制 Markdown 文件地址、打开对应视频；阅读页还可导出 SRT/VTT 字幕。" },
    ],
  },
  {
    group: "付费与权益",
    items: [
      { q: "都有哪些付费项目？", a: "云端整理积分按标准 ¥19.80/500 积分/30 天、专业 ¥49.80/2000 积分/30 天购买；本地转写无需单独付费，登录后即可使用。支付宝页面支付，支付后自动到账。" },
      { q: "积分是怎么消耗的？", a: "1 积分 ≈ 2,000 字整理（更重模型 ×3 消耗）。参考：一分钟口播约 1 积分、十分钟视频约 2 积分、一小时长视频约 8 积分。本地转写不消耗积分。" },
      { q: "积分和套餐有效期多久？", a: "云端积分随套餐 30 天有效。到期前 3 天起每天提醒并在订阅页显示横幅；到期只停云端权益，本地转写不受影响。" },
      { q: "付款后没到账？", a: "支付宝支付自动到账，稍候在订阅页刷新查看。仍未到账请携订单号联系客服 QQ 2029774968 核对。" },
      { q: "退款政策？", a: "已购云端积分未消耗部分可按对应订单比例申请退款，已实际消耗部分原则上不退；法定权利不受限。联系客服 QQ 2029774968 人工处理，无自动退款。" },
      { q: "内测卡是什么权益？", a: "内测卡是受邀内测的兑换凭证：兑换后云端整理不消耗积分（∞ 仅指免积分），每个账号限一次，权益可撤销。兑换入口在订阅页。" },
      { q: "转写并发数是多少？", a: "本地免费层 1 路、标准 2 路、专业 4 路同时转写。" },
      { q: "本地转写需要付费吗？", a: "本地语音转写无需购买云端套餐，也不扣云端积分；开始处理前需要登录。自带密钥模型由用户选择的供应商计费。" },
    ],
  },
  {
    group: "账号与数据",
    items: [
      { q: "我的文稿和视频存在哪？隐私如何？", a: "文稿和媒体保存在本机，目录可在设置中查看。本地转写不上传媒体；云端整理会发送待整理文本，自带模型开启「字幕视觉校对」时还会发送有限字幕候选帧。数据处理细节见《用户须知与数据说明》。" },
      { q: "换电脑或重装系统怎么办？", a: "本机数据不做云同步。请先在设置的数据页导出备份，并另行备份视频和模型；仅复制 library.db、settings.json、device.id 不包含完整文稿。换机后重新登录，云端权益按账户恢复，媒体路径需核对。" },
      { q: "怎么登录？为什么条款又要求同意？", a: "邮箱验证码两步登录（输入邮箱→输入验证码）。条款有更新时需重新勾选同意后方可继续使用，属正常流程。" },
    ],
  },
  {
    group: "安装与更新",
    items: [
      { q: "安装时 Windows 提示「已保护你的电脑」？", a: "安装包未购买代码签名证书，属正常提示。点「更多信息 → 仍要运行」；浏览器提示「不常见的下载」时选择保留即可。" },
      { q: "怎么升级？旧数据会保留吗？", a: "FoilPage 无应用商店，更新来自发行页（「关于」页可检查更新）。升级、登录、退出均保留本机文稿与历史任务。" },
      { q: "为什么找不到浏览器扩展的 .crx 安装文件？", a: "FoilPage 不提供 .crx 安装包——Chrome 对商店外分发的 .crx 扩展会默认禁用，所以扩展以固定解压目录方式安装：打开「工具 → 连接浏览器」点「打开扩展目录」，在浏览器扩展管理页启用开发者模式，用「加载已解压的扩展」选择该目录即可。桌面升级会自动更新同一目录，无需重新安装。" },
    ],
  },
];

/* 常见问题独立页（先生 2026-10-19：置于订阅上方）：骨架与订阅页一致
   （settings-layout / settings-pane / set-group），数据即上方 FAQ_GROUPS。 */
window.FaqPage = (() => {
  function render() {
    const host = document.getElementById("page-faq");
    if (!host || AppState.currentView !== "faq") return;
    host.replaceChildren();
    const layout = document.createElement("div");
    layout.className = "settings-layout";
    const pane = document.createElement("div");
    pane.className = "settings-pane";
    const groups = Array.isArray(window.FAQ_GROUPS) ? window.FAQ_GROUPS : [];
    const total = groups.reduce((sum, g) => sum + (g.items ? g.items.length : 0), 0);
    const head = document.createElement("div");
    head.className = "set-group";
    const headTitle = document.createElement("h3");
    headTitle.textContent = "常见问题";
    head.appendChild(headTitle);
    const headHint = document.createElement("div");
    headHint.className = "hint";
    headHint.textContent = total + " 条，点开问题查看答案；未覆盖的问题请联系客服 QQ 2029774968（附任务页截图与版本号）";
    head.appendChild(headHint);
    pane.appendChild(head);
    groups.forEach((g) => {
      const wrap = document.createElement("div");
      wrap.className = "set-group";
      const title = document.createElement("h3");
      title.textContent = g.group;
      wrap.appendChild(title);
      (g.items || []).forEach((item) => {
        const node = document.createElement("details");
        node.className = "faq-item";
        const summary = document.createElement("summary");
        summary.textContent = item.q;
        const answer = document.createElement("div");
        answer.className = "faq-answer";
        answer.textContent = item.a;
        node.append(summary, answer);
        wrap.appendChild(node);
      });
      pane.appendChild(wrap);
    });
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "常见问题暂不可用";
      pane.appendChild(empty);
    }
    layout.appendChild(pane);
    host.appendChild(layout);
  }
  return { render };
})();
