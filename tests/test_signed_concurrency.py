# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations
import base64
import time
import pytest
from engine import ed25519
from engine.license import sign_license
from engine.subscription import status

@pytest.mark.parametrize("plan,claim,expected",[("standard",2,2),("advanced",4,4),("free",4,1),("standard",3,2),("advanced",10,4),("advanced",None,1),("advanced",True,1),("advanced","4",1),("advanced",-1,1),("advanced",1.5,1)])
def test_concurrency_comes_from_signed_claim_with_fail_closed_legacy_refresh(plan,claim,expected):
    seed=bytes(range(32)); public=base64.b64encode(ed25519.public_key(seed)).decode()
    payload={"sub":"account-a","device_id":"device-a","plan_id":plan,"max_concurrency":claim,"credit_balance":5000,"exp":int(time.time())+3600}
    token=sign_license(payload,signing_key=base64.b64encode(seed).decode(),alg="EdDSA")
    settings={"subscription_license_public_key":public,"license_token":token,"account_user_id":"account-a","subscription_active":True,"subscription_plan":"advanced","max_concurrency":1000}
    assert status(settings,device_id="device-a")["max_concurrency"]==expected
    assert status(settings,device_id="other-device")["max_concurrency"]==1
    assert status({**settings,"account_user_id":"account-b"},device_id="device-a")["max_concurrency"]==1


def test_locally_modified_license_claim_is_rejected():
    seed=bytes(range(32));public=base64.b64encode(ed25519.public_key(seed)).decode()
    token=sign_license({"sub":"account-a","device_id":"device-a","plan_id":"advanced","max_concurrency":1,"exp":int(time.time())+3600},signing_key=base64.b64encode(seed).decode(),alg="EdDSA")
    import json
    parts=token.split(".");raw=json.loads(base64.urlsafe_b64decode(parts[1]+"="*(-len(parts[1])%4)));raw["max_concurrency"]=4
    parts[1]=base64.urlsafe_b64encode(json.dumps(raw).encode()).rstrip(b"=").decode()
    assert status({"subscription_license_public_key":public,"license_token":".".join(parts),"subscription_active":True,"subscription_plan":"advanced"},device_id="device-a")["max_concurrency"]==1
