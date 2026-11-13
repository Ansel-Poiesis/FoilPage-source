// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const sandbox={window:{}};
vm.runInNewContext(fs.readFileSync(new URL('../app/webview_ui/js/workbench.js',import.meta.url),'utf8'),sandbox);
const {stageLines,pipeline,percent}=sandbox.window.Workbench.__progressInternals;
const lines=task=>JSON.parse(JSON.stringify(stageLines(task)));

test('download failure does not claim transcription failure or completed download',()=>{
  const failed=lines({kind:'download',status:'failed',progress:.42});
  assert.equal(failed[0].text,'失败');
  assert.equal(failed[1].text,'未开始');
  assert.equal(pipeline({kind:'download',status:'failed'})[0].state,'failed');
});
test('download complete and ASR failed are separate stages',()=>{
  const failed=lines({kind:'download',status:'failed',downloaded_path:'test.mp4'});
  assert.equal(failed[0].text,'完成');
  assert.equal(failed[1].text,'失败');
  const only=lines({kind:'download',status:'done'});
  assert.equal(only[1].text,'未选择');
});
test('cancel and retry states never imply success',()=>{
  for(const kind of ['download','transcription']) {
    const cancel=lines({kind,status:'cancelled'});
    assert.equal(cancel[kind==='download'?0:1].text,'已取消');
    const retry=lines({kind,status:'retryable'});
    assert.equal(retry[kind==='download'?0:1].text,'待重试');
  }
});
test('active percentage is bounded and belongs only to the active stage',()=>{
  for(const value of [NaN,Infinity,-3,'broken',undefined,null]) assert.equal(percent({progress:value}),0);
  assert.equal(percent({progress:5}),100);
  const task=lines({kind:'download',status:'downloading',progress:.46});
  assert.equal(task[0].text,'46%');
  assert.equal(task[1].text,'未开始');
  assert(!lines({kind:'transcription',status:'done'}).some(row=>row.text.includes('%')));
});

test('render gating key: identical state skips, real changes rerender', () => {
  const { dataKey } = sandbox.window.Workbench.__progressInternals;
  const base = {
    currentView: 'workbench', filter: '全部', selected: new Set(['dl:1']),
    tasks: [
      { task_id: 'dl:1', status: 'downloading', progress: 0.464, error: '' },
      { task_id: 'dl:2', status: 'done', progress: 1, error: '' },
    ],
    summary: { text: '完成度 50%（1/2 已完成）' },
    worker: { running: true },
  };
  // 同内容不同对象身份 → 同 key（2s 推送空转时必须跳过整页重建）
  assert.equal(dataKey(base), dataKey(structuredCloneLike(base)));
  // 百分点内的进度抖动不算变化
  const jitter = structuredCloneLike(base);
  jitter.tasks[0].progress = 0.4641;
  assert.equal(dataKey(jitter), dataKey(base));
  // 选中集不参与数据指纹：点选行由 syncSelection 就地更新，不再全量重建
  const sel = structuredCloneLike(base);
  sel.selected.add('dl:2');
  assert.equal(dataKey(sel), dataKey(base));
  // 状态、过滤、错误的任一变化都必须触发重绘
  const doneState = structuredCloneLike(base);
  doneState.tasks[0] = { task_id: 'dl:1', status: 'done', progress: 1, error: '' };
  assert.notEqual(dataKey(doneState), dataKey(base));
  const filter = structuredCloneLike(base);
  filter.filter = '进行中';
  assert.notEqual(dataKey(filter), dataKey(base));
  const err = structuredCloneLike(base);
  err.tasks[0].error = 'media_forbidden';
  assert.notEqual(dataKey(err), dataKey(base));
  // 独立标题请求和产物写回可能不改变 done 状态，也必须更新行与检查器。
  for (const [field, value] of Object.entries({display_title:'精确的新标题',result_category:'创作',final_path:'final.md',raw_path:'raw.md',downloaded_path:'media.mp4'})) {
    const updated = structuredCloneLike(base);
    updated.tasks[1][field] = value;
    assert.notEqual(dataKey(updated), dataKey(base), `${field} writeback refreshes the view`);
  }
});

function structuredCloneLike(state) {
  return {
    ...state,
    selected: new Set(state.selected),
    tasks: state.tasks.map((t) => ({ ...t })),
    summary: { ...state.summary },
    worker: { ...state.worker },
  };
}
