// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { Console } from "node:console";
import { Writable } from "node:stream";

// Keep VM fixture logs off the test runner's serialized stdout/stderr channel.
// A real Console preserves the full API; only its writable destinations are local.
function createFixtureConsole() {
  const sink = new Writable({
    write(_chunk, _encoding, callback) { callback(); },
  });
  return new Console({ stdout: sink, stderr: sink });
}

// Single source of truth for the release version. The manifest, content script and
// backend APP_VERSION must all agree; deriving the expectation here means a version
// bump only touches app/bridge.py and the markers release.ps1 already enforces.
const APP_VERSION = (
  await readFile(new URL("../app/bridge.py", import.meta.url), "utf8")
).match(/APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"/)[1];

const storage = {};
globalThis.chrome = {
  storage: {
    local: {
      async get(key) { return { [key]: storage[key] ?? "" }; },
      async set(values) { Object.assign(storage, values); },
    },
  },
};

const api = await import("../browser-extension/api.js");
const model = await import("../browser-extension/popup-model.js");

test("token storage trims and persists", async () => {
  await api.setToken("  secret-token  ");
  assert.equal(await api.getToken(), "secret-token");
});

test("health accepts only the ShengJian local service", async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    return { ok: true, async json() { return { ok: true, service: "VideoToArticle", api_version: "1" }; } };
  };
  assert.equal((await api.health()).service, "VideoToArticle");
  assert.match(calls[0], /^http:\/\/127\.0\.0\.1:18765\/health$/);

  globalThis.fetch = async () => ({ ok: true, async json() { return { ok: true, service: "OtherLocalService" }; } });
  await assert.rejects(api.health());
});

test("authorized request sends the pairing token to the loopback service", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, async json() { return { job: { id: "job-1" } }; } };
  };
  assert.deepEqual(await api.request("/v1/downloads", { method: "POST", body: "{}" }), { job: { id: "job-1" } });
  assert.equal(calls[0].options.headers.Authorization, "Bearer secret-token");
  assert.equal(calls[0].options.cache, "no-store");
});

test("local API errors retain backend code and HTTP status for failure reports", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    async json() { return { error: { code: "origin_denied", message: "扩展来源未配对" } }; },
  });
  await assert.rejects(api.request("/v1/downloads"), (error) => {
    assert.equal(error.code, "origin_denied");
    assert.equal(error.status, 403);
    assert.equal(error.message, "扩展来源未配对");
    return true;
  });
});

test("one-click setup stores the token", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    async json() { return { token: "s".repeat(40), api_version: "1", setup: { background_mode: true } }; },
  });
  const result = await api.setup();
  assert.equal(result.api_version, "1");
  assert.equal(await api.getToken(), "s".repeat(40));
});

test("popup status labels cover local download and ASR states", () => {
  assert.equal(model.progressFor({ status: "downloading", progress: 0.426 }), 0.426);
  assert.equal(model.progressFor({ status: "queued", progress: 1 }), 1);
  assert.equal(model.progressLabelFor({ status: "queued", progress: 1 }), "转写排队中");
  assert.equal(model.progressLabelFor({ status: "downloading", progress: .42 }), "正在下载 42%");
  assert.equal(model.progressLabelFor({ status: "done", progress: 1 }), "下载成功");
  assert.equal(model.progressLabelFor({ status: "downloading", progress: .42, waiting_connection: true }), "正在下载 42%（连接恢复后继续）");
  assert.equal(model.progressLabelFor({ status: "transcribing", progress: .68, waiting_connection: true }), "正在转写 68%（连接恢复后继续）");
  assert.equal(model.progressLabelFor({ status: "downloading", progress: .42, cancel_requested: true, waiting_connection: true }), "等待连接后取消");
  assert.equal(model.progressLabelFor({ status: "done", job_id: "linked", transcription_label: "正文转写成功，标题处理失败", transcription_error: true, waiting_connection: true }), "正文转写成功，标题处理失败");
  assert.equal(model.progressFor({ status: "queued" }), 0);
});

test("popup offers a page-level download fallback without extra permissions", async () => {
  const popupHtml = await readFile(new URL("../browser-extension/popup.html", import.meta.url), "utf8");
  const popupJs = await readFile(new URL("../browser-extension/popup.js", import.meta.url), "utf8");
  // 弹窗兑底：当前标签页在 B 站 / 抖音 / YouTube 视频页时提供下载按钮。
  assert.match(popupHtml, /id="pageDownload"/);
  assert.match(popupHtml, /id="pageTranscribe"/);
  assert.match(popupHtml, /id="pagePanel"/);
  assert.match(popupJs, /chrome\.tabs\.query\(\{ active: true, currentWindow: true \}\)/);
  assert.match(popupJs, /bilibili/);
  assert.match(popupJs, /douyin/);
  assert.match(popupJs, /isYoutube/);
  assert.match(popupJs, /YouTube（本地下载）/);
  assert.match(popupJs, /shengjian-page-submit/);
  // 兑底不要求新增权限：弹窗读当前标签依赖 activeTab（用户点图标即授予）。
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.permissions, ["activeTab", "storage", "cookies", "downloads", "alarms"]);
  assert.equal(manifest.permissions.includes("tabs"), false);
});

test("progress toasts reach youtube tabs, not only douyin and bili", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  // notifyTabs 的 tab 查询若漏掉 YouTube 域，content-youtube.js 的 pill
  // 监听永远收不到消息——0.12.7 YouTube 支持曾漏掉这里。
  const notify = background.match(/async function notifyTabs\([\s\S]*?\n\}/);
  assert.ok(notify, "notifyTabs must exist");
  for (const host of ["douyin.com", "bilibili.com", "youtube.com", "youtu.be"]) {
    assert.match(notify[0], new RegExp(host.replace(/\./g, "\\.")), `notifyTabs must cover ${host}`);
  }
});

test("manifest requests only the scoped authorization capability", async () => {
  const manifestUrl = new URL("../browser-extension/manifest.json", import.meta.url);
  const manifest = JSON.parse(await readFile(manifestUrl, "utf8"));
  assert.equal(manifest.version, APP_VERSION);
  assert.deepEqual(manifest.permissions, ["activeTab", "storage", "cookies", "downloads", "alarms"]);
  // 0.12.7 起新增 YouTube 内容脚本（第 4 组），细节在下方专属测试断言。
  assert.equal(manifest.content_scripts.length, 4);
  assert.deepEqual(manifest.content_scripts[0], {
    matches: ["https://*.douyin.com/*", "https://douyin.com/*"],
    js: ["douyin-hosts.js", "content.js"], css: ["content.css"], run_at: "document_start",
  });
  // MAIN-world partner observes the page's own aweme detail/feed JSON so the
  // extension can still see a progressive MP4 when the logged-in player hands
  // <video> an MSE blob. It must stay douyin-only and must not widen the
  // permission surface beyond what the isolated world already has.
  assert.deepEqual(manifest.content_scripts[1], {
    matches: ["https://*.douyin.com/*", "https://douyin.com/*"],
    js: ["content-main.js"], run_at: "document_start", world: "MAIN",
  });
  // B 站内容脚本只提交页面 URL 走本地 yt-dlp 轨，不声明 cookies 用途。
  assert.deepEqual(manifest.content_scripts[2], {
    matches: ["https://*.bilibili.com/*", "https://bilibili.com/*"],
    js: ["content-bili.js"], css: ["content-bili.css"], run_at: "document_idle",
  });
  assert.doesNotMatch(JSON.stringify(manifest), /webRequest|debugger|douyinvod|bytecdn/iu);
  for (const relativePath of Object.values(manifest.icons)) {
    await access(new URL(`../browser-extension/${relativePath}`, import.meta.url));
  }
});

test("background submits Douyin pages to the local downloader and watches progress", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  assert.match(background, /chrome\.cookies\.getAll\(\{ domain: DOUYIN_DOMAIN \}\)/);
  assert.match(background, /function isDouyinPageUrl/);
  assert.match(background, /type === "shengjian-page-submit"/);
  assert.match(background, /job\?\.error\?\.diagnostic\?\.fallback_pending/);
  assert.match(background, /type === "shengjian-capture-diagnostic"/);
  assert.match(background, /\/v1\/diagnostics\/douyin-capture/);
  assert.match(background, /mode: "local"/);
  assert.match(background, /session = isDouyinPageUrl\(rawUrl\) && !mediaUrl \? await oneShotDouyinSession\(\) : null/);
  assert.match(background, /if \(session\) payload\.browser_session = session/);
  assert.match(background, /if \(mediaUrl\) payload\.media_url = mediaUrl/);
  assert.match(background, /if \(session\) session\.length = 0/);
  assert.match(background, /new WebSocket\(DESKTOP_EVENTS_URL, \[DESKTOP_EVENTS_PROTOCOL, token\]\)/);
  assert.match(background, /\/v1\/desktop-downloads\/\$\{requestId\}\/claim/);
  assert.match(background, /type === "desktop-douyin-download"/);
  assert.match(background, /watchLocalTask\(job\)/);
  assert.match(background, /localTaskProgressMessage/);
  assert.match(background, /\/v1\/downloads/);
  assert.doesNotMatch(background, /cookies-from-browser|chrome\.debugger|webRequest|browser_session.*storage/iu);
});

test("content submits the current Douyin page to the local downloader", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  assert.match(content, /data-e2e-vid/);
  assert.match(content, /type: "shengjian-page-submit"/);
  assert.match(content, /https:\/\/www\.douyin\.com\/video/);
  assert.match(content, /waitForMediaUrl\(video\)/);
  assert.match(content, /mediaUrl:\s*mediaCandidates\[0\]\s*\|\|\s*playUrl\s*\|\|\s*mediaUrl/);
  assert.match(content, /mediaCandidates,/);
  // 反馈全部走右下角信息窗：窗口卡片承担 role=status，按钮不再变字
  assert.match(content, /role=\"status\" aria-live=\"polite\"/);
  assert.doesNotMatch(content, /MediaSource|SourceBuffer|page-media-detector|chrome\.downloads/iu);
});

test("the removed media-address detectors are not part of the extension package", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));
  assert.doesNotMatch(JSON.stringify(manifest), /media-detector|native-download/iu);
});

// ---------------------------------------------------------------------------
// Behavioural tests. The cases above only match source text; these actually
// execute the extension logic so a broken predicate or a credential regression
// fails instead of passing on a string that merely exists.
// ---------------------------------------------------------------------------

test("content media-hint predicate accepts and rejects the same URLs as the backend", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");

  // content.js is an IIFE that touches window/document on load. Run it in a sandbox
  // with a minimal DOM stub and read back the internals hook it exposes. MV3 content
  // scripts live in an isolated world, so page scripts cannot reach these internals.
  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    ...props,
  });
  const sandbox = {
    URL,
    console: createFixtureConsole(),
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    requestAnimationFrame: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/1234567890" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "FoilPage",
      documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
      body: { append() {} },
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => fakeElement({ append() {}, addEventListener() {}, classList: { add() {} }, dataset: {}, style: {} }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.innerHeight = 800;
  sandbox.window.innerWidth = 1280;
  sandbox.window.addEventListener = () => {};

  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });

  const internals = sandbox.__shengjianContentInternals;
  assert.ok(internals?.isDouyinMediaHint, "content.js must expose isDouyinMediaHint for testing");

  // Same vectors as tests/test_download_pipeline.py::test_browser_download_url_allowlist.
  assert.equal(internals.isDouyinMediaHint("https://v3-web.douyinvod.com/video.mp4?x=1"), true);
  assert.equal(internals.isDouyinMediaHint("https://www.douyin.com/aweme/v1/play/?video_id=1"), true);
  assert.equal(internals.isDouyinMediaHint("https://www.douyin.com/video/play/?video_id=1"), true);
  assert.equal(internals.isDouyinMediaHint("https://example.com/video.mp4"), false);
  assert.equal(internals.isDouyinMediaHint("https://www.douyin.com/aweme/v1/feed/"), false);
  assert.equal(internals.isDouyinMediaHint("http://v3-web.douyinvod.com/video.mp4"), false);

  // Extension-only guards that the backend does not enforce.
  assert.equal(internals.isDouyinMediaHint("https://v3-web.douyinvod.com/cover.jpg"), false);
  // 2026-09 起抖音 play_addr 幸存形态是 HLS 主清单：捕获放行，桌面端
  // 走 yt-dlp 分片下载；分片本体仍然拒绝。
  assert.equal(internals.isDouyinMediaHint("https://v3-web.douyinvod.com/index.m3u8"), true);
  assert.equal(internals.isDouyinMediaHint("https://v3-web.douyinvod.com/x/seg.ts"), false);
  assert.equal(internals.isDouyinMediaHint("https://v3-web.douyinvod.com/x/seg.m4s"), false);
  assert.equal(internals.isDouyinMediaHint("https://EVILdouyinvod.com/video.mp4"), false);
  assert.equal(internals.isDouyinMediaHint("https://douyinvod.com.evil.test/video.mp4"), false);
  assert.equal(internals.isDouyinMediaHint("blob:https://www.douyin.com/abc"), false);
  assert.equal(internals.isDouyinMediaHint("not a url"), false);
  assert.equal(internals.isDouyinMediaHint(""), false);

  // activeMediaUrl must pick the first supported candidate and stay silent otherwise.
  const videoWith = (currentSrc, sources = []) => fakeElement({
    currentSrc,
    src: currentSrc,
    querySelectorAll: (selector) => (selector === "source" ? sources.map((src) => fakeElement({ src, getAttribute: () => src })) : []),
  });
  const rootWith = (video) => fakeElement({ querySelector: (selector) => (selector === "video" ? video : null) });

  assert.equal(
    internals.activeMediaUrl({ root: rootWith(videoWith("https://v3-web.douyinvod.com/a.mp4")) }),
    "https://v3-web.douyinvod.com/a.mp4",
  );
  // A blob: player source must fall through to a usable <source> child.
  assert.equal(
    internals.activeMediaUrl({
      root: rootWith(videoWith("blob:https://www.douyin.com/x", [
        "https://v9-web.douyinvod.com/b.mp4",
      ])),
    }),
    "https://v9-web.douyinvod.com/b.mp4",
  );
  assert.equal(internals.activeMediaUrl({ root: rootWith(videoWith("https://example.com/c.mp4")) }), "");
  assert.equal(internals.activeMediaUrl({ root: rootWith(null) }), "");
});

test("content archives page-shipped play URLs and attributes them by target", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");

  const boundPlayUrl =
    "https://www.douyin.com/aweme/v1/play/?aid=6383&biz_sign=abc&video_id=v0200fg10000dal6kbnog65sj3u0f0rg"
    + "&target=7686120183102033215&line=0&sign=deadbeef&source=PackSourceEnum_AWEME_DETAIL";
  const unboundCdnUrl =
    "https://v26-web.douyinvod.com/99930b115f304602a821cc1483f16de5/6aad0793/video/tos/cn/tos-cn-ve-15/o8aq"
    + "/?a=6383&br=1277&mime_type=video_mp4&ft=x";

  // The scanner sees the media elements exposed by the live DOM. The fixtures
  // below model attribute-bearing elements; the implementation also reads
  // currentSrc and Resource Timing for MSE players.
  const sandbox = buildContentSandbox({
    scannedSources: [
      { src: boundPlayUrl },
      { src: unboundCdnUrl },
      { src: "https://v26-web.douyinvod.com/x/cover.jpg" },
      { src: "blob:https://www.douyin.com/xyz" },
      { src: "https://v26-web.douyinvod.com/x/stream.m3u8" },
      { src: "" },
    ],
  });
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  assert.ok(internals?.scanCapturedSources, "content.js must expose scanCapturedSources for testing");

  internals.scanCapturedSources();
  // Exact attribution via the target= parameter, junk filtered out.
  assert.equal(internals.capturedByTarget.size, 1);
  assert.equal(internals.capturedByTarget.get("7686120183102033215")?.url, boundPlayUrl);

  // Bound hit for its own video.
  assert.equal(internals.capturedMediaUrl("7686120183102033215"), boundPlayUrl);

  // Another video on a detail page falls back to the latest unbound source —
  // the whole page plays exactly one video there, so attribution is safe.
  // 同为合法媒体时按直链优先级保留（CDN 渐进 3 > play 端点 2 > HLS 1），
  // 后到的 stream.m3u8 不得覆盖已捕获的渐进 MP4。
  assert.equal(sandbox.location.pathname, "/video/1234567890");
  assert.equal(internals.capturedMediaUrl("7683516192920800550"), unboundCdnUrl);

  // On the feed an unbound URL could belong to a preloaded neighbour: never guess.
  sandbox.location.pathname = "/";
  assert.equal(internals.capturedMediaUrl("7683516192920800550"), "");
  sandbox.location.pathname = "/video/7683516192920800550";

  // The live player source always wins over the archive.
  const liveUrl = "https://v3-web.douyinvod.com/live.mp4?x=1";
  const videoWith = (currentSrc) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    currentSrc,
    src: currentSrc,
  });
  const active = { root: { querySelector: (s) => (s === "video" ? videoWith(liveUrl) : null) }, videoId: "7686120183102033215" };
  assert.equal(internals.resolveMediaUrl(active), liveUrl);

  // No live source: the archive answers for the bound video.
  const silent = { root: { querySelector: () => null }, videoId: "7686120183102033215" };
  assert.equal(internals.resolveMediaUrl(silent), boundPlayUrl);
});

