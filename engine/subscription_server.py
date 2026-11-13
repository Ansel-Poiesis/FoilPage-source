# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""订阅服务端合同的最小本地验收实现。

生产环境由真正的支付后端承担订单、授权、额度账本与 webhook 签名校验；
本模块提供契约一致的最小闭环（checkout → paid webhook → 签发许可证 →
状态查询 → 托管额度计量/回滚），只监听 127.0.0.1 临时端口，用于在接入
供应商之前验证客户端行为与合同语义。许可证使用 HS256 开发密钥签发，
上线前必须切换到服务端 EdDSA 签名。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.error import HTTPError, URLError

from engine.license import LicenseError, sign_license, verify_license
from engine.subscription import PLAN_MAP
from engine.usage import usage_period


DEFAULT_EXPIRES_SECONDS = 30 * 24 * 3600


class ContractError(RuntimeError):
    pass


class SubscriptionLedger:
    """账户额度账本：按 request_id 幂等，消费预扣、失败按原请求回滚。"""

    def __init__(self, period: str) -> None:
        self.period = period
        self.used = 0
        self.consumed: dict[str, int] = {}
        self.refunded: set[str] = set()
        self._lock = threading.RLock()

    def consume(self, request_id: str, units: int, quota: int) -> int:
        with self._lock:
            if request_id in self.consumed:
                return self.used  # 重复推送不重复扣减
            if self.used + units > quota:
                raise ContractError("托管额度不足")
            self.consumed[request_id] = units
            self.used += units
            return self.used

    def refund(self, request_id: str) -> int:
        with self._lock:
            if request_id not in self.consumed or request_id in self.refunded:
                return self.used  # 未消费或已回滚
            self.refunded.add(request_id)
            self.used = max(0, self.used - self.consumed[request_id])
            return self.used


