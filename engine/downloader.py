# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""URL validation and yt-dlp adapter for browser-submitted media."""
from __future__ import annotations

import io
import shutil
import tempfile
import time
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Iterable, Protocol
from urllib.parse import urlparse

from .dns_fallback import install_dns_fallback


SUPPORTED_HOSTS = (
  "bilibili.com", "b23.tv", "douyin.com", "youtube.com", "youtu.be",
  "douyinvod.com", "douyinpic.com", "byteimg.com", "bytecdn.cn",
  "ibytedtos.com", "pstatp.com", "snssdk.com", "amemv.com",
)
MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
})
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
MAX_BROWSER_SESSION_COOKIES = 64
MAX_BROWSER_COOKIE_VALUE_LENGTH = 4096


class DownloadAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class DownloadCancelled(DownloadAdapterError):
    def __init__(self) -> None:
        super().__init__("cancelled", "下载已取消")


class HlsRedirectError(DownloadAdapterError):
    """签名播放地址 302 到了 HLS 主清单（抖音 play_addr 近期形态）。

    携带最终 URL 交协调器转投 yt-dlp 分片下载；不可作为单文件直下继续。
    """

    def __init__(self, final_url: str) -> None:
        super().__init__("hls_redirect", "媒体地址重定向到 HLS 主清单", retryable=True)
        self.final_url = final_url


class _EphemeralCookieStream(io.StringIO):
    """In-memory yt-dlp cookie source that is wiped when the task completes."""
    def truncate(self, size: int | None = None) -> int:
        # yt-dlp rewrites cookiefile streams on close. Match file truncation
        # semantics while keeping the write at offset zero after an earlier read.
        if size == 0:
            self.seek(0)
        return super().truncate(size)

    def erase(self) -> None:
        if self.closed:
            return
        length = len(self.getvalue())
        self.seek(0)
        self.write("\0" * length)
        self.seek(0)
        self.truncate(0)
        self.close()


DIRECT_MEDIA_HOSTS = frozenset({
  "douyinvod.com", "douyinpic.com", "byteimg.com", "bytecdn.cn",
  "ibytedtos.com", "pstatp.com", "snssdk.com", "amemv.com",
})


def is_direct_media_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return False
        if any(parsed.path.lower().endswith(ext) for ext in (
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".json", ".js", ".css",
            ".m3u8", ".mpd", ".ts", ".m4s",
        )):
            return False
        if any(host == suffix or host.endswith("." + suffix) for suffix in ("snssdk.com", "amemv.com")) \
                and not re.search(r"/(?:aweme/v1/play|video/play|video/download|play/?)(?:[/?]|$)",
                                  f"{parsed.path}{parsed.query}", re.I):
            return False
        if any(host == suffix or host.endswith("." + suffix) for suffix in DIRECT_MEDIA_HOSTS):
            return True
        return (host == "douyin.com" or host.endswith(".douyin.com")) and bool(re.search(
            r"/(?:aweme/v1/play|video/play|video/download|play/?)(?:[/?]|$)",
            f"{parsed.path}{parsed.query}", re.I,
        ))
    except ValueError:
        return False


def is_hls_media_url(value: str) -> bool:
    """抖音 play_addr 近期形态：HLS 主清单而非渐进 MP4。

    主清单不可单文件直下（那只会存下播放列表文本），必须交 yt-dlp 分片
    下载并按需 ffmpeg 合流；签名直链仍是 Referer 守护、无需 Cookie。
    """
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return False
        if not parsed.path.lower().endswith(".m3u8"):
            return False
        if any(host == suffix or host.endswith("." + suffix) for suffix in DIRECT_MEDIA_HOSTS):
            return True
        return host == "douyin.com" or host.endswith(".douyin.com")
    except ValueError:
        return False


def is_play_endpoint_url(value: str) -> bool:
    """抖音 /aweme/v1/play/ 等播放端点：Cookie 门禁，裸 GET 必 403。

    这类地址不是 CDN 签名直链，必须带上浏览器登录态才能取流。无会话时
    应立刻失败并交扩展 chrome.downloads 通道（浏览器网络栈自带 Cookie），
    而不是在本地用同一 URL 反复撞 403。
    """
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return False
        if not (
            host == "douyin.com" or host.endswith(".douyin.com")
            or any(host == suffix or host.endswith("." + suffix) for suffix in ("snssdk.com", "amemv.com"))
        ):
            return False
        return bool(re.search(
            r"/(?:aweme/v1/play|video/play|video/download|play/?)(?:[/?]|$)",
            f"{parsed.path}{parsed.query}",
            re.I,
        ))
    except ValueError:
        return False


def is_browser_download_url(value: str) -> bool:
    """Return true only for a safe URL Chrome may download directly.

    Douyin player URLs are accepted in addition to media CDN hosts. The
    browser may attach its own Douyin cookies to those requests; FoilPage
    never passes cookies to the downloads API.
    """
    if is_direct_media_url(value):
        return True
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return False
        if host != "douyin.com" and not host.endswith(".douyin.com"):
            return False
        return bool(re.search(
            r"/(?:aweme/v1/play|video/play|video/download|play/?)(?:[/?]|$)",
            f"{parsed.path}{parsed.query}",
            re.I,
        ))
    except ValueError:
        return False


