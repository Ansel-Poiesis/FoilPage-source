# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Durable browser submission receipts and disconnected handoff recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import time

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from app.local_api import LocalApiServer
from engine.downloader import DownloadAdapterError
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account

SOURCE = 'https://www.bilibili.com/video/BV1receipt'
REQUEST_A = 'fixture-request-000000000001'
REQUEST_B = 'fixture-request-000000000002'

@pytest.fixture
def receipt_coordinator(tmp_path):
    settings=SettingsStore(base_dir=tmp_path/'data')
    settings.update(download_dir=str(tmp_path/'downloads'))
    controller=AppController(settings)
    coordinator=DownloadCoordinator(controller)
    try:
        yield coordinator
    finally:
        coordinator.stop()
        controller.close()

def finish_download(coordinator, item):
    coordinator.store.transition(item['id'], 'resolving')
    coordinator.store.transition(item['id'], 'downloading')
    coordinator.store.transition(item['id'], 'imported', downloaded_path='fixture.mp4')
    return coordinator.store.transition(item['id'], 'done')

def test_terminal_receipt_is_reused_after_lost_response_and_process_restart(tmp_path):
    settings=SettingsStore(base_dir=tmp_path/'data')
    first=AppController(settings)
    coordinator=DownloadCoordinator(first)
    item=coordinator.submit(SOURCE,title='原始标题',options={'client_request_id':REQUEST_A})
    finish_download(coordinator,item)
    first.close()
    second=AppController(SettingsStore(base_dir=tmp_path/'data'))
    try:
        recovered=DownloadCoordinator(second).submit(SOURCE,title='后续标题',options={'client_request_id':REQUEST_A})
        assert recovered['id']==item['id'] and recovered['status']=='done'
        assert recovered['title']=='原始标题'
        assert len(second.downloads.list())==1
    finally:
        second.close()

def test_active_url_dedup_binds_every_receipt_even_after_terminal(receipt_coordinator):
    coordinator=receipt_coordinator
    first=coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A})
    alias=coordinator.submit(SOURCE,options={'client_request_id':REQUEST_B})
    assert alias['id']==first['id']
    finish_download(coordinator,first)
    assert coordinator.submit(SOURCE,options={'client_request_id':REQUEST_B})['id']==first['id']
    assert len(coordinator.store.list())==1

def test_concurrent_replayed_receipts_create_one_durable_task(receipt_coordinator):
    coordinator=receipt_coordinator
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks=list(pool.map(lambda _:coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A}),range(64)))
    assert len({item['id'] for item in tasks})==1
    assert len(coordinator.store.list())==1

@pytest.mark.parametrize('changed',[
    {'url':'https://www.bilibili.com/video/BV1other'},
    {'url':SOURCE,'options':{'client_request_id':REQUEST_A,'transcribe_after_download':True}},
])
def test_receipt_cannot_change_original_source_or_intent(receipt_coordinator,changed):
    coordinator=receipt_coordinator
    coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A})
    with pytest.raises(DownloadAdapterError) as caught:
        coordinator.submit(changed['url'],options=changed.get('options',{'client_request_id':REQUEST_A}))
    assert caught.value.code=='invalid_client_request_id'
    assert len(coordinator.store.list())==1

@pytest.mark.parametrize('request_id',['short','a'*65,'https://example.com/signed?secret=fixture',123,None])
def test_receipt_rejects_malformed_or_non_string_identifier(receipt_coordinator,request_id):
    with pytest.raises(DownloadAdapterError) as caught:
        receipt_coordinator.submit(SOURCE,options={'client_request_id':request_id})
    assert caught.value.code=='invalid_client_request_id'
    assert not receipt_coordinator.store.list()

def test_cancel_before_submission_is_a_durable_tombstone(receipt_coordinator):
    coordinator=receipt_coordinator
    assert coordinator.cancel_submission(REQUEST_A) is None
    with pytest.raises(DownloadAdapterError) as caught:
        coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A})
    assert caught.value.code=='cancelled'
    assert not coordinator.store.list()