class MockSubscriptionServerApp:
    """订单、账户、许可证签发与额度账本的进程内实现。"""

    def __init__(
        self,
        signing_key: str,
        *,
        period: str | None = None,
        expires_seconds: int = DEFAULT_EXPIRES_SECONDS,
    ) -> None:
        self.signing_key = signing_key
        self.period = period or usage_period()
        self.expires_seconds = int(expires_seconds)
        self.orders: dict[str, dict[str, Any]] = {}
        self.by_client_request: dict[str, str] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def checkout(self, plan_id: str, client_request_id: str) -> dict[str, Any]:
        if str(plan_id or "") not in PLAN_MAP:
            raise ContractError("套餐不存在")
        cid = str(client_request_id or "").strip()
        if not cid:
            raise ContractError("缺少幂等键 client_request_id")
        with self._lock:
            order_id = self.by_client_request.get(cid)
            if order_id is None:
                order_id = "ord-" + secrets.token_hex(6)
                self.orders[order_id] = {
                    "plan_id": plan_id,
                    "client_request_id": cid,
                    "status": "pending",
                }
                self.by_client_request[cid] = order_id
            return {
                "ok": True,
                "order_id": order_id,
                "plan_id": plan_id,
                "status": "pending",
                "checkout_url": f"/checkout?order={order_id}",
            }

    def mark_paid(self, order_id: str) -> str:
        """供应商支付成功回调：更新订单并签发带过期时间的许可证。"""
        with self._lock:
            order = self.orders.get(str(order_id or ""))
            if not order:
                raise ContractError("订单不存在")
            plan = PLAN_MAP[order["plan_id"]]
            sub = order["client_request_id"]
            if order["status"] != "paid":
                order["status"] = "paid"
                if sub not in self.accounts:
                    self.accounts[sub] = {
                        "plan_id": order["plan_id"],
                        "ledger": SubscriptionLedger(self.period),
                    }
            payload = {
                "sub": sub,
                "iss": "shengjian-local-acceptance",
                "iat": int(time.time()),
                "exp": int(time.time()) + self.expires_seconds,
                "plan_id": order["plan_id"],
                "period": plan["period"],
                "monthly_agent_units": plan["monthly_agent_units"],
                # 元组必须转列表：许可证经 JSON 往返后要与套餐权益严格相等。
                "entitlements": list(plan["entitlements"]),
                "usage_period": self.period,
            }
            token = sign_license(payload, signing_key=self.signing_key)
            self.accounts[sub]["token"] = token
            return token

    def status(self, token: str) -> dict[str, Any]:
        payload = verify_license(token, signing_key=self.signing_key)
        sub = payload["sub"]
        with self._lock:
            account = self.accounts.get(sub)
            if not account:
                raise ContractError("账户不存在")
            ledger: SubscriptionLedger = account["ledger"]
            quota = int(payload.get("monthly_agent_units") or 0)
            return {
                "active": True,
                "sub": sub,
                "plan_id": payload.get("plan_id"),
                "period": payload.get("period"),
                "entitlements": payload.get("entitlements"),
                "monthly_agent_units": quota,
                "usage_period": ledger.period,
                "usage_used": ledger.used,
                "usage_remaining": max(0, quota - ledger.used),
                "expires_at": payload.get("exp"),
            }

    def report_usage(
        self,
        token: str,
        request_id: str,
        units: object,
        action: str,
    ) -> dict[str, Any]:
        payload = verify_license(token, signing_key=self.signing_key)
        sub = payload["sub"]
        rid = str(request_id or "").strip()
        if not rid:
            raise ContractError("缺少 request_id")
        try:
            units_int = int(units or 0)
        except (TypeError, ValueError) as exc:
            raise ContractError("units 必须是整数") from exc
        with self._lock:
            account = self.accounts.get(sub)
            if not account:
                raise ContractError("账户不存在")
            ledger: SubscriptionLedger = account["ledger"]
            quota = int(payload.get("monthly_agent_units") or 0)
            if action == "consume":
                if units_int < 1:
                    raise ContractError("consume 的 units 必须为正数")
                ledger.consume(rid, units_int, quota)
            elif action == "refund":
                ledger.refund(rid)
            else:
                raise ContractError("action 必须是 consume 或 refund")
            return {
                "ok": True,
                "sub": sub,
                "usage_used": ledger.used,
                "usage_remaining": max(0, quota - ledger.used),
            }


class _Handler(BaseHTTPRequestHandler):
    server: "MockSubscriptionHTTPServer"

    def log_message(self, fmt: str, *args: object) -> None:  # 静音测试输出
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ContractError("请求长度无效") from None
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("请求体不是有效 JSON") from exc

    def _handle(self, fn) -> None:
        try:
            result = fn()
        except ContractError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except LicenseError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except Exception:
            self._json(500, {"ok": False, "error": "服务端错误"})
        else:
            self._json(200, result if isinstance(result, dict) else {"ok": True, "result": result})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/v1/health":
            self._json(200, {"ok": True, "service": "shengjian-subscription-mock"})
            return
        if path == "/v1/subscription/plans":
            self._json(200, {"ok": True, "plans": [dict(PLAN_MAP[pid]) for pid in PLAN_MAP]})
            return
        if path == "/v1/subscription/status":
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            token = (query.get("license_token") or [""])[0]
            self._handle(lambda: self.server.app.status(token))
            return
        self._json(404, {"ok": False, "error": "未知端点"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
        except ContractError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        if path == "/v1/subscription/checkout":
            self._handle(
                lambda: self.server.app.checkout(
                    body.get("plan_id"),
                    body.get("client_request_id"),
                )
            )
            return
        if path == "/v1/webhooks/payment":
            try:
                token = self.server.app.mark_paid(body.get("order_id"))
            except ContractError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "license_token": token})
            return
        if path == "/v1/agent/usage":
            self._handle(
                lambda: self.server.app.report_usage(
                    body.get("license_token"),
                    body.get("request_id"),
                    body.get("units"),
                    body.get("action"),
                )
            )
            return
        self._json(404, {"ok": False, "error": "未知端点"})


class MockSubscriptionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: MockSubscriptionServerApp) -> None:
        self.app = app
        super().__init__(("127.0.0.1", 0), _Handler)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class SubscriptionServerClient:
    """订阅服务端 HTTP 客户端；错误以可读 dict 返回，不抛网络异常。"""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout)

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - 本地验收端点
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error = {}
            return {"ok": False, "error": error.get("error") or f"HTTP {exc.code}"}
        except (URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": f"无法连接订阅服务: {exc}"}
        except json.JSONDecodeError:
            return {"ok": False, "error": "订阅服务返回格式无效"}

    def checkout(self, plan_id: str, client_request_id: str) -> dict[str, Any]:
        return self._call(
            "POST",
            "/v1/subscription/checkout",
            {"plan_id": plan_id, "client_request_id": client_request_id},
        )

    def simulate_payment(self, order_id: str) -> dict[str, Any]:
        return self._call("POST", "/v1/webhooks/payment", {"order_id": order_id})

    def status(self, license_token: str) -> dict[str, Any]:
        token = urllib.parse.quote(str(license_token or ""), safe="")
        return self._call("GET", f"/v1/subscription/status?license_token={token}")

    def report_usage(
        self,
        license_token: str,
        request_id: str,
        units: int,
        action: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/v1/agent/usage",
            {
                "license_token": license_token,
                "request_id": request_id,
                "units": units,
                "action": action,
            },
        )


def run_acceptance(
    *,
    signing_key: str = "shengjian-local-acceptance-key",
    period: str | None = None,
    plan_id: str = "standard",
    client_request_id: str = "accept-001",
    units: int = 2,
) -> dict[str, Any]:
    """启动本地验收服务并跑通完整闭环，返回证据字典（不接触任何外部网络）。"""
    app = MockSubscriptionServerApp(signing_key=signing_key, period=period or usage_period())
    server = MockSubscriptionHTTPServer(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence: dict[str, Any] = {}
    try:
        client = SubscriptionServerClient(server.base_url())
        evidence["health"] = client._call("GET", "/v1/health")
        checkout = client.checkout(plan_id, client_request_id)
        evidence["checkout"] = {"ok": bool(checkout.get("ok")), "has_order": bool(checkout.get("order_id"))}
        duplicate = client.checkout(plan_id, client_request_id)
        evidence["checkout_idempotent"] = {
            "same_order": duplicate.get("order_id") == checkout.get("order_id"),
        }
        paid = client.simulate_payment(str(checkout.get("order_id")))
        token = str(paid.get("license_token") or "")
        evidence["payment"] = {"ok": bool(paid.get("ok")), "has_token": bool(token)}
        payload = verify_license(token, signing_key=signing_key)
        plan = PLAN_MAP[plan_id]
        evidence["license_verified"] = {
            "sub": payload.get("sub"),
            "plan_id": payload.get("plan_id"),
            "monthly_agent_units": payload.get("monthly_agent_units"),
            "entitlements_match": payload.get("entitlements") == list(plan["entitlements"]),
            "expires_in_future": bool(payload.get("exp") and payload["exp"] > time.time()),
        }
        initial = client.status(token)
        evidence["status_initial"] = {
            "active": initial.get("active"),
            "used": initial.get("usage_used"),
        }
        used = client.report_usage(token, "accept-u1", units, "consume")
        evidence["usage_consume"] = {
            "used": used.get("usage_used"),
            "remaining": used.get("usage_remaining"),
        }
        repeated = client.report_usage(token, "accept-u1", units, "consume")
        evidence["usage_idempotent"] = {
            "used_unchanged": repeated.get("usage_used") == used.get("usage_used"),
        }
        refunded = client.report_usage(token, "accept-u1", 0, "refund")
        evidence["usage_refund"] = {
            "used": refunded.get("usage_used"),
            "rolled_back": refunded.get("usage_used") == 0,
        }
        evidence["overall"] = "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return evidence