test("the capture archive stays bounded and forgets stale entries", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  for (let i = 0; i < 30; i += 1) {
    internals.rememberCapture(`https://www.douyin.com/aweme/v1/play/?video_id=v${i}&target=${10000000000 + i}`);
  }
  assert.ok(internals.capturedByTarget.size <= 20, "archive must stay bounded");
  // Newest entries survive; oldest were evicted.
  assert.ok(internals.capturedByTarget.has("10000000029"));
  assert.ok(!internals.capturedByTarget.has("10000000000"));
});

test("a full capture batch retains the selected favorite instead of evicting it", async () => {
  const sandbox = buildContentSandbox();
  const target = "7695284409359944995";
  Object.assign(sandbox.location, { origin: "https://www.douyin.com", pathname: "/user/self", search: `?modal_id=${target}` });
  sandbox.Date = class extends Date { static now() { return 10000; } };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8"), sandbox);
  const items = Array.from({length: 24}, (_, index) => ({
    videoId: index === 2 ? target : String(7695000000000000000n + BigInt(index)),
    url: `https://v26.douyinvod.com/batch-${index}.mp4`,
  }));
  const internals = sandbox.__shengjianContentInternals;
  internals.acceptMainWorldCapture({origin: sandbox.location.origin, data: {
    source: "shengjian-media-capture", nonce: "test-nonce", items,
  }});
  assert.equal(internals.capturedMediaUrl(target), items[2].url);
  assert.ok(internals.capturedByTarget.size <= 20);
  sandbox.location.search = "?modal_id=7695999999999999999";
  assert.equal(internals.resolveMediaUrl(internals.activeVideo()), "", "a neighbor is never used for an uncaptured modal");
});

test("content archives signed MSE URLs from resource timing", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const signedMseUrl = "https://v26-web.douyinvod.com/video/tos/cn/segment?mime_type=video_mp4&target=7686120183102033215&sign=abc";
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.performance = { getEntriesByType: () => [{ name: signedMseUrl }] };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  internals.scanCapturedSources();
  assert.equal(internals.capturedMediaUrl("7686120183102033215"), signedMseUrl);
});

// Shared vm harness for the behavioural content tests: a minimal DOM stub whose
// querySelectorAll answers the selectors content.js actually uses.
function buildContentSandbox({ scannedSources = [] } = {}) {
  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    ...props,
  });
  const sandbox = {
    URL,
    console: createFixtureConsole(),
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/1234567890" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "FoilPage",
      documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
      body: { append() {} },
      querySelectorAll: (selector) => (
        selector === "video, source" || selector === "video[src], source[src]" ? scannedSources : []
      ),
      querySelector: () => null,
      createElement: () => fakeElement({ append() {}, addEventListener() {}, classList: { add() {} }, dataset: {}, style: {} }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.innerHeight = 800;
  sandbox.window.innerWidth = 1280;
  sandbox.window.addEventListener = () => {};
  return sandbox;
}

test("background collects Douyin cookies only when no media hint is available", async () => {
  const submittedBodies = [];
  const browserTransfers = [];
  const requestCalls = [];
  let cookiesGetAllCalls = 0;
  const listeners = { message: null, downloadsChanged: null, storageChanged: null };
  const previousWebSocket = globalThis.WebSocket;
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static instances = [];
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      this.readyState = FakeWebSocket.OPEN;
      this.constructor.instances.push(this);
    }
    emit(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  globalThis.WebSocket = FakeWebSocket;

  globalThis.chrome = {
    storage: {
      onChanged: { addListener(fn) { listeners.storageChanged = fn; } },
      local: {
        async get(key) { return key === "localApiToken" ? { localApiToken: "s".repeat(40) } : {}; },
        async set() {},
      },
    },
    cookies: {
      async getAll() {
        cookiesGetAllCalls += 1;
        return [{ name: "sessionid", value: "secret", domain: ".douyin.com", path: "/", secure: true }];
      },
    },
    downloads: {
      async download(options, callback) { browserTransfers.push(options); callback(7); return 7; },
      onChanged: { addListener: (fn) => { listeners.downloadsChanged = fn; } },
      search(query, callback) { callback?.([]); return Promise.resolve([]); },
    },
    tabs: { query(query, callback) { callback?.([]); return Promise.resolve([]); }, sendMessage(id, message, callback) { callback?.(undefined); return Promise.resolve(undefined); } },
    runtime: {
      onMessage: { addListener: (fn) => { listeners.message = fn; } },
      lastError: null,
    },
  };
  globalThis.fetch = async (url, options) => {
    const body = options?.body ? JSON.parse(options.body) : null;
    requestCalls.push({ url, options, body });
    if (body) submittedBodies.push(body);
    return {
      ok: true,
      async json() {
        return {
          job: {
            id: "job-1",
            title: "测试",
            browser_download: {
              download_url: "https://v3-web.douyinvod.com/a.mp4",
              filename: "a.mp4",
              headers: { Accept: "video/*" },
            },
          },
        };
      },
    };
  };

  await import("../browser-extension/background.js");
  assert.equal(typeof listeners.message, "function", "background must register a message listener");
  for (let attempt = 0; attempt < 20 && !FakeWebSocket.instances.length; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(FakeWebSocket.instances.length, 1);
  assert.equal(FakeWebSocket.instances[0].url, "ws://127.0.0.1:18765/v1/extension-events");
  assert.deepEqual(FakeWebSocket.instances[0].protocols, ["shengjian-v1", "s".repeat(40)]);

  const submit = async (message) => {
    let deadline;
    try {
      return await Promise.race([
        new Promise((resolve) => listeners.message(message, {}, resolve)),
        new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error(`提交回调未完成: ${message.title || message.type}`)), 2000); }),
      ]);
    } finally { clearTimeout(deadline); }
  };

  // A captured progressive stream goes to Chrome first, without cookie export.
  cookiesGetAllCalls = 0;
  submittedBodies.length = 0;
  const douyin = await submit({
    type: "shengjian-page-submit",
    url: "https://www.douyin.com/video/1234567890",
    title: "抖音浏览器优先下载",
    mediaUrl: "https://v3-web.douyinvod.com/video/tos/example.mp4?token=test",
    mediaCandidates: [
      "https://v3-web.douyinvod.com/video/tos/example.mp4?token=test",
      "https://v26-web.douyinvod.com/video/tos/backup.mp4?token=backup",
      "https://v3-web.douyinvod.com/video/tos/index.m3u8?token=hls",
    ],
    transcribe: false,
  });
  assert.equal(douyin.ok, true, douyin.message);
  assert.equal(cookiesGetAllCalls, 0, "a media hint must not trigger cookie collection");
  assert.equal(submittedBodies[0].url, "https://www.douyin.com/video/1234567890");
  assert.equal(submittedBodies[0].mode, "browser");
  assert.equal(submittedBodies[0].options.referer, "https://www.douyin.com/video/1234567890");
  assert.equal(submittedBodies[0].media_url, "https://v3-web.douyinvod.com/video/tos/example.mp4?token=test");
  assert.deepEqual(submittedBodies[0].media_candidates, [
    "https://v3-web.douyinvod.com/video/tos/example.mp4?token=test",
    "https://v26-web.douyinvod.com/video/tos/backup.mp4?token=backup",
    "https://v3-web.douyinvod.com/video/tos/index.m3u8?token=hls",
  ]);
  assert.equal("browser_session" in submittedBodies[0], false);
  assert.equal(browserTransfers.length, 1, "Chrome starts the first transfer before any local fallback");

  submittedBodies.length = 0;
  const hlsWithVideo = await submit({type:"shengjian-page-submit",
    url:"https://www.douyin.com/video/1234567893", transcribe:true,
    mediaUrl:"https://v3-web.douyinvod.com/video/index.m3u8",
    mediaCandidates:["https://v26-web.douyinvod.com/video/full.mp4"]});
  assert.equal(hlsWithVideo.ok, true);
  assert.equal(submittedBodies[0].mode, "browser");
  assert.equal(submittedBodies[0].media_url, "https://v26-web.douyinvod.com/video/full.mp4");
  assert.equal(submittedBodies[0].options.transcribe_after_download, true);

  // A standalone playlist requires segment assembly, not a Chrome file save.
  submittedBodies.length = 0;
  const beforePlaylist = browserTransfers.length;
  const playlist = await submit({type:"shengjian-page-submit",
    url:"https://www.douyin.com/video/1234567894", transcribe:false,
    mediaUrl:"https://v3-web.douyinvod.com/video/index.m3u8"});
  assert.equal(playlist.ok, true);
  assert.equal(submittedBodies[0].mode, "local");
  assert.equal(browserTransfers.length, beforePlaylist);

  // Case 1b: without a media hint there is nothing chrome.downloads can fetch
  // safely — the page button now routes this shape to the browser-handoff
  // resolver (mode=browser), which still takes a one-shot Douyin session for
  // yt-dlp resolve. Cookies are collected once and never persisted.
  cookiesGetAllCalls = 0;
  submittedBodies.length = 0;
  const douyinResolve = await submit({
    type: "shengjian-page-submit",
    url: "https://www.douyin.com/video/1234567891",
    title: "抖音解析下载",
    transcribe: false,
  });
  assert.equal(douyinResolve.ok, true, douyinResolve.message);
  assert.equal(cookiesGetAllCalls, 1, "resolve fallback collects one-shot session");
  assert.equal(submittedBodies[0].mode, "browser");
  assert.equal(Array.isArray(submittedBodies[0].browser_session), true);
  assert.equal(submittedBodies[0].browser_session.length, 1);
  assert.equal(submittedBodies[0].browser_session[0].name, "sessionid");

  // Case 1c: a cookie-gated play endpoint goes straight to chrome.downloads
  // (mode=browser) — local urllib without cookies always 403s on this shape.
  cookiesGetAllCalls = 0;
  submittedBodies.length = 0;
  const douyinPlay = await submit({
    type: "shengjian-page-submit",
    url: "https://www.douyin.com/video/1234567892",
    title: "抖音play端点",
    mediaUrl: "https://www.douyin.com/aweme/v1/play/?video_id=v1&target=1234567892",
    transcribe: false,
  });
  assert.equal(douyinPlay.ok, true, douyinPlay.message);
  assert.equal(cookiesGetAllCalls, 0, "play endpoint with media hint must not collect cookies");
  assert.equal(submittedBodies[0].mode, "browser");
  assert.equal(
    submittedBodies[0].media_url,
    "https://www.douyin.com/aweme/v1/play/?video_id=v1&target=1234567892",
  );

  // Case 2: a B 站 page submit goes to the same local yt-dlp rail and does not
  // mode=local, never collects cookies, and never uses a browser handoff.
  cookiesGetAllCalls = 0;
  submittedBodies.length = 0;
  const page = await submit({
    type: "shengjian-page-submit",
    url: "https://www.bilibili.com/video/BV1xx411c7mD",
    title: "B站测试视频",
    transcribe: true,
  });
  assert.equal(page.ok, true, page.message);
  assert.equal(cookiesGetAllCalls, 0, "page submit must not collect cookies");
  assert.equal(submittedBodies[0].url, "https://www.bilibili.com/video/BV1xx411c7mD");
  assert.equal(submittedBodies[0].mode, "local");
  assert.equal(submittedBodies[0].options.transcribe_after_download, true);
  assert.equal("browser_session" in submittedBodies[0], false);
  assert.equal("media_url" in submittedBodies[0], false);

  // Desktop paste-link requests are pushed to the paired extension. Cookies are
  // read only after this explicit one-click event, submitted once, and erased
  // from the extension's in-memory array when the request settles.
  cookiesGetAllCalls = 0;
  submittedBodies.length = 0;
  requestCalls.length = 0;
  FakeWebSocket.instances[0].emit({
    type: "desktop-douyin-download",
    request_id: "a".repeat(32),
    url: "https://v.douyin.com/short-id",
    title: "桌面粘贴",
    transcribe: false,
  });
  for (let attempt = 0; attempt < 50 && !requestCalls.some(({ url }) => url.endsWith(`/v1/desktop-downloads/${"a".repeat(32)}`)); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(cookiesGetAllCalls, 1);
  const claimCall = requestCalls.find(({ url }) => /\/v1\/desktop-downloads\/[a-f0-9]{32}\/claim$/.test(url));
  assert.ok(claimCall);
  const desktopSubmission = requestCalls.find(({ url }) => url.endsWith(`/v1/desktop-downloads/${"a".repeat(32)}`));
  assert.ok(desktopSubmission);
  assert.equal(desktopSubmission.body.browser_session.length, 1);
  assert.equal(desktopSubmission.body.browser_session[0].value, "secret");
  assert.equal(typeof desktopSubmission.body.user_agent, "string");
  assert.ok(desktopSubmission.body.user_agent.length <= 512);
  assert.equal("browser_session" in chrome.storage.local, false, "session is never saved in extension storage");
  assert.equal(submittedBodies.at(-1).browser_session[0].value, "secret");
  FakeWebSocket.instances[0].close();
  globalThis.WebSocket = previousWebSocket;
});

test("content waits briefly for the player source before giving up", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    ...props,
  });
  // Controlled timers: capture interval callbacks so the test can step the
  // waitForMediaUrl retry loop deterministically instead of sleeping.
  const intervals = [];
  let fakeNow = 10000;
  const sandbox = {
    URL,
    Date: class extends Date { static now() { return fakeNow; } },
    console: createFixtureConsole(),
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => {
      if (ms === 5) intervals.push(fn); // only waitForMediaUrl probes use 5ms
      return intervals.length;
    },
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/1234567890" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "FoilPage",
      documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
      body: { append() {} },
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => fakeElement({ append() {}, addEventListener() {}, classList: { add() {} }, dataset: {}, style: {} }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.innerHeight = 800;
  sandbox.window.innerWidth = 1280;
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });

  const internals = sandbox.__shengjianContentInternals;
  assert.ok(internals?.waitForMediaUrl, "content.js must expose waitForMediaUrl for testing");

  const videoWith = (currentSrc) => fakeElement({
    currentSrc,
    src: currentSrc,
    querySelectorAll: () => [],
  });
  const rootWith = (video) => fakeElement({ querySelector: (selector) => (selector === "video" ? video : null) });

  // A ready player URL resolves immediately without waiting.
  const ready = await internals.waitForMediaUrl(
    { root: rootWith(videoWith("https://v3-web.douyinvod.com/a.mp4")), videoId: "1234567890" },
    4, 5,
  );
  assert.equal(ready, "https://v3-web.douyinvod.com/a.mp4");
  assert.equal(intervals.length, 0, "a ready source must not start the retry timer");

  // A source that appears on the second probe is picked up by the retry loop.
  const mutable = {
    currentSrc: "",
    src: "",
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
  };
  const late = { root: fakeElement({ querySelector: () => mutable }), videoId: "1234567890" };
  const promise = internals.waitForMediaUrl(late, 4, 5);
  mutable.currentSrc = "https://v9-web.douyinvod.com/b.mp4";
  intervals[0](); // first tick sees the now-available URL
  assert.equal(await promise, "https://v9-web.douyinvod.com/b.mp4");

  // Nothing available within the probe budget resolves to "" so the backend
  // cookie-bridge fallback still decides instead of hanging the click.
  const never = { root: rootWith(fakeElement({ currentSrc: "", src: "", querySelectorAll: () => [] })), videoId: "1234567890" };
  const exhaustedPromise = internals.waitForMediaUrl(never, 3, 5);
  const neverInterval = intervals.at(-1);
  neverInterval();
  neverInterval();
  neverInterval(); // budget (3 tries) exhausted -> resolve ""
  assert.equal(await exhaustedPromise, "");

  // A completed detail response with no media should not consume the full
  // fallback budget; allow a short follow-up window for another rendition.
  sandbox.document.documentElement.dataset = {
    shengjianMain: APP_VERSION,
    shengjianMainApiResponses: "1",
    shengjianMainMediaItems: "0",
  };
  const noMedia = { root: rootWith(fakeElement({ currentSrc: "", src: "", querySelectorAll: () => [] })), videoId: "1234567890" };
  const noMediaPromise = internals.waitForMediaUrl(noMedia, 20, 5);
  const noMediaInterval = intervals.at(-1);
  noMediaInterval();
  fakeNow += 1900;
  noMediaInterval();
  assert.equal(await noMediaPromise, "");

  // Feed autoplay switches mid-wait: preserve the original identity and never
  // borrow the next video's source.
  const switched = { root: rootWith(fakeElement({ currentSrc: "", src: "", querySelectorAll: () => [] })), videoId: "1234567890" };
  const switchedPromise = internals.waitForMediaUrl(switched, 3, 5);
  const switchTick = intervals.at(-1);
  switchTick(); switchTick();               // 2 of 3 tries spent
  sandbox.location.pathname = "/video/9999999999"; // feed switched to a new aweme
  switchTick();                              // resolves the original capture without following the new id
  assert.equal(await switchedPromise, "");
});