def test_cancel_ambiguous_submission_reuses_receipt_and_never_restarts(receipt_coordinator):
    coordinator=receipt_coordinator
    item=coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A})
    assert coordinator.cancel_submission(REQUEST_A)['id']==item['id']
    assert coordinator.get(item['id'])['status']=='cancelled'
    with pytest.raises(DownloadAdapterError) as caught:
        coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A})
    assert caught.value.code=='cancelled'
    assert len(coordinator.store.list())==1

def test_handoff_disconnection_waits_but_pends_past_the_configured_window_fail(receipt_coordinator):
    # 先生 2026-10-25 裁决：短暂断连仍等待（旧契约），但悬挂超过设置时限
    # （默认 120 分钟）自动判失败并给出收藏→刷新→重新下载指引。
    coordinator=receipt_coordinator
    item=coordinator.store.create('https://www.douyin.com/video/1234567890123456','原视频','douyin',{
        'quality':'best-available','audio_only':False,'transcribe_after_download':False,'language':'zh','mode':'browser','browser_fallback':True,
    })
    coordinator.store.transition(item['id'],'resolving')
    coordinator.store.transition(item['id'],'awaiting_browser',error={'code':'media_forbidden','message':'fixture','retryable':False,'diagnostic':{'fallback_pending':'browser'}})
    # 断连几分钟（远小于默认窗口）：必须继续等待，不得消耗任何尝试。
    from datetime import datetime,timedelta as _td
    minutes_ago=(datetime.now()-_td(minutes=5)).isoformat(timespec='seconds')
    with coordinator.store._lock,coordinator.store._conn:
        coordinator.store._conn.execute("UPDATE download_jobs SET updated_at=? WHERE id=?",(minutes_ago,item['id']))
    coordinator.start()
    time.sleep(.15)
    assert coordinator.get(item['id'])['status']=='awaiting_browser'
    # 悬挂超过窗口（默认 120 分钟）：判失败＋指引＋可重试。
    ancient=(datetime.now()-_td(minutes=200)).isoformat(timespec='seconds')
    with coordinator.store._lock,coordinator.store._conn:
        coordinator.store._conn.execute("UPDATE download_jobs SET updated_at=? WHERE id=?",(ancient,item['id']))
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        swept=coordinator.get(item['id'])
        if swept['status']=='failed': break
        time.sleep(.2)
    swept=coordinator.get(item['id'])
    assert swept['status']=='failed'
    assert swept['error']['code']=='download_pending_timeout'
    assert swept['error']['retryable'] is True

def test_http_submission_receipt_and_cancel_tombstone_contract(receipt_coordinator):
    coordinator=receipt_coordinator
    settings=coordinator.controller.settings_store
    coordinator.controller.settings=grant_account(settings,settings.base_dir)
    coordinator.start=lambda:None
    server=LocalApiServer(coordinator.controller,coordinator,port=0)
    server.start()
    token=coordinator.controller.settings['local_api_token']
    def post(body):
        connection=http.client.HTTPConnection(*server.address,timeout=5)
        try:
            connection.request('POST','/v1/downloads',body=json.dumps(body),headers={'Authorization':f'Bearer {token}','Origin':'chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Content-Type':'application/json'})
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()
    try:
        payload={'url':SOURCE,'title':'原始标题','client_request_id':REQUEST_A}
        status,first=post(payload);assert status==202
        status,replayed=post(payload);assert status==202 and replayed['job']['id']==first['job']['id']
        status,cancelled=post({'cancel_request_id':REQUEST_A});assert status==202 and cancelled['job']['status']=='cancelled'
        status,empty=post({'cancel_request_id':REQUEST_B});assert status==202 and empty['job'] is None
        status,rejected=post({'url':SOURCE,'client_request_id':REQUEST_B});assert status==400 and rejected['error']['code']=='cancelled'
        assert len(coordinator.store.list())==1
    finally:server.stop()


