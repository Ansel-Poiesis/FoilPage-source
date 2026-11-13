# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Only the official website may attest a locally pending desktop request."""
import http.client
import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.local_api import LocalApiServer

ORIGIN = 'https://foilpage.early-snipe-7202.chatgpt.site'
REQUEST = 'desktop-' + 'a' * 64


def test_loopback_login_proof_has_narrow_origin_body_and_route_boundaries():
    callback = Mock(return_value={'ok':True,'request_id':REQUEST,'approval_token':'b' * 64})
    controller = SimpleNamespace(settings={'local_api_port':0,'local_api_token':'local-fixture','extension_origin':''},
                                 settings_store=SimpleNamespace(base_dir='unused-fixture'))
    api = LocalApiServer(controller,Mock(),port=0,desktop_login_callback=callback)
    api.start()
    def request(method='POST', origin=ORIGIN, body=None, path='/v1/desktop-login/proof', **extra):
        connection = http.client.HTTPConnection(*api.address,timeout=5)
        payload = json.dumps({'request_id':REQUEST}) if body is None else body
        headers = {'Content-Type':'application/json',**extra}
        if origin is not None:
            headers['Origin'] = origin
        connection.request(method,path,body=payload,headers=headers)
        response = connection.getresponse()
        result = response.status,dict(response.getheaders()),response.read()
        connection.close()
        return result
    try:
        status,headers,raw = request()
        assert status == 200 and json.loads(raw)['approval_token'] == 'b' * 64
        assert headers['Access-Control-Allow-Origin'] == ORIGIN
        assert headers['Cache-Control'] == 'no-store'
        for origin in [None,'null','https://evil.invalid',ORIGIN+'.evil.invalid']:
            status,headers,_ = request(origin=origin)
            assert status == 403 and 'Access-Control-Allow-Origin' not in headers
        for body in ['{}','[]','{"request_id":"wrong"}',json.dumps({'request_id':REQUEST,'callback':'evil'}),'{"request_id":"x","request_id":"y"}','x'*1025]:
            assert request(body=body)[0] == 400
        assert callback.call_count == 1
        status,headers,_ = request(method='OPTIONS')
        assert status == 204 and headers['Access-Control-Allow-Private-Network'] == 'true'
        assert request(path='/v1/agent/call',body='{}')[0] == 403
        callback.return_value = {'ok':False}
        assert request()[0] == 409
    finally:
        api.stop()