def sanitize_download_filename(value: str, fallback: str = "douyin-video") -> str:
    """Return a Windows-safe filename without the extension."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    cleaned = cleaned[:120].strip(" .") or fallback
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned


# TASK-8.17 封面：抖音封面由扩展从 aweme 提取（图片 CDN 域收紧），yt-dlp
# 轨道的缩略图来自 extractor info。两套白名单都比媒体直链更窄——封面只是
# 增强项，宁可少取不可放大可请求面。
COVER_IMAGE_HOST_SUFFIXES = ("douyinpic.com", "byteimg.com", "pstatp.com")
EXTRACTOR_THUMBNAIL_HOST_SUFFIXES = (
    "douyinpic.com", "byteimg.com", "pstatp.com", "hdslb.com", "ytimg.com",
)
MAX_COVER_BYTES = 8 * 1024 * 1024


def _host_matches(value: str, suffixes: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and bool(host) and any(
            host == suffix or host.endswith("." + suffix) for suffix in suffixes
        )
    except ValueError:
        return False


def is_douyin_cover_url(value: str) -> bool:
    """扩展上送的抖音封面地址：https 且落在图片 CDN 域内。"""
    return len(value) <= 4096 and _host_matches(value, COVER_IMAGE_HOST_SUFFIXES)


def is_extractor_thumbnail_url(value: str) -> bool:
    """yt-dlp info 里的缩略图地址（B 站/YouTube/抖音图片 CDN）。"""
    return len(value) <= 4096 and _host_matches(value, EXTRACTOR_THUMBNAIL_HOST_SUFFIXES)


def download_cover_image(
    url: str,
    destination: Path,
    *,
    referer: str = "",
    user_agent: str = "",
    proxy_route: str | None = None,
) -> Path:
    """Fetch one platform cover image to ``destination`` (a *.jpg file path).

    封面是增强项：调用方（下载协调器/适配器）捕获这里的失败并降级为
    「无封面」，绝不影响主任务。Referer 只对抖音详情页放行，与媒体直下
    同策略。
    """
    if not is_douyin_cover_url(url) and not is_extractor_thumbnail_url(url):
        raise DownloadAdapterError("invalid_cover_url", "封面地址不受支持", retryable=False)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": user_agent.strip()[:512]
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "image/*,*/*;q=0.8",
    }
    if referer.startswith("https://www.douyin.com/") or referer.startswith("https://douyin.com/"):
        headers["Referer"] = referer[:2048]
    install_dns_fallback()
    with _open_media(url, headers, proxy_route=proxy_route) as response:
        data = response.read(MAX_COVER_BYTES + 1)
    if not data or len(data) > MAX_COVER_BYTES:
        raise DownloadAdapterError("cover_download_failed", "封面内容为空或超出大小限制")
    destination.write_bytes(data)
    return destination


def is_douyin_detail_url(value: str) -> bool:
    """Return true only for a stable Douyin video detail page URL."""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not (host == "douyin.com" or host.endswith(".douyin.com")):
            return False
        return bool(re.fullmatch(r"/(?:video|share/video)/\d{10,24}/?", parsed.path))
    except ValueError:
        return False


def sanitize_douyin_session_cookies(value: object) -> tuple[dict[str, object], ...]:
    """Validate an extension-supplied, one-use Douyin cookie set.

    The result is deliberately caller-owned transient data. It must never be
    included in a DownloadJob or written to the download database.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BROWSER_SESSION_COOKIES:
        raise DownloadAdapterError(
            "invalid_browser_session", "浏览器登录信息无效，请回到当前视频重新提交",
        )
    cleaned: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DownloadAdapterError("invalid_browser_session", "浏览器登录信息格式无效")
        name = item.get("name")
        cookie_value = item.get("value")
        domain = item.get("domain")
        path = item.get("path", "/")
        if (
            not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
            or not isinstance(cookie_value, str) or not cookie_value
            or len(cookie_value) > MAX_BROWSER_COOKIE_VALUE_LENGTH
            or _CONTROL_CHARACTERS.search(cookie_value)
            or not isinstance(domain, str)
        ):
            raise DownloadAdapterError("invalid_browser_session", "浏览器登录信息格式无效")
        normalized_domain = domain.strip().lower()
        domain_without_dot = normalized_domain.lstrip(".")
        if not (domain_without_dot == "douyin.com" or domain_without_dot.endswith(".douyin.com")):
            raise DownloadAdapterError("invalid_browser_session", "浏览器登录信息不属于抖音")
        if (
            not isinstance(path, str) or not path.startswith("/")
            or len(path) > 1024 or _CONTROL_CHARACTERS.search(path)
        ):
            raise DownloadAdapterError("invalid_browser_session", "浏览器登录信息格式无效")
        expires = item.get("expirationDate")
        if expires is not None and (
            not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires < 0
        ):
            raise DownloadAdapterError("invalid_browser_session", "浏览器登录信息格式无效")
        cleaned.append({
            "name": name,
            "value": cookie_value,
            "domain": normalized_domain,
            "path": path,
            "secure": bool(item.get("secure", False)),
            "expirationDate": int(expires) if expires is not None else None,
        })
    return tuple(cleaned)


_NO_PROXY_OPENER: urllib.request.OpenerDirector | None = None


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    global _NO_PROXY_OPENER
    if _NO_PROXY_OPENER is None:
        _NO_PROXY_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        )
    return _NO_PROXY_OPENER


