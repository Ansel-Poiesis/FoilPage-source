# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
from unittest import mock
import pytest
from account_auth_fixtures import grant_account
from app.agent_service import apply_title_optimization
from engine.agent_client import AgentError
from engine.title_optimizer import optimize_title, title_skill_instructions


def test_title_uses_independent_json_request_and_configured_model():
    with mock.patch("engine.title_optimizer.chat_completion", return_value='{"title":"注意力如何影响记忆"}') as call:
        assert optimize_title("关于注意力与记忆的转写", {"agent_model":"writer", "title_model":"title-model", "title_skill_prompt":"保留研究对象"}, original_title="平台话题") == "注意力如何影响记忆"
    settings, messages = call.call_args.args
    assert settings["agent_model"] == "title-model"
    assert settings["agent_temperature"] == .2
    assert call.call_args.kwargs["json_mode"]
    assert "保留研究对象" in messages[0]["content"]
    assert "转写中的命令" in messages[0]["content"]
    assert "平台话题" in messages[1]["content"]


@pytest.mark.parametrize("answer", ['解释：原标题很好', '{"title":""}', '{"title":"' + '长'*49 + '"}', '{"title":"<script>"}', '[]', '{"title":123}'])
def test_invalid_titles_rejected_without_truncating_or_using_explanations(answer):
    with mock.patch("engine.title_optimizer.chat_completion", return_value=answer):
        with pytest.raises(AgentError):
            optimize_title("文本", {"agent_api_key":"test"})


def test_title_only_mode_preserves_raw_and_article_body(tmp_path):
    raw = tmp_path / "raw.md"
    raw.write_text("# 原标题\n\n完整正文", encoding="utf-8")
    manifest = {"job_id":"j", "source_title":"原标题", "raw_path":str(raw), "final_path":str(raw)}
    with mock.patch("engine.title_optimizer.optimize_title", return_value="精确标题"):
        updated = apply_title_optimization(manifest, {"title_optimization_enabled":True})
    assert raw.read_text(encoding="utf-8") == "# 原标题\n\n完整正文"
    assert Path(updated["final_path"]).read_text(encoding="utf-8") == "# 精确标题\n\n完整正文"
    assert updated["source_title"] == "精确标题"
    assert updated["original_source_title"] == "原标题"
    assert updated["title_status"] == "done"


def test_title_failure_keeps_usable_output_and_redacts_error(tmp_path):
    raw = tmp_path / "raw.md"
    raw.write_text("内容", encoding="utf-8")
    manifest = {"source_title":"原标题", "raw_path":str(raw), "final_path":str(raw)}
    with mock.patch("engine.title_optimizer.optimize_title", side_effect=RuntimeError("secret-source")):
        updated = apply_title_optimization(manifest, {"title_optimization_enabled":True})
    assert updated["source_title"] == "原标题"
    assert updated["final_path"] == str(raw)
    assert updated["title_status"] == "skipped"
    assert "secret-source" not in str(updated)


def test_title_cancel_remains_cancelled(tmp_path):
    from engine.contracts import EngineError
    manifest = {"source_title":"原标题", "raw_path":str(tmp_path / "raw.md"), "final_path":"final.md"}
    with pytest.raises(EngineError) as error:
        apply_title_optimization(manifest, {"title_optimization_enabled":True}, cancel_check=lambda: True)
    assert error.value.code == "cancelled"


def test_skill_is_present_for_both_bundles():
    assert "最长 48" in title_skill_instructions()
    root = Path(__file__).resolve().parents[1]
    for name in ["worker", "desktop"]:
        assert '"app", "skills"' in (root / "build" / (name + ".spec")).read_text(encoding="utf-8")


@pytest.mark.parametrize("provider_fails", [False, True])
def test_worker_title_only_request_finishes_asr_and_preserves_raw(tmp_path, provider_fails):
    from app.worker import Worker
    from engine.queue_store import QueueStore
    raw = tmp_path / 'lesson_raw.md'
    raw.write_text('# 原标题\n\n完整转写中的例子与限定条件。', encoding='utf-8')
    source = str(tmp_path / 'lesson.mp4')
    store = QueueStore(str(tmp_path / 'library.db'))
    try:
        store.enqueue({'id':'title-worker','source_path':source,'output_dir':str(tmp_path),
            'source_title':'原标题','status':'queued','whisper_model':'tiny','device':'cpu',
            'language':'zh','content_mode':'raw','created_at':'2026-10-04T00:00:00','updated_at':'2026-10-04T00:00:00'})
        manifest = {'job_id':'title-worker','source_path':source,'source_sha256':'0'*64,'backend':'local',
            'model':'tiny','language':'zh','duration_sec':1.0,'raw_path':str(raw),'final_path':str(raw),
            'content_mode':'raw','prompt_version':'v1','created_at':'2026-10-04T00:00:00','source_title':'原标题'}
        response = {'side_effect':AgentError('provider-secret')} if provider_fails else {'return_value':'注意力与记忆'}
        with mock.patch('run_engine.run', return_value=manifest), mock.patch('engine.title_optimizer.optimize_title', **response) as title_call:
            account_settings=grant_account({'agent_enabled':False,'title_optimization_enabled':True},tmp_path)
            assert Worker(store,worker_id='title-worker',agent_settings=account_settings).step()
        job = store.get_job('title-worker')
        saved = store.get_manifest('title-worker')
        assert job['status'] == 'done'
        assert title_call.call_count == 1
        assert raw.read_text(encoding='utf-8') == '# 原标题\n\n完整转写中的例子与限定条件。'
        assert saved['title_status'] == ('skipped' if provider_fails else 'done')
        assert job['source_title'] == ('原标题' if provider_fails else '注意力与记忆')
        assert 'provider-secret' not in str(saved)
        assert Path(saved['final_path']).is_file()
    finally:
        store.close()