def test_transcribe_intent_merges_into_the_active_download(receipt_coordinator):
    """先生 2026-10-08 裁决：下载后再点转写，同一任务就地升级转写，不重复
    下载。回执各自可查（取消走各自凭据），但不再新建第二行；同一回执改写
    意图仍被拒绝（防篡改语义不变，见下个用例）。"""
    coordinator=receipt_coordinator
    download=coordinator.submit(SOURCE,options={'client_request_id':REQUEST_A,'transcribe_after_download':False})
    transcribe=coordinator.submit(SOURCE,options={'client_request_id':REQUEST_B,'transcribe_after_download':True})
    assert transcribe['id']==download['id']
    assert coordinator.get(download['id'])['options']['transcribe_after_download'] is True
    assert len(coordinator.store.list())==1
    assert transcribe['options']['transcribe_after_download'] is True
    assert coordinator.submit(SOURCE,options={'client_request_id':REQUEST_B,'transcribe_after_download':True})['id']==transcribe['id']


@pytest.mark.parametrize('body,title,body_status,title_status,label,failed',[
    (True,False,'done','disabled','正文转写成功',False),
    (False,True,'disabled','done','标题转写成功',False),
    (True,True,'done','done','正文与标题转写成功',False),
    (True,True,'done','skipped','正文转写成功，标题处理失败',True),
    (True,True,'skipped','done','标题转写成功，正文处理失败',True),
    (True,True,'skipped','skipped','转写完成，正文与标题处理失败',True),
    (False,False,'disabled','disabled','转写成功',False),
])
def test_linked_stage_outcome_is_public_read_only_and_matches_desktop(receipt_coordinator,monkeypatch,body,title,body_status,title_status,label,failed):
    from app.local_api import public_download_job
    coordinator=receipt_coordinator
    item=coordinator.submit(SOURCE,title='原视频标题')
    coordinator.store.transition(item['id'],'resolving')
    coordinator.store.transition(item['id'],'downloading')
    coordinator.store.transition(item['id'],'imported',downloaded_path='C:/private/source.mp4')
    coordinator.store.transition(item['id'],'queued',job_id='linked-stage-fixture')
    linked={'id':'linked-stage-fixture','status':'done','source_path':'C:/private/source.mp4','final_path':'C:/private/result.txt',
            'body_enabled_snapshot':body,'title_enabled_snapshot':title,'body_status':body_status,'title_status':title_status}
    monkeypatch.setattr(coordinator.controller.queue,'get_job',lambda job_id:linked if job_id==linked['id'] else None)
    for row in (coordinator.get(item['id']),coordinator.list()[0]):
        assert row['status']=='done' and row['title']=='原视频标题'
        assert row['body_enabled_snapshot'] is body and row['title_enabled_snapshot'] is title
        assert row['body_status']==body_status and row['title_status']==title_status
        assert row['transcription_label']==label and row['transcription_error'] is failed
        public=public_download_job(row)
        assert public['download_complete'] is True
        assert not any(key in public for key in ('source_path','final_path','downloaded_path'))
        assert 'C:/private' not in json.dumps(public,ensure_ascii=False)
    stored=coordinator.store.get(item['id'])
    assert 'body_status' not in stored and 'transcription_label' not in stored


def test_running_linked_stage_snapshots_ignore_live_settings_and_private_fields(receipt_coordinator,monkeypatch):
    coordinator=receipt_coordinator
    item=coordinator.submit(SOURCE)
    coordinator.store.transition(item['id'],'resolving')
    coordinator.store.transition(item['id'],'downloading')
    coordinator.store.transition(item['id'],'imported')
    coordinator.store.transition(item['id'],'queued',job_id='linked-stage-fixture')
    linked={'id':'linked-stage-fixture','status':'running','body_enabled_snapshot':False,'title_enabled_snapshot':True,
            'body_status':'disabled','title_status':'running','source_path':'private','title_error':'private diagnostic'}
    monkeypatch.setattr(coordinator.controller.queue,'get_job',lambda job_id:linked)
    row=coordinator.get(item['id'])
    assert row['status']=='transcribing'
    assert row['body_enabled_snapshot'] is False and row['title_enabled_snapshot'] is True
    assert row['title_status']=='running' and row['body_status']=='disabled'
    assert not any(key in row for key in ('source_path','title_error','transcription_label'))