def _open_media(url: str, headers: dict[str, str], *, proxy_route: str | None = None):
    """Open a player-CDN request under the configured proxy route.

    urllib honours the system proxy by default. When the proxy process is
    down but its Windows/registry setting lingers (common while a local proxy
    tool restarts), every default request is refused on loopback and the
    browser-visible network looks fine — the user just sees "网络问题".
    Douyin's media CDNs are domestically reachable, so one proxy-less retry
    rescues that state without affecting setups that genuinely need a proxy.

    ``proxy_route`` 语义同 ``proxy_route_for_platform``：None=默认（系统代
    理＋网络类失败直连救援，即上述原有行为）；""=强制直连（国内站默认，
    直接跳过系统代理首次尝试）；http(s) URL=强制经该代理（失败仍回退直
    连）。SOCKS 代理 urllib 不支持，遇到时按 None 处理——那类地址只有
    yt-dlp 路径能用，设置校验仍接受。

    A fresh Request must be built for the retry: the proxied opener rewrites
    the original Request in place (set_proxy), so reusing it would connect
    straight to the dead proxy address again.
    """
    if proxy_route and not proxy_route.startswith(("http://", "https://")):
        proxy_route = None
    if proxy_route == "":
        request = urllib.request.Request(url, headers=headers, method="GET")
        return _no_proxy_opener().open(request, timeout=30)
    if proxy_route:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_route, "https": proxy_route}),
        )
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return opener.open(request, timeout=30)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            if not _is_network_like_error(exc, str(exc).casefold()):
                raise
            retry = urllib.request.Request(url, headers=headers, method="GET")
            return _no_proxy_opener().open(retry, timeout=30)
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        return urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError:
        # The server answered (through whatever proxy was configured); a
        # proxy-less retry would only hit the same origin policy.
        raise
    except Exception as exc:
        if not _is_network_like_error(exc, str(exc).casefold()):
            raise
        retry = urllib.request.Request(url, headers=headers, method="GET")
        return _no_proxy_opener().open(retry, timeout=30)