test("content shortens the wait when no Douyin detail request is observed", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  let fakeNow = 10000;
  sandbox.Date = class extends Date { static now() { return fakeNow; } };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  const pending = internals.waitForMediaUrl({ root: null, videoId: "1234567890" }, 20, 300);
  const probe = sandbox.__intervals.at(-1);
  probe();
  fakeNow += 1200;
  probe();
  assert.equal(await pending, "", "without a detail request, hand off after the short fallback window");
});

test("waitForMediaUrl replays a favorite dropped by the batch cap from the MAIN archive", async () => {
  // 2026-10-12 收藏页实机形态：信息流批次在弹窗打开前送达（当时 URL 无
  // modal_id），51 项灌满隔离侧 20 槽档案，目标被挤出；点击下载后
  // waitForMediaUrl 应先向 MAIN 发一次定向重放请求，重放回包经
  // acceptMainWorldCapture 验证管线重新绑定目标并快速解析出地址。
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const target = "7695284409359944995";
  const replayUrl = "https://v26.douyinvod.com/fav-5.mp4";
  const postedMessages = [];
  const intervals = [];
  const sandbox = {
    URL,
    Date: class extends Date { static now() { return 10000; } },
    console: createFixtureConsole(),
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => { if (ms === 5) intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { origin: "https://www.douyin.com", pathname: "/user/self", search: "" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "FoilPage",
      documentElement: { dataset: { shengjianMainNonce: "test-nonce" } },
      body: { append() {} },
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => ({ append() {}, addEventListener() {}, classList: { add() {} }, dataset: {}, style: {} }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.postMessage = (message, origin) => { postedMessages.push({ message, origin }); };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  // 预灌 51 项（3 条 ≤24 消息）——真实失败形态：此刻 URL 还没有 modal_id，
  // 选中保护不生效，目标（第 6 项）被后续插入挤出 20 槽档案。
  for (let offset = 0; offset < 51; offset += 24) {
    const items = Array.from({ length: Math.min(24, 51 - offset) }, (_, i) => {
      const index = offset + i;
      return {
        videoId: index === 5 ? target : String(7695000000000000000n + BigInt(index)),
        url: `https://v26.douyinvod.com/fav-${index}.mp4`,
      };
    });
    internals.acceptMainWorldCapture({ origin: "https://www.douyin.com", data: {
      source: "shengjian-media-capture", nonce: "test-nonce", items,
    } });
  }
  assert.equal(internals.capturedByTarget.size, 20);
  assert.ok(!internals.capturedByTarget.has(target), "预置：目标已被 20 槽档案挤出");
  // 弹窗此刻才打开（SPA 追加 modal_id），随后点击下载进入等待。
  sandbox.location.search = `?modal_id=${target}`;

  const promise = internals.waitForMediaUrl({ root: null, videoId: target }, 8, 5);
  // 等待起点即发出一次定向重放请求：页内 nonce + 目标 aweme_id + 同源。
  const replayRequests = postedMessages.filter(({ message }) => message.source === "shengjian-capture-replay");
  assert.equal(replayRequests.length, 1, "exactly one replay request at wait start");
  assert.equal(replayRequests[0].message.videoId, target);
  assert.equal(replayRequests[0].message.nonce, "test-nonce");
  assert.equal(replayRequests[0].origin, "https://www.douyin.com");

  // MAIN 的重放回包走同一条 acceptMainWorldCapture 管线，重绑目标。
  internals.acceptMainWorldCapture({ origin: "https://www.douyin.com", data: {
    source: "shengjian-media-capture", nonce: "test-nonce",
    items: [{ videoId: target, url: replayUrl }],
  } });
  intervals.splice(0).forEach((tick) => tick());
  assert.equal(await promise, replayUrl);
  assert.ok(internals.capturedByTarget.has(target));
});

test("bilibili page helper extracts video ids and never touches cookies", async () => {
  const source = await readFile(new URL("../browser-extension/content-bili.js", import.meta.url), "utf8");
  // B 站路径不得读取 Cookie、不得触碰播放器媒体地址或直提下载：
  // 它只提交页面 canonical URL，由桌面 yt-dlp（带 Referer）下载公开视频。
  assert.doesNotMatch(source, /cookies|currentSrc|chrome\.downloads|MediaSource|browser_session/iu);

  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    setAttribute() {},
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    remove() {},
    ...props,
  });
  const sandbox = {
    URL,
    console: createFixtureConsole(),
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/BV1xx411c7mD", origin: "https://www.bilibili.com", href: "https://www.bilibili.com/video/BV1xx411c7mD" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "测试视频_哔哩哔哩_bilibili",
      documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
      body: { append() {}, querySelector: () => null },
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => fakeElement({
        append() {}, addEventListener() {},
        classList: { add() {}, remove() {} },
        dataset: {}, style: {},
      }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "content-bili.js" });
  const internals = sandbox.__shengjianBiliInternals;
  assert.ok(internals?.videoIdFromLocation, "content-bili.js must expose videoIdFromLocation");

  assert.equal(internals.videoIdFromLocation(), "BV1xx411c7mD");
  assert.equal(internals.canonicalUrl("BV1xx411c7mD"), "https://www.bilibili.com/video/BV1xx411c7mD");
  assert.equal(internals.pageTitle(), "测试视频");
});

test("bilibili path never adds cookie or downloads permissions", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));
  // B 站只用于提交本地下载：permissions 必须保持原样（cookies 只服务抖音）。
  assert.deepEqual(manifest.permissions, ["activeTab", "storage", "cookies", "downloads", "alarms"]);
  // 扩展整体仍不得声明 webRequest/debugger。
  assert.doesNotMatch(JSON.stringify(manifest), /webRequest|debugger/iu);
});

// ---------------------------------------------------------------------------
// MAIN-world capture (content-main.js). Douyin's logged-in player hands
// <video> an MSE blob whose segments are filtered media types, so the only
// durable progressive-MP4 source is the aweme detail/feed JSON the page itself
// consumes. These tests execute the hook in a sandboxed page world.
// ---------------------------------------------------------------------------

function buildMainWorldSandbox() {
  const posted = [];
  const timers = [];
  const nativeFetch = async () => ({ ok: true, clone() { return this; }, async text() { return "{}"; } });
  const sandbox = {
    URL,
    console: createFixtureConsole(),
    setTimeout: (fn, ms) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    location: { origin: "https://www.douyin.com" },
    document: { documentElement: { dataset: {} } },
    window: null,
    XMLHttpRequest: class {
      constructor() { this.responseText = "{}"; }
      open(method, url) { this.__shengjianUrl = String(url || ""); this.url = String(url || ""); }
      send() {}
      addEventListener(_type, fn) { this.onload = fn; }
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.location = sandbox.location;
  sandbox.window.fetch = nativeFetch;
  sandbox.window.XMLHttpRequest = sandbox.XMLHttpRequest;
  sandbox.window.postMessage = (message, origin) => { posted.push({ message, origin }); };
  sandbox.window.addEventListener = () => {};
  sandbox.globalThis = sandbox;
  return { sandbox, posted, timers, nativeFetch };
}

test("MAIN entry initializes and captures without a shared script from another world", async () => {
  const source = await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8");
  const { sandbox, posted, timers, nativeFetch } = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "content-main.js" });
  assert.notEqual(sandbox.window.fetch, nativeFetch);
  sandbox.__shengjianMainInternals.offer(JSON.stringify({aweme_detail:{
    aweme_id:"7691206606389808419",video:{play_addr:{url_list:["https://v26.douyinvod.com/valid-video.mp4"]}}
  }}));
  for (const timer of timers) timer();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].message.items[0].videoId, "7691206606389808419");
  assert.equal(sandbox.document.documentElement.dataset.shengjianMainMediaItems, "1");
});

test("MAIN retains the selected favorite beyond the normal response cap", async () => {
  const {sandbox} = buildMainWorldSandbox();
  const target = "7695284409359944995";
  Object.assign(sandbox.location, {pathname: "/user/self", search: `?modal_id=${target}`});
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox);
  const aweme_list = Array.from({length: 30}, (_, index) => ({
    aweme_id: index === 29 ? target : String(7695000000000000000n + BigInt(index)),
    video: {play_addr: {url_list: [`https://v26.douyinvod.com/list-${index}.mp4`]}},
  }));
  const captured = sandbox.__shengjianMainInternals.collectAwemes({aweme_list});
  assert.equal(captured.length, 24);
  assert.equal(captured[0].videoId, target);
  assert.equal(captured[0].url, "https://v26.douyinvod.com/list-29.mp4");
});

test("MAIN flush delivers both coalesced responses in bounded batches", async () => {
  const {sandbox, posted, timers} = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox);
  const makePayload = (start, length) => JSON.stringify({aweme_list:
    Array.from({length}, (_, index) => ({aweme_id: String(7695000000000000000n + BigInt(start + index)),
      video: {play_addr: {url_list: [`https://v26.douyinvod.com/flush-${start + index}.mp4`]}}}))});
  sandbox.__shengjianMainInternals.offer(makePayload(0, 12));
  sandbox.__shengjianMainInternals.offer(makePayload(12, 18));
  for (const timer of timers) timer();
  assert.equal(posted.length, 2);
  assert.ok(posted.every(({message}) => message.items.length <= 24));
  assert.equal(new Set(posted.flatMap(({message}) => Array.from(message.items, item => item.videoId))).size, 30);
});

test("MAIN observes favorite and collection video responses but ignores account APIs", async () => {
  const {sandbox, posted, timers} = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox);
  const body = JSON.stringify({aweme_list: [{aweme_id: "7695284409359944995",
    video: {play_addr: {url_list: ["https://v26.douyinvod.com/favorite.mp4"]}}}]});
  for (const path of ["/aweme/v1/web/aweme/favorite/", "/aweme/v1/web/aweme/listcollection/", "/aweme/v1/web/user/profile/self/"]) {
    const xhr = new sandbox.XMLHttpRequest();
    xhr.responseText = body;
    xhr.open("GET", `https://www.douyin.com${path}`);
    xhr.send();
    xhr.onload?.();
  }
  for (const timer of timers) timer();
  assert.equal(sandbox.document.documentElement.dataset.shengjianMainApiRequests, "2");
  assert.equal(sandbox.document.documentElement.dataset.shengjianMainMediaItems, "2");
  assert.equal(posted.flatMap(({message}) => Array.from(message.items)).length, 2);
});

test("MAIN pending budget keeps the selected video through later feed responses", async () => {
  const {sandbox, posted, timers} = buildMainWorldSandbox();
  const target = "7695284409359944995";
  Object.assign(sandbox.location, {pathname: "/user/self", search: `?modal_id=${target}`});
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox);
  const offer = sandbox.__shengjianMainInternals.offer;
  offer(JSON.stringify({aweme_detail: {aweme_id: target,
    video: {play_addr: {url_list: ["https://v26.douyinvod.com/selected.mp4"]}}}}));
  for (let batch = 0; batch < 3; batch += 1) {
    offer(JSON.stringify({aweme_list: Array.from({length: 24}, (_, index) => ({
      aweme_id: String(7695000000000000000n + BigInt(batch * 24 + index)),
      video: {play_addr: {url_list: [`https://v26.douyinvod.com/budget-${batch}-${index}.mp4`]}},
    }))}));
  }
  for (const timer of timers) timer();
  const delivered = posted.flatMap(({message}) => Array.from(message.items));
  assert.ok(delivered.length <= 48);
  assert.ok(delivered.some(item => item.videoId === target));
});

test("MAIN archive replays a favorite that arrived before the modal opened", async () => {
  const { sandbox, posted, timers } = buildMainWorldSandbox();
  const target = "7695284409359944995";
  // 2026-10-12 实机失败形态：收藏列表批次送达时 URL 还没有 modal_id，
  // 选中保护不生效；目标在批次中间，隔离世界 20 槽档案装不下 51 项。
  Object.assign(sandbox.location, { pathname: "/user/self", search: "" });
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox);
  const internals = sandbox.__shengjianMainInternals;
  const list = Array.from({ length: 51 }, (_, index) => ({
    aweme_id: index === 5 ? target : String(7695000000000000000n + BigInt(index)),
    video: { play_addr: { url_list: [`https://v26.douyinvod.com/fav-${index}.mp4`] } },
  }));
  internals.offer(JSON.stringify({ aweme_list: list }));
  for (const timer of timers.splice(0)) timer();
  const nonce = sandbox.document.documentElement.dataset.shengjianMainNonce;

  // 定向重放必须找回批次中间的目标条目，回包携带同一 nonce。
  assert.ok(internals.replayCapture({ nonce, videoId: target }));
  const replay = posted.find(({ message }) => message.items?.[0]?.videoId === target && message.nonce === nonce);
  assert.ok(replay, "replay must post the archived item");
  assert.equal(replay.message.items[0].url, "https://v26.douyinvod.com/fav-5.mp4");
  assert.equal(replay.origin, "https://www.douyin.com");

  // 错 nonce、未知 id、垃圾 id 一律不回；档案条目数有上界。
  const before = posted.length;
  assert.ok(!internals.replayCapture({ nonce: "wrong", videoId: target }));
  assert.ok(!internals.replayCapture({ nonce, videoId: "7695999999999999999" }));
  assert.ok(!internals.replayCapture({ nonce, videoId: "junk" }));
  assert.equal(posted.length, before);
  assert.ok(internals.captureArchive.size <= 240, "archive must stay bounded");

  // 档案按新鲜度裁剪：collectAwemes 对外每响应只放行 24 条，分 12 批
  // 灌入越过 240 上界后最早条目被淘汰；批次中途重放过的目标移到队尾，
  // 紧随其后的洪峰不会把反复点击中的目标挤掉。
  for (let batch = 0; batch < 12; batch += 1) {
    internals.offer(JSON.stringify({ aweme_list: Array.from({ length: 100 }, (_, index) => ({
      aweme_id: String(7710000000000000000n + BigInt(batch * 100 + index)),
      video: { play_addr: { url_list: [`https://v26.douyinvod.com/burst-${batch}-${index}.mp4`] } },
    })) }));
    for (const timer of timers.splice(0)) timer();
    if (batch === 7) assert.ok(internals.replayCapture({ nonce, videoId: target }), "hot target replayed mid-flood");
  }
  assert.ok(internals.captureArchive.size <= 240, "archive must stay bounded across batches");
  assert.ok(!internals.captureArchive.has("7710000000000000000"), "oldest burst entry evicted");
  assert.ok(internals.replayCapture({ nonce, videoId: target }), "hot target survives the flood");
});

