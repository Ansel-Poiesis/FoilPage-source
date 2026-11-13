# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Ed25519（RFC 8032）纯 Python 实现许可证签名所需的最小面。

只做三件事：种子生成密钥对、签名、验签。无第三方依赖——客户端
（PyInstaller 打包的桌面端）只带 verify 与公钥，伪造许可证需要
服务端私钥，客户端二进制里不存在。

实现按 RFC 8032 参考代码改编（vars 命名保留 RFC 原样便于对照），
tests/test_license.py 用 RFC 8032 §7.1 官方测试向量钉死正确性。
"""
from __future__ import annotations

import hashlib
import os

p = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I = pow(2, (p - 1) // 4, p)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, p - 2, p)


def _recover_x(y: int, sign: int) -> int:
    if y >= p:
        return None
    x2 = (y * y - 1) * _inv(d * y * y + 1) % p
    if x2 == 0:
        if sign:
            return None
        return 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * I % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


def _point_add(P, Q):
    # 仿射坐标点加（性能足够：许可证签名低频）。
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - d * x1 * x2 * y1 * y2)
    return x3 % p, y3 % p


def _point_mul(s: int, P):
    Q = (0, 1)
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _compress(P) -> bytes:
    x, y = P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y)


def _secret_expand(secret: bytes):
    if len(secret) != 32:
        raise ValueError("Ed25519 种子必须 32 字节")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


# 基点 G（按标准常数展开，避免手工抄写错一位）
_G_y = 4 * _inv(5) % p
_G_x = _recover_x(_G_y, 0)
if _G_x is None:
    _G_x = _recover_x(_G_y, 1)
G = (_G_x, _G_y)


def generate_seed() -> bytes:
    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    a, _ = _secret_expand(seed)
    return _compress(_point_mul(a, G))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _secret_expand(seed)
    A = _compress(_point_mul(a, G))
    r = int.from_bytes(_sha512(prefix + msg), "little") % L
    R = _point_mul(r, G)
    Rs = _compress(R)
    h = int.from_bytes(_sha512(Rs + A + msg), "little") % L
    s = (r + h * a) % L
    return Rs + int.to_bytes(s, 32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    A = _decompress(pub)
    if A is None:
        return False
    Rs = sig[:32]
    R = _decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= L:
        return False
    h = int.from_bytes(_sha512(Rs + pub + msg), "little") % L
    sG = _point_mul(s, G)
    hA = _point_mul(h, A)
    RhA = _point_add(R, hA)
    return _compress(sG) == _compress(RhA)
