// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 抖音解析通道的 Cookie 按名收窄（00-待办 #5 / 101 号 F06）。
   采集原则不变：有直链时一枚都不采（0.8.1 起保持）；确需后端 resolve 时，
   也只交给 yt-dlp 解析真正需要的登录/风控 Cookie，而不是整个 .douyin.com
   域的存储。名单覆盖登录态（sessionid 家族）、设备/风控（ttwid、odin_tt、
   msToken）与护照校验；yt-dlp 生态对抖音登录解析的通用要求即这些。 */

const DOUYIN_RESOLVE_COOKIE_NAMES = new Set([
  "sessionid",
  "sessionid_ss",
  "sid_guard",
  "sid_tt",
  "sid_tt_ss",
  "uid_tt",
  "uid_tt_ss",
  "ttwid",
  "msToken",
  "odin_tt",
  "passport_csrf_token",
  "passport_assist_user",
]);

function pickDouyinResolveCookies(cookies) {
  return (Array.isArray(cookies) ? cookies : [])
    .filter((cookie) => DOUYIN_RESOLVE_COOKIE_NAMES.has(String(cookie?.name || "")));
}

export { DOUYIN_RESOLVE_COOKIE_NAMES, pickDouyinResolveCookies };