test("main-world hook extracts play_addr from detail JSON and forwards it", async () => {
  const source = await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8");
  const { sandbox, posted, timers } = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content-main.js" });

  const internals = sandbox.__shengjianMainInternals;
  assert.ok(internals?.collectAwemes, "content-main.js must expose internals for testing");

  // detail shape: aweme_detail.video.bit_rate lists progressive renditions.
  const detail = {
    aweme_detail: {
      aweme_id: "7673838983419530149",
      bit_rate: [
        { play_addr: { url_list: ["https://v26-web.douyinvod.com/high.mp4?sign=1", "https://www.douyin.com/aweme/v1/play/?video_id=v1"] } },
        { play_addr: { url_list: ["https://v26-web.douyinvod.com/low.mp4?sign=1"] } },
      ],
      video: { play_addr: { url_list: ["https://v3-web.douyinvod.com/default.mp4?sign=1"] } },
    },
  };
  const items = internals.collectAwemes(detail);
  assert.equal(items.length, 1);
  assert.equal(items[0].videoId, "7673838983419530149");
  // Highest bitrate rendition wins over the default play_addr.
  assert.equal(items[0].url, "https://v26-web.douyinvod.com/high.mp4?sign=1");
  assert.deepEqual(Array.from(items[0].candidates), [
    "https://v26-web.douyinvod.com/high.mp4?sign=1",
    "https://v26-web.douyinvod.com/low.mp4?sign=1",
    "https://v3-web.douyinvod.com/default.mp4?sign=1",
  ]);
  assert.equal(items[0].playUrl, "https://www.douyin.com/aweme/v1/play/?video_id=v1");

  // feed shape: nested aweme_list entries are each bound to their own id.
  const feed = {
    aweme_list: [
      { aweme_info: { aweme_id: "1111111111111111111", video: { play_addr: { url_list: ["https://v26-web.douyinvod.com/a.mp4"] } } } },
      { aweme_info: { aweme_id: "2222222222222222222", video: { play_addr: { url_list: ["https://v26-web.douyinvod.com/b.mp4"] } } } },
    ],
  };
  const feedItems = internals.collectAwemes(feed);
  assert.equal(feedItems.length, 2);
  assert.equal(feedItems[1].videoId, "2222222222222222222");

  // Newer page envelopes can move awemes under arbitrary nested keys and put
  // bitrate renditions under video.bit_rate. Both still yield a safe media hint.
  const wrappedDetail = {
    data: { item_list: [{ payload: { aweme_info: {
      aweme_id: "6666666666666666666",
      video: { bit_rate: [{ play_addr: { url_list: ["https://v26-web.douyinvod.com/nested.mp4"] } }] },
    } } }] },
  };
  const wrappedItems = internals.collectAwemes(wrappedDetail);
  assert.equal(wrappedItems.length, 1);
  assert.equal(wrappedItems[0].videoId, "6666666666666666666");
  assert.equal(wrappedItems[0].url, "https://v26-web.douyinvod.com/nested.mp4");
  const shape = internals.inspectPayloadShape(wrappedDetail);
  assert.equal(shape.awemeNodes, 1);
  assert.equal(shape.videoNodes, 1);
  assert.equal(shape.playAddrNodes, 1);
  assert.equal(shape.urlCandidates, 1);
  assert.equal(shape.httpsCandidates, 1);
  assert.equal(shape.allowedUrls, 1);

  // 主清单（.m3u8）已放行：抖音 play_addr 幸存形态交桌面端 yt-dlp；
  // 封面与站外 mp4 仍然拒绝。
  const accepted = internals.collectAwemes({
    aweme_detail: {
      aweme_id: "3333333333333333333",
      video: { play_addr: { url_list: ["https://v26-web.douyinvod.com/cover.webp", "https://example.com/x.mp4", "https://v26-web.douyinvod.com/index.m3u8"] } },
    },
  });
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].url, "https://v26-web.douyinvod.com/index.m3u8");

  // 同一 url_list 内渐进 MP4 优先于主清单（免合流）。
  const prefersMp4 = internals.collectAwemes({
    aweme_detail: {
      aweme_id: "4444444444444444444",
      video: { play_addr: { url_list: ["https://v26-web.douyinvod.com/index.m3u8", "https://v26-web.douyinvod.com/api.mp4"] } },
    },
  });
  assert.equal(prefersMp4.length, 1);
  assert.equal(prefersMp4[0].url, "https://v26-web.douyinvod.com/api.mp4");

  // CDN 渐进 MP4 优先于 Cookie 门禁的 play 端点（本地裸 GET play 必 403）。
  const prefersCdnOverPlay = internals.collectAwemes({
    aweme_detail: {
      aweme_id: "5555555555555555555",
      video: {
        play_addr: {
          url_list: [
            "https://www.douyin.com/aweme/v1/play/?video_id=v1&target=5555555555555555555",
            "https://v26-web.douyinvod.com/cdn.mp4?sign=1",
          ],
        },
      },
    },
  });
  assert.equal(prefersCdnOverPlay.length, 1);
  assert.equal(prefersCdnOverPlay[0].url, "https://v26-web.douyinvod.com/cdn.mp4?sign=1");

  // offer() batches then flushes to the isolated world via same-origin postMessage.
  internals.offer(JSON.stringify(detail));
  assert.equal(posted.length, 0, "messages are batched briefly");
  timers.pop()(); // run the flush timer
  assert.equal(posted.length, 1);
  assert.equal(posted[0].origin, "https://www.douyin.com");
  assert.equal(posted[0].message.source, "shengjian-media-capture");
  // MAIN↔ISOLATED nonce 握手：消息携带 documentElement dataset 里的一次性 nonce。
  assert.equal(posted[0].message.nonce, sandbox.document.documentElement.dataset.shengjianMainNonce);
  assert.match(String(posted[0].message.nonce), /[0-9a-f-]{16,}/);
  assert.equal(posted[0].message.items[0].videoId, "7673838983419530149");
  assert.equal(posted[0].message.items[0].candidates.length, 3);

  // Non-JSON and non-aweme payloads stay silent.
  internals.offer("not json");
  internals.offer(JSON.stringify({ status_code: 0, unrelated: true }));
  timers.pop()?.();
  assert.equal(posted.length, 1);
  assert.equal(sandbox.document.documentElement.dataset.shengjianMainJsonFailures, "1");
  assert.equal(sandbox.document.documentElement.dataset.shengjianMainJsonResponses, "2");
});

test("main-world fetch wrapper observes matching URLs without breaking the page", async () => {
  const source = await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8");
  const { sandbox, posted, timers } = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content-main.js" });

  const bodies = [];
  const calls = [];
  const detailBody = JSON.stringify({
    aweme_detail: { aweme_id: "1234567890123456789", video: { play_addr: { url_list: ["https://v3-web.douyinvod.com/x.mp4?sig=1"] } } },
  });
  sandbox.fetch = async (url, init) => {
    calls.push(String(url));
    return {
      ok: true,
      clone() { return this; },
      async text() { return detailBody; },
    };
  };
  // Re-wrap after replacing fetch the way a page would own its own instance.
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content-main.js" });

  const observed = await sandbox.fetch("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1234567890123456789");
  assert.equal(observed.ok, true, "wrapped fetch returns the page's response untouched");
  await sandbox.fetch("https://www.douyin.com/aweme/v1/web/feed/hot/");
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  timers.pop()?.();
  assert.ok(calls.length >= 2);
  assert.equal(posted.length, 1, "both aweme API responses were observed and batched once");

  // A non-aweme URL must not be inspected at all.
  const beforeFetchCalls = calls.length;
  await sandbox.fetch("https://example.com/video.json");
  assert.equal(calls.length, beforeFetchCalls + 1);
});

test("main-world XHR wrapper observes aweme detail responses", async () => {
  const source = await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8");
  const { sandbox, posted, timers } = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content-main.js" });
  const xhr = new sandbox.XMLHttpRequest();
  xhr.responseText = JSON.stringify({
    aweme_detail: { aweme_id: "9876543210987654321", video: { play_addr: { url_list: ["https://v3-web.douyinvod.com/y.mp4?sig=2"] } } },
  });
  xhr.open("GET", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=9876543210987654321");
  xhr.send();
  xhr.onload(); // simulate the network completing
  timers.pop()?.();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].message.items[0].videoId, "9876543210987654321");
});

test("content accepts MAIN-world captures and binds them to their video id", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(content, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  assert.ok(internals?.acceptMainWorldCapture, "content.js must expose acceptMainWorldCapture");

  const makeEvent = (data, origin = "https://www.douyin.com") => ({ data, origin });

  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    nonce: "test-nonce",
    items: [
      {
        videoId: "7673838983419530149",
        url: "https://v26-web.douyinvod.com/api.mp4?sig=1",
        candidates: [
          "https://v26-web.douyinvod.com/api.mp4?sig=1",
          "https://v3-web.douyinvod.com/backup.mp4?sig=2",
          "https://v26-web.douyinvod.com/index.m3u8?sig=3",
        ],
        playUrl: "https://www.douyin.com/aweme/v1/play/?target=7673838983419530149",
      },
      { videoId: "bad", url: "https://v26-web.douyinvod.com/ignore.mp4" },
      { videoId: "7673502504189108977", url: "https://v26-web.douyinvod.com/index.m3u8" },
      { videoId: "7673502504189108977", url: "https://example.com/evil.mp4" },
    ],
  }));

  // Exact attribution: the API-derived URL answers for its own video even on
  // the feed, where an unbound neighbour URL would never be trusted.
  sandbox.location.pathname = "/";
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "https://v26-web.douyinvod.com/api.mp4?sig=1");
  assert.deepEqual(Array.from(internals.capturedMediaCandidates("7673838983419530149")), [
    "https://v26-web.douyinvod.com/api.mp4?sig=1",
    "https://v3-web.douyinvod.com/backup.mp4?sig=2",
    "https://v26-web.douyinvod.com/index.m3u8?sig=3",
  ]);
  assert.equal(internals.playEndpointFor("7673838983419530149"), "https://www.douyin.com/aweme/v1/play/?target=7673838983419530149");
  // HLS 主清单与渐进 MP4 同为可捕获媒体（桌面端据此分流 yt-dlp / 直下）。
  assert.equal(internals.capturedMediaUrl("7673502504189108977"), "https://v26-web.douyinvod.com/index.m3u8");
  assert.equal(internals.capturedByTarget.size, 2, "junk ids and disallowed urls are dropped");

  // Wrong origin or unknown source markers are ignored.
  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    nonce: "test-nonce",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/hijack.mp4" }],
  }, "https://evil.example"));
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "https://v26-web.douyinvod.com/api.mp4?sig=1");
  internals.acceptMainWorldCapture(makeEvent({ source: "other-extension", items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/hijack2.mp4" }] }));
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "https://v26-web.douyinvod.com/api.mp4?sig=1");
});

test("content drops forged MAIN-world captures without the matching nonce", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(content, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  const makeEvent = (data, origin = "https://www.douyin.com") => ({ data, origin });

  // 页内脚本盲喷同源 postMessage：无 nonce / 错 nonce 一律丢弃（00-待办 #7）。
  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/forged-no-nonce.mp4" }],
  }));
  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    nonce: "wrong-nonce",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/forged-bad-nonce.mp4" }],
  }));
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "");

  // 带正确 nonce 的同源消息正常入档。
  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    nonce: "test-nonce",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/legit.mp4?sig=1" }],
  }));
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "https://v26-web.douyinvod.com/legit.mp4?sig=1");

  // dataset 尚未就绪（MAIN 未注入）时：任何捕获消息都不入档。
  sandbox.document.documentElement.dataset.shengjianMainNonce = "";
  internals.acceptMainWorldCapture(makeEvent({
    source: "shengjian-media-capture",
    nonce: "",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/pre-main.mp4" }],
  }));
  assert.equal(internals.capturedByTarget.size, 1);
});

// ---------------------------------------------------------------------------
// 抖音多通道冗余（0.12.4）：无直链守门、fresh-media 应答、本地失败转投。
// ---------------------------------------------------------------------------

function buildContentSandboxV2() {
  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    ...props,
  });
  const keydownHandlers = [];
  const messageListeners = [];
  const runtimeMessages = [];
  const toastMessages = [];
  const intervals = [];
  const feedbackButton = {
    _title: "",
    set title(value) { this._title = String(value); toastMessages.push(this._title); },
    get title() { return this._title; },
  };
  const sandbox = {
    URL, console: createFixtureConsole(),
    setTimeout, clearTimeout,
    // submit 走默认 300ms 等待窗，显式用例用 5ms：全部收集，按需步进。
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/7673838983419530149" },
    chrome: {
      runtime: {
        onMessage: { addListener: (fn) => messageListeners.push(fn) },
        sendMessage: (msg, cb) => { runtimeMessages.push(msg); cb?.({ ok: true }); },
      },
    },
    document: {
      title: "FoilPage",
      documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
      body: { append() {} },
      querySelectorAll: (selector) => selector === ".shengjian-player-button" ? [feedbackButton] : [],
      querySelector: () => null,
      createElement: () => fakeElement({ append() {}, addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} }, dataset: {}, style: {} }),
      addEventListener: () => {},
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.__runtimeMessages = runtimeMessages;
  sandbox.__toastMessages = toastMessages;
  sandbox.__messageListeners = messageListeners;
  sandbox.__intervals = intervals;
  return sandbox;
}

test("content hands missing MAIN hook to the authorized resolver instead of stopping", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  const button = { disabled: false, classList: { add() {}, remove() {} } };
  internals.submit(button, "download");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(sandbox.__intervals.length, 1, "a missing hook must fail without adding the eight-second read ticker");
  // 0.18.0 起下载动作先做同视频预检（先生 2026-10-08），再走诊断+提交。
  const types = sandbox.__runtimeMessages.map((message) => message.type).join(", ");
  assert.equal(types, "shengjian-check-existing, shengjian-capture-diagnostic, shengjian-page-submit", `record the diagnostic and still submit a resolver task: ${types}`);
  const diagnostic = sandbox.__runtimeMessages.find((m) => m.type === "shengjian-capture-diagnostic");
  assert.equal(diagnostic.diagnostic.reason, "main_world_missing");
  assert.ok(diagnostic.diagnostic.elapsed_ms < 100, "the missing hook path must not wait for player capture");
  assert.equal("videoId" in diagnostic.diagnostic, false);
  const submitMsg = sandbox.__runtimeMessages.find((m) => m.type === "shengjian-page-submit");
  assert.equal(submitMsg.url, "https://www.douyin.com/video/7673838983419530149");
  assert.equal(submitMsg.mediaUrl, "");
  assert.equal(button.disabled, false, "button becomes available after background acknowledgement");
});

