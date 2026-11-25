# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""登录、账户会话与签名许可证应用的 Bridge 混入。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import re
import urllib.parse
import urllib.request

from engine import service_http
from engine.service_http import payment_service_headers


def _subscription_session_rejected(value: object) -> bool:
    """Only an explicit account-service rejection invalidates offline login.

    A platform/WAF 403 or a concurrent-license 409 is not a session verdict.
    Keep compatibility with the Python service's exact legacy error messages.
    """
    if (not isinstance(value, dict) or value.get("ok") is not False
            or set(value) - {"ok", "status", "error"}
            or any(key in value and not isinstance(value[key], str) for key in ("status", "error"))):
        return False
    if value.get("status") == "device_limit":
        return True
    if "status" in value and value["status"] != "invalid":
        return False
    return value.get("error") in {
        "未登录，请重新登录", "登录已过期，请重新登录", "登录已失效，请重新登录",
        "设备已解绑，请重新登录", "请先完成设备管理并重新登录", "未登录，无法刷新许可证",
    }


class AccountBridgeMixin:
    def wechat_login_start(self) -> dict:
        """发起微信扫码登录：服务端生成会话并在系统浏览器打开二维码页。"""
        import urllib.request
        import webbrowser

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        existing_session = str(settings.get("account_login_token") or "")
        existing_pending = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        device = device_id(str(getattr(self.settings_store, "base_dir", "") or "."))
        body = json.dumps({"device_id": device}, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/wechat/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": str((result or {}).get("error") or "微信登录暂不可用")}
        if not self._valid_account_auth_url(result.get("auth_url"), "wechat"):
            return {"ok": False, "error": "登录授权地址无效，请检查服务配置"}
        applied = self._save_pending_account_login(result, base, existing_session, existing_pending, revision)
        if not applied["ok"]:
            return applied
        try:
            webbrowser.open(str(result.get("auth_url") or ""))
        except Exception:
            pass
        result["ok"] = True
        return result

    def wechat_login_poll(self) -> dict:
        """轮询登录结果；成功即验证服务端许可证并写入本地订阅状态。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        pending = str(settings.get("account_pending_login_token") or "").strip()
        existing_session = str(settings.get("account_login_token") or "")
        revision = self._account_revision
        profile_revision = self._profile_revision
        token = pending or str(settings.get("account_login_token") or "").strip()
        if not base or not token:
            return {"ok": False, "status": "invalid"}
        body = json.dumps({"login_token": token, "session_id": pending or token,
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))}, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/poll", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                failure = json.loads(exc.read(65536).decode("utf-8"))
                if isinstance(failure, dict) and failure.get("status") in ("cancelled", "denied", "error", "account_link_required", "invalid"):
                    return failure
            except Exception:
                pass
            return {"ok": False, "status": "error", "error": "授权未完成，请重新登录"}
        except Exception as exc:
            return {"ok": False, "status": "unreachable", "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or result.get("status") != "ok":
            if isinstance(result, dict) and result.get("status") == "device_limit":
                return self._remember_device_management(result, base=base,
                    expected_session=existing_session,
                    expected_pending=pending, expected_revision=revision)
            return result or {"ok": False, "status": "invalid"}
        applied = self._apply_remote_account(result, expected_pending=token if pending else None,
                                              expected_session=token if not pending else None, expected_base=base,
                                              expected_revision=revision, expected_profile_revision=profile_revision)
        if not applied.get("ok"):
            return {**applied, "status": "bad_license"}
        result["ok"] = True
        return result

    def email_login_start(self, email: str) -> dict:
        """请求邮箱登录验证码（联调 DEV 模式下返回 dev_code）。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        body = json.dumps({
            "email": str(email or "").strip(),
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/email/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"验证码发送失败（HTTP {exc.code}）"}
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}

    def email_login_verify(self, email: str, code: str, intent: str = "login") -> dict:
        """校验验证码：验证服务端许可证后写入本地订阅状态。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        if intent not in ("login", "bind"):
            return {"ok": False, "error": "邮箱验证操作无效"}
        device = device_id(str(getattr(self.settings_store, "base_dir", "") or "."))
        existing_session = str(settings.get("account_login_token") or "")
        pending_session = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        profile_revision = self._profile_revision
        body = json.dumps({
            "email": str(email or "").strip(),
            "code": str(code or "").strip(),
            "device_id": device,
            "login_token": str(settings.get("account_login_token") or ""),
            "intent": intent,
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/email/verify", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"验证失败（HTTP {exc.code}）"}
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict) and result.get("status") == "device_limit":
                return self._remember_device_management(result, base=base,
                    expected_session=existing_session, expected_pending=pending_session,
                    expected_revision=revision)
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}
        applied = self._apply_remote_account(result, expected_session=existing_session,
                                              expected_pending=pending_session, expected_base=base,
                                              expected_revision=revision, expected_profile_revision=profile_revision)
        if not applied.get("ok"):
            return applied
        result["ok"] = True
        return result

    def web_login_start(self) -> dict:
        """Open a one-time request; only the desktop retains the polling capability."""
        import webbrowser
        import hashlib
        import secrets
        import time
        from engine.device import device_id
        with self._lock:
            current = self.controller.settings
            base = str(current.get('subscription_server_url') or '').strip().rstrip('/')
            session = str(current.get('account_login_token') or '')
            pending = str(current.get('account_pending_login_token') or '')
            revision = self._account_revision
        if not base:
            return {'ok':False,'error':'尚未配置登录服务'}
        api = self._local_api()
        if api is None or not api.running:
            return {'ok':False,'error':'请在设置中启用本机连接后重试'}
        port = int(api.address[1])
        if not 1024 <= port <= 65535:
            return {'ok':False,'error':'本机连接端口无效'}
        device = device_id(self.settings_store.base_dir)
        approval = secrets.token_hex(32)
        approval_hash = 'sha256:' + hashlib.sha256(('shengjian-auth-v1:desktop-approval:' + approval).encode()).hexdigest()
        result = self._account_request(base, '/v1/auth/desktop/start', {'device_id':device,'approval_hash':approval_hash})
        if result.get('ok') is not True:
            return {'ok':False,'error':result.get('error') or '网页授权暂不可用'}
        poll = result.get('session_id')
        request_id = result.get('request_id')
        expected_url = 'https://foilpage.early-snipe-7202.chatgpt.site/app.html#desktop-login=' + str(request_id)
        if (not isinstance(poll,str) or not re.fullmatch(r'poll-[a-f0-9]{64}',poll)
                or not isinstance(request_id,str) or not re.fullmatch(r'desktop-[a-f0-9]{64}',request_id)
                or result.get('automatic') is not True
                or result.get('authorize_url') != expected_url):
            return {'ok':False,'error':'网页授权请求无效，请重试'}
        with self._lock:
            applied = self._save_pending_account_login({'login_token':poll},base,session,pending,revision)
            attempt = {'base':base,'session_id':poll,'device_id':device,'session':session,'revision':self._account_revision,
                       'request_id':request_id,'approval_token':approval,'expires_at':time.monotonic()+600}
            if applied.get('ok'):
                self._web_login_pending = attempt
        if not applied.get('ok'):
            self._account_request(base,'/v1/auth/desktop/cancel',{'session_id':poll,'device_id':device})
            return applied
        try:
            if webbrowser.open(expected_url + '&port=' + str(port)) is False:
                raise RuntimeError('browser unavailable')
        except Exception:
            self._cancel_web_login(attempt)
            return {'ok':False,'error':'未能打开浏览器，请重试'}
        return {'ok':True,'status':'pending','expires_in_seconds':600}

    def _web_login_proof(self, request_id: str) -> dict:
        """Only the exact official web origin may obtain this pending local proof."""
        import time
        with self._lock:
            attempt = getattr(self,'_web_login_pending',None)
            if (not attempt or not self._web_login_current(attempt) or attempt['request_id'] != request_id
                    or time.monotonic() >= attempt['expires_at']):
                return {'ok':False,'error':'请从本机 FoilPage 重新发起登录'}
            return {'ok':True,'request_id':request_id,'approval_token':attempt['approval_token']}

    def _web_login_current(self, attempt: dict) -> bool:
        current = self.controller.settings
        return (getattr(self,'_web_login_pending',None) is attempt and self._account_revision == attempt['revision']
                and str(current.get('subscription_server_url') or '').strip().rstrip('/') == attempt['base']
                and str(current.get('account_login_token') or '') == attempt['session']
                and current.get('account_pending_login_token') == attempt['session_id'])

    def web_login_poll(self) -> dict:
        with self._lock:
            attempt = getattr(self,'_web_login_pending',None)
            if not attempt or not self._web_login_current(attempt):
                return {'ok':False,'status':'invalid','error':'登录请求已变更'}
            profile_revision = self._profile_revision
        result = self._account_request(attempt['base'],'/v1/auth/desktop/poll',{'session_id':attempt['session_id'],'device_id':attempt['device_id']})
        if not self._web_login_current(attempt):
            return {'ok':False,'status':'invalid','error':'登录请求已变更，已忽略旧响应'}
        if result.get('ok') is True and result.get('status') == 'pending':
            return {'ok':True,'status':'pending'}
        if result.get('ok') is not True:
            return {'ok':False,'status':result.get('status') or 'error','error':result.get('error') or '网页登录未完成'}
        applied = self._apply_remote_account(result,expected_session=attempt['session'],expected_pending=attempt['session_id'],
            expected_base=attempt['base'],expected_revision=attempt['revision'],expected_profile_revision=profile_revision)
        if applied.get('ok'):
            self._web_login_pending = None
            window = self._window()
            if window is not None:
                try:
                    window.show()
                    window.restore()
                except Exception:
                    pass
            return {'ok':True,'status':'ok'}
        return {**applied,'status':'bad_license'}

    def _cancel_web_login(self, attempt: dict | None) -> dict:
        if not attempt:
            return {'ok':True}
        with self._lock:
            if self.controller.settings.get('account_pending_login_token') == attempt['session_id']:
                self.controller.save_settings({'account_pending_login_token':''})
                self._account_revision += 1
        result = self._account_request(attempt['base'],'/v1/auth/desktop/cancel',{'session_id':attempt['session_id'],'device_id':attempt['device_id']})
        if result.get('ok') and getattr(self,'_web_login_pending',None) is attempt:
            self._web_login_pending = None
        return {'ok':result.get('ok') is True,**({} if result.get('ok') else {'error':result.get('error') or '取消尚未确认'})}

    def web_login_cancel(self) -> dict:
        return self._cancel_web_login(getattr(self,'_web_login_pending',None))

    def google_login_start(self, intent: str = "login") -> dict:
        """发起 Google 快捷登录：系统浏览器打开授权页，应用内轮询 wechat_login_poll。"""
        import urllib.request
        import webbrowser

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        if intent not in ("login", "bind"):
            return {"ok": False, "error": "Google 登录操作无效"}
        existing_session = str(settings.get("account_login_token") or "")
        if intent == "bind" and not existing_session:
            return {"ok": False, "error": "请先登录，再关联 Google"}
        existing_pending = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        body = json.dumps({
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
            "intent": intent,
            "login_token": existing_session if intent == "bind" else "",
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/google/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}
        result = {**result, "auth_url": result.get("authorize_url") or result.get("auth_url"),
            "login_token": result.get("session_id") or result.get("login_token")}
        if not self._valid_account_auth_url(result.get("auth_url"), "google"):
            return {"ok": False, "error": "登录授权地址无效，请检查服务配置"}
        with self._lock:
            applied = self._save_pending_account_login(result, base, existing_session, existing_pending, revision)
            if applied["ok"]:
                self._google_login_pending = {"base": base, "session_id": result["login_token"],
                    "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))}
        if not applied["ok"]:
            # A logout or service switch during the request must also invalidate
            # the newly created remote challenge, not just reject it locally.
            self._account_request(base, "/v1/auth/google/cancel", {
                "session_id": result["login_token"],
                "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
            })
            return applied
        try:
            webbrowser.open(str(result.get("auth_url") or ""))
        except Exception:
            pass
        return result

    def google_login_cancel(self) -> dict:
        """Invalidate only this client's pending Google challenge; keep full login."""
        with self._lock:
            attempt = getattr(self, "_google_login_pending", None)
        return self._cancel_google_login(attempt)

    def _cancel_google_login(self, attempt: dict | None) -> dict:
        with self._lock:
            current = self.controller.settings
            if not attempt:
                return {"ok": True, "status": "cancelled"}
            # Close local acceptance before the network request, so a held poll
            # cannot install a full session while cancellation is in flight.
            if (current.get("account_pending_login_token") == attempt["session_id"]
                    and str(current.get("subscription_server_url") or "").strip().rstrip("/") == attempt["base"]):
                self.controller.save_settings({"account_pending_login_token": ""})
                self._account_revision += 1
        result = self._account_request(attempt["base"], "/v1/auth/google/cancel",
            {"session_id": attempt["session_id"], "device_id": attempt["device_id"]})
        if result.get("ok") is not True:
            return result
        with self._lock:
            if getattr(self, "_google_login_pending", None) == attempt:
                self._google_login_pending = None
        return result

    @staticmethod
    def _valid_account_auth_url(value: object, provider: str) -> bool:
        if not isinstance(value, str) or len(value) > 8192:
            return False
        try:
            parsed = urllib.parse.urlsplit(value)
            return (parsed.scheme == "https" and parsed.hostname == {
                "google": "accounts.google.com", "wechat": "open.weixin.qq.com"}.get(provider)
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment)
        except ValueError:
            return False

    def _save_pending_account_login(self, result: dict, base: str, session: str, pending: str, revision: int) -> dict:
        token = str(result.get("login_token") or "")
        if not token:
            return {"ok": False, "error": "登录请求缺少有效会话"}
        with self._lock:
            settings = self.controller.settings
            if (self._account_revision != revision
                    or str(settings.get("subscription_server_url") or "").strip().rstrip("/") != base
                    or str(settings.get("account_login_token") or "") != session
                    or str(settings.get("account_pending_login_token") or "") != pending):
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            self.controller.save_settings({"account_pending_login_token": token})
            self._account_management = None
            self._account_revision += 1
            self._buyer_verification = None
            self._alipay_cashiers = {}
        return {"ok": True}

    def account_logout(self) -> dict:
        """退出账户（清除本地登录会话；服务端令牌一并注销）。"""
        import urllib.request

        with self._lock:
            settings = self.controller.settings
            base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
            token = str(settings.get("account_login_token") or "").strip()
            pending = str(settings.get("account_pending_login_token") or "").strip()
            management = self._account_management or {}
            tokens = {token, pending}
            if management.get("base") == base:
                tokens.add(str(management.get("token") or ""))
            google_attempt = getattr(self, "_google_login_pending", None)
            web_attempt = getattr(self, '_web_login_pending', None)
            self._clear_account()
        # The public logout endpoint handles full sessions; a Google polling
        # capability has a separate cancellation endpoint, including callbacks
        # that completed remotely but have not been accepted by this client.
        self._cancel_google_login(google_attempt)
        self._cancel_web_login(web_attempt)
        for token in tokens - {""} if base else ():
            body = json.dumps({"login_token": token}, ensure_ascii=False).encode("utf-8")
            try:
                request = urllib.request.Request(
                    base + "/v1/auth/logout", data=body,
                    headers=payment_service_headers(), method="POST",
                )
                urllib.request.urlopen(request, timeout=10).read()
            except Exception:
                pass
        return {"ok": True}

    def _clear_account(self, *, expected_session: str | None = None) -> None:
        with self._lock:
            if expected_session is not None and str(self.controller.settings.get("account_login_token") or "") != expected_session:
                return
            self._account_revision += 1
            self._buyer_verification = None
            self._alipay_cashiers = {}
            changes = {"account_login_token": "",
                "account_display_name": "", "account_avatar": "", "account_user_id": "", "license_token": "", "account_access_token":"",
                "license_expires_at": None, "subscription_plan": "free", "subscription_active": False,
                "subscription_expires_at": None, "credit_balance": 0, "subscription_last_order_id": "",
                "subscription_expiry_notice_history": None}
            if expected_session is None:
                changes["account_pending_login_token"] = ""
                self._account_management = None
            elif self._account_management and self._account_management["session"] == expected_session:
                self._account_management["session"] = ""
            self.controller.save_settings(changes)

    def _remember_device_management(self, result: dict, *, base: str,
                                    expected_session: str, expected_pending: str,
                                    expected_revision: int | None = None) -> dict:
        token = str(result.get("management_token") or "")
        account = result.get("account")
        if not token or not isinstance(account, dict) or account.get("device_management_only") is not True:
            return {"ok": False, "error": "设备管理响应无效，请重新登录"}
        with self._lock:
            current = self.controller.settings
            if ((expected_revision is not None and self._account_revision != expected_revision)
                    or str(current.get("subscription_server_url") or "").strip().rstrip("/") != base
                    or str(current.get("account_login_token") or "") != expected_session
                    or str(current.get("account_pending_login_token") or "") != expected_pending):
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            self._account_management = {"token": token, "base": base,
                "session": expected_session, "pending": expected_pending}
        return {"ok": False, "status": "device_limit", "account": account,
                "error": "设备数量已达上限，请解绑一台旧设备后继续登录"}

    def _account_context(self) -> tuple[str, str, dict | None]:
        with self._lock:
            current = self.controller.settings
            base = str(current.get("subscription_server_url") or "").strip().rstrip("/")
            session = str(current.get("account_login_token") or "")
            management = self._account_management
            if management and (management["base"] != base or management["session"] != session
                    or management["pending"] != str(current.get("account_pending_login_token") or "")):
                self._account_management = management = None
            return base, str(management["token"] if management else session), management

    @staticmethod
    def _account_request(base: str, path: str, payload: dict) -> dict:
        try:
            request = urllib.request.Request(base + path,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=payment_service_headers(), method="POST")
            result = service_http.request_json(
                request,
                timeout=12,
                service="account",
                operation=path,
                opener=urllib.request.urlopen,
            )
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应无效"}
        except service_http.ServiceHttpError as exc:
            result = dict(exc.payload or {})
            raw_error = result.get("error")
            if isinstance(raw_error, dict):
                message = str(raw_error.get("message") or "")
                code = str(raw_error.get("code") or "")
            else:
                message, code = str(raw_error or ""), ""
            result.update(ok=False, error=message[:500] or "账户操作未完成")
            if code and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
                result["code"] = code
            if exc.status == 401 or _subscription_session_rejected(result):
                result["status"] = "invalid"
            return result
        except (TypeError, ValueError):
            return {"ok": False, "error": "暂时无法连接账户服务，请稍后重试"}

    def _account_context_current(self, base: str, token: str, management: dict | None) -> bool:
        return self._account_context() == (base, token, management)

    def update_account_profile(self, display_name: str, avatar: str | None = None) -> dict:
        """Save only profile fields for the current full session, without granting rights."""
        from engine.account_profile import normalize_display_name, validate_avatar

        try:
            name = normalize_display_name(display_name)
            image = validate_avatar(avatar)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._profile_busy:
                return {"ok": False, "error": "资料正在保存，请稍候"}
            base, token, management = self._account_context()
            user_id = str(self.controller.settings.get("account_user_id") or "")
            revision = self._account_revision
            if management or not base or not token or not user_id:
                return {"ok": False, "error": "请完成账户登录后编辑资料"}
            self._profile_busy = True
        try:
            return self._save_account_profile(base, token, management, user_id, revision, name, image)
        finally:
            with self._lock:
                self._profile_busy = False

    def _save_account_profile(self, base, token, management, user_id, revision, name, image) -> dict:
        from engine.account_profile import normalize_display_name, validate_avatar

        result = self._account_request(base, "/v1/account/profile", {
            "login_token": token, "display_name": name, "avatar": image,
        })
        with self._lock:
            if (self._account_revision != revision
                    or not self._account_context_current(base, token, management)
                    or str(self.controller.settings.get("account_user_id") or "") != user_id):
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if result.get("ok") is not True:
                if result.get("status") == "invalid":
                    self._clear_account(expected_session=token)
                return result
            account = result.get("account")
            if not isinstance(account, dict) or account.get("user_id") != user_id:
                return {"ok": False, "error": "账户资料响应无效，请刷新重试"}
            try:
                saved_name = normalize_display_name(account.get("display_name"))
                if not isinstance(account.get("avatar"), str):
                    raise ValueError
                saved_avatar = validate_avatar(account["avatar"])
                if saved_name != name or (image is not None and saved_avatar != image):
                    raise ValueError
            except ValueError:
                return {"ok": False, "error": "账户资料响应无效，请刷新重试"}
            self.controller.save_settings({"account_display_name": saved_name, "account_avatar": saved_avatar})
            self._profile_revision += 1
            return {"ok": True, "account": {"user_id": user_id, "display_name": saved_name, "avatar": saved_avatar}}

    def get_account_devices(self) -> dict:
        """Read account devices; an unsigned view never grants local entitlement."""
        base, token, management = self._account_context()
        if not base or not token:
            return {"ok": False, "error": "登录后可查看设备"}
        result = self._account_request(base, "/v1/account", {"login_token": token})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "账户已变更，已忽略旧响应"}
        if result.get("status") == "invalid":
            if management:
                self.cancel_account_login()
            else:
                self._clear_account(expected_session=token)
        return result

    def unbind_account_device(self, target_device: str) -> dict:
        base, token, management = self._account_context()
        target = str(target_device or "").strip()
        if not base or not token or not target:
            return {"ok": False, "error": "登录后可解绑指定设备"}
        result = self._account_request(base, "/v1/devices/unbind",
            {"login_token": token, "device_id": target})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "账户已变更，已忽略旧响应"}
        if result.get("ok") and result.get("current_device_unbound") is True and not management:
            self._clear_account(expected_session=token)
        elif result.get("status") == "invalid":
            if management:
                self.cancel_account_login()
            else:
                self._clear_account(expected_session=token)
        return result

    def complete_account_login(self) -> dict:
        base, token, management = self._account_context()
        revision = self._account_revision
        profile_revision = self._profile_revision
        if not management:
            return {"ok": False, "error": "请重新验证身份后登录"}
        result = self._account_request(base, "/v1/auth/complete", {"management_token": token})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
        if not result.get("ok"):
            return {key: value for key, value in result.items() if key != "management_token"}
        applied = self._apply_remote_account(result, expected_session=management["session"],
            expected_pending=management["pending"], expected_base=base, expected_revision=revision,
            expected_profile_revision=profile_revision)
        return result if applied.get("ok") else applied

    def cancel_account_login(self) -> dict:
        """Cancel unfinished identity verification without logging out a current account."""
        web = self.web_login_cancel()
        if web.get('ok') is False:
            return web
        google = self.google_login_cancel()
        if google.get("ok") is False:
            return google
        with self._lock:
            current = self.controller.settings
            base = str(current.get("subscription_server_url") or "").strip().rstrip("/")
            pending = str(current.get("account_pending_login_token") or "")
            management = self._account_management or {}
            tokens = {pending}
            if management.get("base") == base:
                tokens.add(str(management.get("token") or ""))
            self._account_management = None
            self._account_revision += 1
            self._buyer_verification = None
            self.controller.save_settings({"account_pending_login_token": ""})
        for token in tokens - {""} if base else ():
            self._account_request(base, "/v1/auth/logout", {"login_token": token})
        return {"ok": True}

    def _buyer_context_current(self, attempt: dict) -> bool:
        with self._lock:
            current = self.controller.settings
            return (self._account_revision == attempt["revision"]
                and str(current.get("subscription_server_url") or "").strip().rstrip("/") == attempt["base"]
                and str(current.get("account_login_token") or "") == attempt["session"]
                and str(current.get("account_user_id") or "") == attempt["user_id"]
                and self._account_management is None)

    def buyer_verification_start(self) -> dict:
        """Open official buyer authorization; the private polling nonce stays in memory."""
        import webbrowser
        from engine.device import device_id
        with self._lock:
            current = self.controller.settings
            self._buyer_revision += 1
            attempt = {"base": str(current.get("subscription_server_url") or "").strip().rstrip("/"),
                "session": str(current.get("account_login_token") or ""),
                "user_id": str(current.get("account_user_id") or ""), "revision": self._account_revision,
                "buyer_revision": self._buyer_revision}
        if not attempt["base"] or not attempt["session"] or not attempt["user_id"] or self._account_management:
            return {"ok": False, "error": "请先登录 FoilPage 账户，再验证爱发电买家身份"}
        result = self._account_request(attempt["base"], "/v1/account/buyer/start",
            {"login_token": attempt["session"], "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))})
        if not self._buyer_context_current(attempt):
            return {"ok": False, "error": "账户已变更，已忽略旧授权响应"}
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error") or "爱发电身份验证暂不可用")}
        url = result.get("authorize_url")
        nonce = result.get("session_id")
        try:
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) and len(url) <= 8192 else None
            valid = bool(parsed and parsed.scheme == "https" and parsed.hostname == "afdian.com"
                and parsed.path == "/oauth2/authorize" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid or not isinstance(nonce, str) or not nonce or len(nonce) > 512:
            return {"ok": False, "error": "爱发电授权地址或会话无效，请检查服务配置"}
        with self._lock:
            if not self._buyer_context_current(attempt):
                return {"ok": False, "error": "账户已变更，已忽略旧授权响应"}
            superseded = self._buyer_revision != attempt["buyer_revision"]
            if not superseded:
                self._buyer_verification = {**attempt, "nonce": nonce}
        if superseded:
            self._cancel_buyer_nonce(attempt, nonce)
            return {"ok": False, "error": "授权请求已取消，已忽略旧响应"}
        try:
            opened = webbrowser.open(url)
            if opened is False:
                raise RuntimeError("browser unavailable")
        except Exception:
            self.buyer_verification_stop()
            return {"ok": False, "error": "浏览器未能打开，请重试爱发电验证"}
        return {"ok": True, "status": "pending"}

    def buyer_verification_poll(self) -> dict:
        attempt = self._buyer_verification
        if not attempt or not self._buyer_context_current(attempt):
            self._buyer_verification = None
            return {"ok": False, "status": "error", "error": "验证会话已失效，请重新发起"}
        result = self._account_request(attempt["base"], "/v1/account/buyer/poll", {"session_id": attempt["nonce"]})
        if self._buyer_verification is not attempt or not self._buyer_context_current(attempt):
            return {"ok": False, "status": "error", "error": "账户或授权请求已变更，已忽略旧响应"}
        if result.get("status") == "pending" and result.get("ok"):
            return {"ok": True, "status": "pending"}
        if result.get("status") != "verified" or result.get("buyer_verified") is not True:
            return {"ok": False, "status": "error", "error": str(result.get("error") or "爱发电尚未确认身份验证，请重试")}
        account = self._account_request(attempt["base"], "/v1/account", {"login_token": attempt["session"]})
        if self._buyer_verification is not attempt or not self._buyer_context_current(attempt):
            return {"ok": False, "status": "error", "error": "账户已变更，已忽略旧响应"}
        if not account.get("ok") or account.get("buyer_verified") is not True or account.get("user_id") != attempt["user_id"]:
            return {"ok": False, "status": "error", "error": "买家身份尚未绑定到当前账户，请刷新后重试"}
        self._buyer_verification = None
        return {"ok": True, "status": "verified", "buyer_verified": True,
            "account": {key: account[key] for key in ("user_id", "display_name", "buyer_verified", "order_review") if key in account}}

    def buyer_verification_stop(self) -> dict:
        with self._lock:
            attempt = self._buyer_verification
            self._buyer_verification = None
            self._buyer_revision += 1
        if not attempt or not self._buyer_context_current(attempt):
            return {"ok": True, "status": "cancelled"}
        result = self._cancel_buyer_nonce(attempt, attempt["nonce"])
        if not self._buyer_context_current(attempt):
            return {"ok": False, "error": "账户已变更，已忽略旧取消响应"}
        if not result.get("ok"):
            return {"ok": False, "error": "无法确认服务器取消状态，请在浏览器取消授权"}
        if result.get("status") in ("verified", "completed") or result.get("was_completed") is True:
            account = self._account_request(attempt["base"], "/v1/account", {"login_token": attempt["session"]})
            if (not self._buyer_context_current(attempt) or not account.get("ok")
                    or account.get("buyer_verified") is not True or account.get("user_id") != attempt["user_id"]):
                return {"ok": False, "error": "请刷新账户确认买家验证状态"}
            return {"ok": True, "status": "verified"}
        return {"ok": True, "status": "cancelled"}

    def _cancel_buyer_nonce(self, attempt: dict, nonce: str) -> dict:
        from engine.device import device_id
        return self._account_request(attempt["base"], "/v1/account/buyer/cancel",
            {"session_id": nonce, "login_token": attempt["session"],
             "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))})

    def _apply_remote_account(self, result: dict, *, order_no: str = "",
                              expected_session: str | None = None, expected_pending: str | None = None,
                              expected_base: str | None = None, expected_revision: int | None = None,
                              expected_profile_revision: int | None = None, expected_user_id: str | None = None,
                              expected_beta_status: str | None = None) -> dict:
        """Verify the signed license before updating account or balance locally."""
        from engine.device import device_id
        from engine.license import LicenseError, verify_license, signed_beta_access
        settings = self.controller.settings
        public_key = str(settings.get("subscription_license_public_key") or "").strip()
        key = public_key or str(settings.get("subscription_verify_key") or "").strip()
        if not key:
            return {"ok": False, "error": "尚未配置许可证公钥"}
        token = str(result.get("license_token") or "")
        try:
            payload = verify_license(token, signing_key=key,
                expected_alg="EdDSA" if public_key else "HS256",
                device_id=device_id(str(getattr(self.settings_store, "base_dir", "") or ".")))
            account = result.get("account") or result.get("status") or {}
            if not isinstance(account, dict):
                raise ValueError("账户格式无效")
            if account.get("user_id") and str(account["user_id"]) != payload["sub"]:
                raise ValueError("账户与许可证不一致")
            if expected_user_id is not None and payload["sub"] != expected_user_id:
                raise ValueError("许可证与当前登录账户不一致")
            if expected_beta_status is not None:
                beta = signed_beta_access(payload)
                if (expected_beta_status == "active" and beta["active"] is not True
                        or expected_beta_status == "revoked" and beta["revoked"] is not True
                        or expected_beta_status == "expired" and (beta["active"] or not beta["expires_at"]
                            or datetime.fromisoformat(beta["expires_at"]) > datetime.now(timezone.utc))):
                    raise ValueError("内测资格与签名许可证不一致")
            balance = payload.get("credit_balance", 0)
            if type(balance) is not int or balance < 0:
                raise ValueError("积分余额无效")
        except (LicenseError, TypeError, ValueError):
            return {"ok": False, "error": "许可证校验失败，请检查服务配置"}
        changes = {
            "license_token": token, "license_expires_at": payload.get("exp"),
            "subscription_plan": payload.get("plan_id") or "free",
            "subscription_active": bool(account.get("active")),
            "subscription_expires_at": account.get("expires_at"), "credit_balance": balance,
        }
        if result.get('account_access_token'):
            from engine.account_access import account_access
            access_settings = {**settings, **changes, 'account_user_id':payload['sub'],
                'account_login_token':result.get('login_token') or settings.get('account_login_token') or '',
                'account_access_token':result['account_access_token']}
            if not account_access(access_settings, device_id=device_id(str(self.settings_store.base_dir)))['allowed']:
                return {'ok':False,'error':'登录凭证校验失败，请重试'}
            changes['account_access_token'] = result['account_access_token']
        elif result.get('login_token') and result['login_token'] != settings.get('account_login_token'):
            changes['account_access_token'] = ''
        if result.get("login_token"):
            changes["account_login_token"] = str(result["login_token"])
            if expected_pending is not None or result['login_token'] != settings.get('account_login_token'):
                changes["account_pending_login_token"] = ""
        if account.get("user_id"):
            changes["account_user_id"] = str(account["user_id"])
            changes["account_display_name"] = str(account.get("display_name") or "爱发电账户")
            from engine.account_profile import validate_avatar
            try:
                changes["account_avatar"] = validate_avatar(account.get("avatar") or "") or ""
            except ValueError:
                # A malformed image cannot turn a verified login into a failure.
                changes["account_avatar"] = ""
        if order_no:
            changes["subscription_last_order_id"] = order_no
        with self._lock:
            current = self.controller.settings
            if expected_revision is not None and self._account_revision != expected_revision:
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            if expected_session is not None and str(current.get("account_login_token") or "") != expected_session:
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if expected_user_id is not None and str(current.get("account_user_id") or "").strip() != expected_user_id:
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if expected_pending is not None and str(current.get("account_pending_login_token") or "") != expected_pending:
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            if expected_base is not None and str(current.get("subscription_server_url") or "").strip().rstrip("/") != expected_base:
                return {"ok": False, "error": "服务配置已变更，已忽略旧响应"}
            current_key = str(current.get("subscription_license_public_key") or "").strip() or str(current.get("subscription_verify_key") or "").strip()
            if current_key != key:
                return {"ok": False, "error": "许可证配置已变更，已忽略旧响应"}
            if (expected_profile_revision is not None and self._profile_revision != expected_profile_revision
                    and str(current.get("account_user_id") or "") == payload["sub"]):
                # A request issued before a successful profile edit may still
                # carry an older account view. Apply its verified entitlement,
                # while retaining the more recently saved name and avatar.
                changes.pop("account_display_name", None)
                changes.pop("account_avatar", None)
            history = current.get("subscription_expiry_notice_history")
            service = str(current.get("subscription_server_url") or "").rstrip("/")
            if not isinstance(history, dict) or history.get("account") != payload["sub"] or history.get("service") != service:
                history = None
            observed_expiry = account.get("expires_at") if account.get("active") and payload.get("plan_id") in {"standard", "advanced"} else None
            if not observed_expiry and current.get("account_user_id") == payload["sub"] and current.get("subscription_plan") in {"standard", "advanced"}:
                observed_expiry = current.get("subscription_expires_at")
            if observed_expiry:
                history = {"account": payload["sub"], "service": service, "expires_at": observed_expiry}
            changes["subscription_expiry_notice_history"] = history
            self.controller.save_settings(changes)
            if "account_login_token" in changes:
                attempt = getattr(self, "_google_login_pending", None)
                if attempt and expected_pending == attempt["session_id"]:
                    # This session was successfully consumed. A later cleanup
                    # must never revoke the newly installed full login.
                    self._google_login_pending = None
                self._account_management = None
                self._account_revision += 1
                web_attempt = getattr(self,'_web_login_pending',None)
                if web_attempt and self.controller.settings.get('account_pending_login_token') == web_attempt['session_id']:
                    web_attempt['revision'] = self._account_revision
                self._buyer_verification = None
        return {"ok": True}
