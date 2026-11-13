# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""本地积分账本：带签名哈希链的防篡改记账内核。

威胁模型：本地磁盘上的积分记录可被用户改写。对策是每条记录携带
prev_hash 与自身 HMAC——对金额、余额、顺序或时间的任何篡改都会破坏
链，下一次 verify 即定位到断点；对账时服务端账本权威，本地账本只是
离线扣减缓存与审计回放，拿不出有效签名就无法自造 grant。

签名密钥随许可证下发并与设备绑定（见 engine.device 与章程「许可证」），
因此「有权限入账」和「能验证入账」在本地不可兼得伪造。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

ENTRY_TYPES = ("grant", "consume", "refund")
_CREDIT_TYPES = ("grant", "refund")


class LedgerError(ValueError):
    """账本操作无效：入参不合法或余额不足。"""


def make_signer(secret: str) -> Callable[[str], str]:
    key = str(secret).encode("utf-8")

    def signer(text: str) -> str:
        return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()

    return signer


def _now_iso(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    id: str
    type: str
    units: int          # 恒为正数；方向由 type 决定（consume 为支出）
    balance_after: int
    created_at: str
    order_id: str = ""
    note: str = ""
    prev_hash: str = ""
    hash: str = ""


def _canonical(entry: Mapping) -> str:
    body = {k: v for k, v in dict(entry).items() if k != "hash"}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _hash_entry(entry: LedgerEntry, signer: Callable[[str], str]) -> str:
    return signer(_canonical(asdict(entry)))


def balance(entries: list[LedgerEntry]) -> int:
    return int(entries[-1].balance_after) if entries else 0


def append(
    entries: list[LedgerEntry],
    *,
    id: str,
    type: str,
    units: int,
    signer: Callable[[str], str],
    order_id: str = "",
    note: str = "",
    now: Optional[datetime] = None,
) -> tuple[list[LedgerEntry], LedgerEntry]:
    """追加一条记账；同一 id 幂等（返回既有记录，不重复入账）。

    余额不足以 consume 时抛 LedgerError 且不修改账本。
    """
    if type not in ENTRY_TYPES:
        raise LedgerError(f"未知记账类型: {type}")
    if int(units) < 1:
        raise LedgerError("记账数量必须为正数")
    if not str(id or "").strip():
        raise LedgerError("记账必须携带幂等 id")
    for existing in entries:
        if existing.id == id:
            return list(entries), existing
    delta = int(units) if type in _CREDIT_TYPES else -int(units)
    next_balance = balance(entries) + delta
    if next_balance < 0:
        raise LedgerError(f"积分不足：当前余额 {balance(entries)}，需消耗 {int(units)}")
    unsigned = LedgerEntry(
        seq=len(entries) + 1,
        id=str(id),
        type=type,
        units=int(units),
        balance_after=next_balance,
        created_at=_now_iso(now),
        order_id=str(order_id or ""),
        note=str(note or ""),
        prev_hash=entries[-1].hash if entries else "",
    )
    entry = replace(unsigned, hash=_hash_entry(unsigned, signer))
    return list(entries) + [entry], entry


def verify(entries: list[LedgerEntry], signer: Callable[[str], str]) -> tuple[bool, int]:
    """校验整条链：序号连续、prev_hash 衔接、签名有效、余额连续。

    返回 (是否完好, 首个断点 seq)；完好时断点为 0。
    """
    previous_hash = ""
    running = 0
    for index, entry in enumerate(entries, start=1):
        if entry.seq != index:
            return False, entry.seq
        if entry.prev_hash != previous_hash:
            return False, entry.seq
        delta = entry.units if entry.type in _CREDIT_TYPES else -entry.units
        running += delta
        if entry.balance_after != running or entry.balance_after < 0:
            return False, entry.seq
        if entry.type not in ENTRY_TYPES or entry.units < 1:
            return False, entry.seq
        if entry.hash != _hash_entry(entry, signer):
            return False, entry.seq
        previous_hash = entry.hash
    return True, 0