test("capture retry guidance appears only with a submitted or terminal failure notice", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const hosts = await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8");
  const pageUrl = "https://www.douyin.com/video/7673838983419530149";
  const loadContent = (sandbox) => {
    vm.createContext(sandbox);
    vm.runInContext(hosts, sandbox, { filename: "douyin-hosts.js" });
    vm.runInContext(source, sandbox, { filename: "content.js" });
  };
  const sendToast = (sandbox, message) => {
    for (const listener of sandbox.__messageListeners) listener(message, {}, () => {});
  };

  const successSandbox = buildContentSandboxV2();
  loadContent(successSandbox);
  successSandbox.chrome.runtime.sendMessage = (message, callback) => {
    successSandbox.__runtimeMessages.push(message);
    if (message.type === "shengjian-check-existing") callback?.({ ok: true, active: false, done: false });
    else if (message.type === "shengjian-page-submit") callback?.({
      ok: true,
      feedback_sent: false,
      job: { id: "abc123def456", status: "downloading", download_complete: false, queued: false, options: {} },
    });
    else callback?.({ ok: true });
  };
  successSandbox.__shengjianContentInternals.submit({ disabled: false, classList: { add() {}, remove() {} } }, "download");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(successSandbox.__toastMessages.length, 1, "the initiating page shows accepted status if the background toast was lost");
  assert.match(successSandbox.__toastMessages[0], /^已提交下载（若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。）$/);
  sendToast(successSandbox, { type: "shengjian-toast", taskUrl: pageUrl, stage: "progress", message: "正在解析" });
  assert.equal(successSandbox.__toastMessages.length, 1, "progress remains hidden on Douyin");
  sendToast(successSandbox, { type: "shengjian-toast", taskUrl: pageUrl, taskId: "abc123def456", stage: "submitted", message: "已提交下载" });
  assert.equal(successSandbox.__toastMessages.length, 1, "the later broadcast is acknowledged without duplicating the fallback receipt");
  sendToast(successSandbox, { type: "shengjian-toast", taskUrl: pageUrl, taskId: "abc123def456", stage: "download", message: "下载成功" });
  assert.equal(successSandbox.__toastMessages.length, 2, "the matching video's success remains a single clean notice");
  assert.equal(successSandbox.__toastMessages[1], "下载成功");

  const failureSandbox = buildContentSandboxV2();
  loadContent(failureSandbox);
  failureSandbox.chrome.runtime.sendMessage = (message, callback) => {
    failureSandbox.__runtimeMessages.push(message);
    if (message.type === "shengjian-check-existing") callback?.({ ok: true, active: false, done: false });
    else if (message.type === "shengjian-page-submit") callback?.({ ok: false, feedback_sent: true, message: "下载未能提交" });
    else callback?.({ ok: true });
  };
  failureSandbox.__shengjianContentInternals.submit({ disabled: false, classList: { add() {}, remove() {} } }, "download");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(failureSandbox.__toastMessages, [], "background owns the single submission-failure notice");
  sendToast(failureSandbox, { type: "shengjian-toast", taskUrl: pageUrl, taskId: "sj_request_1", stage: "failure", error: true, message: "下载未能提交" });
  assert.equal(failureSandbox.__toastMessages.length, 1);
  assert.match(failureSandbox.__toastMessages[0], /下载未能提交（若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。）$/);

  const broadcastFirstSandbox = buildContentSandboxV2();
  loadContent(broadcastFirstSandbox);
  broadcastFirstSandbox.chrome.runtime.sendMessage = (message, callback) => {
    broadcastFirstSandbox.__runtimeMessages.push(message);
    if (message.type === "shengjian-check-existing") callback?.({ ok: true, active: false, done: false });
    else if (message.type === "shengjian-page-submit") {
      sendToast(broadcastFirstSandbox, { type: "shengjian-toast", taskUrl: pageUrl, taskId: "abc123def456", stage: "submitted", message: "已提交下载" });
      callback?.({ ok: true, job: { id: "abc123def456", status: "downloading", download_complete: false, queued: false, options: {} } });
    } else callback?.({ ok: true });
  };
  broadcastFirstSandbox.__shengjianContentInternals.submit({ disabled: false, classList: { add() {}, remove() {} } }, "download");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(broadcastFirstSandbox.__toastMessages.length, 1, "broadcast-first and response-first paths both show one receipt");

  const directFailureSandbox = buildContentSandboxV2();
  loadContent(directFailureSandbox);
  directFailureSandbox.chrome.runtime.sendMessage = (message, callback) => {
    directFailureSandbox.__runtimeMessages.push(message);
    if (message.type === "shengjian-check-existing") callback?.({ ok: true, active: false, done: false });
    else if (message.type === "shengjian-page-submit") callback?.({ ok: false, message: "请先在扩展中连接 FoilPage" });
    else callback?.({ ok: true });
  };
  directFailureSandbox.__shengjianContentInternals.submit({ disabled: false, classList: { add() {}, remove() {} } }, "download");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(directFailureSandbox.__toastMessages.length, 1, "a direct terminal rejection emits one error notice");
  assert.match(directFailureSandbox.__toastMessages[0], /请先在扩展中连接 FoilPage（若这次未完成：留在当前抖音视频页，按 F5 刷新；等待视频画面加载后，再点「下载」。）$/);
});

test("favorite modal id overrides an unrelated active feed and never borrows its player URL", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  const unrelatedPlayer = {
    currentSrc: "https://v3-web.douyinvod.com/wrong-video.mp4?sig=wrong",
    src: "https://v3-web.douyinvod.com/wrong-video.mp4?sig=wrong",
    querySelectorAll: () => [],
  };
  const activeFeed = {
    getBoundingClientRect: () => ({ width: 600, height: 700, top: 50, bottom: 750, right: 600, left: 0 }),
    getAttribute: (name) => name === "data-e2e-vid" ? "9999999999" : null,
    querySelector: (selector) => selector === "video" ? unrelatedPlayer : null,
    querySelectorAll: () => [],
  };
  sandbox.location = {
    pathname: "/user/self",
    search: "?modal_id=12345678901234567890&showTab=favorite_collection",
  };
  sandbox.window.location = sandbox.location;
  sandbox.document.querySelectorAll = (selector) => (
    selector === '[data-e2e="feed-active-video"]' ? [activeFeed] : []
  );
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  const active = internals.activeVideo();
  assert.equal(active.videoId, "12345678901234567890");
  assert.equal(active.playerMatchesTarget, false);
  assert.equal(internals.activeMediaUrl(active), "", "do not submit another feed item's signed URL");

  const button = { disabled: false, classList: { add() {}, remove() {} } };
  internals.submit(button, "download");
  await new Promise((r) => setTimeout(r, 0));
  const pageSubmit = sandbox.__runtimeMessages.find((message) => message.type === "shengjian-page-submit");
  assert.ok(pageSubmit);
  assert.equal(pageSubmit.url, "https://www.douyin.com/video/12345678901234567890");
  assert.equal(pageSubmit.mediaUrl, "");
});

test("content answers fresh-media requests with the freshest capture", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  internals.rememberCapture("https://v26-web.douyinvod.com/fresh.mp4?sig=2", "7673838983419530149");
  const responses = [];
  for (const listener of sandbox.__messageListeners) {
    listener({ type: "shengjian-fresh-media" }, null, (resp) => responses.push(resp));
  }
  assert.ok(responses.length >= 1, "应有监听器应答");
  assert.equal(responses[0].ok, true);
  assert.equal(responses[0].videoId, "7673838983419530149");
  assert.equal(responses[0].mediaUrl, "https://v26-web.douyinvod.com/fresh.mp4?sig=2");
  assert.deepEqual(Array.from(responses[0].mediaCandidates), [
    "https://v26-web.douyinvod.com/fresh.mp4?sig=2",
  ]);
});

test("content submits bounded alternate media candidates with the video task", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  internals.acceptMainWorldCapture({
    origin: "https://www.douyin.com",
    data: {
      source: "shengjian-media-capture",
    nonce: "test-nonce",
      items: [{
        videoId: "7673838983419530149",
        url: "https://v26-web.douyinvod.com/high.mp4?sig=1",
        candidates: [
          "https://v26-web.douyinvod.com/high.mp4?sig=1",
          "https://v3-web.douyinvod.com/backup.mp4?sig=2",
          "https://v26-web.douyinvod.com/index.m3u8?sig=3",
        ],
        playUrl: "https://www.douyin.com/aweme/v1/play/?target=7673838983419530149",
      }],
    },
  });

  internals.submit({ disabled: false, classList: { add() {}, remove() {} } }, "download");
  await new Promise((resolve) => setTimeout(resolve, 0));
  const submitted = sandbox.__runtimeMessages.find((message) => message.type === "shengjian-page-submit");
  assert.ok(submitted);
  assert.deepEqual(Array.from(submitted.mediaCandidates), [
    "https://v26-web.douyinvod.com/high.mp4?sig=1",
    "https://v3-web.douyinvod.com/backup.mp4?sig=2",
    "https://v26-web.douyinvod.com/index.m3u8?sig=3",
  ]);
  assert.equal(submitted.playUrl, "https://www.douyin.com/aweme/v1/play/?target=7673838983419530149");
});

test("background continues the same durable task and keeps channel failures quiet", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  assert.match(background, /fallback_pending/);
  assert.match(background, /downloads\/\$\{taskId\}\/fallback/);
  assert.match(background, /downloads\/\$\{taskId\}\/finish-fallback/);
  assert.doesNotMatch(background, /本地下载失败，已改用/);
  assert.doesNotMatch(background, /submitDouyinResolverFallback/);
  assert.match(background, /WATCHED_TASKS_KEY/);
  assert.match(background, /restoreWatchedTasks/);
  assert.match(background, /taskUrl: job\?\.url/);
});

// ---------------------------------------------------------------------------
// 0.13.0：抖音回退序修正。2026-10-01 实测：签名地址本地 403 后，play 端点在
// chrome.downloads 里也被拒（SERVER_FORBIDDEN），yt-dlp 解析被 a_bogus 挡死——
// 旧「media_forbidden 只试 play 端点」的序会把三通道全部走死。新签名媒体地址
// （回扫/正在播放的 currentSrc，未参与过被拒请求）改为优先，play 端点次之。
// ---------------------------------------------------------------------------

test("fallback policy prefers an untried fresh media url over play endpoints on 403", async () => {
  const { browserFallbackMediaUrl } = await import("../browser-extension/fallback-policy.js");
  const remembered = {
    mediaUrl: "https://v.douyin.com/old-signed.mp4?sign=expired",
    mediaCandidates: ["https://v.douyin.com/old-signed.mp4?sign=expired"],
    playUrl: "https://www.douyin.com/aweme/v1/play/?video_id=123",
  };
  const fresh = {
    videoId: "123",
    mediaUrl: "https://v.douyin.com/new-signed.mp4?sign=fresh",
    mediaCandidates: ["https://v.douyin.com/new-signed.mp4?sign=fresh"],
    playUrl: "https://www.douyin.com/aweme/v1/play/?video_id=123",
  };
  assert.equal(
    browserFallbackMediaUrl({ code: "media_forbidden", remembered, fresh, matchingFresh: fresh }),
    "https://v.douyin.com/new-signed.mp4?sign=fresh",
  );
});

test("fallback policy skips rejected urls and hls playlists, then falls to play", async () => {
  const { browserFallbackMediaUrl } = await import("../browser-extension/fallback-policy.js");
  // 回扫只有 HLS 新地址：chrome.downloads 下载不了清单，跳过后用 play 端点。
  const remembered = { mediaUrl: "https://v.douyin.com/rejected.m3u8" };
  const fresh = {
    videoId: "123",
    mediaUrl: "https://v.douyin.com/rejected.m3u8",
    mediaCandidates: ["https://v.douyin.com/other.m3u8"],
    playUrl: "https://www.douyin.com/aweme/v1/play/?video_id=123",
  };
  assert.equal(
    browserFallbackMediaUrl({ code: "media_forbidden", remembered, fresh, matchingFresh: fresh }),
    "https://www.douyin.com/aweme/v1/play/?video_id=123",
  );
  // 回扫拿到的地址全部被拒过：退到记忆 play 端点（被拒地址永不重放）。
  const remembered2 = {
    mediaUrl: "https://v.douyin.com/x.mp4",
    playUrl: "https://www.douyin.com/aweme/v1/play/?video_id=123&line=1",
  };
  const fresh2 = { videoId: "123", mediaUrl: "https://v.douyin.com/x.mp4" };
  assert.equal(
    browserFallbackMediaUrl({ code: "media_forbidden", remembered: remembered2, fresh: fresh2, matchingFresh: fresh2 }),
    "https://www.douyin.com/aweme/v1/play/?video_id=123&line=1",
  );
  // 无任何可用地址：返回空串，由调用方转入一次性授权解析回退。
  const fresh3 = { videoId: "123", mediaUrl: "https://v.douyin.com/x.mp4" };
  assert.equal(
    browserFallbackMediaUrl({ code: "media_forbidden", remembered: "https://v.douyin.com/x.mp4", fresh: fresh3, matchingFresh: fresh3 }),
    "",
  );
});

test("fallback policy keeps play-first order for non-signature failures", async () => {
  const { browserFallbackMediaUrl } = await import("../browser-extension/fallback-policy.js");
  // 网络类失败不涉及签名被拒，维持既有次序：play → 记忆 play → 新媒体地址。
  const remembered = { mediaUrl: "https://v.douyin.com/old.mp4" };
  const fresh = {
    videoId: "123",
    mediaUrl: "https://v.douyin.com/new.mp4",
    playUrl: "https://www.douyin.com/aweme/v1/play/?video_id=123",
  };
  assert.equal(
    browserFallbackMediaUrl({ code: "network_error", remembered, fresh, matchingFresh: fresh }),
    "https://www.douyin.com/aweme/v1/play/?video_id=123",
  );
});

test("background wires the fallback policy and asks every douyin tab for fresh media", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  assert.match(background, /import \{ browserFallbackMediaUrls \} from "\.\/fallback-policy\.js"/);
  assert.match(background, /browserFallbackMediaUrls\(\{/);
  // 目标页不在活动标签（弹窗提交/多窗口/后台页）时也要能问到：按 host 权限
  // 匹配所有抖音标签，只取原 videoId 一致的应答；不借用新活动视频。
  assert.match(background, /chrome\.tabs\.query\(\{ url: \["https:\/\/\*\.douyin\.com\/\*"\] \}/);
  assert.match(background, /String\(response\.videoId \|\| ""\) === String\(expectedVideoId\)/);
  assert.match(background, /await askTabsForFreshMedia\(expectedVideoId\)/);
  // 只有 play 端点、无媒体地址的应答同样有效（此前会被当成无效应答丢弃）。
  assert.match(background, /!response\?\.mediaUrl && !response\?\.playUrl/);
});

// ---------------------------------------------------------------------------
// 0.13.0：抖音解析 Cookie 按名收窄（00-待办 #5 / 101 号 F06）。有直链时
// 0 采集的原则不变；确需 resolve 时会话长度以名单为上限。
// ---------------------------------------------------------------------------

test("douyin resolve cookies are narrowed to a name allowlist", async () => {
  const { pickDouyinResolveCookies, DOUYIN_RESOLVE_COOKIE_NAMES } = await import("../browser-extension/douyin-cookies.js");
  const jar = [
    { name: "sessionid", value: "a" },
    { name: "ttwid", value: "b" },
    { name: "msToken", value: "c" },
    { name: "SEARCH_CARD_LIST", value: "无关 UI 状态" },
    { name: "theme_mode", value: "无关外观" },
    { name: "download_guide", value: "无关弹窗" },
  ];
  const picked = pickDouyinResolveCookies(jar);
  assert.deepEqual(picked.map((c) => c.name).sort(), ["msToken", "sessionid", "ttwid"]);
  // 会话长度以名单为上限：无论域里存了多少枚，交出的不会超过名单。
  assert.ok(picked.length <= DOUYIN_RESOLVE_COOKIE_NAMES.size);
  assert.equal(pickDouyinResolveCookies(null).length, 0);
});

test("background funnels the douyin session through the name allowlist", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  assert.match(background, /import \{ pickDouyinResolveCookies \} from "\.\/douyin-cookies\.js"/);
  assert.match(background, /pickDouyinResolveCookies\(cookies\)\.map\(normalizeSessionCookie\)/);
});

// ---------------------------------------------------------------------------
// 0.18.0：抖音反馈重做（先生 2026-10-08）——进度条上方居中、静态「正在处理」
// 不读秒、忙态驻留、同视频重复下载先确认、复用已下载媒体直接转写。
// ---------------------------------------------------------------------------

test("feedback window pins above the progress bar instead of adaptive corners", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  // 进度条上方居中定位助手；不再有「页面空白处」四象限自适应（全屏时
  // 卡片会落到左上角——先生看到的读秒就在那）。
  assert.match(source, /function positionFeedbackCard/);
  assert.match(source, /const bottomOffset = hasActionableHint \? 205 : 88/);
  assert.doesNotMatch(source, /fitsRight/);
  assert.doesNotMatch(source, /fitsAbove/);
  // Page feedback has no intermediate busy card or download progress bar.
  assert.doesNotMatch(source, /正在读取播放器…（\$\{/);
  assert.doesNotMatch(source, /showBusyWindow|sjFeedbackPulse|class="bar"|has-bar/);
  assert.match(source, /该视频正在处理中，无需重复提交/);
  assert.match(source, /该视频已下载完成/);
});

test("Douyin feedback ignores progress and other videos while accepting two receipt stages", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  const button = {title:"unchanged"};
  const originalQuery = sandbox.document.querySelectorAll;
  sandbox.document.querySelectorAll = selector => selector === '.shengjian-player-button' ? [button] : originalQuery(selector);
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, {filename:"douyin-hosts.js"});
  vm.runInContext(source, sandbox, {filename:"content.js"});
  const listener = sandbox.__messageListeners[0];
  const event = {type:"shengjian-toast",taskUrl:"https://www.douyin.com/video/7673838983419530149"};
  listener({...event,stage:"progress",message:"浏览器下载中 50%"});
  listener({...event,stage:"transcribe",message:"转写成功"});
  listener({...event,stage:"download",taskUrl:"https://www.douyin.com/video/1111111111111111",message:"下载成功"});
  assert.equal(button.title,"unchanged");
  listener({...event,stage:"submitted",message:"已提交下载"});
  assert.equal(button.title,"已提交下载");
  listener({...event,stage:"download",message:"下载成功"});
  assert.equal(button.title,"下载成功");
});

