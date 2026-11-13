# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Public identification for desktop requests to the payment service."""


def payment_service_headers() -> dict[str, str]:
    # A stable product name avoids generic Python clients being rejected by
    # API gateways. Do not include device IDs, account data or credentials.
    return {
        "User-Agent": "ShengJianDesktop",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
