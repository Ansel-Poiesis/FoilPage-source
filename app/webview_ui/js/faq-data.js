// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* FoilPage 内置常见问题（独立页「常见问题」数据源）。
   维护纪律：以当前源码实现、docs/user-guide.md 与 terms.html 为事实基准。 */
'use strict';

window.FAQ_GROUPS = [
  {
    group: "下载与任务",
    items: [
      {
        q: "下载失败了，需要马上做什么吗？",
        a: "不必惊慌。首次出错后，FoilPage 会自动尝试其他可用下载通道，页面提示正在换通道重试时无需手动干预，最终成功或失败都会再给出明确结果。单条任务失败不会影响其他任务，也不会损坏已下载的文件。",
      },
      {
        q: "重新下载同一个视频会怎样？",
        a: "系统有三层防重保护：正在进行中的同一任务会自动合并并提示「正在处理中」；已下载完成的视频再次点下载会先确认（确认后会另存一份）；如果只是要出文字稿，直接点「转写」即可复用已下载的本地视频，不会重复下载。",
      },
      {
        q: "下载到一半退出 FoilPage 或关机，任务会丢吗？",
        a: "不会。已提交的任务会持久化在本地队列中，重新打开 FoilPage 后会自动续接；已经由桌面端接管的下载，关闭或切换浏览器标签页也不影响后台继续。若任务仍停在「等待浏览器提供视频地址」，则需要回到对应视频页完成捕获。",
      },
      {
        q: "某个平台（如 B 站）一直下载失败，但浏览器能正常看？",
        a: "常见原因是系统级 VPN 或 TUN 代理把国内平台流量绕到了境外出口：浏览器或许仍能播放，但 B 站会对境外出口返回 HTTP 412，抖音则会拒绝签名地址或明显限速。先确认「设置 → 数据 → 网络策略」为「自动（国内直连，境外走系统代理）」；若仍受系统网卡级 VPN 接管，请二选一处理：① 下载国内平台前暂时关闭 VPN；② 在 VPN 拆分隧道（Split Tunneling）中放行国内域名直连——B 站建议放行 bilibili.com、b23.tv、hdslb.com、acgvideo.com、bilivideo.com、bilivideo.cn、biliapi.net；抖音建议放行 douyin.com、douyinvod.com、bytecdn.cn、ibytedtos.com、pstatp.com、snssdk.com、amemv.com。因 VPN 拦截失败的任务会被自动标记，关闭 VPN 后约 1 分钟内会自动重新入队（48 小时内、每任务最多自动重试 3 次）。DNS 拦截已内置自动回退，具体原因以任务详情的诊断信息为准。",
      },
      {
        q: "抖音下载失败，提示「校验暂拦了后台解析」？",
        a: "抖音对后台解析设有网页防刷校验，不属于账号异常。建议直接通过已配对的浏览器扩展提交（比单独粘贴链接更稳）：停留在当前视频页，若页面弹出滑块或验证码先完成验证，让视频正常播放 2 秒左右后再点一次「下载」；若仍失败，可在任务页展开诊断详情查看原因。",
      },
      {
        q: "视频页开了很久，下载开始失败或任务一直悬挂？",
        a: "浏览器捕获的视频播放地址通常有约 2 小时有效期，页面停留过久后地址会失效（停留超过约 100 分钟时扩展会主动提醒）。建议先把该视频加入收藏，再按 F5 刷新视频页，等画面正常播放后重新点击下载。处于等待浏览器等悬挂状态的任务，超过设定时限（默认 120 分钟）会自动判失败并给出恢复指引，时限可在「设置 → 数据 → 悬挂任务判失败（分钟）」中调整。",
      },
      {
        q: "重载了浏览器扩展，页面上按钮没反应？",
        a: "在浏览器中重载扩展后，新脚本只会注入到之后加载的标签页，已经打开的视频页仍停留在旧脚本上下文。按 F5 刷新该视频页即可恢复响应。",
      },
      {
        q: "视频页没有下载／转写按钮（比如合集、长视频页）？",
        a: "先按 F5 刷新当前视频页：遇到合集、专题等特殊页面结构时，扩展会自动切换为浮动按钮兜底显示。如果刷新后依然没有按钮，请打开浏览器的扩展管理页（如 chrome://extensions 或 edge://extensions），确认 FoilPage 扩展已启用且目录指向「工具 → 浏览器」中的固定扩展目录，点一次「重新加载」后再刷新视频页。",
      },
      {
        q: "下载成功后又弹出失败提示？",
        a: "不会。桌面端已按终态优先过滤迟到回调：已经成功的下载不会被延后到达的失败事件覆盖，同一任务的终态失败也只会提醒一次。",
      },
    ],
  },
  {
    group: "转写与文本",
    items: [
      {
        q: "转写进行中退出 FoilPage，进度会丢吗？",
        a: "不会丢失任务。退出时 Worker 会在当前语音分段边界有序收尾，并将未完成的转写自动放回等待队列（不消耗重试次数）；下次打开 FoilPage 并保持登录状态后会自动继续处理。",
      },
      {
        q: "云端积分用完了怎么办？",
        a: "在「订阅」页购买积分后，直接对已转写完成的任务重新发起整理即可，原有转写文稿不会丢失。本地语音转写以及「设置 → 文本整理」中的自带密钥模式均不受云端积分余额影响，随时可用。",
      },
      {
        q: "正文能直接复制吗？怎么导出？",
        a: "可以在阅读区直接拖选文字按 Ctrl+C，或点阅读栏右上角的「复制」一键复制当前版本（支持在「原始转写」「整理稿」「译文」之间切换，也支持点「编辑」原地修改）。点击阅读栏右上角「⋯」（更多操作）或右键左侧文稿列表项，即可播放源视频、定位文稿或视频文件、定位关联任务、发起翻译，以及导出 SRT / VTT 字幕文件。",
      },
    ],
  },
  {
    group: "付费与权益",
    items: [
      {
        q: "都有哪些付费项目？",
        a: "付费项目仅面向可选的云端托管整理服务，按次购买、不自动续费：标准版 ¥19.80（500 积分 / 30 天）、专业版 ¥49.80（2,000 积分 / 30 天），可重复购买并顺延有效期与叠加积分。本地语音转写和自带密钥整理无需购买云端套餐，登录后即可使用。支付渠道以「订阅」页当前开放方式（如支付宝）为准，付款验证后自动到账。",
      },
      {
        q: "积分是怎么消耗的？",
        a: "托管正文整理按字数计费，标准模型 1 积分 ≈ 2,000 字，更重模型按标注倍率计费（例如 ×3）；按实测估算，1 分钟口播约 1 积分、10 分钟视频约 2 积分、1 小时视频约 8 积分。若开启「AI 优化标题」，每条转写另计 1 次基数积分（随所选托管模型倍率）。本地转写和自带密钥整理不消耗 FoilPage 积分。",
      },
      {
        q: "很长的视频能直接整理吗？",
        a: "可以。云端托管整理会自动对长转写稿分段处理并汇总，无需手动拆分。若单篇字数超出服务端单次上限，系统会明确提示并自动退回预扣积分；超长内容可改用自带密钥模型整理。",
      },
      {
        q: "积分和套餐有效期多久？",
        a: "每次购买顺延 30 天有效期，重复购买时剩余积分不清零、直接累加。到期前 3 天起应用会每日提醒并在「订阅」页显示提示条；到期后云端整理会暂停，未用完的积分会被冻结保留，下次购买恢复有效期后即可继续使用。本地转写和自带密钥功能不受到期影响。",
      },
      {
        q: "付款后没到账？",
        a: "支付宝等在线订单会在服务端完成回调核验后自动入账，请稍候片刻并在「订阅」页点击刷新查看。若几分钟后仍未到账，请携带订单号联系客服（QQ 2029774968 或邮箱 aachuan@foxmail.com）核对账本，切勿发送支付密码、验证码或 API 密钥。",
      },
      {
        q: "退款政策？",
        a: "已购且在有效期内的未消耗积分，可按原订单未消耗比例申请退款；已实际消耗部分原则上不退，但不限制任何法定消费者权利。退款由人工核单处理，请携带订单号联系客服 QQ 2029774968，暂不提供全自动自助退款。",
      },
      {
        q: "内测卡是什么权益？",
        a: "内测卡是面向受邀用户的兑换凭证，在「订阅」页输入卡密即可兑换（每个账号限兑一次）。生效后余额显示为「∞」，表示云端托管正文整理与标题优化免扣 FoilPage 积分，账号原有的数值积分会完整保留。内测卡不覆盖自带密钥的第三方 API 费用，仍受并发与风控配额保护，且可能到期或被撤销。",
      },
      {
        q: "转写并发数是多少？",
        a: "云端订阅档位中，标准版支持 2 路并发、专业版支持 4 路并发；当前已登录的免费账户在桌面端本地转写默认开放 3 路并发。实际生效并发数以账户已验签权益及「设置 → 启动」中 Worker 运行状态为准。",
      },
      {
        q: "本地转写需要付费吗？",
        a: "不需要。本机 Whisper 语音转写登录后即可免费使用，不扣云端积分。若在「设置 → 文本整理」中配置自带密钥（BYOK）模型，其 API 费用由您所选的服务商直接计费。",
      },
    ],
  },
  {
    group: "账号与数据",
    items: [
      {
        q: "我的文稿和视频存在哪？隐私如何？",
        a: "文稿、任务库与下载媒体均保存在本机（默认位于 %LOCALAPPDATA%\\FoilPage，可在「设置 → 数据」查看具体目录）。本地语音转写完全在本机执行，不会上传音视频；仅在开启在线文本整理或标题优化时发送对应文字；开启「字幕视觉校对」时（支持自带多模态模型，以及托管模式下的 deepseek-flash 档；默认托管档为纯文本），还会发送少量视频字幕候选帧。完整数据处理与第三方说明见《用户须知与数据说明》。",
      },
      {
        q: "换电脑或重装系统怎么办？",
        a: "FoilPage 不做本地文稿云同步。迁移前请先停止 Worker 并等待下载、翻译和批量整理结束，再进入「设置 → 数据」点击「导出文稿备份」生成带完整性校验的 zip 备份包（含任务库、脱敏设置、全部文稿、译文与字幕，不含视频文件和 Whisper 模型）；视频与本地模型请按需另行拷贝。新设备登录同一账号后即可恢复云端权益，通过「导入文稿备份」还原文稿，再按需核对视频路径。请勿只手动拷贝 library.db，那样会漏掉正文文件。",
      },
      {
        q: "怎么登录？为什么条款又要求同意？",
        a: "采用邮箱验证码两步登录（输入邮箱 → 填写 6 位验证码，验证码约 10 分钟有效，登录会话默认保持 30 天）。当服务协议或数据说明发生影响用户权益的修订时，系统会再次提示审阅并勾选同意，属于正常合规流程。",
      },
    ],
  },
  {
    group: "安装与更新",
    items: [
      {
        q: "安装时 Windows 提示「已保护你的电脑」？",
        a: "个人开发安装包目前未购买商业代码签名证书，Windows SmartScreen 弹出拦截属于常见提示。确认安装包来自官方发行页后，点击「更多信息 → 仍要运行」即可；若浏览器提示「不常见的下载」，选择保留即可。",
      },
      {
        q: "怎么升级？旧数据会保留吗？",
        a: "FoilPage 暂未上架应用商店，升级通过「设置 → 关于 → 检查更新」或展开「通知与更新」前往发行页下载新版安装包覆盖安装。正常升级、切换登录或退出账号均会完整保留本机文稿、任务库和历史记录。",
      },
      {
        q: "为什么找不到浏览器扩展的 .crx 安装文件？",
        a: "Chrome 和 Edge 默认会禁用非应用商店来源的 .crx 扩展，因此 FoilPage 采用固定解压目录安装：打开左侧「工具 → 浏览器」点击「打开扩展目录」，在浏览器扩展管理页开启「开发者模式」，点击「加载已解压的扩展」选中该目录即可。后续升级桌面端会自动刷新同一目录内的扩展文件，刷新已打开的视频页即可生效，无需重新安装。",
      },
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
    headHint.textContent = total + " 条，点开问题查看最新处理办法；未覆盖的问题请联系 QQ 2029774968 或邮箱 aachuan@foxmail.com（建议在 设置 → 数据 导出诊断包一并提供）";
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