test("content gates duplicate downloads behind a confirm card", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  // 预检返回「已下载完成」：必须停在确认卡，不产生提交消息。
  sandbox.chrome.runtime.sendMessage = (msg, cb) => {
    sandbox.__runtimeMessages.push(msg);
    if (msg.type === "shengjian-check-existing") cb?.({ ok: true, active: false, done: true });
    else cb?.({ ok: true });
  };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  const button = { disabled: false, classList: { add() {}, remove() {} } };
  internals.submit(button, "download");
  await new Promise((r) => setTimeout(r, 0));
  const types = sandbox.__runtimeMessages.map((m) => m.type);
  assert.equal(types.join(","), "shengjian-check-existing", "must stop at the confirm card");
  // 确认后才真正提交。
  internals.pendingConfirm().confirm();
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(sandbox.__runtimeMessages.some((m) => m.type === "shengjian-page-submit"), "confirmed submit proceeds");
});

test("content cancels duplicate downloads and skips confirm for transcribe", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  sandbox.chrome.runtime.sendMessage = (msg, cb) => {
    sandbox.__runtimeMessages.push(msg);
    if (msg.type === "shengjian-check-existing") cb?.({ ok: true, active: false, done: true });
    else cb?.({ ok: true });
  };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  const button = { disabled: false, classList: { add() {}, remove() {} } };
  // 取消：永远不提交。
  internals.submit(button, "download");
  await new Promise((r) => setTimeout(r, 0));
  internals.pendingConfirm().cancel();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sandbox.__runtimeMessages.some((m) => m.type === "shengjian-page-submit"), false, "cancelled confirm never submits");
  // 转写动作不弹确认卡，但预检以给重复点击即时反馈（先生 2026-10-18）；
  // 无进行中任务时照常提交。
  sandbox.__runtimeMessages.length = 0;
  internals.submit(button, "transcribe");
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(sandbox.__runtimeMessages.some((m) => m.type === "shengjian-check-existing"), "transcribe preflights for instant feedback");
  assert.ok(sandbox.__runtimeMessages.some((m) => m.type === "shengjian-page-submit" && m.transcribe === true));
  // 进行中且已带转写：提示后不再重复提交。
  sandbox.__runtimeMessages.length = 0;
  sandbox.__toastMessages.length = 0;
  sandbox.chrome.runtime.sendMessage = (msg, cb) => {
    sandbox.__runtimeMessages.push(msg);
    if (msg.type === "shengjian-check-existing") cb?.({ ok: true, active: true, done: false, activeTranscribing: true });
    else cb?.({ ok: true });
  };
  internals.submit(button, "transcribe");
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sandbox.__runtimeMessages.some((m) => m.type === "shengjian-page-submit"), false, "already transcribing never resubmits");
  assert.ok(sandbox.__toastMessages.some((t) => /转写会自动接续/.test(String(t))));
});

test("background answers check-existing from the desktop task library", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  assert.match(background, /shengjian-check-existing/);
  assert.match(background, /checkExistingDownloads/);
  assert.match(background, /\/v1\/downloads\?limit=50&url=/);
  // 复用已下载媒体的提交应答如实告知，不再笼统「已提交下载」。
  assert.match(background, /已复用已下载的视频，直接开始转写/);
});

test("popup retains the authorized resolver when capture is missing", async () => {
  const popup = await readFile(new URL("../browser-extension/popup.js", import.meta.url), "utf8");
  assert.match(popup, /shengjian-fresh-media/);
  assert.match(popup, /begin\(""\)/);
  assert.match(popup, /if \(!isDouyin\) \{ begin\(""\); return; \}/);
});

test("extension self-updates when the bundled version advances", async () => {
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  // Health/version and bounded reload plumbing; lifecycle behavior is covered in extension_update_behavior.
  assert.match(background, /extension_latest/);
  assert.match(background, /chrome\.runtime\.getManifest\?\.\(\)\.version/);
  assert.match(background, /chrome\.runtime\.reload\(\)/);
  assert.match(background, /shengjianSelfReloadAt/);
});

// ---------------------------------------------------------------------------
// 0.12.5：按钮贴合原生（无打光）、抖音直播/广告隐藏、B站进度弹窗。
// ---------------------------------------------------------------------------

test("douyin hides the controls on live rooms and ad cards", async () => {
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  assert.ok(internals?.shouldHideControls, "content.js must expose shouldHideControls");

  // 直播间整站：live.douyin.com 域名或 www.douyin.com 的 /live 路径都隐藏。
  sandbox.location = { hostname: "live.douyin.com", pathname: "/1234567890" };
  sandbox.window.location = sandbox.location;
  assert.equal(internals.isLiveRoomLocation(), true);
  sandbox.location = { hostname: "www.douyin.com", pathname: "/live/1234567890" };
  sandbox.window.location = sandbox.location;
  assert.equal(internals.isLiveRoomLocation(), true);
  assert.equal(internals.shouldHideControls(), true, "live room pages must hide the buttons");
  sandbox.location = { hostname: "www.douyin.com", pathname: "/video/1234567890" };
  sandbox.window.location = sandbox.location;
  assert.equal(internals.isLiveRoomLocation(), false);

  // 卡片层信号：直播间/推广链接或精确的直播/广告角标文案。
  const rootWith = (matchedSelectors = [], badges = []) => ({
    querySelector: (selector) => (matchedSelectors.includes(selector) ? {} : null),
    querySelectorAll: () => badges.map((badge) => (
      typeof badge === "string" ? { childElementCount: 0, textContent: badge } : badge
    )),
  });
  assert.equal(internals.rootSignalsLiveOrAd(rootWith(['a[href*="live.douyin.com"]'])), true);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith(['a[href^="/live"]'])), true);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith(['a[href*="ec.douyin.com"]'])), true);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith([], ["广告"])), true);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith([], ["直播中"])), true);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith([], ["点击进入直播间"])), true);
  // 普通视频文案与嵌套文本节点不算角标。
  assert.equal(internals.rootSignalsLiveOrAd(rootWith([], ["三连投稿", "展开"])), false);
  assert.equal(internals.rootSignalsLiveOrAd(rootWith([], [{ childElementCount: 2, textContent: "广告" }])), false);

  // 常规视频页保持显示：URL 直达的 detail 页即使卡片未水合也不隐藏。
  sandbox.location = { hostname: "www.douyin.com", pathname: "/video/1234567890" };
  sandbox.window.location = sandbox.location;
  assert.equal(internals.shouldHideControls(), false, "detail pages must keep the buttons");
  // 识别不出视频 id 的页面也保持显示（实机教训：抖音改版会让 feed 容器
  // 检测失效，按 id 缺席隐藏会误伤普通视频；点击时的引导更安全）。
  sandbox.location = { hostname: "www.douyin.com", pathname: "/search/cats" };
  sandbox.window.location = sandbox.location;
  assert.equal(internals.shouldHideControls(), false, "unknown video id must not hide the buttons");
});

test("bilibili pill maps backend progress texts onto pill states", async () => {
  const source = await readFile(new URL("../browser-extension/content-bili.js", import.meta.url), "utf8");
  const sandbox = {
    URL, console: createFixtureConsole(),
    setTimeout, clearTimeout,
    setInterval: () => 0,
    MutationObserver: class { observe() {} },
    navigator: { userAgent: "node-test" },
    location: { pathname: "/video/BV1xx411c7mD", origin: "https://www.bilibili.com", href: "https://www.bilibili.com/video/BV1xx411c7mD" },
    chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
    document: {
      title: "测试视频_哔哩哔哩_bilibili",
      documentElement: { querySelector: () => null, querySelectorAll: () => [] },
      body: { append() {}, querySelector: () => null },
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => ({ append() {}, addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} }, dataset: {}, style: {} }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.top = sandbox;
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "content-bili.js" });
  const internals = sandbox.__shengjianBiliInternals;
  assert.ok(internals?.pillStatusFor, "content-bili.js must expose pillStatusFor");

  // 后台 pollLocalTask 的文案族 → 弹窗状态。
  // 跨 vm context 的对象原型不同，逐字段断言而非 deepEqual。
  assert.equal(internals.pillStatusFor("正在下载 42%", false).percent, 42);
  assert.equal(internals.pillStatusFor("正在下载 42%", false).mode, "percent");
  assert.equal(internals.pillStatusFor("正在下载 137%", false).percent, 100);
  assert.equal(internals.pillStatusFor("任务完成", false).mode, "done");
  assert.equal(internals.pillStatusFor("正在转写", false).text, "正在转写");
  assert.equal(internals.pillStatusFor("正文与标题转写成功", false).mode, "done");
  assert.equal(internals.pillStatusFor("正文转写成功，标题处理失败", true).text, "正文转写成功，标题处理失败");
  assert.equal(internals.pillStatusFor("标题转写成功，正文处理失败", true).mode, "error");
  assert.equal(internals.pillStatusFor("正在解析", false).text, "正在解析");
  assert.equal(internals.pillStatusFor("已提交下载", false).text, "已提交下载");
  assert.equal(internals.pillStatusFor("正在提交…", false).text, "正在提交");
  // 错误族收敛为可行动的短文案。
  assert.equal(internals.pillStatusFor("扩展已更新，请刷新页面后重试", true).mode, "error");
  assert.equal(internals.pillStatusFor("本地服务未运行", true).text, "请先打开 FoilPage");
  assert.equal(internals.pillStatusFor("任务失败，正在尝试改用浏览器下载…", true).text, "下载失败，请在 FoilPage 查看");
});

test("injected buttons drop the neumorphic backlight and custom page font", async () => {
  const douyinCss = await readFile(new URL("../browser-extension/content.css", import.meta.url), "utf8");
  const biliCss = await readFile(new URL("../browser-extension/content-bili.css", import.meta.url), "utf8");
  // 双向「打光」阴影（白色高光层）不允许再出现在页面注入样式里。
  for (const css of [douyinCss, biliCss]) {
    for (const shadow of css.match(/box-shadow:[^;]+;/gu) || []) {
      assert.doesNotMatch(shadow, /255 255 255/, `backlit shadow must go: ${shadow.trim()}`);
    }
  }
  // 抖音页不再向页面注入霞鹜字体：按钮继承站点字体栈。
  assert.doesNotMatch(douyinCss, /font-face|LXGW/iu);
  assert.match(douyinCss, /font-family: inherit !important/);
  assert.match(biliCss, /font-family: inherit !important/);
  // B 站反馈不再使用整行横幅，改为进度弹窗（数字 → 勾勾动画）。
  assert.doesNotMatch(biliCss, /\.shengjian-toast/);
  assert.match(biliCss, /\.shengjian-bili-pill/);
  assert.match(biliCss, /sj-num-out/);
  assert.match(biliCss, /sj-check-in/);
  // manifest 不再向视频站暴露字体资源（页面注入已改用站点字体）。
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));
  assert.equal(manifest.web_accessible_resources, undefined);
});

// ---------------------------------------------------------------------------
// YouTube page helper (content-youtube.js). Same privacy posture as Bilibili:
// the page script only hands over the canonical watch URL for the local
// yt-dlp track; it never reads cookies, credentials or player media URLs.
// ---------------------------------------------------------------------------

test("youtube page helper extracts video ids across watch/shorts/youtu.be", async () => {
  const source = await readFile(new URL("../browser-extension/content-youtube.js", import.meta.url), "utf8");
  // 与 B 站一致：YouTube 轨不得读取 Cookie、不得触碰播放器媒体地址或直提下载。
  assert.doesNotMatch(source, /cookies|currentSrc|chrome\.downloads|MediaSource|browser_session/iu);

  const fakeElement = (props = {}) => ({
    querySelector: () => null,
    querySelectorAll: () => [],
    append() {},
    setAttribute() {},
    addEventListener() {},
    classList: { add() {}, remove() {}, toggle() {} },
    dataset: {},
    style: {},
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, right: 0, left: 0 }),
    remove() {},
    ...props,
  });
  const buildSandbox = (url) => {
    const parsed = new URL(url);
    const sandbox = {
      URL,
      console: createFixtureConsole(),
      setTimeout: () => 0,
      clearTimeout: () => {},
      setInterval: () => 0,
      MutationObserver: class { observe() {} },
      navigator: { userAgent: "node-test" },
      location: { href: url, pathname: parsed.pathname, search: parsed.search, origin: parsed.origin },
      chrome: { runtime: { onMessage: { addListener() {} }, sendMessage() {} } },
      document: {
        title: "测试视频 - YouTube",
        documentElement: fakeElement({ dataset: { shengjianMainNonce: "test-nonce" } }),
        body: { append() {} },
        querySelectorAll: () => [],
        querySelector: () => null,
        createElement: () => fakeElement(),
        createElementNS: () => fakeElement({ append() {}, setAttribute() {} }),
      },
    };
    sandbox.window = sandbox;
    sandbox.window.top = sandbox;
    sandbox.window.addEventListener = () => {};
    return sandbox;
  };

  // watch 页：从 query 提取 v 并给出规范 watch URL。
  const watch = buildSandbox("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz&t=42s");
  vm.createContext(watch);
  vm.runInContext(source, watch, { filename: "content-youtube.js" });
  const internals = watch.__shengjianYoutubeInternals;
  assert.ok(internals?.videoIdFromLocation, "content-youtube.js must expose videoIdFromLocation");
  assert.equal(internals.videoIdFromLocation(), "dQw4w9WgXcQ");
  assert.equal(internals.canonicalUrl("dQw4w9WgXcQ"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(internals.pageTitle(), "测试视频");

  // Shorts / youtu.be 短链 / 非视频页。
  const shorts = buildSandbox("https://www.youtube.com/shorts/abcdefghijk");
  vm.createContext(shorts);
  vm.runInContext(source, shorts, { filename: "content-youtube.js" });
  assert.equal(shorts.__shengjianYoutubeInternals.videoIdFromLocation(), "abcdefghijk");

  const shortLink = buildSandbox("https://youtu.be/dQw4w9WgXcQ?si=tag");
  vm.createContext(shortLink);
  vm.runInContext(source, shortLink, { filename: "content-youtube.js" });
  assert.equal(shortLink.__shengjianYoutubeInternals.videoIdFromLocation(), "dQw4w9WgXcQ");

  const home = buildSandbox("https://www.youtube.com/feed/subscriptions");
  vm.createContext(home);
  vm.runInContext(source, home, { filename: "content-youtube.js" });
  assert.equal(home.__shengjianYoutubeInternals.videoIdFromLocation(), "");

  // 状态语言与 B 站同构：错误 → 可操作文案；JS 运行时缺失有专门提示。
  assert.equal(internals.pillStatusFor("标题转写成功", false).mode, "done");
  assert.equal(internals.pillStatusFor("正文转写成功，标题处理失败", true).text, "正文转写成功，标题处理失败");
  assert.equal(internals.pillStatusFor("标题转写成功，正文处理失败", true).mode, "error");
  const runtimeHint = internals.pillStatusFor("平台升级了反爬验证，需要 JavaScript 运行时", true);
  assert.match(runtimeHint.text, /JavaScript 运行时/);
  assert.equal(internals.pillStatusFor("任务完成", false).mode, "done");
  // pillStatusFor 在 vm 沙箱内构造对象，跨 realm 不能用 deepStrictEqual。
  const percent = internals.pillStatusFor("正在下载 42%", false);
  assert.equal(percent.mode, "percent");
  assert.equal(percent.percent, 42);
});

test("manifest registers youtube content script with least-privilege hosts", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));
  const youtubeEntry = manifest.content_scripts.find((entry) => entry.js?.includes("content-youtube.js"));
  assert.ok(youtubeEntry, "content-youtube.js must be registered in the manifest");
  assert.deepEqual(youtubeEntry.matches, ["https://www.youtube.com/*", "https://m.youtube.com/*", "https://youtu.be/*"]);
  assert.ok(youtubeEntry.css?.includes("content-youtube.css"), "youtube entry must carry its stylesheet");
  for (const host of youtubeEntry.matches) {
    assert.ok(manifest.host_permissions.includes(host), `host_permissions must include ${host}`);
  }
  // 权限面保持最小：不因 YouTube 新增任何危险权限。
  assert.deepEqual(manifest.permissions, ["activeTab", "storage", "cookies", "downloads", "alarms"]);

  const youtubeCss = await readFile(new URL("../browser-extension/content-youtube.css", import.meta.url), "utf8");
  assert.match(youtubeCss, /\.shengjian-yt-host/);
  assert.match(youtubeCss, /\.shengjian-yt-pill/);
});

