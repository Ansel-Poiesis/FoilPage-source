// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// FoilPage 链接助手 · 抖音媒体主机单一事实源。
// 本文件不依赖任何扩展 API，被 ISOLATED（content.js）与 MAIN（content-main.js）
// ISOLATED先加载；MAIN由build/sync-extension-main.cjs合并进单文件入口。
// 新增/下线 CDN 域名只改这里并重新生成，
// 两个世界不再各自维护副本（91 §4.4 的两处重复定义债）。
(function () {
  "use strict";
  const frozen = (list) => Object.freeze(list.slice());
  globalThis.SHENGJIAN_DOUYIN_HOSTS = Object.freeze({
    cdnSuffixes: frozen([
      "douyinvod.com", "douyinpic.com", "byteimg.com", "bytecdn.cn",
      "ibytedtos.com", "pstatp.com",
    ]),
    playSuffixes: frozen(["snssdk.com", "amemv.com"]),
    playPathSource: "\\/(?:aweme\\/v1\\/play|video\\/play|video\\/download|play\\/?)(?:[/?]|$)",
    playPathFlags: "i",
  });
})();