def download_direct_media(
    url: str,
    destination: Path,
    *,
    referer: str = "",
    user_agent: str = "",
    session_cookies: Iterable[Mapping[str, object]] | None = None,
    rate_limit_bps: int | None,
    progress: Callable[[int, int | None], None],
    cancel_check: Callable[[], bool],
    attempts: int = 3,
    proxy_route: str | None = None,
) -> Path:
    """Download one signed player URL with browser-like headers and retries."""
    if not is_direct_media_url(url):
        raise DownloadAdapterError("invalid_url", "不是受支持的播放器媒体地址")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS or suffix in {".m3u8", ".mpd"}:
        suffix = ".mp4"
    staging = Path(tempfile.mkdtemp(prefix=".direct-media-", dir=destination))
    target = destination / f"browser-media-{int(time.time() * 1000)}{suffix}"
    headers = {
        "User-Agent": user_agent.strip()[:512] or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    if referer.startswith("https://www.douyin.com/") or referer.startswith("https://douyin.com/"):
        headers["Referer"] = referer[:2048]
    parsed_url = urlparse(url)
    request_host = (parsed_url.hostname or "").lower().rstrip(".")
    request_path = parsed_url.path or "/"
    cookie_items = []
    for item in tuple(session_cookies or ()):
        raw_domain = str(item.get("domain") or "").strip().lower()
        domain_cookie = raw_domain.startswith(".")
        cookie_domain = raw_domain.lstrip(".").rstrip(".")
        domain_matches = bool(cookie_domain) and (
            request_host == cookie_domain
            or (domain_cookie and request_host.endswith(f".{cookie_domain}"))
        )
        cookie_path = str(item.get("path") or "/")
        path_matches = request_path == cookie_path or (
            request_path.startswith(cookie_path.rstrip("/") + "/")
            if cookie_path != "/" else True
        )
        if domain_matches and path_matches and (not item.get("secure") or parsed_url.scheme == "https"):
            cookie_items.append(item)
    if cookie_items:
        headers["Cookie"] = "; ".join(
            f"{item['name']}={item['value']}" for item in cookie_items
            if item.get("name") and item.get("value")
        )[:12000]
    try:
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            if cancel_check():
                raise DownloadCancelled()
            try:
                with _open_media(url, headers, proxy_route=proxy_route) as response:
                    final_url = response.geturl()
                    if is_hls_media_url(final_url):
                        raise HlsRedirectError(final_url)
                    if not is_direct_media_url(final_url):
                        raise DownloadAdapterError("media_redirect_denied", "媒体地址重定向到未授权域名")
                    status = int(getattr(response, "status", 200) or 200)
                    if status not in (200, 206):
                        raise DownloadAdapterError("media_http_error", f"媒体服务器返回 HTTP {status}", retryable=True)
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    # 无后缀的带签 HLS 地址靠 Content-Type 识别（mpegurl）。
                    if "mpegurl" in content_type:
                        raise HlsRedirectError(response.geturl())
                    if (
                        content_type.startswith("text/")
                        or "json" in content_type
                        or "html" in content_type
                        or "dash+xml" in content_type
                    ):
                        raise DownloadAdapterError("media_not_video", "媒体服务器返回的不是视频文件", retryable=True)
                    total_header = response.headers.get("Content-Length")
                    total = int(total_header) if total_header and total_header.isdigit() else None
                    part = staging / "media.part"
                    done = 0
                    with part.open("wb") as stream:
                        while True:
                            if cancel_check():
                                raise DownloadCancelled()
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            stream.write(chunk)
                            done += len(chunk)
                            progress(done, total)
                            if rate_limit_bps:
                                time.sleep(len(chunk) / rate_limit_bps)
                    if done <= 0:
                        raise DownloadAdapterError("media_empty", "媒体服务器返回空文件", retryable=True)
                    part.replace(target)
                    progress(done, done if total is None else max(done, total))
                    return target
            except DownloadCancelled:
                raise
            except DownloadAdapterError as exc:
                # HLS 重定向、403/401 等已有明确下游路径的结局不能拿同一
                # URL 再撞一遍；由协调器 / 扩展浏览器回退决定下一步。
                if exc.code in ("hls_redirect", "media_forbidden") or not exc.retryable:
                    raise
                last_error = exc
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 410):
                    # 过期签名、IP 绑定或 Cookie 门禁：同 URL 重试必败。
                    # 快速失败（retryable=False）才能让扩展立刻改走
                    # chrome.downloads（带真实登录态 / 重新取直链）。
                    raise DownloadAdapterError(
                        "media_forbidden",
                        f"媒体服务器拒绝了该播放地址（HTTP {exc.code}）；可能是签名过期或请求来源校验，不能据此判断账号登录状态",
                        retryable=False,
                    ) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(0.35 * (2 ** attempt))
        if isinstance(last_error, DownloadAdapterError):
            raise last_error
        raise DownloadAdapterError(
            "media_download_failed",
            f"播放器媒体下载失败: {last_error}",
            retryable=True,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _is_network_like_error(exc: Exception, lowered_message: str) -> bool:
    """True when an exception is a transient network/upstream failure rather
    than an authorization or content problem.

    yt-dlp surfaces network trouble as URLError/TimeoutError subclasses or as
    message text such as "timed out" / "Connection reset" / "HTTP Error 5xx".
    Treating these as authorization failures produced the misleading
    "当前 Chrome 的抖音登录态未被平台接受" message on flaky networks.

    403/412 are NOT treated as network errors: 403 usually means the platform
    is blocking this request shape (anti-bot), and 412 has its own rate-limit
    branch below. File-system OSErrors (small errno, e.g. 2/13/22) are not
    network failures either.
    """
    import urllib.error as _urllib_error

    if isinstance(exc, (_urllib_error.URLError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        # Windows WSA errors (100xx) and POSIX socket errno (104/110/111/113)
        # are network trouble; small errno values are filesystem errors.
        if isinstance(errno, int):
            return errno >= 100
        return False
    markers = (
        "timed out", "timeout", "connection reset", "connection refused",
        "connection aborted", "name or service not known",
        "temporary failure in name resolution", "network is unreachable",
        "no route to host", "server disconnected", "remote end closed",
        "connection closed unexpectedly", "http error 5", "http error 429",
        "too many requests", "temporarily unavailable", "service unavailable",
        "errno 110", "errno 1006", "errno 10054",
    )
    return any(marker in lowered_message for marker in markers)


def normalized_download_error(
    exc: Exception, *, browser_session_supplied: bool = False, platform: str = "",
) -> DownloadAdapterError:
    message = str(exc)
    lowered = message.casefold()
    # 网络层/瞬时错误必须与授权类错误分开归类：网络抖动、超时、DNS、
    # 平台 5xx/429 都只是「暂时不可用」，重试可能成功；把它们归为
    # authorization_required 会误导用户去刷新登录，且 retryable=False
    # 让任务无法自动重试。
    if _is_network_like_error(exc, lowered):
        return DownloadAdapterError(
            "network_error",
            "网络连接不稳定或平台暂时无响应，请稍后重试",
            retryable=True,
        )
    if "fresh cookies" in lowered:
        if platform == "douyin":
            return DownloadAdapterError(
                "douyin_verification_required",
                "抖音详情解析被平台的网页校验拦截（yt-dlp 要求 Fresh cookies）；"
                "这不代表账号一定已退出。请刷新当前视频页并让视频播放后重试；"
                "若重复出现，说明当前抖音校验暂不兼容本机解析器。",
                retryable=False,
            )
        return DownloadAdapterError(
            "authorization_required",
            (
                "抖音解析失败（可能是网络波动或登录状态已过期）；请回到视频页"
                "让视频播放后重新点击下载，若仍失败请刷新抖音页面后重试"
                if browser_session_supplied else
                "已获取视频详情链接，但平台要求登录；可在设置 - 浏览器启用兼容下载"
            ),
            retryable=False,
        )
    if any(marker in lowered for marker in (
        "login required", "log in to access", "only available for registered users",
        "会员", "大会员", "需要登录", "登录后观看",
    )):
        return DownloadAdapterError(
            "authorization_required",
            "该视频需要登录或会员权限，请先在浏览器确认账号可正常播放后重试",
            retryable=False,
        )
    if "412" in lowered or "precondition failed" in lowered:
        return DownloadAdapterError(
            "source_rate_limited",
            "B 站暂时拒绝了请求（HTTP 412），请稍后重试或先在浏览器登录后再下载",
            retryable=True,
        )
    if any(marker in lowered for marker in (
        "sign in to confirm", "confirm you're not a bot", "确认您不是机器人",
    )):
        return DownloadAdapterError(
            "authorization_required",
            "平台要求验证非机器人访问；请在浏览器登录该平台后重试，或在设置中配置 JavaScript 运行时",
            retryable=False,
        )
    if any(marker in lowered for marker in (
        "javascript runtime", "challenge solver", "signature solving failed",
    )):
        return DownloadAdapterError(
            "js_runtime_missing",
            "平台升级了反爬验证，需要 JavaScript 运行时（推荐 Deno）。"
            "请安装 Deno（https://docs.deno.com）或在设置中指定其路径后重试",
            retryable=True,
        )
    if platform == "youtube" and ("http error 403" in lowered or "forbidden" in lowered):
        # YouTube throttled/403 on an unsolved challenge URL is the typical
        # symptom of a missing JS runtime, not a transient network fault.
        return DownloadAdapterError(
            "js_runtime_missing",
            "YouTube 拒绝了下载请求（HTTP 403），通常因缺少 JavaScript 运行时。"
            "请安装 Deno（https://docs.deno.com）或在设置中指定其路径后重试",
            retryable=True,
        )
    if any(marker in lowered for marker in (
        "private video", "video is unavailable", "稿件不存在", "视频不存在",
        "该视频已失效", "视频已被删除",
    )):
        return DownloadAdapterError(
            "source_unavailable", "该视频不存在、已删除或当前不可访问", retryable=False,
        )
    if "unsupported url" in lowered:
        return DownloadAdapterError(
            "unsupported_source",
            "未识别到可下载的视频详情链接，请回到视频画面重新添加",
            retryable=False,
        )
    if getattr(exc, "errno", None) == 22 or "invalid argument" in lowered:
        return DownloadAdapterError(
            "invalid_filename",
            "平台标题包含 Windows 不支持的文件名字符，已清理；请重试任务",
            retryable=True,
        )
    return DownloadAdapterError("download_failed", message, retryable=True)


class DownloadAdapter(Protocol):
    def resolve(
        self,
        url: str,
        *,
        referer: str = "",
        user_agent: str = "",
        session_cookies: Iterable[Mapping[str, object]] | None = None,
        js_runtime_path: str = "",
        proxy_policy: str = "auto",
        proxy_url: str = "",
    ) -> dict[str, str]: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool,
        rate_limit_bps: int | None,
        referer: str = "",
        user_agent: str = "",
        session_cookies: Iterable[Mapping[str, object]] | None = None,
        progress: Callable[[int, int | None], None],
        cancel_check: Callable[[], bool],
        js_runtime_path: str = "",
        proxy_policy: str = "auto",
        proxy_url: str = "",
        with_cover: bool = False,
    ) -> Path: ...


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "b23.tv" or host.endswith(".bilibili.com") or host == "bilibili.com":
        return "bilibili"
    if (
        host == "douyin.com" or host.endswith(".douyin.com")
        or any(host == suffix or host.endswith("." + suffix) for suffix in (
            "douyinvod.com", "douyinpic.com", "byteimg.com", "bytecdn.cn",
            "ibytedtos.com", "pstatp.com",
        ))
    ):
        return "douyin"
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    return "unknown"


# 国内平台的媒体/解析端点本土直连即可达；经系统代理（尤其 VPN 境外出口）
# 反而是故障面——B 站会以 412 风控拒绝境外出口，VPN 的 DNS 黑洞另由
# dns_fallback 兜底。境外平台（YouTube）默认尊重系统代理。
DOMESTIC_PLATFORMS = frozenset({"bilibili", "douyin"})

_PROXY_URL_RE = re.compile(
    r"^(?:http|https|socks4|socks5|socks5h)://[A-Za-z0-9._~!$&*()\',;=:@%-]+(?::\d{1,5})?(?:/[!-~]*)?$"
)


def proxy_route_for_platform(
    platform: str, *, policy: str = "auto", custom_url: str = "",
) -> str | None:
    """按平台与设置策略返回 yt-dlp ``proxy`` 选项值。

    语义（对齐 yt-dlp YoutubeDL.proxies 的三态）：
    - ``None``：不设该选项，走系统/环境代理；
    - ``""``：强制直连（yt-dlp 内部映射为 ``__noproxy__``）；
    - 代理 URL：强制经该代理。

    策略：auto=国内平台直连、境外平台走系统代理（默认）；direct=全部
    直连；system=全部走系统代理；custom=全部走自定义代理（地址为空或
    形态非法时退回系统代理，与设置层的宽松保存一致）。
    """
    if policy == "direct":
        return ""
    if policy == "custom":
        url = str(custom_url or "").strip()
        return url if _PROXY_URL_RE.match(url) else None
    if policy == "system":
        return None
    # auto（含未知策略值）：国内直连，境外尊重系统代理。
    return "" if platform in DOMESTIC_PLATFORMS else None


def validate_supported_url(value: str, *, allow_any_url: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise DownloadAdapterError("invalid_url", "链接格式无效")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DownloadAdapterError("invalid_url", "只接受 http/https 视频链接")
    host = parsed.hostname.lower().rstrip(".")
    if not allow_any_url:
        allowed = any(host == domain or host.endswith("." + domain) for domain in SUPPORTED_HOSTS)
        if not allowed:
            raise DownloadAdapterError("unsupported_source", "当前链接来源尚未启用")
    if parsed.username or parsed.password:
        raise DownloadAdapterError("invalid_url", "链接不能包含账号信息")
    return parsed.geturl()


# yt-dlp supports external JavaScript runtimes for anti-bot challenges
# (YouTube PoToken/nsig). Runtime type is derived from the executable name;
# anything unrecognized falls back to deno, the upstream default.
_KNOWN_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def js_runtime_options(js_runtime_path: str) -> dict:
    """Map a configured runtime executable to yt-dlp's ``js_runtimes`` option."""
    path = str(js_runtime_path or "").strip()
    if not path:
        return {}
    name = Path(path).stem.lower()
    if name not in _KNOWN_JS_RUNTIMES:
        name = "deno"
    return {"js_runtimes": {name: {"path": path}}}


class WarningCollector:
    """Minimal yt-dlp logger that keeps warnings for post-flight inspection.

    ``quiet``/``no_warnings`` suppress console output; supplying this logger
    lets the adapter still notice degradations such as a missing JavaScript
    runtime and surface them in the application log.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        text = str(msg)
        self.messages.append(text)
        print(f"[yt-dlp] 警告: {text}", flush=True)

    def error(self, msg: str) -> None:
        print(f"[yt-dlp] 错误: {msg}", flush=True)


class YtDlpAdapter:
    @staticmethod
    def _format_selector(url: str, *, audio_only: bool) -> str:
        """Return a selector that keeps B 站 DASH audio instead of a silent video track."""
        if audio_only:
            return "bestaudio/best"
        if detect_platform(url) in ("bilibili", "youtube"):
            # 两个平台都以 DASH（音轨分离）为主：优先兼容的 MP4+M4A 组合
            # （YouTube 最高画质只有分离轨），退到渐进式 MP4，最后交回
            # yt-dlp 兜底。合并依赖 ffmpeg，download() 会传入 ffmpeg_location。
            return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        return "best[ext=mp4]/best"

    def resolve(
        self,
        url: str,
        *,
        referer: str = "",
        user_agent: str = "",
        session_cookies: Iterable[Mapping[str, object]] | None = None,
        js_runtime_path: str = "",
        proxy_policy: str = "auto",
        proxy_url: str = "",
    ) -> dict[str, str]:
        """Extract one direct player URL without downloading media."""
        try:
            import yt_dlp
        except ImportError as exc:
            raise DownloadAdapterError("downloader_missing", "yt-dlp 未安装") from exc
        # VPN/过滤工具可能把平台域名在系统 DNS 层黑洞（2026-10-19 实机 bilibili）；
        # 正常环境透传零影响，被劫持时经 DoH 补出真实地址。
        install_dns_fallback()

        platform = detect_platform(url)
        staging = Path(tempfile.mkdtemp(prefix=".resolve-"))
        options = {
            "format": self._format_selector(url, audio_only=False),
            "skip_download": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 4,
            "extractor_retries": 3,
            "fragment_retries": 4,
            "file_access_retries": 3,
            "windowsfilenames": True,
        }
        options.update(js_runtime_options(js_runtime_path))
        # 分域路由：国内站强制直连（VPN/系统代理是 B 站 412 与 DNS 黑洞的
        # 主要故障面），境外站默认尊重系统代理；详见 proxy_route_for_platform。
        proxy_route = proxy_route_for_platform(platform, policy=proxy_policy, custom_url=proxy_url)
        if proxy_route is not None:
            options["proxy"] = proxy_route
        session_cookie_items = tuple(session_cookies or ())
        cookie_stream: _EphemeralCookieStream | None = None
        if session_cookie_items:
            cookie_stream = self._write_cookie_stream(session_cookie_items)
            options["cookiefile"] = cookie_stream
        headers: dict[str, str] = {}
        if user_agent.strip():
            headers["User-Agent"] = user_agent.strip()[:512]
        if referer.startswith("https://"):
            headers["Referer"] = referer[:2048]
        if detect_platform(url) == "bilibili":
            headers.setdefault(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "Chrome/131.0.0.0 Safari/537.36",
            )
            headers.setdefault("Referer", "https://www.bilibili.com/")
        if headers:
            options["http_headers"] = headers
        try:
            try:
                warnings = WarningCollector()
                options["logger"] = warnings
                with yt_dlp.YoutubeDL(options) as client:
                    info = client.extract_info(url, download=False)
                if platform == "youtube":
                    self._report_js_runtime_warnings(warnings)
            except Exception as exc:
                raise normalized_download_error(
                    exc, browser_session_supplied=bool(session_cookie_items),
                    platform=platform,
                ) from exc
        finally:
            if cookie_stream:
                cookie_stream.erase()
            session_cookie_items = ()
            shutil.rmtree(staging, ignore_errors=True)

        if not isinstance(info, dict):
            raise DownloadAdapterError(
                "resolve_failed", "平台返回的解析结果无效", retryable=True,
            )
        download_url = self._selected_media_url(info)
        if not download_url or not is_browser_download_url(download_url):
            raise DownloadAdapterError(
                "resolve_failed",
                "未能解析到可直接下载的视频文件；请回到视频页面重新提交",
                retryable=True,
            )
        stem = sanitize_download_filename(str(info.get("title") or ""))
        suffix = Path(urlparse(download_url).path).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            suffix = ".mp4"
        # The Chrome downloads API only accepts XHR-safe headers, so the
        # browser-handoff contract must not advertise Referer/User-Agent.
        resolved_headers = {
            "Accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.8",
        }
        return {
            "download_url": download_url,
            "filename": f"{stem}{suffix}",
            "headers": resolved_headers,
        }

    @staticmethod
    def _selected_media_url(info: dict) -> str:
        direct = str(info.get("url") or "").strip()
        if direct and is_browser_download_url(direct):
            return direct
        formats = info.get("formats")
        if not isinstance(formats, list):
            return ""
        candidates = [item for item in formats if isinstance(item, dict) and item.get("url")]
        for field, wanted in (("ext", "mp4"), ("vcodec", "h264")):
            for item in candidates:
                url = str(item.get("url") or "")
                if item.get(field) == wanted and is_browser_download_url(url):
                    return url
        for item in sorted(
            candidates,
            key=lambda entry: float(entry.get("tbr") or 0),
            reverse=True,
        ):
            url = str(item.get("url") or "")
            if is_browser_download_url(url):
                return url
        return ""

    def download(
        self,
        url: str,
        destination: Path,
        *,
        audio_only: bool,
        rate_limit_bps: int | None,
        referer: str = "",
        user_agent: str = "",
        session_cookies: Iterable[Mapping[str, object]] | None = None,
        progress: Callable[[int, int | None], None],
        cancel_check: Callable[[], bool],
        js_runtime_path: str = "",
        proxy_policy: str = "auto",
        proxy_url: str = "",
        with_cover: bool = False,
    ) -> Path:
        try:
            import yt_dlp
        except ImportError as exc:
            raise DownloadAdapterError("downloader_missing", "yt-dlp 未安装") from exc
        install_dns_fallback()

        platform = detect_platform(url)
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".download-", dir=destination))

        def hook(data: dict) -> None:
            if cancel_check():
                raise DownloadCancelled()
            if data.get("status") == "downloading":
                done = int(data.get("downloaded_bytes") or 0)
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                progress(done, int(total) if total else None)

        options = {
            # Prefer a single progressive MP4. Douyin commonly offers one;
            # selecting separate tracks adds a second transfer and FFmpeg merge
            # before the user can see the downloaded file.
            "format": self._format_selector(url, audio_only=audio_only),
            "outtmpl": str(staging / "%(title).80s-%(id)s.%(ext)s"),
            "windowsfilenames": True,
            "noplaylist": True,
            "progress_hooks": [hook],
            "quiet": True,
            "no_warnings": True,
            "overwrites": False,
            "continuedl": True,
            "retries": 4,
            "extractor_retries": 3,
            "fragment_retries": 8,
            "file_access_retries": 3,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
        }
        options.update(js_runtime_options(js_runtime_path))
        # 分域路由（同 resolve）：国内站直连优先，境外站尊重系统代理/自定义。
        proxy_route = proxy_route_for_platform(platform, policy=proxy_policy, custom_url=proxy_url)
        if proxy_route is not None:
            options["proxy"] = proxy_route
        session_cookie_items = tuple(session_cookies or ())
        cookie_stream: _EphemeralCookieStream | None = None
        if session_cookie_items:
            cookie_stream = self._write_cookie_stream(session_cookie_items)
            options["cookiefile"] = cookie_stream
        headers: dict[str, str] = {}
        if user_agent.strip():
            headers["User-Agent"] = user_agent.strip()[:512]
        if referer.startswith("https://"):
            headers["Referer"] = referer[:2048]
        if detect_platform(url) == "bilibili":
            headers.setdefault(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "Chrome/131.0.0.0 Safari/537.36",
            )
            headers.setdefault("Referer", "https://www.bilibili.com/")
        if headers:
            options["http_headers"] = headers
        if rate_limit_bps:
            options["ratelimit"] = int(rate_limit_bps)
        # DASH/HLS 音视频分离轨（B 站/YouTube 高画质、抖音 HLS 主清单）需要
        # ffmpeg 合并。打包后 ffmpeg 不在系统 PATH，显式把 audio.get_ffmpeg
        # 的解析链交给 yt-dlp；找不到时保持缺省（纯渐进式格式仍可下载，
        # 合并轨由 yt-dlp 报错）。
        if detect_platform(url) in ("bilibili", "youtube", "douyin"):
            try:
                from . import audio as audio_engine

                options["ffmpeg_location"] = audio_engine.get_ffmpeg()
            except Exception:
                pass
        last_warnings: WarningCollector | None = None
        def extract(outtmpl: str) -> object:
            nonlocal last_warnings
            last_warnings = WarningCollector()
            options["outtmpl"] = outtmpl
            options["logger"] = last_warnings
            with yt_dlp.YoutubeDL(options) as client:
                return client.extract_info(url, download=True)

        try:
            # 策略已强制直连时，直连重试分支自然跳过（重复设置无意义）。
            used_direct = proxy_route == ""
            used_combined = False
            used_safe_filename = False
            template = str(staging / "%(title).80s-%(id)s.%(ext)s")
            while True:
                if cancel_check():
                    raise DownloadCancelled()
                try:
                    info = extract(template)
                    if platform == "youtube":
                        self._report_js_runtime_warnings(last_warnings)
                    break
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    classified = normalized_download_error(exc, browser_session_supplied=bool(session_cookie_items), platform=platform)
                    format_failure = any(value in str(exc).lower() for value in ("requested format is not available", "ffmpeg is not installed", "ffmpeg not found", "unable to merge"))
                    # Bilibili can reject a proxy exit with HTTP 412 while
                    # the same public video remains available over direct access.
                    direct_route_failure = classified.code == "network_error" or (
                        platform == "bilibili" and classified.code == "source_rate_limited"
                    )
                    if direct_route_failure and not used_direct and urllib.request.getproxies():
                        options["proxy"] = ""
                        used_direct = True
                    elif format_failure and not used_combined and platform in {"bilibili", "youtube"}:
                        options["format"] = "bestaudio/best" if audio_only else "best[ext=mp4]/best"
                        used_combined = True
                    elif self._is_invalid_filename_error(exc) and not used_safe_filename:
                        used_safe_filename = True
                        shutil.rmtree(staging, ignore_errors=True)
                        staging.mkdir(parents=True, exist_ok=True)
                    else:
                        raise
                    template = str(staging / "%(id)s.%(ext)s")

            if cancel_check():
                raise DownloadCancelled()
            candidates = self._media_candidates(staging, info)
            if not candidates:
                raise DownloadAdapterError("download_failed", "下载完成但未找到媒体文件", retryable=True)
            source = candidates[0]
            target = destination / source.name
            if target.exists():
                target = destination / f"{source.stem}-{staging.name[-8:]}{source.suffix}"
            source.replace(target)
            if with_cover:
                # TASK-8.17：缩略图与媒体同名落位（<stem>.jpg）。任何失败都
                # 只降级为无封面，绝不让增强项拖垮主下载。
                thumbnail = str((info or {}).get("thumbnail") or "")
                try:
                    if is_extractor_thumbnail_url(thumbnail):
                        download_cover_image(
                            thumbnail, destination / f"{target.stem}.jpg",
                            referer=referer, user_agent=user_agent,
                            proxy_route=proxy_route_for_platform(
                                platform, policy=proxy_policy, custom_url=proxy_url,
                            ),
                        )
                except Exception:
                    pass
            progress(target.stat().st_size, target.stat().st_size)
            return target
        except DownloadAdapterError:
            raise
        except Exception as exc:
            raise normalized_download_error(
                exc, browser_session_supplied=bool(session_cookie_items),
                platform=platform,
            ) from exc
        finally:
            if cookie_stream:
                cookie_stream.erase()
            session_cookie_items = ()
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _report_js_runtime_warnings(warnings: "WarningCollector | None") -> None:
        """Make YouTube JS-runtime degradation visible in the app log.

        yt-dlp reports a missing runtime as a warning and continues with
        fewer formats; without this the task can look 'successful' while
        the user gets a throttled or silent-audio result.
        """
        if not warnings:
            return
        for message in warnings.messages:
            if "javascript runtime" in message.casefold() or "signature solving" in message.casefold():
                print(
                    "[下载] YouTube 反爬验证未完全通过：未检测到可用的 "
                    "JavaScript 运行时。建议安装 Deno（https://docs.deno.com）"
                    "并在 设置 → 下载 中指定路径",
                    flush=True,
                )
                return

    @staticmethod
    def _is_invalid_filename_error(exc: Exception) -> bool:
        return getattr(exc, "errno", None) == 22 or "invalid argument" in str(exc).casefold()

    @staticmethod
    def _write_cookie_stream(
        session_cookies: Iterable[Mapping[str, object]],
    ) -> _EphemeralCookieStream:
        stream = _EphemeralCookieStream()
        stream.write("# Netscape HTTP Cookie File\n# This file is generated by yt-dlp.  Do not edit.\n\n")
        for item in session_cookies:
            domain = str(item["domain"])
            expires = item.get("expirationDate")
            fields = (
                domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                str(item["path"]),
                "TRUE" if bool(item.get("secure", False)) else "FALSE",
                str(int(expires)) if isinstance(expires, (int, float)) else "",
                str(item["name"]),
                str(item["value"]),
            )
            stream.write("\t".join(fields) + "\n")
        stream.seek(0)
        return stream

    @staticmethod
    def _media_candidates(staging: Path, info: object) -> list[Path]:
        # Both sides of containment must use the same canonical path namespace,
        # including Windows 8.3 names; resolved links outside staging stay denied.
        staging = staging.resolve()
        hinted: list[Path] = []
        if isinstance(info, dict):
            for field in ("filepath", "_filename"):
                value = info.get(field)
                if isinstance(value, str):
                    hinted.append(Path(value))
            downloads = info.get("requested_downloads")
            if isinstance(downloads, list):
                for download in downloads:
                    if not isinstance(download, dict):
                        continue
                    for field in ("filepath", "filename"):
                        value = download.get(field)
                        if isinstance(value, str):
                            hinted.append(Path(value))

        discovered = [path for path in staging.iterdir() if path.is_file()]
        unique: dict[Path, Path] = {}
        for path in (*hinted, *discovered):
            try:
                resolved = path.resolve()
                resolved.relative_to(staging)
            except (OSError, ValueError):
                continue
            if resolved.is_file() and resolved.suffix.lower() in MEDIA_EXTENSIONS:
                unique[resolved] = resolved
        return sorted(unique.values(), key=lambda path: path.stat().st_size, reverse=True)
