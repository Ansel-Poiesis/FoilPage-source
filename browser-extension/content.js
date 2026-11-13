// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
(function () {
  "use strict";

  if (window.top !== window) return;

  const CONTROL_CLASS = "shengjian-player-control";
  const EXTENSION_VERSION = "0.1.1";
  const CAPTURE_RETRY_GUIDE = "若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。";
  const captureRetryGuides = new Map();
  let syncQueued = false;

  function rememberCaptureRetryGuide(videoId) {
    const id = String(videoId || "");
    if (!id) return;
    const now = Date.now();
    for (const [key, expiresAt] of captureRetryGuides) {
      if (expiresAt <= now) captureRetryGuides.delete(key);
    }
    captureRetryGuides.set(id, now + 10 * 60 * 1000);
    while (captureRetryGuides.size > 32) captureRetryGuides.delete(captureRetryGuides.keys().next().value);
  }

  function decorateCaptureRetryNotice(message, videoId, stage) {
    const id = String(videoId || "");
    const expiresAt = captureRetryGuides.get(id);
    if (!id || !expiresAt) return String(message || "");
    if (expiresAt <= Date.now()) {
      captureRetryGuides.delete(id);
      return String(message || "");
    }
    if (stage === "download") {
      captureRetryGuides.delete(id);
      return String(message || "");
    }
    if (stage !== "submitted" && stage !== "failure") return String(message || "");
    captureRetryGuides.delete(id);
    return `${String(message || "下载未成功")}（${CAPTURE_RETRY_GUIDE}）`;
  }

  function mainWorldStatus() {
    // MAIN 在 document_start 写 dataset；MV3 自更新后旧页常无标记或版本不一致。
    const dataset = (document.documentElement && document.documentElement.dataset) || {};
    const version = String(dataset.shengjianMain || "");
    return {
      present: Boolean(version),
      version,
      current: EXTENSION_VERSION,
      stale: Boolean(version) && version !== EXTENSION_VERSION,
      apiRequests: readMainCount(dataset.shengjianMainApiRequests),
      apiResponses: readMainCount(dataset.shengjianMainApiResponses),
      jsonResponses: readMainCount(dataset.shengjianMainJsonResponses),
      jsonFailures: readMainCount(dataset.shengjianMainJsonFailures),
      mediaItems: readMainCount(dataset.shengjianMainMediaItems),
      awemeNodes: readMainCount(dataset.shengjianMainAwemeNodes),
      videoNodes: readMainCount(dataset.shengjianMainVideoNodes),
      playAddrNodes: readMainCount(dataset.shengjianMainPlayAddrNodes),
      urlCandidates: readMainCount(dataset.shengjianMainUrlCandidates),
      httpsCandidates: readMainCount(dataset.shengjianMainHttpsCandidates),
      allowedUrls: readMainCount(dataset.shengjianMainAllowedUrls),
      coverNodes: readMainCount(dataset.shengjianMainCoverNodes),
      coverAccepted: readMainCount(dataset.shengjianMainCoverAccepted),
    };
  }

  function readMainCount(value) {
    const parsed = Number(value || 0);
    return Number.isFinite(parsed) ? Math.max(0, Math.min(1000000, Math.floor(parsed))) : 0;
  }

  function gateReason(status, capture = {}) {
    if (!status.present) {
      return "main_world_missing";
    }
    if (status.stale) {
      return "extension_stale";
    }
    if (status.mediaItems > 0 && !capture.targetCaptured) return "captured_video_mismatch";
    if (status.apiRequests > 0 && status.apiResponses === 0) return "detail_response_unavailable";
    if (status.apiResponses > 0 && status.jsonResponses === 0 && status.jsonFailures > 0) return "detail_response_invalid_json";
    if (status.jsonResponses > 0 && status.awemeNodes === 0) return "detail_payload_unrecognized";
    if (status.urlCandidates > 0 && status.allowedUrls === 0) return "detail_media_url_rejected";
    if (status.apiResponses > 0 && status.mediaItems === 0) return "detail_response_without_media";
    if (status.apiRequests === 0) return "detail_request_not_seen";
    return "player_url_not_ready";
  }

  function gateMessage(status = mainWorldStatus(), capture = {}) {
    switch (gateReason(status, capture)) {
      case "main_world_missing":
        return "抖音采集脚本未加载；请刷新当前页面后重试";
      case "extension_stale":
        return `扩展已更新到 ${status.current}，当前页面仍在使用旧脚本；请刷新页面后重试`;
      case "captured_video_mismatch":
        return "抖音已返回播放信息，但未匹配当前视频；请重新打开该视频并等待播放稳定后重试";
      case "detail_response_invalid_json":
        return "抖音详情响应无法解析；本次采集已记录，请刷新页面并播放视频后重试";
      case "detail_payload_unrecognized":
        return "抖音已返回详情，但响应结构未被识别；诊断数据已记录，需要更新媒体解析适配";
      case "detail_media_url_rejected":
        return "抖音已返回媒体地址候选，但当前格式未被接受；诊断数据已记录，需要更新地址规则";
      case "detail_response_without_media":
        return "详情响应中暂未找到可用播放地址；请让当前视频播放 2 秒后重试，本次采集已记录";
      case "detail_response_unavailable":
        return "已观察到抖音详情请求，但响应未能读取；本次采集已记录，请刷新页面后重试";
      case "detail_request_not_seen":
        return "采集脚本已加载，但未观察到当前视频详情请求；请在视频详情页播放 2 秒后重试";
      default:
        return "播放器暂未提供可用视频地址；本次采集已记录，请播放 2 秒后重试";
    }
  }

  function cleanTitle(value) {
    return String(value || "")
      .replace(/\s*展开\s*$/u, "")
      .replace(/\s+/gu, " ")
      .trim()
      .slice(0, 240);
  }

  function activeFeedRoot() {
    const roots = Array.from(document.querySelectorAll('[data-e2e="feed-active-video"]'));
    const visible = roots.filter(isVisible);
    const candidates = visible.length ? visible : roots;
    return candidates.sort((left, right) => feedScore(right) - feedScore(left))[0] || null;
  }

  function isVisible(element) {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0
      && rect.bottom > 0 && rect.top < window.innerHeight;
  }

  function feedScore(root) {
    const rect = root.getBoundingClientRect();
    const center = Math.abs((rect.top + rect.bottom) / 2 - window.innerHeight / 2);
    const video = root.querySelector("video");
    let score = Math.max(0, rect.width) * Math.max(0, rect.height) - center * 20;
    if (video && !video.paused && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) score += 10_000_000;
    if (root.getAttribute("data-e2e-vid")) score += 100_000;
    return score;
  }

  function visibleVideo() {
    const candidates = Array.from(document.querySelectorAll("video")).filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 160 && rect.height > 120 && rect.bottom > 0 && rect.top < window.innerHeight;
    });
    return candidates.sort((left, right) => videoScore(right) - videoScore(left))[0] || null;
  }

  function videoScore(video) {
    const rect = video.getBoundingClientRect();
    const visibleWidth = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
    const visibleHeight = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
    let score = visibleWidth * visibleHeight;
    if (!video.paused && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) score += 10_000_000;
    if (video.closest('[data-e2e="feed-active-video"]')) score += 5_000_000;
    return score;
  }

  function visibleVideoIdLink(scope) {
    const links = Array.from(scope.querySelectorAll?.('a[href*="/video/"]') || []);
    const visible = links.find((link) => {
      const rect = link.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
    });
    return visible?.getAttribute("href")?.match(/\/video\/(\d{10,24})/)?.[1] || "";
  }

  function feedVideoId(root) {
    if (!root) return "";
    return root.getAttribute("data-e2e-vid")
      || root.querySelector('[data-e2e-vid]')?.getAttribute("data-e2e-vid")
      || visibleVideoIdLink(root);
  }

  function activeVideo() {
    const direct = location.pathname.match(/\/(?:video|share\/video|note|share\/note)\/(\d{10,24})/)
      // 收藏夹弹窗形态：/user/self?...&modal_id=<aweme_id>&showTab=favorite_collection
      // 路径里没有 /video/<id>，视频号只能从 modal_id 取（先生按「先收藏」
      // 指引操作后必经此形态，识别缺失即守门拦截）。/note/ 是图集（TASK-8.18）。
      || (location.search || "").match(/[?&]modal_id=(\d{10,24})/)
      || undefined;
    const explicitId = direct?.[1] || "";
    const active = activeFeedRoot();
    const activeId = feedVideoId(active);
    // 在收藏夹弹窗里，底层信息流仍可能保留一个“活跃视频”。URL 上的
    // video/modal_id 是用户实际选中的目标，不能被底层视频编号覆盖；若
    // 编号冲突，也绝不能把底层播放器的签名地址交给弹窗目标。
    const activeMatchesTarget = !explicitId || !activeId || activeId === explicitId;
    const mediaRoot = activeMatchesTarget ? active : null;
    const playerVideo = mediaRoot?.querySelector("video") || (activeMatchesTarget ? visibleVideo() : null);
    // Feed 容器的 data-e2e 值随改版变动（feed-active-video / feed-video / …），
    // 直接沿祖先链找带 data-e2e-vid 的节点最稳——id 属性本身从未变过。
    const root = mediaRoot
      || playerVideo?.closest('[data-e2e="feed-active-video"], [data-e2e="feed-video"], [data-e2e-vid]')
      || playerVideo?.parentElement
      || null;
    const nearbyId = feedVideoId(root) || visibleVideoIdLink(root || document);
    const videoId = explicitId || nearbyId;
    const desc = root?.querySelector('[data-e2e="video-desc"]')
      || Array.from(document.querySelectorAll('[data-e2e="video-desc"]')).find(isVisible);
    const title = cleanTitle(desc?.textContent)
      || cleanTitle(document.title.replace(/\s*-\s*抖音.*$/u, ""));
    return {
      root,
      videoId: /^\d{10,24}$/.test(videoId) ? videoId : "",
      title,
      playerMatchesTarget: activeMatchesTarget,
    };
  }

  // 主机白名单来自 douyin-hosts.js（两个世界共享的单一事实源）。
  const _hosts = globalThis.SHENGJIAN_DOUYIN_HOSTS;
  const DOUYIN_CDN_HOST_SUFFIXES = _hosts.cdnSuffixes;
  const DOUYIN_PLAY_HOST_SUFFIXES = _hosts.playSuffixes;
  const DOUYIN_PLAY_PATH = new RegExp(_hosts.playPathSource, _hosts.playPathFlags);

  function isDouyinMediaHint(value) {
    try {
      const parsed = new URL(value);
      if (parsed.protocol !== "https:") return false;
      const host = parsed.hostname.toLowerCase();
      // 与 MAIN 世界捕获口径一致：.m3u8 主清单放行（桌面端走 yt-dlp 分片
      // 下载）；分片本体与图片/配置仍拒绝。
      if (/(?:\.jpg|\.jpeg|\.png|\.gif|\.webp|\.json|\.js|\.css|\.mpd|\.ts|\.m4s)(?:$|\?)/i.test(parsed.pathname)) {
        return false;
      }
      if (DOUYIN_CDN_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) {
        return true;
      }
      if (
        host === "douyin.com" || host.endsWith(".douyin.com")
        || DOUYIN_PLAY_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))
      ) {
        return DOUYIN_PLAY_PATH.test(`${parsed.pathname}${parsed.search}`);
      }
      return false;
    } catch {
      return false;
    }
  }

  function activeMediaUrl(active = activeVideo()) {
    if (active.playerMatchesTarget === false) return "";
    const video = active.root?.querySelector("video") || visibleVideo();
    const candidates = [];
    const direct = video?.currentSrc || video?.src || "";
    if (direct) candidates.push(direct);
    if (video) {
      for (const source of video.querySelectorAll("source")) {
        const src = source.src || source.getAttribute("src") || "";
        if (src) candidates.push(src);
      }
    }
    for (const candidate of candidates) {
      if (isDouyinMediaHint(candidate)) return candidate;
    }
    return "";
  }

  // 页面 SSR 标记自带可直提的播放地址（合成 MP4 与带 target=<视频id> 的
  // /aweme/v1/play/ 链接），但播放器水合后会把这份标记换掉——长视频切换到
  // blob 分轨流后，实时 DOM 里可能什么都不剩。于是在标记还在时就记下来，
  // 提交时复用；带 target= 的地址可精确归属到视频，未绑定的只在详情页
  // （整页只有一个视频）兜底使用，信息流里预加载会使其归属不可信。
  // 有效期 2 小时（2026-10-14 实机：长会话页面回看早前视频时 10 分钟
  // TTL 让采集数据过早过期，目标落入 resolver 又被平台校验拦截）。
  // 直链签名通常数小时内有效；即便个别过期，下载失败仍走既有回退。
  const CAPTURE_TTL_MS = 120 * 60 * 1000;
  const MAX_TRACKED_TARGETS = 20;
  const MAX_CAPTURED_PROGRESSIVE = 3;
  const MAX_CAPTURED_HLS = 1;
  const MAX_PERFORMANCE_ENTRIES = 240;
  const capturedByTarget = new Map();
  let latestUnboundCapture = { url: "", at: 0 };
  let performanceScanIndex = 0;

  function captureTargetId(url) {
    try {
      const target = new URL(url).searchParams.get("target") || "";
      return /^\d{10,24}$/.test(target) ? target : "";
    } catch {
      return "";
    }
  }

  function rankMediaHint(value) {
    if (!isDouyinMediaHint(value)) return 0;
    if (/\.m3u8(?:$|\?)/i.test(value)) return 1;
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase();
      const isCdn = DOUYIN_CDN_HOST_SUFFIXES.some(
        (suffix) => host === suffix || host.endsWith(`.${suffix}`)
      );
      const isPlay = DOUYIN_PLAY_PATH.test(`${parsed.pathname}${parsed.search}`);
      if (isCdn && !isPlay) return 3;
      return 2;
    } catch {
      return 2;
    }
  }

  function mediaHintIdentity(value) {
    try {
      const parsed = new URL(value);
      return `${parsed.origin}${parsed.pathname}`;
    } catch {
      return value;
    }
  }

  // TASK-8.17 封面：与 MAIN 世界钩子、桌面 engine.downloader 同源的抖音
  // 图片 CDN 白名单——封面地址只进这个域，不与媒体直链混用。
  const COVER_HOST_SUFFIXES = ["douyinpic.com", "byteimg.com", "pstatp.com"];
  function isDouyinCoverUrl(value) {
    const raw = String(value || "");
    if (!raw || raw.length > 4096) return false;
    try {
      const parsed = new URL(raw.startsWith("//") ? `https:${raw}` : raw);
      if (parsed.protocol !== "https:") return false;
      const host = parsed.hostname.toLowerCase();
      return COVER_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
    } catch {
      return false;
    }
  }

  function rememberCover(videoId, coverUrl) {
    if (!/^\d{10,24}$/.test(videoId) || !isDouyinCoverUrl(coverUrl)) return;
    const existing = capturedByTarget.get(videoId);
    const now = Date.now();
    if (existing) {
      if (existing.coverUrl !== coverUrl || now - (existing.coverAt || 0) > CAPTURE_TTL_MS) {
        existing.coverUrl = coverUrl;
        existing.coverAt = now;
      }
      return;
    }
    capturedByTarget.set(videoId, {
      url: "", at: now, playUrl: "", playAt: 0, candidates: [],
      coverUrl, coverAt: now,
    });
  }

  function coverUrlFor(videoId) {
    const hit = capturedByTarget.get(String(videoId || ""));
    if (!hit || !hit.coverUrl) return "";
    return Date.now() - (hit.coverAt || 0) <= CAPTURE_TTL_MS ? hit.coverUrl : "";
  }

  // TASK-8.18 图集档案：每图 {urls:[候选…]}，候选已由 MAIN 世界按配置
  // 白名单/水印排序收窄；这里再做一道固定图片域复核（与桌面校验同口径）。
  function rememberGallery(videoId, images) {
    if (!/^\d{10,24}$/.test(videoId) || !Array.isArray(images) || !images.length) return;
    const clean = images
      .filter((image) => image && Array.isArray(image.urls) && image.urls.length
        && image.urls.every((url) => isDouyinCoverUrl(url)))
      .slice(0, 60);
    if (!clean.length) return;
    const existing = capturedByTarget.get(videoId);
    const now = Date.now();
    if (existing) {
      existing.gallery = clean;
      existing.galleryAt = now;
      return;
    }
    capturedByTarget.set(videoId, {
      url: "", at: now, playUrl: "", playAt: 0, candidates: [],
      coverUrl: "", coverAt: 0, gallery: clean, galleryAt: now,
    });
  }

  function galleryFor(videoId) {
    const hit = capturedByTarget.get(String(videoId || ""));
    if (!hit || !Array.isArray(hit.gallery) || !hit.gallery.length) return null;
    return Date.now() - (hit.galleryAt || 0) <= CAPTURE_TTL_MS ? hit.gallery : null;
  }

  // TASK-8.18：从 chrome.storage 读 8.16 热切换配置并中继给 MAIN 钩子
  // （nonce 握手同捕获消息；storage 不可用或形状缺失时 MAIN 保持默认禁用）。
  function relayGalleryConfig() {
    try {
      chrome.storage.local.get("shengjianClientConfig", (stored) => {
        const entry = stored?.shengjianClientConfig;
        const flags = entry?.config?.flags || {};
        const gallery = entry?.config?.douyin_gallery || {};
        const nonce = String(document.documentElement?.dataset?.shengjianMainNonce || "");
        if (!nonce) return;
        window.postMessage({
          source: "shengjian-client-config",
          nonce,
          config: {
            enabled: flags.douyin_gallery_enabled === true,
            imagesFieldNames: gallery.images_field_names,
            imageCdnSuffixes: gallery.image_cdn_suffixes,
            watermarkMarkers: gallery.watermark_markers,
            maxImagesPerTask: gallery.max_images_per_task,
          },
        }, window.location.origin);
      });
    } catch { /* MAIN 维持默认禁用 */ }
  }
  relayGalleryConfig();
  try {
    chrome.storage.onChanged?.addListener?.((changes, area) => {
      if (area === "local" && changes.shengjianClientConfig) relayGalleryConfig();
    });
  } catch { /* storage 监听不可用时 MAIN 维持已中继的配置 */ }

  function isPlayEndpointMedia(value) {
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase();
      const isPlayHost = host === "douyin.com" || host.endsWith(".douyin.com")
        || host.endsWith(".snssdk.com") || host.endsWith(".amemv.com");
      return isPlayHost && /\/aweme\/v1\/play|\/video\/play|\/video\/download/i.test(parsed.pathname);
    } catch { return false; }
  }

  function rememberCapture(url, boundTarget = "") {
    const target = boundTarget || captureTargetId(url);
    const rank = rankMediaHint(url);
    if (!rank) return;
    if (target) {
      const existing = capturedByTarget.get(target);
      const now = Date.now();
      const candidates = Array.isArray(existing?.candidates)
        ? existing.candidates.filter((entry) => now - entry.at < CAPTURE_TTL_MS)
        : [];
      let playUrl = existing?.playAt && now - existing.playAt < CAPTURE_TTL_MS
        ? (existing.playUrl || "")
        : "";
      let playAt = playUrl ? existing.playAt : 0;
      if (isPlayEndpointMedia(url)) {
        if (!playUrl) {
          playUrl = url;
          playAt = now;
        }
      } else {
        const identity = mediaHintIdentity(url);
        const known = candidates.find((entry) => mediaHintIdentity(entry.url) === identity);
        if (known) {
          known.url = url;
          known.at = now;
        } else {
          candidates.push({ url, at: now, rank });
        }
      }
      // Preserve the page's bitrate order (highest first), while replacing a
      // same resource path with its freshest signature when the page refreshes.
      const progressive = candidates.filter((entry) => entry.rank === 3)
        .slice(0, MAX_CAPTURED_PROGRESSIVE);
      const hls = candidates.filter((entry) => entry.rank === 1)
        .slice(0, MAX_CAPTURED_HLS);
      const retained = [...progressive, ...hls];
      const primary = retained[0]?.url || playUrl;
      capturedByTarget.set(target, {
        url: primary,
        at: now,
        playUrl,
        playAt,
        candidates: retained,
        // TASK-8.17：条目整体替换时保住已入库封面——同一视频后续无封面/
        // 非法封面的捕获（SSR 扫描、信息流重放）不得抹掉早前的合法封面。
        coverUrl: existing?.coverUrl || "",
        coverAt: existing?.coverAt || 0,
      });
      while (capturedByTarget.size > MAX_TRACKED_TARGETS) {
        const selected = activeVideo().videoId;
        let oldest = "";
        for (const [id, hit] of capturedByTarget) {
          // A 24-item list would otherwise evict its first four videos from
          // this 20-target cache, including the currently selected modal.
          if (id === selected) continue;
          if (!oldest || hit.at < capturedByTarget.get(oldest).at) oldest = id;
        }
        if (!oldest) break;
        capturedByTarget.delete(oldest);
      }
      return;
    }
    if (rankMediaHint(latestUnboundCapture.url) > rank && Date.now() - latestUnboundCapture.at < CAPTURE_TTL_MS) {
      return;
    }
    latestUnboundCapture = { url, at: Date.now() };
  }

  // MAIN-world partner (content-main.js) observes the aweme detail/feed JSON
  // the page itself consumes and forwards {videoId, candidates, playUrl} data. A
  // logged-in player often plays an MSE blob whose segment URLs are filtered
  // media types, so this API observation is the most reliable progressive-MP4
  // source in that state. Only same-origin messages with our source marker are
  // accepted, and every URL still passes the shared allowlist below.
  function acceptMainWorldCapture(event) {
    const data = event?.data;
    if (!data || data.source !== "shengjian-media-capture") return;
    if (event.origin !== window.location.origin) return;
    // MAIN↔ISOLATED nonce 握手（00-待办 #7 / F07）：MAIN 注入时把一次性
    // nonce 写进 documentElement dataset 并随每条捕获消息携带；dataset
    // 尚未就绪、消息缺 nonce 或不匹配一律丢弃——页内盲喷的伪造捕获不再
    // 进入档案。同页两个世界同版本注入，nonce 天然一致。
    const expectedNonce = String(document.documentElement?.dataset?.shengjianMainNonce || "");
    if (!expectedNonce || String(data.nonce || "") !== expectedNonce) return;
    const items = Array.isArray(data.items) ? data.items : [];
    for (const item of items.slice(0, 24)) {
      const videoId = String(item?.videoId || "");
      const url = String(item?.url || "");
      if (!/^\d{10,24}$/.test(videoId)) continue;
      const candidates = Array.isArray(item?.candidates) ? item.candidates.slice(0, 8) : [];
      for (const candidate of candidates) {
        const value = String(candidate || "");
        if (value.length <= 4096 && isDouyinMediaHint(value)) rememberCapture(value, videoId);
      }
      const playUrl = String(item?.playUrl || "");
      if (playUrl.length <= 4096 && isDouyinMediaHint(playUrl)) rememberCapture(playUrl, videoId);
      if (url.length <= 4096 && isDouyinMediaHint(url)) rememberCapture(url, videoId);
      // TASK-8.17：封面独立于媒体地址入库（媒体地址过期不影响封面）。
      const coverUrl = String(item?.coverUrl || "");
      if (coverUrl.length <= 4096) rememberCover(videoId, coverUrl);
      // TASK-8.18：图集整组入库（候选已由 MAIN 按配置收窄）。
      if (Array.isArray(item?.gallery) && item.gallery.length) rememberGallery(videoId, item.gallery);
    }
  }

  function scanPerformanceSources() {
    // Douyin's current player often keeps only a blob: URL on <video> while
    // fetching the signed MP4 through MSE. Resource Timing still exposes the
    // request URL to the content script, so retain only the same allow-listed
    // media origins used by activeMediaUrl().
    const entries = globalThis.performance?.getEntriesByType?.("resource") || [];
    if (entries.length < performanceScanIndex) performanceScanIndex = 0;
    const start = Math.max(performanceScanIndex, entries.length - MAX_PERFORMANCE_ENTRIES);
    for (let index = start; index < entries.length; index += 1) {
      const value = String(entries[index]?.name || "");
      if (value && isDouyinMediaHint(value)) rememberCapture(value);
    }
    performanceScanIndex = entries.length;
  }

  function scanEmbeddedStates() {
    // SSR/hydration JSON 可能仍挂着 play_addr；只扫带媒体痕迹的 script。
    let scripts;
    try {
      scripts = document.querySelectorAll("script");
    } catch {
      return;
    }
    for (const script of scripts) {
      const text = String(script.textContent || "");
      if (text.length < 40 || text.length > 1500000) continue;
      if (!text.includes("play_addr") && !text.includes("douyinvod") && !text.includes("url_list")) continue;
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        const idMatch = text.match(/"aweme_id"\s*:\s*"(\d{10,24})"/) || text.match(/[?&]target=(\d{10,24})/);
        const urlMatch = text.match(/https:\/\/[^"\s\\]+(?:douyinvod|snssdk|amemv)[^"\s\\]*/);
        if (idMatch && urlMatch && isDouyinMediaHint(urlMatch[0])) {
          rememberCapture(urlMatch[0], idMatch[1]);
        }
        continue;
      }
      for (const hit of collectPlayAddrs(payload)) rememberCapture(hit.url, hit.videoId);
    }
  }

  function collectPlayAddrs(root) {
    const out = [];
    const seen = new Set();
    const visit = (node, depth) => {
      if (!node || typeof node !== "object" || depth > 8 || out.length >= 24) return;
      if (Array.isArray(node)) {
        for (const item of node) visit(item, depth + 1);
        return;
      }
      const id = String(node.aweme_id || node.id || "");
      if (/^\d{10,24}$/.test(id) && node.video && typeof node.video === "object") {
        const play = node.video.play_addr || node.video.play_addr_h264;
        if (play && typeof play === "object") {
          const list = Array.isArray(play.url_list) ? play.url_list : [];
          for (const raw of list) {
            const url = String(raw || "");
            if (url && isDouyinMediaHint(url) && !seen.has(`${id}|${url}`)) {
              seen.add(`${id}|${url}`);
              out.push({ videoId: id, url });
            }
          }
        }
      }
      for (const value of Object.values(node)) visit(value, depth + 1);
    };
    visit(root, 0);
    return out;
  }

  function scanCapturedSources() {
    // video[src] only matches the attribute, so blob: assignments made via the
    // property (MSE players) never enter the archive. Read both properties and
    // attributes, then supplement them with the page's resource timing buffer.
    for (const element of document.querySelectorAll("video, source")) {
      const value = String(element.currentSrc || element.src || element.getAttribute?.("src") || "");
      if (value && isDouyinMediaHint(value)) rememberCapture(value);
    }
    scanPerformanceSources();
    scanEmbeddedStates();
  }

  function capturedMediaUrl(videoId) {
    if (videoId) {
      const hit = capturedByTarget.get(String(videoId));
      if (hit && Date.now() - hit.at < CAPTURE_TTL_MS) {
        const candidate = hit.candidates?.find((entry) => Date.now() - entry.at < CAPTURE_TTL_MS)?.url;
        if (candidate && isDouyinMediaHint(candidate)) return candidate;
        if (hit.playAt && Date.now() - hit.playAt < CAPTURE_TTL_MS
            && hit.playUrl && isDouyinMediaHint(hit.playUrl)) return hit.playUrl;
      }
    }
    if (/\/(?:video|share\/video)\/\d{10,24}/.test(location.pathname)) {
      const { url, at } = latestUnboundCapture;
      if (url && Date.now() - at < CAPTURE_TTL_MS && isDouyinMediaHint(url)) return url;
    }
    // TASK-8.28（先生 2026-10-27「博主主页往回下载较早条目恒失败」）：弹窗
    // 形态补未绑定兜底——modal_id 是用户显式点开的目标，弹窗打开时页面唯
    // 一在播的就是它（信息流/主页网格不会在弹窗后面继续播放）。目标已滚出
    // 20 槽采集档案且重放未命中时，此前只能落 yt-dlp 解析并撞平台校验墙。
    // 归因收紧：地址必须是弹窗播放期间新产生的（≤90 秒，远严于 2 小时档
    // 案 TTL），且页面上确有正在播放的视频。
    const modalId = ((location.search || "").match(/[?&]modal_id=(\d{10,24})/) || [])[1] || "";
    if (videoId && modalId === String(videoId) && _playerActivelyPlaying()) {
      const { url, at } = latestUnboundCapture;
      if (url && Date.now() - at <= 90_000 && isDouyinMediaHint(url)) return url;
    }
    return "";
  }

  function _playerActivelyPlaying() {
    try {
      const video = document.querySelector("video");
      return !!video && video.readyState === 4 && video.paused === false;
    } catch {
      return false;
    }
  }

  function capturedMediaCandidates(videoId) {
    if (!videoId) return [];
    const hit = capturedByTarget.get(String(videoId));
    if (!hit || Date.now() - hit.at >= CAPTURE_TTL_MS) return [];
    return (hit.candidates || [])
      .filter((entry) => Date.now() - entry.at < CAPTURE_TTL_MS && isDouyinMediaHint(entry.url))
      .map((entry) => entry.url);
  }

  function mediaCandidatesFor(videoId, ...preferred) {
    const values = [
      ...preferred,
      ...capturedMediaCandidates(videoId),
    ];
    const unique = [];
    const seen = new Set();
    for (const candidate of values) {
      const value = String(candidate || "");
      if (!value || isPlayEndpointMedia(value) || !isDouyinMediaHint(value) || seen.has(value)) continue;
      seen.add(value);
      unique.push(value);
    }
    // The page-world hook already bounds quality variants; this is a final
    // cap for SSR/resource-timing captures so one click stays quick and bounded.
    return unique.slice(0, MAX_CAPTURED_PROGRESSIVE + MAX_CAPTURED_HLS);
  }

  function playEndpointFor(videoId, ...preferred) {
    const hit = capturedByTarget.get(String(videoId || ""));
    const freshPlayUrl = hit?.playAt && Date.now() - hit.playAt < CAPTURE_TTL_MS
      ? (hit.playUrl || "")
      : "";
    const values = [freshPlayUrl, ...preferred];
    return values.map((value) => String(value || ""))
      .find((value) => value.length <= 4096 && isPlayEndpointMedia(value) && isDouyinMediaHint(value)) || "";
  }

  function resolveMediaUrl(active) {
    return activeMediaUrl(active) || capturedMediaUrl(active.videoId);
  }

  // On a slow or flaky network the player may not have assigned currentSrc yet
  // when the user clicks. Waiting a short moment usually lets the video start
  // buffering, so the extension can hand the real player URL straight to the
  // downloader instead of falling back to the fragile cookie-bridge resolve
  // path (which is what used to surface as a fake Chrome-authorization error).
  // Lock the click to its original video. A feed switch never borrows the
  // new player's media or resets the original bounded capture budget.

  // 收藏夹弹窗常在信息流批次送达之后才打开：送达时刻没有 modal_id，
  // capturedByTarget 的 20 槽档案可能已把目标挤出（2026-10-12 实机：
  // media_items=51 / captured_targets=20 / target_captured=false）。
  // MAIN 世界按 aweme_id 留有更长档案；等待开始时定向请求一次重放，
  // 回包沿用 acceptMainWorldCapture 的 nonce+同源+白名单验证，不新增信任面。
  function requestCaptureReplay(videoId) {
    const id = String(videoId || "");
    const nonce = String(document.documentElement?.dataset?.shengjianMainNonce || "");
    if (!id || !nonce) return;
    try {
      window.postMessage({ source: "shengjian-capture-replay", nonce, videoId: id }, window.location.origin);
    } catch { /* MAIN 世界缺席时后台解析兜底，不影响任务 */ }
  }

  function waitForMediaUrl(active, tries = 10, gapMs = 300) {
    scanCapturedSources();
    const first = resolveMediaUrl(active);
    if (first) return Promise.resolve(first);
    if (active.videoId && !capturedByTarget.has(String(active.videoId))) requestCaptureReplay(active.videoId);
    return new Promise((resolve) => {
      let remaining = tries;
      const lastVideoId = active.videoId;
      const initialStatus = mainWorldStatus();
      let lastResponseCount = initialStatus.apiResponses;
      let noMediaSince = lastResponseCount > 0 && initialStatus.mediaItems === 0 ? Date.now() : null;
      const waitStartedAt = Date.now();
      let noRequestSince = initialStatus.apiRequests === 0 ? waitStartedAt : null;
      // 点击时锁定的播放器 root 是最可靠的媒体来源；连播切换后它属于
      // 旧视频，改用新视频自己的 root，避免把旧视频的地址套在新 id 上。
      let probeRoot = active.root;
      let playerMatchesTarget = active.playerMatchesTarget;
      const timer = setInterval(() => {
        const nowActive = activeVideo();
        if (nowActive.videoId && nowActive.videoId !== lastVideoId) {
          clearInterval(timer);
          resolve(capturedMediaUrl(lastVideoId));
          return;
        }
        scanCapturedSources();
        const status = mainWorldStatus();
        if (status.apiRequests > 0) noRequestSince = null;
        if (status.apiResponses > lastResponseCount) {
          lastResponseCount = status.apiResponses;
          noMediaSince = status.mediaItems === 0 ? Date.now() : null;
        } else if (status.mediaItems > 0) {
          noMediaSince = null;
        }
        const value = resolveMediaUrl({ root: probeRoot, videoId: lastVideoId, playerMatchesTarget });
        // Once a readable detail response produces no media, wait briefly for a
        // follow-up rendition response; do not burn the full retry budget on a
        // paused player or an API payload with no usable address.
        const responseSettledWithoutMedia = noMediaSince !== null
          && status.mediaItems === 0
          && Date.now() - noMediaSince >= 1800;
        // 若点击后仍未观察到详情请求，最多等 1.2 秒；继续空等 6 秒只会
        // 拉长失败体感。后台解析会使用规范详情 URL 与本次授权 Cookie 重试。
        const detailRequestNotSeen = noRequestSince !== null
          && Date.now() - noRequestSince >= 1200;
        if (value || responseSettledWithoutMedia || detailRequestNotSeen || --remaining <= 0) {
          clearInterval(timer);
          resolve(value);
        }
      }, gapMs);
    });
  }

  function sendRuntimeMessage(message, callback) {
    try {
      chrome.runtime.sendMessage(message, callback);
    } catch {
      callback(null);
    }
  }

  function captureDiagnostic(outcome, reason, elapsedMs, target, urlSource = "") {
    const status = mainWorldStatus();
    const video = target.root?.querySelector("video") || visibleVideo();
    return {
      outcome,
      reason,
      elapsed_ms: Math.max(0, Math.min(30000, Math.round(elapsedMs))),
      main_world_present: status.present,
      main_world_stale: status.stale,
      main_world_version: status.version,
      detail_requests: status.apiRequests,
      detail_responses: status.apiResponses,
      detail_json_responses: status.jsonResponses,
      detail_json_failures: status.jsonFailures,
      media_items: status.mediaItems,
      response_aweme_nodes: status.awemeNodes,
      response_video_nodes: status.videoNodes,
      response_play_addr_nodes: status.playAddrNodes,
      response_url_candidates: status.urlCandidates,
      response_https_candidates: status.httpsCandidates,
      response_allowed_urls: status.allowedUrls,
      // 封面取证（2026-11-01）：cover_nodes>0 而 cover_accepted=0 即白名单
      // 漂移；两者>0 但 target_cover_held=false 即提交时封面已过期/未命中。
      response_cover_nodes: status.coverNodes,
      response_cover_accepted: status.coverAccepted,
      captured_targets: capturedByTarget.size,
      target_captured: Boolean(target.videoId && capturedByTarget.has(String(target.videoId))),
      target_cover_held: Boolean(target.videoId && coverUrlFor(String(target.videoId))),
      unbound_capture: Boolean(latestUnboundCapture.url),
      player_present: Boolean(video),
      player_ready_state: Number.isFinite(Number(video?.readyState))
        ? Math.max(0, Math.min(4, Number(video.readyState)))
        : 0,
      player_paused: video ? Boolean(video.paused) : null,
      url_source: urlSource,
    };
  }

  function reportCaptureDiagnostic(event) {
    // Record only capture stages and player state. Never send the page URL,
    // title, video ID, cookies or signed media URL to the local diagnostics log.
    sendRuntimeMessage({ type: "shengjian-capture-diagnostic", diagnostic: event }, () => {});
  }

  // —— 点击反馈信息窗（先生裁决：点击后弹窗告知，而非仅按钮文字变化）——
  // 固定右下角浮层 + Shadow DOM 隔离站点样式；同一窗口就地更新状态
  // （提交中 → 已加入 → 下载百分比 → 完成），完成后自动淡出；错误红点
  // 常驻更久并给可执行指引；点击 × 或卡片任意处可立即关闭。按钮短标签
  // 保留为辅位提示。受限页面环境（无 body / 无 Shadow DOM）静默退回
  // 纯按钮文字，不影响提交链路。
  let feedbackHideTimer = null;
  function ensureFeedbackWindow() {
    const mount = document.body || document.documentElement;
    if (!mount || typeof mount.appendChild !== 'function') return null;
    let host = document.querySelector('.shengjian-feedback-host');
    if (host) return host.shadowRoot || null;
    host = document.createElement('div');
    host.className = 'shengjian-feedback-host';
    // A concise status card uses measured empty space outside the picture.
    // The popup remains available when a full-screen player leaves no safe gap.
    host.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483646;pointer-events:none;';
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        .card{font:14px/1.55 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
          width:max-content;min-width:200px;max-width:340px;box-sizing:border-box;
          background:rgba(20,23,20,.82);backdrop-filter:blur(14px) saturate(1.2);
          color:#eef3ed;border:1px solid rgba(154,205,174,.28);border-radius:12px;padding:11px 14px 12px;
          box-shadow:0 10px 34px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.06);
          opacity:0;transform:translateY(10px);transition:opacity .2s ease,transform .26s cubic-bezier(.34,1.56,.64,1);
          cursor:pointer;pointer-events:auto}
        .card.show{opacity:1;transform:none}
        .main{display:flex;align-items:flex-start;gap:9px;font-weight:600;color:#eef3ed}
        .dot{width:9px;height:9px;border-radius:50%;background:#9acdae;flex:none;margin-top:6px;
          box-shadow:0 0 6px rgba(154,205,174,.5)}
        .text{flex:1;word-break:break-word}
        .x{cursor:pointer;border:0;background:transparent;color:#94a096;font-size:15px;line-height:1;padding:2px 2px}
        .card.error .dot{background:#f0909a;box-shadow:0 0 6px rgba(240,144,154,.5)}
        .card.error{border-color:rgba(240,144,154,.4)}
        .hint{margin:5px 0 0 18px;color:#94a096;font-size:12px;line-height:1.6;word-break:break-word}
        .hint.actionable{color:#eef3ed;font-size:13px;font-weight:600}
        .actions{display:none;gap:8px;margin:10px 0 0 18px}
        .actions button{font:inherit;font-size:12.5px;border-radius:8px;padding:5px 12px;cursor:pointer;border:1px solid transparent}
        .actions .ok{background:#9acdae;color:#102016;border-color:#9acdae;font-weight:600}
        .actions .no{background:transparent;color:#c5d0c7;border-color:rgba(154,205,174,.35)}
      </style>
      <div class="card" role="status" aria-live="polite">
        <div class="main"><span class="dot"></span><span class="text"></span>
          <button class="x" aria-label="关闭">×</button></div>
        <div class="hint" hidden></div>
        <div class="actions"><button class="no">取消</button><button class="ok">确认</button></div>
      </div>`;
    const card = root.querySelector('.card');
    const close = () => { card.classList.remove('show'); if (feedbackHideTimer) clearTimeout(feedbackHideTimer); };
    root.querySelector('.x').onclick = (e) => { e.stopPropagation(); close(); };
    card.onclick = close;
    mount.appendChild(host);
    return root;
  }

  function showFeedbackWindow(message, error, holdMs) {
    try {
      const root = ensureFeedbackWindow();
      if (!root) return;
      const card = root.querySelector('.card');
      card.classList.toggle('error', Boolean(error));
      // 括号提示自动拆为弱化次行，主句保持一行（先生裁决的排版口径）。
      const match = String(message).match(/^(.+?)（(.+?)）$/);
      const mainText = root.querySelector('.text');
      const hint = root.querySelector('.hint');
      if (match && match[1] && match[2]) {
        mainText.textContent = match[1].trim();
        hint.textContent = match[2].trim();
        hint.hidden = false;
        hint.classList.toggle("actionable", match[2].includes("F5 刷新"));
      } else {
        mainText.textContent = String(message);
        hint.hidden = true;
        hint.classList.remove("actionable");
      }
      positionFeedbackCard(root);
      requestAnimationFrame(() => card.classList.add('show'));
      clearTimeout(feedbackHideTimer);
      feedbackHideTimer = setTimeout(() => card.classList.remove('show'), holdMs);
    } catch { /* 受限环境退回按钮文字 */ }
  }

  function showToast(message, error = false) {
    // Feedback uses measured empty space outside the video.
    // 页面有「下载/转写」两个按钮，旧实现用 querySelector 永远改第一个
    // 的文字——点下载会把红色报错落到「转写」按钮上，语义错位。按钮
    // 标签现在保持不变（交互禁用态 is-busy 除外），状态全部进窗口。
    document.querySelectorAll('.shengjian-player-button').forEach((button) => { button.title = String(message); });
    showFeedbackWindow(message, error, holdForMessage(message, error));
  }

  // 进度条上方居中定位（先生 2026-10-08 裁决）：showFeedbackWindow 与
  // showConfirmWindow 共用；无可见播放器时退回右上角。
  function positionFeedbackCard(root) {
    const videoRect = visibleVideo()?.getBoundingClientRect();
    const host = root.host;
    if (!host) return;
    if (videoRect) {
      const cardW = root.querySelector('.card').getBoundingClientRect().width || 280;
      const centered = Math.round(videoRect.left + (videoRect.width - cardW) / 2);
      const left = Math.min(Math.max(8, centered), Math.max(8, innerWidth - cardW - 8));
      const hasActionableHint = root.querySelector('.hint')?.classList.contains('actionable');
      const bottomOffset = hasActionableHint ? 205 : 88;
      const top = Math.max(8, Math.round(videoRect.bottom - bottomOffset));
      host.style.cssText = `position:fixed;left:${left}px;top:${top}px;z-index:2147483646;pointer-events:none;`;
    } else {
      host.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483646;pointer-events:none;';
    }
  }

  // 确认卡（先生 2026-10-08：同一视频重复下载须先确认）：主句+弱化次行+
  // 两个动作按钮；确认回调由调用方给出，取消/15 秒超时不提交。
  let pendingConfirmResolver = null;
  function showConfirmWindow(mainText, hintText, confirmLabel, onConfirm) {
    // 待决动作先登记（测试/诊断钩子可见），渲染失败时它就是「未决＝不提交」。
    let confirmTimer = null;
    const done = (confirmed) => {
      if (pendingConfirmResolver && pendingConfirmResolver.done === done) pendingConfirmResolver = null;
      clearTimeout(confirmTimer);
      if (confirmed && typeof onConfirm === 'function') onConfirm();
    };
    pendingConfirmResolver = { confirm: () => done(true), cancel: () => done(false), done };
    try {
      const root = ensureFeedbackWindow();
      // 宿主不可用（受限环境）时默认取消：宁可少一次下载，不可静默重下。
      if (!root) return;
      const card = root.querySelector('.card');
      const actions = root.querySelector('.actions');
      const ok = actions.querySelector('.ok');
      const no = actions.querySelector('.no');
      clearTimeout(feedbackHideTimer);
      card.classList.remove('error');
      root.querySelector('.text').textContent = String(mainText);
      const hint = root.querySelector('.hint');
      hint.textContent = String(hintText || '');
      hint.hidden = !hintText;
      ok.textContent = String(confirmLabel || '确认');
      ok.onclick = () => { actions.style.display = 'none'; ok.onclick = null; no.onclick = null; card.classList.remove('show'); done(true); };
      no.onclick = () => { actions.style.display = 'none'; ok.onclick = null; no.onclick = null; card.classList.remove('show'); done(false); };
      actions.style.display = 'flex';
      positionFeedbackCard(root);
      requestAnimationFrame(() => card.classList.add('show'));
      confirmTimer = setTimeout(() => { actions.style.display = 'none'; ok.onclick = null; no.onclick = null; card.classList.remove('show'); done(false); }, 15000);
    } catch { /* 受限环境默认取消，不提交 */ }
  }

  // 测试/诊断钩子：当前确认卡的确认与取消动作（无确认卡时为 null）。
  function pendingConfirm() { return pendingConfirmResolver; }

  // 驻留按内容长度伸缩（中文 ≈220ms/字 + 基线驻留；错误需读完指引，
  // 上限更高）。短确认 ~2.2s 即走，长错误最长 8s——替代一刀切时长。
  function holdForMessage(message, error) {
    const len = Math.max(1, String(message).replace(/（[^）]*）/g, "").length);
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    if (String(message).includes(CAPTURE_RETRY_GUIDE)) return 9000;
    if (error) return clamp(2600 + 280 * len, 4000, 8000);
    if (/完成/.test(message)) return 4500;
    if (/\d+%/.test(message)) return 4000;
    return clamp(1200 + 220 * len, 2000, 4000);
  }

  // 页面长停留提醒（先生 2026-10-14：两小时采集窗口的边界要在明显位置
  // 主动告知，不能只靠失败提示兜底）。页面年龄超 100 分钟时，在用户的
  // 下一次点击前提示一次：早前视频的下载地址可能已过期，可刷新后重试。
  // 每页只提示一次；点击时检查（而非定时器）以避开标签冻结的时钟漂移。
  let pageStaleHintShown = false;
  function maybeShowPageStaleHint() {
    if (pageStaleHintShown) return;
    try {
      const pageAgeMs = Date.now() - performance.timeOrigin;
      if (pageAgeMs < 100 * 60 * 1000) return;
      pageStaleHintShown = true;
      showToast("本页已停留较久：早前视频的下载地址可能已过期；按 F5 刷新，等待视频画面加载后重试。仍失败时请在 FoilPage 任务页查看原因。", false);
    } catch { /* performance 不可用时静默跳过 */ }
  }

  function submit(button, action) {
    maybeShowPageStaleHint();
    const video = activeVideo();
    if (!video.videoId) {
      reportCaptureDiagnostic(captureDiagnostic("url_missing", "video_id_unrecognized", 0, video));
      showToast("未识别当前视频，请停留在视频画面后重试", true);
      return;
    }
    const initialStatus = mainWorldStatus();
    const restore = () => { button.disabled = false; button.classList.remove("is-busy"); };
    const busy = () => { button.disabled = true; button.classList.add("is-busy"); };
    const detailUrl = `https://www.douyin.com/video/${video.videoId}`;
    // 预检已知「进行中未带转写」时，提交会被服务端就地并入既有任务——
    // 该任务的首次回执早已展示过，showTaskFeedbackOnce 会静默；此时改用
    // 直达提示告知合并结果（先生 2026-10-18：重复点击必须有反馈）。
    let mergedSubmitNotice = false;
    // 媒体读取与提交主体：确认（如需）通过后执行。
    const beginSubmission = () => {
      busy();
      const readStartedAt = Date.now();
      const immediateMedia = resolveMediaUrl(video);
      const initialCandidates = mediaCandidatesFor(video.videoId, activeMediaUrl(video), immediateMedia);
      const initialPlayUrl = playEndpointFor(video.videoId, activeMediaUrl(video), immediateMedia);
      const canWaitForPlayer = Boolean(initialStatus.present && !initialStatus.stale && !immediateMedia);
      // Collect candidates bound to this video for the browser-first handoff.
      const mediaPromise = immediateMedia
        ? Promise.resolve(immediateMedia)
        : canWaitForPlayer ? waitForMediaUrl(video) : Promise.resolve("");
      mediaPromise.then((mediaUrl) => {
      const target = video;
      const stillActive = activeVideo().videoId === video.videoId;
      const activeUrl = stillActive ? activeMediaUrl(target) : "";
      const mediaCandidates = [...new Set([...initialCandidates, ...mediaCandidatesFor(target.videoId, activeUrl, mediaUrl)])].slice(0,8);
      const playUrl = initialPlayUrl || playEndpointFor(target.videoId, activeUrl, mediaUrl);
      // TASK-8.18：图集整组提交（无媒体地址/无播放端点/无 Cookie——桌面
      // 按图片签名直链逐张下载，取证④）。图集优先于视频回退 resolver。
      const gallery = (!immediateMedia && !mediaUrl && !playUrl)
        ? galleryFor(target.videoId) : null;
      if (gallery) {
        reportCaptureDiagnostic(captureDiagnostic("url_captured", "", Date.now() - readStartedAt, target, "gallery"));
        sendRuntimeMessage({
          type: "shengjian-page-submit",
          kind: "gallery",
          url: `https://www.douyin.com/note/${target.videoId}`,
          title: target.title,
          videoId: target.videoId,
          galleryImages: gallery.map((image) => image.urls.slice(0, 8)),
          coverUrl: coverUrlFor(target.videoId) || (gallery[0] && gallery[0].urls[0]) || "",
          transcribe: false,
        }, (response) => {
          restore();
          if (activeVideo().videoId !== target.videoId) return;
          if (chrome.runtime.lastError) {
            showToast(decorateCaptureRetryNotice("扩展已更新，请刷新页面后重试", target.videoId, "failure"), true);
            return;
          }
          if (!response?.ok) {
            if (response?.feedback_sent) return;
            showToast(decorateCaptureRetryNotice(response?.message || "图集任务未能创建：请确认 FoilPage 桌面端正在运行", target.videoId, "failure"), true);
            return;
          }
          const acceptedJob = response.job;
          if (acceptedJob?.queued) { showOfflineLaunchCard(target.videoId); return; }
          if (acceptedJob?.id) showTaskFeedbackOnce(acceptedJob.id, "submitted", "已提交图集下载", false, target.videoId);
        });
        return;
      }
      if (!mediaUrl && !playUrl) {
        // 记录页面采集阶段的缺失，但仍交给后台 resolver：后端已具备本次
        // 点击的一次性 Cookie 读取与规范详情页解析，页面守门不能截断任务。
        const status = mainWorldStatus();
        const targetCaptured = Boolean(target.videoId && capturedByTarget.has(String(target.videoId)));
        const reasonCode = gateReason(status, { targetCaptured });
        const reason = gateMessage(status, { targetCaptured });
        rememberCaptureRetryGuide(target.videoId);
        reportCaptureDiagnostic(captureDiagnostic("url_missing", reasonCode, Date.now() - readStartedAt, target));
        try {
          console.warn(`[FoilPage] 抖音未捕获播放地址，转交后台解析：${JSON.stringify({
            reason: reasonCode,
            message: reason,
            main: {
              present: status.present,
              stale: status.stale,
              apiRequests: status.apiRequests,
              apiResponses: status.apiResponses,
              jsonResponses: status.jsonResponses,
              mediaItems: status.mediaItems,
              urlCandidates: status.urlCandidates,
              allowedUrls: status.allowedUrls,
            },
            targetCaptured,
            unboundCaptured: Boolean(latestUnboundCapture.url),
          })}`);
        } catch { /* console may be restricted */ }
      }
      if (mediaUrl || playUrl) {
        const urlSource = activeMediaUrl(target) ? "player" : mediaUrl ? "captured_media" : "play_endpoint";
        reportCaptureDiagnostic(captureDiagnostic("url_captured", "", Date.now() - readStartedAt, target, urlSource));
      }
        sendRuntimeMessage({
          type: "shengjian-page-submit",
          url: `https://www.douyin.com/video/${target.videoId}`,
          title: target.title,
          mediaUrl: mediaCandidates[0] || playUrl || mediaUrl,
          mediaCandidates,
          playUrl,
          coverUrl: coverUrlFor(target.videoId),
          transcribe: action === "transcribe",
        }, (response) => {
          restore();
          if (activeVideo().videoId !== target.videoId) return;
          if (chrome.runtime.lastError) {
            showToast(decorateCaptureRetryNotice("扩展已更新，请刷新页面后重试", target.videoId, "failure"), true);
            return;
          }
          if (!response?.ok) {
            if (response?.feedback_sent) return;
            showToast(decorateCaptureRetryNotice(response?.message || "下载任务未能创建：请确认 FoilPage 桌面端正在运行；留在本页让视频播放 2 秒后重试", target.videoId, "failure"), true);
            return;
          }
          const acceptedJob = response.job;
          // The background broadcast is best-effort (the tab may be navigating
          // or its listener may not yet be ready). The initiating page has an
          // authoritative acceptance response, so use it as a one-time
          // fallback when no receipt arrived. A merely queued request is not
          // reported as accepted——但必须告知去向（先生 2026-10-19：桌面端
          // 未连接时点击不能无反馈）：已暂存＋一键拉起 FoilPage。
          if (!acceptedJob?.id) return;
          if (acceptedJob.queued) {
            showOfflineLaunchCard(target.videoId);
            return;
          }
          if (mergedSubmitNotice) {
            mergedSubmitNotice = false;
            showToast("已并入进行中的任务：下载完成后自动转写（进度见 FoilPage 任务页）");
            return;
          }
          const reusedForTranscribe = Boolean(acceptedJob.download_complete)
            && Boolean(acceptedJob.options?.transcribe_after_download);
          if (acceptedJob.download_complete && !reusedForTranscribe) {
            showTaskFeedbackOnce(acceptedJob.id, "download", "下载成功", false, target.videoId);
          } else {
            showTaskFeedbackOnce(acceptedJob.id, "submitted",
              reusedForTranscribe ? "已复用已下载的视频，直接开始转写" : "已提交下载",
              false, target.videoId);
          }
        });
      });
    };

    // 先生 2026-10-08：同一视频重复提交先确认——「下载」命中已有任务时弹
    // 确认卡；「转写」不加确认（服务端复用已下载媒体/合并进行中任务）。
    // 先生 2026-10-18：「转写」重复点击虽不确认、但必须即时反馈——
    // 进行中且已带转写 → 提示后不再提交；进行中未带转写 → 照常提交
    // （服务端补置转写旗标），回执走合并直达提示。
    busy();
    if (action === "download") {
      sendRuntimeMessage({ type: "shengjian-check-existing", url: detailUrl }, (check) => {
        if (chrome.runtime.lastError || !check?.ok) { beginSubmission(); return; }
        if (activeVideo().videoId !== video.videoId) { restore(); return; }
        if (check.active) {
          restore();
          showToast("该视频正在处理中，无需重复提交（进度见 FoilPage 任务页）");
          return;
        }
        if (check.done) {
          restore();
          showConfirmWindow(
            "该视频已下载完成",
            "重新下载将新增一份文件；需要文稿请点「转写」",
            "重新下载",
            beginSubmission,
          );
          return;
        }
        beginSubmission();
      });
      return;
    }
    sendRuntimeMessage({ type: "shengjian-check-existing", url: detailUrl }, (check) => {
      if (chrome.runtime.lastError || !check?.ok || activeVideo().videoId !== video.videoId) { beginSubmission(); return; }
      if (check.active && check.activeTranscribing) {
        restore();
        showToast("该视频已在处理中，转写会自动接续（进度见 FoilPage 任务页）");
        return;
      }
      if (check.active) mergedSubmitNotice = true;
      beginSubmission();
    });
  }

  function createControl(action, label, glyph) {
    const control = document.createElement("xg-icon");
    control.className = `${CONTROL_CLASS} shengjian-${action}`;
    control.dataset.shengjianAction = action;
    control.dataset.shengjianVersion = EXTENSION_VERSION;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "shengjian-player-button";
    button.title = action === "download" ? "下载当前视频" : "转写当前视频";
    button.setAttribute("aria-label", button.title);
    const icon = document.createElement("span");
    icon.className = "shengjian-player-glyph";
    icon.textContent = glyph;
    const text = document.createElement("span");
    text.textContent = label;
    button.append(icon, text);
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      submit(button, action);
    });
    control.append(button);
    return control;
  }

  function playerControlTarget() {
    const active = activeVideo();
    const root = active.root;
    const video = root?.querySelector("video") || visibleVideo();
    const player = video?.closest(".xgplayer")
      || video?.closest("[class*='xgplayer']") || root;
    const grids = Array.from(player?.querySelectorAll?.(".xg-right-grid") || document.querySelectorAll(".xg-right-grid"));
    const grid = grids.find(isVisible);
    if (grid) return { container: grid, anchor: grid.querySelector("xg-icon.xgplayer-autoplay-setting"), floating: false };
    const host = player || video?.parentElement || root;
    if (host) return { container: host, anchor: null, floating: true };
    // 先生 2026-10-19：视频页绝不能没有入口——结构识别失败（如长视频页
    // 无 feed-active-video 容器）时退到页面级浮动按钮，而不是把按钮移除。
    if (location.pathname.match(/\/(?:video|share\/video)\/\d{10,24}/) || (location.search || "").includes("modal_id=")) {
      return { container: document.body, anchor: null, floating: true };
    }
    return null;
  }

  /* 桌面端未连接时的暂存告知＋一键拉起（先生 2026-10-19）。点击后任务已进
   * 扩展离线队列：拉起 FoilPage→本地服务上线→队列自动提交→回执照常送达。 */
  function showOfflineLaunchCard(videoId) {
    showConfirmWindow(
      "FoilPage 桌面端未连接",
      "任务已暂存到扩展。点「启动 FoilPage」后，恢复连接时会尝试自动提交；应用将以后台方式启动。若仍未开始，请在扩展弹窗查看任务状态。",
      "启动 FoilPage",
      () => {
        try {
          const launcher = document.createElement("a");
          launcher.href = "shengjian://launch";
          launcher.style.display = "none";
          document.body.appendChild(launcher);
          launcher.click();
          setTimeout(() => launcher.remove(), 1000);
          showToast("正在启动 FoilPage…启动后任务会自动提交；若浏览器询问是否打开 FoilPage，请选择「打开」");
        } catch {
          showToast("无法自动启动：请手动打开 FoilPage 桌面端，任务会自动提交", true);
        }
      },
    );
    reportCaptureDiagnostic(captureDiagnostic("desktop_offline_queued", "", 0, activeVideo(), "offline_queue"));
  }

  /* 按钮被隐藏/移除时留痕（先生 2026-10-19 实例：个别视频页无按钮）——
   * 每页每原因只报一次，进 desktop.log 的诊断金矿，下次报告可直接定位。 */
  const controlsHiddenReported = new Map();
  function reportControlsHiddenOnce(reason) {
    const key = `${location.pathname}:${location.search}:${reason}`;
    if (controlsHiddenReported.has(key)) return;
    controlsHiddenReported.set(key, 1);
    reportCaptureDiagnostic(captureDiagnostic("controls_hidden", reason, 0, activeVideo(), "controls_sync"));
  }

  // 直播与广告形态没有可下载的常规视频，注入的按钮还会顶开直播间的
  // 原生功能位（点赞/礼物/连麦）。检测到即整组隐藏；轮询同步会在回到
  // 普通视频时自动恢复按钮。
  const BLOCK_BADGE_TEXTS = new Set(["广告", "直播中", "进入直播间", "点击进入直播间", "直播回放"]);
  const BLOCK_LINK_SELECTORS = [
    'a[href*="live.douyin.com"]',
    'a[href^="/live"]',
    'a[href*="ec.douyin.com"]',
    'a[href*="ecom.douyin.com"]',
  ];

  function isLiveRoomLocation() {
    return location.hostname === "live.douyin.com" || /^\/live(?:\/|$)/.test(location.pathname);
  }

  function rootSignalsLiveOrAd(root) {
    if (!root?.querySelector) return false;
    for (const selector of BLOCK_LINK_SELECTORS) {
      if (root.querySelector(selector)) return true;
    }
    for (const element of root.querySelectorAll("span, div, i, p")) {
      if (!element.childElementCount && BLOCK_BADGE_TEXTS.has((element.textContent || "").trim())) return true;
    }
    return false;
  }

  function shouldHideControls(active = activeVideo()) {
    if (isLiveRoomLocation()) return true;
    if (rootSignalsLiveOrAd(active.root)) return true;
    // 只在明确的直播/广告信号上隐藏。曾试过「识别不出视频 id 也隐藏」，
    // 实机上抖音改版会让 feed 容器检测短暂失效，把普通视频的按钮一起
    // 藏掉——点击时的「未识别当前视频」引导比整组消失安全得多。
    return false;
  }

  let feedbackVideoId = "";
  let feedbackRevision = 0;
  const taskFeedbackStages = new Map();
  function showTaskFeedbackOnce(taskId, stage, message, error = false, videoId = activeVideo().videoId) {
    const key = String(taskId || "");
    // 终态失败与下载成功同级：未见过成功的任务显示失败提示；已显示
    // 「下载成功」的任务不被迟到的失败覆盖（先生 2026-10-08 裁决，
    // extension_visual_smoke 守护）。
    const rank = stage === "download" || stage === "failure" ? 2 : 1;
    if (key && (taskFeedbackStages.get(key) || 0) >= rank) return false;
    if (key) {
      taskFeedbackStages.delete(key);
      taskFeedbackStages.set(key, rank);
      while (taskFeedbackStages.size > 64) taskFeedbackStages.delete(taskFeedbackStages.keys().next().value);
    }
    feedbackVideoId = videoId;
    feedbackRevision++;
    showToast(decorateCaptureRetryNotice(message, videoId, stage), error);
    return true;
  }
  function restoreTaskFeedback(videoId) {
    if (!videoId || videoId === feedbackVideoId) return;
    feedbackVideoId = videoId;
    const revision = ++feedbackRevision;
    document.querySelector('.shengjian-feedback-host')?.remove();
    sendRuntimeMessage({type:"shengjian-task-state",url:`https://www.douyin.com/video/${videoId}`}, response => {
      if (activeVideo().videoId !== videoId || revision !== feedbackRevision || !response?.job) return;
      const job=response.job;
      if (job.download_complete) showTaskFeedbackOnce(job.id, "download", "下载成功", false, videoId);
      else if (!["done", "failed", "cancelled"].includes(job.status) && !job.queued) showTaskFeedbackOnce(job.id, "submitted", "已提交下载", false, videoId);
    });
  }
  function syncControls() {
    syncQueued = false;
    // Release previous fallback positioning before selecting the current player.
    // A native absolute video container can collapse when forced to relative.
    document.querySelectorAll(".shengjian-player-host").forEach((node) => {
      node.classList.remove("shengjian-player-host");
    });
    restoreTaskFeedback(activeVideo().videoId);
    scanCapturedSources();
    if (shouldHideControls()) {
      document.querySelectorAll(`.${CONTROL_CLASS}`).forEach((node) => node.remove());
      reportControlsHiddenOnce("live_or_ad");
      return;
    }
    const target = playerControlTarget();
    if (!target) {
      document.querySelectorAll(`.${CONTROL_CLASS}`).forEach((node) => node.remove());
      reportControlsHiddenOnce("no_player_target");
      return;
    }
    const { container, anchor, floating } = target;
    document.querySelectorAll(`.${CONTROL_CLASS}`).forEach((node) => {
      if (node.parentElement !== container) node.remove();
    });
    let download = container.querySelector(`.${CONTROL_CLASS}.shengjian-download`);
    let transcribe = container.querySelector(`.${CONTROL_CLASS}.shengjian-transcribe`);
    if (!download) download = createControl("download", "下载", "↓");
    if (!transcribe) transcribe = createControl("transcribe", "转写", "文");
    if (floating) {
      if (getComputedStyle(container).position === "static") {
        container.classList.add("shengjian-player-host");
      }
      download.classList.add("shengjian-floating-control");
      transcribe.classList.add("shengjian-floating-control");
      if (!download.parentElement) container.append(download);
      if (!transcribe.parentElement) container.append(transcribe);
      return;
    }
    container.dataset.shengjianGrid = "true";
    if (anchor) {
      if (transcribe.previousElementSibling !== anchor) anchor.insertAdjacentElement("afterend", transcribe);
      if (download.previousElementSibling !== transcribe) transcribe.insertAdjacentElement("afterend", download);
    } else {
      if (!container.contains(download)) container.prepend(download);
      if (!container.contains(transcribe)) container.insertBefore(transcribe, download.nextSibling);
    }
  }

  function queueSync() {
    if (syncQueued) return;
    syncQueued = true;
    requestAnimationFrame(syncControls);
  }

  queueSync();
  new MutationObserver(queueSync).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  window.addEventListener("resize", queueSync, { passive: true });
  window.addEventListener("message", acceptMainWorldCapture);
  setInterval(queueSync, 1200);
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "shengjian-toast") {
      if (message.taskUrl && message.taskUrl !== `https://www.douyin.com/video/${activeVideo().videoId}`) return undefined;
      const submissionRejected = message.stage === "failure" && !/^[a-f0-9]{12}$/.test(String(message.taskId || ""));
      const terminalTaskFailure = message.stage === "failure" && /^[a-f0-9]{12}$/.test(String(message.taskId || ""));
      const legacyReceipt = !message.stage && ["已提交下载", "下载成功"].includes(String(message.message || ""));
      if (!["submitted", "download"].includes(message.stage) && !submissionRejected && !legacyReceipt && !terminalTaskFailure) return undefined;
      const stage = message.stage || (String(message.message || "") === "已提交下载" ? "submitted" : "download");
      const videoId = activeVideo().videoId;
      if (stage === "submitted" || stage === "download") {
        // Direct response and background broadcast can race. The task-stage
        // guard keeps one receipt while allowing download success to replace
        // an earlier submission notice.
        showTaskFeedbackOnce(message.taskId, stage, message?.message || "", Boolean(message?.error), videoId);
      } else if (terminalTaskFailure) {
        // 终态失败按同级提示：带 F5 重试指引，且不覆盖已达成的下载成功。
        showTaskFeedbackOnce(message.taskId, "failure", message?.message || "", Boolean(message?.error), videoId);
      } else {
        feedbackVideoId = videoId;
        feedbackRevision++;
        showToast(decorateCaptureRetryNotice(message?.message || "", videoId, stage), Boolean(message?.error));
      }
      sendResponse?.({ ok: true });
      return undefined;
    }
    if (message?.type === "shengjian-fresh-media") {
      // Requests for an original video may use only its captured cache. A
      // different current video must never lend it the current player URL.
      scanCapturedSources();
      const current = activeVideo();
      const expected = String(message.videoId || "");
      const active = expected && expected !== current.videoId
        ? {videoId:expected, title:"", root:null, playerMatchesTarget:false} : current;
      sendResponse?.({
        ok: true,
        videoId: active.videoId,
        title: active.title,
        mediaUrl: resolveMediaUrl(active),
        mediaCandidates: mediaCandidatesFor(active.videoId, activeMediaUrl(active)),
        playUrl: playEndpointFor(active.videoId, activeMediaUrl(active), resolveMediaUrl(active)),
        // TASK-8.17：离线重放/服务唤醒后按 aweme_id 重建提示时封面也要跟上。
        coverUrl: coverUrlFor(active.videoId),
        // TASK-8.18：图集重建同样带上（service worker 重启后队列提示丢失时）。
        gallery: (galleryFor(active.videoId) || []).map((image) => image.urls.slice(0, 8)),
      });
      return undefined;
    }
    return undefined;
  });

  // Test/diagnostics hook. MV3 content scripts run in an isolated world, so page
  // scripts cannot reach these internals; only the extension and Node harness can.
  globalThis.__shengjianContentInternals = {
    isDouyinMediaHint,
    rankMediaHint,
    activeVideo,
    activeMediaUrl,
    waitForMediaUrl,
    rememberCapture,
    scanCapturedSources,
    scanPerformanceSources,
    scanEmbeddedStates,
    collectPlayAddrs,
    capturedMediaUrl,
    capturedMediaCandidates,
    mediaCandidatesFor,
    playEndpointFor,
    resolveMediaUrl,
    capturedByTarget,
    acceptMainWorldCapture,
    requestCaptureReplay,
    maybeShowPageStaleHint,
    mainWorldStatus,
    gateMessage,
    rememberCaptureRetryGuide,
    decorateCaptureRetryNotice,
    captureRetryGuides,
    submit,
    showConfirmWindow,
    pendingConfirm,
    positionFeedbackCard,
    isLiveRoomLocation,
    rootSignalsLiveOrAd,
    shouldHideControls,
  };
})();
