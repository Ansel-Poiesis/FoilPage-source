// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* 抖音本地下载失败后的浏览器通道回退策略（纯函数，供后台与测试共用）。
   2026-10-01 实测定标：签名 CDN 地址本地 403 后，play 端点在
   chrome.downloads 里同样被拒（SERVER_FORBIDDEN，346 字节错误页），yt-dlp
   解析被 a_bogus 挡死——旧回退序「只试 play 端点」会把三通道全部走死。
   回扫拿到的新签名媒体地址（含正在播放的 currentSrc / 新一轮资源时序）
   没有参与过被拒请求，在浏览器上下文里仍可能有效——2026-09-22 20:48 的
   唯一真实成功正是浏览器通道下载媒体地址的形态。因此被拒码的回退序改为：
   未试过的新签名媒体地址 → 新 play 端点 → 记忆 play 端点；被拒的那批
   地址永不重放。HLS 清单 chrome.downloads 下载不了，一律跳过。 */

const REJECTED_REPLAY_SKIP_CODES = new Set([
  "media_forbidden",
  "douyin_verification_required",
]);

export { REJECTED_REPLAY_SKIP_CODES };

function isHlsPlaylist(value) {
  return String(value).includes(".m3u8");
}

function rememberedCandidates(remembered) {
  if (typeof remembered === "string") return [remembered].filter(Boolean);
  if (!remembered || typeof remembered !== "object") return [];
  const values = [remembered.mediaUrl];
  if (Array.isArray(remembered.mediaCandidates)) values.push(...remembered.mediaCandidates);
  return values.map((v) => String(v || "")).filter(Boolean);
}

export function browserFallbackMediaUrl({ code, remembered, fresh, matchingFresh }) {
  const rejected = new Set(rememberedCandidates(remembered));
  const rememberedPlay = String(
    (remembered && typeof remembered === "object" ? remembered.playUrl : "") || "",
  );
  const source = matchingFresh || null;
  const freshPlay = String(source?.playUrl || "");
  const freshMediaPool = [
    String(source?.mediaUrl || ""),
    ...(Array.isArray(source?.mediaCandidates) ? source.mediaCandidates : []),
  ].map((v) => String(v || ""));
  const freshUntriedMedia = freshMediaPool.find(
    (v) => v && !isHlsPlaylist(v) && !rejected.has(v),
  );

  if (!REJECTED_REPLAY_SKIP_CODES.has(code)) {
    // 非拒签失败（网络类）：保持既有次序 play 优先，媒体地址兜底。
    return String(freshPlay || rememberedPlay || freshMediaPool.find(Boolean) || "").trim();
  }
  // 拒签失败：先试没参与过被拒请求的新签名地址，再退 play 端点。
  return String(freshUntriedMedia || freshPlay || rememberedPlay || "").trim();
}

export function browserFallbackMediaUrls({ code, remembered, matchingFresh }) {
  const source = matchingFresh || null;
  const rejected = new Set(rememberedCandidates(remembered));
  const progressive = [source?.mediaUrl, ...(Array.isArray(source?.mediaCandidates) ? source.mediaCandidates : [])]
    .map(value => String(value || "").trim())
    .filter(value => value && !isHlsPlaylist(value));
  const play = [source?.playUrl, remembered?.playUrl].map(value => String(value || "").trim()).filter(Boolean);
  const ordered = REJECTED_REPLAY_SKIP_CODES.has(code)
    ? [...progressive.filter(value => !rejected.has(value)), ...play]
    : [...play, ...progressive, ...rememberedCandidates(remembered).filter(value => !isHlsPlaylist(value))];
  return [...new Set(ordered)].slice(0, 8);
}
