// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
(function () {
  "use strict";

  // MAIN-world capture hook. The isolated-world content script cannot read page
  // JavaScript data, and a logged-in Douyin player frequently hands <video> an
  // MSE blob: URL whose segment requests are filtered media types (.m3u8/.m4s).
  // The only durable source of a downloadable progressive MP4 in that state is
  // the aweme detail/feed JSON the page itself consumes. Wrapping fetch/XHR in
  // the page world lets the extension observe those responses and forward the
  // signed play_addr URLs to the isolated world, which owns the allowlist and
  // the submission flow. Nothing else is read: no cookies, no credentials, no
  // page state beyond aweme_id + play_addr.

  if (window.top !== window) return;

  const MESSAGE_SOURCE = "shengjian-media-capture";
  // 2026-09 实机：detail/feed 可能带尾斜杠、query 变体或 item/detail；
  // URL 命中 aweme web API 即观察响应，仍由 collectAwemes + 隔离世界白名单过滤。
  const API_HINT = /\/aweme\/v1\/web\/.*(?:aweme\/detail|feed|mix\/aweme|aweme\/(?:post|favorite|listcollection)|item\/detail|\/detail)/i;
  const MAX_ITEMS_PER_MESSAGE = 24;
  const MAIN_MARK_VERSION = "0.1.1";
  // 收藏夹弹窗常在信息流批次送达之后才打开：批次送达时刻 URL 还没有
  // modal_id，selectCapturedItems 的选中保护不生效，隔离世界 20 槽档案
  // 可能把目标视频挤出（2026-10-12 实机诊断：media_items=51、
  // captured_targets=20、target_captured=false）。MAIN 世界按 aweme_id
  // 留一份更长的采集档案，提交时按目标 id 定向重放一次。
  const REPLAY_REQUEST_SOURCE = "shengjian-capture-replay";
  // TASK-8.18 图集：ISOLATED 从 chrome.storage 中继 8.16 热切换配置（纯数据）。
  // 默认禁用＝「关闭时与现状完全一致」；形状坏/无 nonce 一律回落默认。
  const CONFIG_SOURCE = "shengjian-client-config";
  const GALLERY_DEFAULTS = Object.freeze({
    enabled: false,
    imagesFieldNames: ["images"],
    imageCdnSuffixes: ["douyinpic.com", "byteimg.com", "pstatp.com"],
    watermarkMarkers: [],
    maxImagesPerTask: 60,
  });
  let galleryConfig = GALLERY_DEFAULTS;

  function normalizeGalleryConfig(value) {
    if (!value || typeof value !== "object") return GALLERY_DEFAULTS;
    const list = (items, cap) => Array.isArray(items)
      ? [...new Set(items.map((item) => String(item || "").trim())
          .filter((item) => item && item.length <= 128))].slice(0, cap)
      : [];
    const fields = list(value.imagesFieldNames, 8);
    const suffixes = list(value.imageCdnSuffixes, 12);
    const markers = list(value.watermarkMarkers, 12);
    const maxImages = Number(value.maxImagesPerTask);
    return Object.freeze({
      enabled: value.enabled === true,
      imagesFieldNames: fields.length ? fields : GALLERY_DEFAULTS.imagesFieldNames,
      imageCdnSuffixes: suffixes.length ? suffixes : GALLERY_DEFAULTS.imageCdnSuffixes,
      watermarkMarkers: markers,
      maxImagesPerTask: Number.isInteger(maxImages) && 1 <= maxImages && maxImages <= 200
        ? maxImages : GALLERY_DEFAULTS.maxImagesPerTask,
    });
  }
  // 与 ISOLATED 的 CAPTURE_TTL_MS 对齐：长会话回看场景需要 2 小时。
  const ARCHIVE_TTL_MS = 120 * 60 * 1000;
  const MAX_ARCHIVE_ITEMS = 240;
  const MAX_ARCHIVE_CANDIDATES = 4;
  const captureArchive = new Map();

  // 隔离世界靠 DOM 标记判断 MAIN 是否随当前扩展注入；自更新后未 F5 的旧页无标记。
  try {
    const root = document.documentElement;
    if (root) {
      root.dataset.shengjianMain = MAIN_MARK_VERSION;
      root.dataset.shengjianMainReady = "1";
      root.dataset.shengjianMainApiRequests = "0";
      root.dataset.shengjianMainApiResponses = "0";
      root.dataset.shengjianMainJsonResponses = "0";
      root.dataset.shengjianMainJsonFailures = "0";
      root.dataset.shengjianMainMediaItems = "0";
      root.dataset.shengjianMainAwemeNodes = "0";
      root.dataset.shengjianMainVideoNodes = "0";
      root.dataset.shengjianMainPlayAddrNodes = "0";
      root.dataset.shengjianMainUrlCandidates = "0";
      root.dataset.shengjianMainHttpsCandidates = "0";
      root.dataset.shengjianMainAllowedUrls = "0";
      root.dataset.shengjianMainCoverNodes = "0";
      root.dataset.shengjianMainCoverAccepted = "0";
    }
  } catch { /* document may not be ready */ }

  // MAIN↔ISOLATED 消息握手（00-待办 #7 / F07）：每次注入生成一次性 nonce
  // 写进 documentElement dataset 并随每条捕获消息携带；隔离世界校验一致
  // 才入档。页内脚本仍能读到 DOM 里的值（MV3 双世界架构的已知边界），
  // 但无 nonce 的盲喷伪造消息从此被直接丢弃，不再能污染捕获档案。
  const CAPTURE_NONCE = (() => {
    try {
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      const bytes = new Uint8Array(16);
      globalThis.crypto?.getRandomValues?.(bytes);
      if (bytes.some((b) => b !== 0)) {
        return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
      }
    } catch { /* fall through */ }
    return `${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`;
  })();
  try {
    const root = document.documentElement;
    if (root) root.dataset.shengjianMainNonce = CAPTURE_NONCE;
  } catch { /* document may not be ready */ }

  function addMainMetric(name, amount = 1) {
    try {
      const root = document.documentElement;
      if (!root) return;
      const current = Number(root.dataset[name] || 0);
      root.dataset[name] = String(Math.min(1000000, (Number.isFinite(current) ? current : 0) + amount));
    } catch { /* document may not be ready */ }
  }

  // The generated entry places douyin-hosts.js immediately before this hook.
  // Do not rely on a preceding content script retaining a MAIN-world global.
  const _sharedHosts = globalThis.SHENGJIAN_DOUYIN_HOSTS;
  const CDN_HOST_SUFFIXES = _sharedHosts.cdnSuffixes;
  const PLAY_HOST_SUFFIXES = _sharedHosts.playSuffixes;
  const PLAY_PATH = new RegExp(_sharedHosts.playPathSource, _sharedHosts.playPathFlags);
  const MAX_PROGRESSIVE_CANDIDATES = 3;
  const MAX_HLS_CANDIDATES = 1;

  function absolutize(value) {
    const raw = String(value || "").trim();
    if (raw.startsWith("//")) return `https:${raw}`;
    return raw;
  }

  function isMediaUrl(value) {
    try {
      const parsed = new URL(absolutize(value));
      if (parsed.protocol !== "https:") return false;
      const host = parsed.hostname.toLowerCase();
      // .m3u8 是登录态播放器 detail JSON 里幸存的主清单形态，2026-09 起按
      // 「捕获但交由桌面 yt-dlp 分片下载」放行；分片本体（.ts/.m4s/.mpd）
      // 与图片/配置仍然拒绝——要的是主清单，不是片段。
      if (/(?:\.jpg|\.jpeg|\.png|\.gif|\.webp|\.json|\.js|\.css|\.mpd|\.ts|\.m4s)(?:$|\?)/i.test(parsed.pathname)) {
        return false;
      }
      if (CDN_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) {
        return true;
      }
      if (
        host === "douyin.com" || host.endsWith(".douyin.com")
        || PLAY_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))
      ) {
        return PLAY_PATH.test(`${parsed.pathname}${parsed.search}`);
      }
      return false;
    } catch {
      return false;
    }
  }

  function isHlsPathname(value) {
    try {
      return /\.m3u8(?:$|\?)/i.test(new URL(value).pathname);
    } catch {
      return false;
    }
  }

  // url_list 里常混着三类地址：CDN 渐进 MP4（签名直链，免 Cookie）、
  // /aweme/v1/play/ 播放端点（Cookie 门禁）、HLS 主清单（需分片合流）。
  // 优先级 3 > 2 > 1：渐进 MP4 先尝试，Cookie 门禁的 play 端点另存作
  // 浏览器回退，HLS 清单留在本机 yt-dlp 轨道作为低优先级备选。
  function classifyMediaUrl(value) {
    if (!isMediaUrl(value)) return 0;
    if (isHlsPathname(value)) return 1;
    try {
      const parsed = new URL(absolutize(value));
      const host = parsed.hostname.toLowerCase();
      const isCdn = CDN_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
      const isPlay = PLAY_PATH.test(`${parsed.pathname}${parsed.search}`);
      if (isCdn && !isPlay) return 3;
      return 2;
    } catch {
      return 2;
    }
  }

  function mediaUrlsFromPlayAddr(playAddr) {
    if (!playAddr || typeof playAddr !== "object") return [];
    const values = Array.isArray(playAddr.url_list) ? playAddr.url_list.slice(0, 64) : [];
    if (typeof playAddr.uri === "string") values.push(playAddr.uri);
    return [...new Set(values.map(absolutize).filter((url) => url && classifyMediaUrl(url)))];
  }

  // TASK-8.17 封面：与桌面 engine.downloader 的 douyin 图片 CDN 白名单同源
  // （douyinpic/byteimg/pstatp）。封面只走图片域，比媒体直链更窄。
  const COVER_HOST_SUFFIXES = ["douyinpic.com", "byteimg.com", "pstatp.com"];
  function isCoverUrl(value) {
    try {
      const parsed = new URL(absolutize(value));
      if (parsed.protocol !== "https:") return false;
      const host = parsed.hostname.toLowerCase();
      return COVER_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
    } catch {
      return false;
    }
  }

  function coverUrlFromAweme(aweme) {
    if (!aweme || typeof aweme !== "object") return "";
    const video = aweme.video;
    // origin_cover 是发布方原图，cover 是平台再压版本；两者都可能缺失。
    const addresses = [
      video?.origin_cover, video?.cover, aweme.origin_cover, aweme.cover,
    ];
    for (const address of addresses) {
      const values = Array.isArray(address?.url_list) ? address.url_list.slice(0, 8) : [];
      for (const value of values) {
        const url = absolutize(value);
        if (url && isCoverUrl(url)) return url;
      }
    }
    return "";
  }

  // TASK-8.18 图集提取：每图收集 download_url_list（下载变体优先，取证④）
  // 与 url_list 的合法候选，配置的水印标记靠后、多候选保留供逐个回退。
  function isGalleryImageUrl(value) {
    try {
      const parsed = new URL(absolutize(value));
      if (parsed.protocol !== "https:") return false;
      const host = parsed.hostname.toLowerCase();
      return galleryConfig.imageCdnSuffixes.some(
        (suffix) => host === suffix || host.endsWith(`.${suffix}`),
      );
    } catch {
      return false;
    }
  }

  function galleryImageUrls(image) {
    if (!image || typeof image !== "object") return [];
    const values = [];
    for (const key of ["download_url_list", "url_list"]) {
      if (Array.isArray(image[key])) values.push(...image[key].slice(0, 16));
    }
    const seen = new Set();
    const urls = [];
    for (const value of values) {
      const url = absolutize(value);
      if (url && !seen.has(url) && isGalleryImageUrl(url)) {
        seen.add(url);
        urls.push(url);
      }
    }
    const marked = (url) => galleryConfig.watermarkMarkers.some((marker) => url.includes(marker));
    return [...urls.filter((url) => !marked(url)), ...urls.filter(marked)].slice(0, 6);
  }

  function galleryFromAweme(aweme) {
    if (!galleryConfig.enabled) return null;
    for (const field of galleryConfig.imagesFieldNames) {
      const images = aweme?.[field];
      if (!Array.isArray(images) || !images.length) continue;
      const collected = [];
      for (const image of images.slice(0, galleryConfig.maxImagesPerTask)) {
        const urls = galleryImageUrls(image);
        if (urls.length) collected.push({ urls });
      }
      if (collected.length) {
        return { images: collected, coverUrl: coverUrlFromAweme(aweme) };
      }
    }
    return null;
  }

  // Keep a few alternatives from the page response. The first signed CDN URL
  // may already be expired or refused; the previous single-URL handoff then
  // repeated that same failure through chrome.downloads, which cannot supply
  // the page Referer. Try a bounded set of progressive variants and one HLS
  // manifest in the local worker instead.
  function mediaCandidatesFromAweme(aweme) {
    if (!aweme || typeof aweme !== "object") return { candidates: [], playUrl: "", coverUrl: "" };
    const video = aweme.video;
    const ordered = [];
    const seen = new Set();
    const addAddress = (address) => {
      for (const url of mediaUrlsFromPlayAddr(address)) {
        if (!seen.has(url)) {
          seen.add(url);
          ordered.push(url);
        }
      }
    };
    // Higher bitrate renditions are listed first; check both API layouts.
    for (const rates of [aweme.bit_rate, video?.bit_rate]) {
      if (Array.isArray(rates)) {
        for (const rate of rates) {
          addAddress(rate?.play_addr);
          addAddress(rate?.play_addr_h264);
        }
      }
    }
    if (video && typeof video === "object") {
      addAddress(video.play_addr);
      addAddress(video.play_addr_h264);
    }
    const playUrl = ordered.find((url) => classifyMediaUrl(url) === 2) || "";
    const progressive = ordered.filter((url) => classifyMediaUrl(url) === 3)
      .slice(0, MAX_PROGRESSIVE_CANDIDATES);
    const hls = ordered.filter((url) => classifyMediaUrl(url) === 1)
      .slice(0, MAX_HLS_CANDIDATES);
    const candidates = [...progressive, ...hls];
    const coverUrl = coverUrlFromAweme(aweme);
    return { candidates, playUrl, coverUrl };
  }

  function selectCapturedItems(items, limit, newest = false) {
    const selected = (String(location.pathname || "").match(/\/(?:video|share\/video)\/(\d{10,24})/)
      || String(location.search || "").match(/[?&]modal_id=(\d{10,24})/))?.[1] || "";
    const protectedItems = selected ? items.filter(item => item.videoId === selected) : [];
    const others = selected ? items.filter(item => item.videoId !== selected) : items;
    const retained = newest ? protectedItems.slice(-limit) : protectedItems.slice(0, limit);
    const remaining = limit - retained.length;
    return retained.concat(remaining > 0 ? (newest ? others.slice(-remaining) : others.slice(0, remaining)) : []);
  }

  function collectAwemes(payload) {
    if (!payload || typeof payload !== "object") return [];
    const found = [];
    const seenAwemes = new Set();
    const visited = new Set();
    let visitedCount = 0;
    const push = (aweme) => {
      if (!aweme || typeof aweme !== "object" || seenAwemes.has(aweme)) return;
      seenAwemes.add(aweme);
      const id = String(aweme.aweme_id || aweme.id || "");
      if (!/^\d{10,24}$/.test(id)) return;
      const media = mediaCandidatesFromAweme(aweme);
      const url = media.candidates[0] || media.playUrl;
      // TASK-8.18：无视频候选时走图集分支（字段表驱动）；有视频则以视频为准。
      const gallery = url ? null : galleryFromAweme(aweme);
      const coverUrl = media.coverUrl || (gallery ? gallery.coverUrl : "");
      // TASK-8.17：封面与媒体地址独立交付——播放地址过期时封面仍可送达。
      if (url || coverUrl || gallery) {
        found.push({
          videoId: id, url, candidates: media.candidates, playUrl: media.playUrl,
          coverUrl, ...(gallery ? { gallery: gallery.images } : {}),
        });
      }
    };
    const visit = (node, depth) => {
      if (!node || typeof node !== "object" || depth > 12 || visitedCount >= 20000 || found.length >= MAX_ITEMS_PER_MESSAGE * 4) return;
      if (visited.has(node)) return;
      visited.add(node);
      visitedCount += 1;
      if (Array.isArray(node)) {
        for (const entry of node) visit(entry, depth + 1);
        return;
      }
      push(node);
      // The web app changes envelope keys periodically. Walk nested data
      // objects instead of relying on aweme_detail/data/aweme_list locations.
      for (const value of Object.values(node)) visit(value, depth + 1);
    };
    visit(payload, 0);
    // A selected favorite can occur after the first page of list entries.
    // Preserve its exact aweme_id before applying the bounded message cap.
    return selectCapturedItems(found, MAX_ITEMS_PER_MESSAGE);
  }

  function inspectPayloadShape(payload) {
    const stats = {
      awemeNodes: 0,
      videoNodes: 0,
      playAddrNodes: 0,
      urlCandidates: 0,
      httpsCandidates: 0,
      allowedUrls: 0,
      // 封面取证（2026-11-01）：实机 455 个完成任务封面全空——aweme 节点在
      // 捕获、封面却从未送达。coverNodes=响应里出现过的封面地址节点数，
      // coverAccepted=其中通过图片域白名单的地址数；两者背离即字段漂移
      // 或 CDN 换域，下一轮适配就有依据。
      coverNodes: 0,
      coverAccepted: 0,
    };
    const visited = new Set();
    const seenAddresses = new Set();
    let visitedCount = 0;
    const inspectAddress = (address) => {
      if (!address || typeof address !== "object" || seenAddresses.has(address)) return;
      seenAddresses.add(address);
      stats.playAddrNodes += 1;
      const values = Array.isArray(address.url_list) ? address.url_list.slice(0, 64) : [];
      if (typeof address.uri === "string") values.push(address.uri);
      for (const value of values) {
        if (typeof value !== "string" || !value.trim()) continue;
        stats.urlCandidates += 1;
        const absolute = absolutize(value);
        if (absolute.startsWith("https://")) stats.httpsCandidates += 1;
        if (isMediaUrl(absolute)) stats.allowedUrls += 1;
      }
    };
    // 与 coverUrlFromAweme 同一地址集合：video.origin_cover / video.cover /
    // aweme.origin_cover / aweme.cover。
    const inspectCover = (address) => {
      if (!address || typeof address !== "object" || seenAddresses.has(address)) return;
      seenAddresses.add(address);
      stats.coverNodes += 1;
      const values = Array.isArray(address.url_list) ? address.url_list.slice(0, 8) : [];
      for (const value of values) {
        if (typeof value !== "string" || !value.trim()) continue;
        if (isCoverUrl(absolutize(value))) stats.coverAccepted += 1;
      }
    };
    const visit = (node, depth) => {
      if (!node || typeof node !== "object" || depth > 12 || visitedCount >= 20000) return;
      if (visited.has(node)) return;
      visited.add(node);
      visitedCount += 1;
      if (Array.isArray(node)) {
        for (const entry of node) visit(entry, depth + 1);
        return;
      }
      const id = String(node.aweme_id || node.id || "");
      if (/^\d{10,24}$/.test(id)) stats.awemeNodes += 1;
      inspectCover(node?.origin_cover);
      inspectCover(node?.cover);
      if (node.video && typeof node.video === "object") {
        stats.videoNodes += 1;
        inspectAddress(node.video.play_addr);
        inspectAddress(node.video.play_addr_h264);
        inspectCover(node.video?.origin_cover);
        inspectCover(node.video?.cover);
        if (Array.isArray(node.video.bit_rate)) {
          for (const rate of node.video.bit_rate.slice(0, 64)) {
            inspectAddress(rate?.play_addr);
            inspectAddress(rate?.play_addr_h264);
          }
        }
      }
      inspectAddress(node.play_addr);
      inspectAddress(node.play_addr_h264);
      if (Array.isArray(node.bit_rate)) {
        for (const rate of node.bit_rate.slice(0, 64)) {
          inspectAddress(rate?.play_addr);
          inspectAddress(rate?.play_addr_h264);
        }
      }
      for (const value of Object.values(node)) visit(value, depth + 1);
    };
    visit(payload, 0);
    return stats;
  }

  let pending = [];
  let flushTimer = null;

  // 归档随送达更新：delete+set 让插入顺序即新鲜度顺序，头部即最旧；
  // 过期与超量都从头部裁剪，内存上界 = MAX_ARCHIVE_ITEMS × 每条一URL集。
  function archiveCapturedItems(items) {
    const now = Date.now();
    for (const item of items) {
      captureArchive.delete(item.videoId);
      captureArchive.set(item.videoId, {
        videoId: item.videoId,
        url: item.url,
        candidates: item.candidates.slice(0, MAX_ARCHIVE_CANDIDATES),
        playUrl: item.playUrl,
        coverUrl: item.coverUrl || "",
        // 图集档案按条限幅（每图 urls 已 ≤6）：240 槽 × 8 图内嵌足够重放。
        gallery: Array.isArray(item.gallery) ? item.gallery.slice(0, 8) : undefined,
        at: now,
      });
    }
    while (captureArchive.size) {
      const [id, entry] = captureArchive.entries().next().value;
      if (now - entry.at <= ARCHIVE_TTL_MS && captureArchive.size <= MAX_ARCHIVE_ITEMS) break;
      captureArchive.delete(id);
    }
  }

  // 定向重放：nonce 与捕获消息同一握手，只回请求里那个 aweme_id 的
  // 档案条目；回包沿用捕获消息的形状，隔离世界仍走 acceptMainWorldCapture
  // 的既有验证管线（同源＋白名单＋每条上限）。
  function replayCapture(data) {
    if (String(data?.nonce || "") !== CAPTURE_NONCE) return false;
    const videoId = String(data?.videoId || "");
    if (!/^\d{10,24}$/.test(videoId)) return false;
    const hit = captureArchive.get(videoId);
    if (!hit || Date.now() - hit.at > ARCHIVE_TTL_MS) return false;
    // 重放即“正在被使用”：刷新时间戳并移到队尾，洪峰裁剪不会挤掉
    // 反复点击中的目标。
    captureArchive.delete(videoId);
    hit.at = Date.now();
    captureArchive.set(videoId, hit);
    const { at, ...item } = hit;
    try {
      window.postMessage({ source: MESSAGE_SOURCE, nonce: CAPTURE_NONCE, items: [item] },
        window.location.origin);
    } catch {
      return false;
    }
    return true;
  }

  function flush() {
    flushTimer = null;
    if (!pending.length) return;
    const items = pending;
    pending = [];
    try {
      // ISOLATED accepts 24 items per message. Coalesced responses must be
      // split, or their second batch is silently lost at that receiver.
      for (let offset = 0; offset < items.length; offset += MAX_ITEMS_PER_MESSAGE) {
        window.postMessage({ source: MESSAGE_SOURCE, nonce: CAPTURE_NONCE,
          items: items.slice(offset, offset + MAX_ITEMS_PER_MESSAGE) }, window.location.origin);
      }
    } catch {
      // The isolated world may be gone (extension reload); drop the batch.
    }
  }

  function offer(text) {
    addMainMetric("shengjianMainApiResponses");
    let payload;
    try {
      payload = JSON.parse(String(text));
    } catch {
      addMainMetric("shengjianMainJsonFailures");
      return;
    }
    addMainMetric("shengjianMainJsonResponses");
    const shape = inspectPayloadShape(payload);
    for (const [key, value] of Object.entries(shape)) {
      const metricName = `shengjianMain${key[0].toUpperCase()}${key.slice(1)}`;
      addMainMetric(metricName, value);
    }
    const items = collectAwemes(payload);
    if (!items.length) return;
    addMainMetric("shengjianMainMediaItems", items.length);
    archiveCapturedItems(items);
    pending = selectCapturedItems(pending.concat(items), MAX_ITEMS_PER_MESSAGE * 2, true);
    // Batch briefly so one feed response with many awemes emits one message.
    if (flushTimer === null) flushTimer = setTimeout(flush, 150);
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function (input, init) {
      const promise = nativeFetch.apply(this, arguments);
      try {
        const url = typeof input === "string" ? input : String(input?.url || "");
        if (API_HINT.test(url)) {
          addMainMetric("shengjianMainApiRequests");
          promise.then((response) => {
            try {
              if (response && typeof response.clone === "function") {
                response.clone().text().then(offer, () => {});
              }
            } catch {
              // Never let observation break the page's own request chain.
            }
          }, () => {});
        }
      } catch {
        // Restore a pristine fetch if wrapping failed mid-way.
        window.fetch = nativeFetch;
      }
      return promise;
    };
  }

  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__shengjianUrl = String(url || "");
    return nativeOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (API_HINT.test(this.__shengjianUrl || "")) {
      addMainMetric("shengjianMainApiRequests");
      this.addEventListener("load", () => {
        try {
          offer(this.responseText);
        } catch {
          // Response may be unreadable (opaque); nothing to do.
        }
      });
    }
    return nativeSend.apply(this, arguments);
  };

  // 隔离世界的定向重放请求：nonce 校验在 replayCapture 内完成，监听器
  // 只做同源过滤后转发。
  window.addEventListener("message", (event) => {
    const data = event?.data;
    if (!data || data.source !== REPLAY_REQUEST_SOURCE) return;
    if (event.origin !== window.location.origin) return;
    replayCapture(data);
  });

  // ISOLATED 中继的 8.16 热切换配置（同源＋nonce＋形状校验，坏形状回落
  // 默认禁用——配置是数据不是代码，MV3 合规）。
  window.addEventListener("message", (event) => {
    const data = event?.data;
    if (!data || data.source !== CONFIG_SOURCE) return;
    if (event.origin !== window.location.origin) return;
    if (String(data.nonce || "") !== CAPTURE_NONCE) return;
    galleryConfig = normalizeGalleryConfig(data.config);
  });

  globalThis.__shengjianMainInternals = {
    isMediaUrl,
    classifyMediaUrl,
    isCoverUrl,
    coverUrlFromAweme,
    collectAwemes,
    mediaCandidatesFromAweme,
    galleryFromAweme,
    normalizeGalleryConfig,
    galleryConfig: () => galleryConfig,
    inspectPayloadShape,
    offer,
    flush,
    replayCapture,
    captureArchive,
    MESSAGE_SOURCE,
  };
})();