// ---------------------------------------------------------------------------
// 三站通知形态一致性：抖音信息窗（Shadow DOM 内联样式）之外，B 站与 YouTube
// 的页内 pill 必须共享同一状态语言与动效集；驻留时长统一到先生裁决口径
// （常规 3.5s / 错误 6s / 完成 4.5s，2026-09-23）。本测试守护不漂移。
// ---------------------------------------------------------------------------
test("bili and youtube pills share one notification state language", async () => {
  const read = (name) => readFile(new URL(`../browser-extension/${name}`, import.meta.url), "utf8");
  const [biliCss, ytCss, douyinCss, biliJs, ytJs, popupCss] = await Promise.all([
    read("content-bili.css"), read("content-youtube.css"), read("content.css"),
    read("content-bili.js"), read("content-youtube.js"), read("popup.css"),
  ]);

  // 状态语言：百分比态只留数字（隐藏文字）。
  assert.match(biliCss, /\.shengjian-bili-pill\.is-percent \.sj-text\s*\{\s*display: none !important/);
  assert.match(ytCss, /\.shengjian-yt-pill\.is-percent \.sj-text\s*\{\s*display: none !important/);
  // 完成态与错误态都必须有底色变化（不是只改文字色）。
  for (const css of [biliCss, ytCss]) {
    assert.match(css, /\.is-done\s*\{[^}]*background/s, "done state must change background");
    assert.match(css, /\.is-error\s*\{[^}]*background/s, "error state must change background");
  }
  // 动效集：入场浮起、数字收缩退场、勾勾划入，两站齐备。
  for (const [pillIn, numOut, checkIn] of [
    [/sj-pill-in/, /sj-num-out/, /sj-check-in/],
    [/shengjian-yt-pill-in/, /shengjian-yt-num-out/, /shengjian-yt-check-in/],
  ]) {
    assert.match(biliCss + ytCss, pillIn);
    assert.match(biliCss + ytCss, numOut);
    assert.match(biliCss + ytCss, checkIn);
  }
  // pathLength=1 的勾必须用 1/1 dash 起止；dashoffset 带 !important 会压过
  // 划入动画，keyframes 内 !important 则整条被浏览器丢弃（0.12.25 修过的坑）。
  assert.match(ytCss, /stroke-dasharray: 1 !important/);
  assert.match(ytCss, /\.sj-check path \{[^}]*stroke-dashoffset: 1;/s, "base dashoffset must stay non-important so the draw-in animation can run");
  assert.doesNotMatch(ytCss, /@keyframes[^{]*\{(?:[^}]|\{[^}]*\})*important/s, "keyframes must not carry !important");
  // 减动效兜底：两站 pill 与 popup 都要有 prefers-reduced-motion 块。
  for (const css of [biliCss, ytCss, popupCss]) {
    assert.match(css, /prefers-reduced-motion/);
  }

  // 驻留口径：两站同一公式，按内容长度伸缩（短确认 2-4s / 错误 4-8s / 完成 4.5s）。
  for (const js of [biliJs, ytJs]) {
    assert.match(js, /function pillHoldFor\(/, "pills must compute hold from content length");
    assert.match(js, /clamp\(2600 \+ 280 \* len, 4000, 8000\)/, "error hold must scale 4-8s");
    assert.match(js, /clamp\(1200 \+ 220 \* len, 2000, 4000\)/, "text hold must scale 2-4s");
    assert.doesNotMatch(js, /PILL_HOLD_MS = \{ text:/, "static hold durations must be gone");
    assert.doesNotMatch(js, /text\.slice\(0,\s*12\)/, "fallback must not hard-truncate into broken phrases");
    // 关键分类两站齐备（导入失败/回原页/JS 运行时/已合并/换通道）。
    for (const phrase of ["已下载，导入 FoilPage 失败", "请回原视频页重试", "需 JavaScript 运行时，见 FoilPage 设置", "已在处理中", "已换通道重试"]) {
      assert.ok(js.includes(phrase), `missing shared classification: ${phrase}`);
    }
  }

  // 抖音页内通知已统一走 Shadow DOM 信息窗：content.css 不再有页内通知样式。
  assert.doesNotMatch(douyinCss, /\.shengjian-toast/);
  // popup 等待态呼吸点（与桌面浏览器连接页同一语言）且有减动效兜底。
  assert.match(popupCss, /prefers-reduced-motion/);
});

test("browser download filenames keep their suffix and pending imports stop on permanent rejection", async () => {
  // 2026-10 实机回归：固定长度截断把超长中文标题的 ".mp4" 一起切掉，
  // Chrome 存出无后缀文件，桌面端 415 拒绝导入后待导入队列无限重试
  // （10-04/10-11 各留一个无后缀残留，每次重试都向页面弹失败提示）。
  const fetchCalls = [];
  const storageWrites = [];
  let pendingImports = [
    // queuedAt 必须是新鲜时间戳：真实 entry 都带 Date.now()，超过一天的
    // 滞留 entry 会在 flush 入口直接丢弃（2026-10-20 自更新冻结修复）。
    { downloadId: 7, taskId: "aaaaaaaaaaaa", title: "超长标题", transcribe: false, queuedAt: Date.now() },
  ];
  globalThis.chrome = {
    storage: {
      onChanged: { addListener() {} },
      local: {
        async get(key) {
          if (key === "shengjianPendingImports") return { shengjianPendingImports: pendingImports };
          if (key === "localApiToken") return { localApiToken: "s".repeat(40) };
          return {};
        },
        async set(obj) {
          storageWrites.push(obj);
          if (obj && obj.shengjianPendingImports !== undefined) pendingImports = obj.shengjianPendingImports;
        },
      },
    },
    downloads: {
      async download(options, callback) { callback?.(7); return 7; },
      onChanged: { addListener() {} },
      async search() { return [{ id: 7, filename: "C:\Downloads\FoilPage\无后缀残留", state: "complete" }]; },
    },
    tabs: { async query() { return []; }, sendMessage(id, message, callback) { callback?.(undefined); return Promise.resolve(undefined); } },
    runtime: { onMessage: { addListener() {} }, lastError: null },
    cookies: { async getAll() { return []; } },
    alarms: { create() {}, onAlarm: { addListener() {} } },
  };
  globalThis.fetch = async (url, options) => {
    const method = String(options?.method || "GET").toUpperCase();
    const body = options?.body ? JSON.parse(options.body) : null;
    fetchCalls.push({ url: String(url), method, body });
    if (String(url).endsWith("/v1/downloads/aaaaaaaaaaaa/browser-failure")) {
      return { ok: true, status: 200, async json() { return {}; } };
    }
    if (String(url).endsWith("/v1/imports")) {
      return { ok: false, status: 415, async json() { return { error: { code: "unsupported_media_format", message: "不支持的媒体格式: <无扩展名>" } }; } };
    }
    if (String(url).endsWith("/v1/downloads/aaaaaaaaaaaa")) {
      return { ok: true, status: 200, async json() { return { job: { id: "aaaaaaaaaaaa", status: "awaiting_browser", options: { mode: "browser" } } }; } };
    }
    return { ok: true, status: 200, async json() { return {}; } };
  };
  delete globalThis.WebSocket;

  await import("../browser-extension/background.js");
  const internals = globalThis.__shengjianBackgroundInternals;
  assert.ok(internals?.safeRelativeFilename, "background must expose internals for testing");

  // ① 后缀保护：截断只作用标题主体，".mp4" 永远保留且总长有界。
  const longTitle = "行".repeat(200) + ".mp4";
  const name = internals.safeRelativeFilename(longTitle);
  assert.ok(name.endsWith(".mp4"), `suffix preserved: ${JSON.stringify(name)}`);
  assert.ok(name.length <= 88, `bounded length: ${name.length}`);
  assert.ok(internals.safeRelativeFilename("无后缀纯标题").endsWith(".mp4"));
  assert.ok(internals.safeRelativeFilename("正常标题.mp4").endsWith(".mp4"));

  // ② 永久拒绝终止重试：415 后上报任务终态失败，entry 丢弃、队列清空。
  await internals.flushPendingImports();
  const failureReport = fetchCalls.find(({ url, method }) => url.includes("/browser-failure") && method === "POST");
  assert.ok(failureReport, "browser-failure reported once");
  assert.equal(failureReport.body?.browser_error, "UNSUPPORTED_MEDIA_FORMAT");
  const importAttempt = fetchCalls.filter(({ url }) => url.endsWith("/v1/imports"));
  assert.equal(importAttempt.length, 1, "import attempted exactly once, no retry loop");
  const finalWrite = storageWrites.at(-1);
  assert.deepEqual(finalWrite?.shengjianPendingImports, [], "queue drained without keeping the entry");
});

test("restored terminal tasks are pruned and never replay failure notices", async () => {
  // 2026-10-14 实机回归：旧失败任务随每次 SW 唤醒重放“下载失败”提示，
  // 穿插在新任务的成功回执之间。恢复时终态任务必须跳过并清理，活跃
  // 任务的失败提示带持久去重。
  const fetchCalls = [];
  const tabMessages = [];
  const storageWrites = [];
  let watched = [
    { id: "aaaaaaaaaaaa", downloadNotified: true, transcribeNotified: true,
      lastJob: { id: "aaaaaaaaaaaa", status: "failed", url: "https://www.douyin.com/video/1111111111111111111" } },
    { id: "bbbbbbbbbbbb", downloadNotified: false, transcribeNotified: false,
      lastJob: { id: "bbbbbbbbbbbb", status: "downloading", url: "https://www.douyin.com/video/2222222222222222222" } },
    { id: "cccccccccccc", downloadNotified: false, transcribeNotified: false,
      lastJob: { id: "cccccccccccc", status: "awaiting_browser", url: "https://www.douyin.com/video/3333333333333333333" } },
  ];
  globalThis.chrome = {
    storage: {
      onChanged: { addListener() {} },
      local: {
        async get(key) {
          if (key === "watchedDownloadTasks") return { watchedDownloadTasks: watched };
          if (key === "localApiToken") return { localApiToken: "s".repeat(40) };
          if (key === "shengjianSubmissionQueue") return {};
          return {};
        },
        async set(obj) {
          storageWrites.push(obj);
          if (obj && obj.watchedDownloadTasks !== undefined) watched = obj.watchedDownloadTasks;
        },
      },
    },
    downloads: {
      async download(options, callback) { callback?.(7); return 7; },
      onChanged: { addListener() {} },
      async search() { return []; },
    },
    tabs: {
      async query() { return [{ id: 1, url: "https://www.douyin.com/video/2222222222222222222" }]; },
      sendMessage(id, message, callback) { tabMessages.push(message); callback?.({ ok: true }); return Promise.resolve(true); },
    },
    runtime: { onMessage: { addListener() {} }, lastError: null, getManifest: () => ({ version: "1.0.9" }) },
    cookies: { async getAll() { return []; } },
    alarms: { create() {}, onAlarm: { addListener() {} } },
  };
  const jobById = {
    // 终态失败任务：不得被恢复、不得被轮询、不得重放失败提示。
    aaaaaaaaaaaa: { id: "aaaaaaaaaaaa", status: "failed", url: "https://www.douyin.com/video/1111111111111111111", progress: 0 },
    // 活跃任务一：轮询即完成——只允许一次“下载成功”。
    bbbbbbbbbbbb: { id: "bbbbbbbbbbbb", status: "done", url: "https://www.douyin.com/video/2222222222222222222", progress: 1, download_complete: true },
    // 活跃任务二（抖音 URL）：轮询发现终态失败——1.0.11 起终态失败必须
    // 让发起页知道恰好一次（沉默失败修复），failureNotified 持久去重。
    cccccccccccc: { id: "cccccccccccc", status: "failed", url: "https://www.douyin.com/video/3333333333333333333", progress: 0, download_complete: false },
  };
  globalThis.fetch = async (url) => {
    const id = String(url).match(/\/v1\/downloads\/([a-f0-9]{12})$/)?.[1];
    fetchCalls.push(String(url));
    return { ok: true, status: 200, async json() { return { job: jobById[id] || null }; } };
  };
  delete globalThis.WebSocket;

  await import("../browser-extension/background.js");
  const internals = globalThis.__shengjianBackgroundInternals;
  await internals.restoreWatchedTasks();
  await new Promise((resolve) => setTimeout(resolve, 50));

  // 终态失败任务未被轮询，也不在清理后的观察列表里。
  assert.ok(!fetchCalls.some((url) => url.includes("aaaaaaaaaaaa")), "terminal failed task must not be polled");
  assert.ok(!watched.some((task) => task.id === "aaaaaaaaaaaa"), "terminal failed task must be pruned from the watch list");
  // 旧失败任务零重放；真实失败的非抖音任务恰好提示一次；成功照常。
  assert.ok(!tabMessages.some((message) => message?.taskId === "aaaaaaaaaaaa"), "no replayed notice for the terminal failed task");
  const failureNotices = tabMessages.filter((message) => message?.message === "下载失败，请在 FoilPage 查看");
  assert.equal(failureNotices.length, 1, "exactly one failure notice for the genuinely failed live task");
  assert.equal(failureNotices[0]?.taskId, "cccccccccccc");
  assert.ok(tabMessages.some((message) => message?.message === "下载成功"), "live task success still notified");
});

test("submission failure notices always carry actionable guidance", async () => {
  // 先生 2026-10-14：重载后弹"本地任务未能创建"，没有任何反馈或解决
  // 办法的引导。所有提交失败提示必须带下一步指引。
  globalThis.chrome = {
    storage: { onChanged: { addListener() {} }, local: {
      async get(key) { return key === "localApiToken" ? { localApiToken: "s".repeat(40) } : {}; },
      async set() {},
    } },
    downloads: { async download(o, cb) { cb?.(7); return 7; }, onChanged: { addListener() {} }, async search() { return []; } },
    tabs: { async query() { return []; }, sendMessage(id, m, cb) { cb?.(undefined); return Promise.resolve(undefined); } },
    runtime: { onMessage: { addListener() {} }, lastError: null },
    cookies: { async getAll() { return []; } },
    alarms: { create() {}, onAlarm: { addListener() {} } },
  };
  globalThis.fetch = async () => ({ ok: true, status: 200, async json() { return {}; } });
  delete globalThis.WebSocket;
  await import("../browser-extension/background.js");
  const notice = globalThis.__shengjianBackgroundInternals.submissionFailureNotice;
  const cases = [
    [{ code: "douyin_verification_required" }, "播放 2 秒"],
    [{ code: "local_api_unavailable" }, "FoilPage 桌面端未连接"],
    [{ code: "whatever", message: "平台校验拦截" }, "FoilPage 任务页"],
    [{}, "FoilPage 桌面端正在运行"],
  ];
  for (const [error, expected] of cases) {
    const text = notice(error);
    assert.ok(text.includes(expected), `guidance present for ${error.code || "no-code"}: ${text}`);
    assert.ok(text.length > 20, "notice must be a full sentence, not a bare error");
  }
});

test("a long-lived page gets one proactive stale hint before the next click", async () => {
  // 先生 2026-10-14：两小时采集窗口的边界要主动告知——页面年龄超 100
  // 分钟时，下一次点击前提示一次 F5 恢复；新鲜页面与重复点击不提示。
  const source = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandboxV2();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(source, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  assert.equal(typeof internals.maybeShowPageStaleHint, "function", "hint hook exposed");
  // 新鲜页面：无提示。
  internals.maybeShowPageStaleHint();
  assert.equal(sandbox.__toastMessages.length, 0, "fresh page stays quiet");
  // 页面老化到 100 分钟：提示恰好一次，重复调用不再提示。
  sandbox.performance = { timeOrigin: Date.now() - 101 * 60 * 1000 };
  internals.maybeShowPageStaleHint();
  internals.maybeShowPageStaleHint();
  const staleHints = sandbox.__toastMessages.filter((text) => String(text).includes("F5"));
  assert.equal(staleHints.length, 1, "exactly one stale-page hint");
  assert.match(staleHints[0], /等待视频画面加载后重试/);
  assert.match(staleHints[0], /任务页查看原因/);
  assert.doesNotMatch(staleHints[0], /即可恢复|顺畅下载|不受影响/);
});

test("repeat transcribe gets instant feedback and fallback start reassures the page once", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const background = await readFile(new URL("../browser-extension/background.js", import.meta.url), "utf8");
  // 转写重复点击：预检提示＋合并直达提示（先生 2026-10-18）
  assert.match(content, /该视频已在处理中，转写会自动接续/);
  assert.match(content, /已并入进行中的任务：下载完成后自动转写/);
  assert.match(content, /check\.activeTranscribing/);
  assert.match(background, /const activeTranscribing = activeJobs\.some/);
  // 首错换道安抚：每任务一次，经 fallback stage 放行抖音页
  assert.match(background, /别急，正在尝试其他下载办法/);
  assert.match(background, /\["submitted", "download", "fallback"\]/);
  assert.match(background, /fallbackReassured/);
});

test("offline click shows the launch card instead of silence and video pages never lose controls", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  // ① 桌面未连接：queued 回执必须弹「启动 FoilPage」卡（shengjian:// 协议），不再静默
  assert.match(content, /if \(acceptedJob\.queued\) \{\s*showOfflineLaunchCard/);
  assert.match(content, /shengjian:\/\/launch/);
  assert.match(content, /FoilPage 桌面端未连接/);
  assert.match(content, /任务已暂存到扩展/);
  assert.match(content, /若仍未开始，请在扩展弹窗查看任务状态/);
  // ② 视频页结构识别失败时浮动兜底，不再移除按钮
  assert.match(content, /视频页绝不能没有入口/);
  assert.match(content, /container: document\.body, anchor: null, floating: true/);
  // ③ 按钮隐藏原因进诊断
  assert.match(content, /reportControlsHiddenOnce\("live_or_ad"\)/);
  assert.match(content, /reportControlsHiddenOnce\("no_player_target"\)/);
});

// ---------------------------------------------------------------------------
// TASK-8.17 封面：MAIN 钩子提取、ISOLATED 入库与查询响应。封面是增强项，
// 域名白名单（douyinpic/byteimg/pstatp）与桌面 engine.downloader 同源；
// 与媒体地址独立交付——播放地址过期不拖累封面。
// ---------------------------------------------------------------------------

test("MAIN hook extracts douyin covers with origin_cover preference and host gating", async () => {
  const { sandbox } = buildMainWorldSandbox();
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox, { filename: "content-main.js" });
  const internals = sandbox.__shengjianMainInternals;
  const detail = (id, video, extra = {}) => ({ aweme_detail: { aweme_id: id, video, ...extra } });

  // origin_cover（发布方原图）优先于 cover（平台再压版本）。
  const origin = internals.collectAwemes(detail("1111111111111111111", {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/a.mp4"] },
    origin_cover: { url_list: ["https://p3-ecmc.douyinpic.com/obj/origin.jpeg"] },
    cover: { url_list: ["https://p9-pc.douyinpic.com/obj/pressed.webp"] },
  }));
  assert.equal(origin[0].coverUrl, "https://p3-ecmc.douyinpic.com/obj/origin.jpeg");

  // origin 缺失回退 cover；协议相对地址归一为 https。
  const fallback = internals.collectAwemes(detail("2222222222222222222", {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/b.mp4"] },
    cover: { url_list: ["//p9-pc.douyinpic.com/obj/pressed.jpeg"] },
  }));
  assert.equal(fallback[0].coverUrl, "https://p9-pc.douyinpic.com/obj/pressed.jpeg");

  // 白名单外主机不入封面；同 url_list 的次一个合法地址接续。
  const gated = internals.collectAwemes(detail("3333333333333333333", {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/c.mp4"] },
    origin_cover: { url_list: ["https://example.com/evil.jpeg", "https://si.byteimg.com/obj/ok.jpeg"] },
  }));
  assert.equal(gated[0].coverUrl, "https://si.byteimg.com/obj/ok.jpeg");

  // 无封面字段 → 空串；aweme 级 cover 兜底也生效。
  const none = internals.collectAwemes(detail("4444444444444444444", {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/d.mp4"] },
  }));
  assert.equal(none[0].coverUrl, "");
  const awemeLevel = internals.collectAwemes(detail("5555555555555555555",
    { play_addr: { url_list: ["https://v26-web.douyinvod.com/e.mp4"] } },
    { cover: { url_list: ["https://p3-ecmc.douyinpic.com/obj/aweme-level.jpeg"] } }));
  assert.equal(awemeLevel[0].coverUrl, "https://p3-ecmc.douyinpic.com/obj/aweme-level.jpeg");

  // 仅封面无播放地址（图集形态前置）：条目仍交付，url 保持为空。
  const coverOnly = internals.collectAwemes(detail("6666666666666666666", {
    origin_cover: { url_list: ["https://p3-ecmc.douyinpic.com/obj/gallery-cover.jpeg"] },
  }));
  assert.equal(coverOnly.length, 1);
  assert.equal(coverOnly[0].coverUrl, "https://p3-ecmc.douyinpic.com/obj/gallery-cover.jpeg");
  assert.equal(coverOnly[0].url, "");

  // 定向重放档案（captureArchive）同样携带封面。
  internals.offer(JSON.stringify(detail("7777777777777777777", {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/f.mp4"] },
    origin_cover: { url_list: ["https://p3-ecmc.douyinpic.com/obj/archived.jpeg"] },
  })));
  assert.equal(internals.captureArchive.get("7777777777777777777")?.coverUrl,
    "https://p3-ecmc.douyinpic.com/obj/archived.jpeg");
});

test("content stores page-world covers under the image-host allowlist", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(content, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  const makeEvent = (items, origin = "https://www.douyin.com") => ({
    data: { source: "shengjian-media-capture", nonce: "test-nonce", items }, origin,
  });

  internals.acceptMainWorldCapture(makeEvent([
    { videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/api.mp4?sig=1",
      coverUrl: "https://p3-ecmc.douyinpic.com/obj/cover.jpeg" },
  ]));
  assert.equal(internals.capturedByTarget.get("7673838983419530149")?.coverUrl,
    "https://p3-ecmc.douyinpic.com/obj/cover.jpeg");

  // 白名单外主机的封面不进档案，也不覆盖已入库的合法封面。
  internals.acceptMainWorldCapture(makeEvent([
    { videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/api.mp4?sig=1",
      coverUrl: "https://example.com/evil.jpeg" },
  ]));
  assert.equal(internals.capturedByTarget.get("7673838983419530149")?.coverUrl,
    "https://p3-ecmc.douyinpic.com/obj/cover.jpeg");

  // 仅封面条目（无媒体地址）：允许入库，但不伪装成可下载媒体。
  internals.acceptMainWorldCapture(makeEvent([
    { videoId: "7777777777777777777", coverUrl: "https://si.byteimg.com/obj/only-cover.jpeg" },
  ]));
  assert.equal(internals.capturedByTarget.get("7777777777777777777")?.coverUrl,
    "https://si.byteimg.com/obj/only-cover.jpeg");
  assert.equal(internals.capturedMediaUrl("7777777777777777777"), "");
});

test("shengjian-fresh-media response carries the captured cover", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const listeners = [];
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.window.location = sandbox.location;
  sandbox.chrome = { runtime: { onMessage: { addListener(fn) { listeners.push(fn); } }, sendMessage() {} } };
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(content, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;
  internals.acceptMainWorldCapture({ origin: "https://www.douyin.com", data: {
    source: "shengjian-media-capture", nonce: "test-nonce",
    items: [{ videoId: "7673838983419530149", url: "https://v26-web.douyinvod.com/api.mp4?sig=1",
      coverUrl: "https://p3-ecmc.douyinpic.com/obj/fresh.jpeg" }],
  } });
  let response = null;
  listeners[0]({ type: "shengjian-fresh-media", videoId: "7673838983419530149" }, {}, (value) => { response = value; });
  assert.equal(response?.coverUrl, "https://p3-ecmc.douyinpic.com/obj/fresh.jpeg",
    "service-worker 重启后的提示重建必须连同封面一起带回");
});

// ---------------------------------------------------------------------------
// TASK-8.18 图集（TASK-8.16 字段表驱动）：默认禁用；ISOLATED 中继配置
// （同源＋nonce）开启后按字段表提取 images；视频优先于图集。
// ---------------------------------------------------------------------------
function buildGallerySandbox() {
  const listeners = [];
  const { sandbox } = buildMainWorldSandbox();
  sandbox.window.addEventListener = (type, fn) => { listeners.push({ type, fn }); };
  return { sandbox, listeners };
}
async function loadGalleryHook(sandbox) {
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(await readFile(new URL("../browser-extension/content-main.js", import.meta.url), "utf8"), sandbox, { filename: "content-main.js" });
}
function dispatchWindowMessage(sandbox, listeners, data, origin) {
  const event = { origin: origin || sandbox.location.origin, data };
  for (const listener of listeners.filter((item) => item.type === "message")) {
    listener.fn(event);
  }
}
function enableGallery(sandbox, listeners, overrides = {}) {
  const nonce = sandbox.document.documentElement.dataset.shengjianMainNonce;
  dispatchWindowMessage(sandbox, listeners, {
    source: "shengjian-client-config", nonce, config: { enabled: true, ...overrides },
  });
}

const GALLERY_AWEME = (id, images, video) => ({
  aweme_detail: {
    aweme_id: id,
    ...(video ? { video } : {}),
    images,
  },
});
const PIC = (n, markers = []) => ({
  width: 1080, height: 1440,
  download_url_list: [`https://p3-sign.douyinpic.com/tos-cn-i-0813/obj-${n}-dl.webp`],
  url_list: [`https://p9-pc.douyinpic.com/tos-cn-i-0813/obj-${n}.webp`, ...markers.map(m => `https://p9-pc.douyinpic.com/tos-cn-i-0813/obj-${n}-${m}.webp`)],
});

test("gallery stays dark until the relayed config enables it", async () => {
  const { sandbox } = buildGallerySandbox();
  await loadGalleryHook(sandbox);
  const internals = sandbox.__shengjianMainInternals;
  const items = internals.collectAwemes(GALLERY_AWEME("1111111111111111111", [PIC(1)], {
    cover: { url_list: ["https://p3-sign.douyinpic.com/obj/cover.webp"] },
  }));
  assert.equal(items.length, 1);
  assert.equal(items[0].gallery, undefined, "默认禁用：不产图集条目");
  assert.equal(items[0].coverUrl, "https://p3-sign.douyinpic.com/obj/cover.webp", "封面照常");
});

test("enabled config extracts the gallery with candidate ordering and capping", async () => {
  const { sandbox, listeners } = buildGallerySandbox();
  await loadGalleryHook(sandbox);
  const internals = sandbox.__shengjianMainInternals;
  enableGallery(sandbox, listeners, {
    watermarkMarkers: ["~watermark"],
    maxImagesPerTask: 2,
  });
  const items = internals.collectAwemes(GALLERY_AWEME("2222222222222222222", [
    PIC(1), PIC(2, ["~watermark"]), { broken: true }, PIC(3),
  ]));
  assert.equal(items.length, 1);
  const gallery = items[0].gallery;
  assert.equal(gallery.length, 2, "maxImagesPerTask 限幅＋坏条目剔除");
  // download_url_list 优先于 url_list；水印候选靠后保留（回退面）。
  assert.deepEqual([...gallery[0].urls], [
    "https://p3-sign.douyinpic.com/tos-cn-i-0813/obj-1-dl.webp",
    "https://p9-pc.douyinpic.com/tos-cn-i-0813/obj-1.webp",
  ]);
  assert.ok(gallery[1].urls[gallery[1].urls.length - 1].includes("~watermark"));
  // 白名单外主机不入候选。
  const foreign = internals.collectAwemes(GALLERY_AWEME("3333333333333333333", [{
    url_list: ["https://evil.example/x.webp", "https://si.byteimg.com/obj/ok.webp"],
  }]));
  assert.deepEqual([...foreign[0].gallery[0].urls], ["https://si.byteimg.com/obj/ok.webp"]);
});

test("field table hot-fix and video-priority against gallery", async () => {
  const { sandbox, listeners } = buildGallerySandbox();
  await loadGalleryHook(sandbox);
  const internals = sandbox.__shengjianMainInternals;
  enableGallery(sandbox, listeners, { imagesFieldNames: ["slides"] });
  const renamed = internals.collectAwemes({
    aweme_detail: {
      aweme_id: "4444444444444444444",
      slides: [PIC(9)],
      images: "stale-shape",
    },
  });
  assert.equal(renamed[0].gallery.length, 1, "字段表改名可热修（配置驱动）");
  // 有视频候选时视频优先，图集不产出（图文/视频混合形态）。
  const mixed = internals.collectAwemes(GALLERY_AWEME("5555555555555555555", [PIC(1)], {
    play_addr: { url_list: ["https://v26-web.douyinvod.com/mixed.mp4"] },
  }));
  assert.equal(mixed[0].url, "https://v26-web.douyinvod.com/mixed.mp4");
  assert.equal(mixed[0].gallery, undefined);
});

test("config relay rejects foreign origins and bad nonces", async () => {
  const { sandbox, listeners } = buildGallerySandbox();
  await loadGalleryHook(sandbox);
  const internals = sandbox.__shengjianMainInternals;
  const nonce = sandbox.document.documentElement.dataset.shengjianMainNonce;
  dispatchWindowMessage(sandbox, listeners, { source: "shengjian-client-config", nonce, config: { enabled: true } }, "https://evil.example");
  dispatchWindowMessage(sandbox, listeners, { source: "shengjian-client-config", nonce: "wrong", config: { enabled: true } });
  assert.equal(internals.galleryConfig().enabled, false, "坏来源/坏 nonce 的配置不得生效");
});

test("gallery archive entry survives for targeted replay", async () => {
  const { sandbox, listeners } = buildGallerySandbox();
  await loadGalleryHook(sandbox);
  const internals = sandbox.__shengjianMainInternals;
  enableGallery(sandbox, listeners);
  internals.offer(JSON.stringify(GALLERY_AWEME("6666666666666666666", [PIC(1), PIC(2)])));
  const archived = internals.captureArchive.get("6666666666666666666");
  assert.equal(archived.gallery.length, 2, "重放档案携带图集");
  // 重放回路把档案条目原样发回 ISOLATED。
  const posted = [];
  sandbox.window.postMessage = (message, origin) => posted.push({ message, origin });
  const ok = internals.replayCapture({ nonce: sandbox.document.documentElement.dataset.shengjianMainNonce, videoId: "6666666666666666666" });
  assert.equal(ok, true);
  assert.equal(posted[0].message.items[0].gallery.length, 2);
});

// ---------------------------------------------------------------------------
// TASK-8.28：弹窗形态的未绑定媒体地址兜底——博主主页往回点较早条目时，
// 目标已滚出 20 槽档案，弹窗在播且地址新鲜（≤90s）即归因采用。
// ---------------------------------------------------------------------------
test("modal target may adopt a fresh unbound media url while playing", async () => {
  const content = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
  const sandbox = buildContentSandbox({ scannedSources: [] });
  sandbox.location.origin = "https://www.douyin.com";
  sandbox.location.pathname = "/user/self";
  sandbox.location.search = "?modal_id=7673838983419530149&showTab=post";
  sandbox.window.location = sandbox.location;
  sandbox.document.querySelector = () => ({ readyState: 4, paused: false });
  vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL("../browser-extension/douyin-hosts.js", import.meta.url), "utf8"), sandbox, { filename: "douyin-hosts.js" });
  vm.runInContext(content, sandbox, { filename: "content.js" });
  const internals = sandbox.__shengjianContentInternals;

  // 未绑定捕获：弹窗播放产生的 CDN 渐进地址（无 target 归属）。
  internals.rememberCapture("https://v26-web.douyinvod.com/video/tos/modal-stream.mp4?sig=1");
  assert.equal(internals.capturedMediaUrl("7673838983419530149"),
    "https://v26-web.douyinvod.com/video/tos/modal-stream.mp4?sig=1",
    "弹窗目标在播时采用新鲜未绑定地址");

  // 目标不符：别的 videoId 不借用（防下错视频）。
  assert.equal(internals.capturedMediaUrl("7673502504189108977"), "");
  internals.capturedMediaUrl("7673838983419530149");

  // 播放器暂停：归因条件失效。
  sandbox.document.querySelector = () => ({ readyState: 4, paused: true });
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "",
    "播放器未在播时不得采用未绑定地址");

  // 无 modal_id 的普通 /user 页面：维持原行为（不兜底）。
  sandbox.location.search = "";
  sandbox.document.querySelector = () => ({ readyState: 4, paused: false });
  assert.equal(internals.capturedMediaUrl("7673838983419530149"), "");
});
