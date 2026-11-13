// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { browserFallbackMediaUrls } from '../browser-extension/fallback-policy.js';
import { pickDouyinResolveCookies } from '../browser-extension/douyin-cookies.js';

async function harness(initial) {
  const saved = {}; const sent = []; const calls = []; let job = initial;
  let source = await readFile(new URL('../browser-extension/background.js', import.meta.url), 'utf8');
  source = source.replace(/^import .*;\r?\n/gm, '');
  const chrome = {
    runtime: { getManifest:()=>({version:'test'}), onMessage:{addListener(){}}, lastError:null },
    storage: { local:{async get(key){return typeof key==='string'?{[key]:saved[key]}:saved;},async set(data){Object.assign(saved,data);}},onChanged:{addListener(){}} },
    tabs: {query(query,cb){const result=[{id:1}];cb?.(result);return Promise.resolve(result);},sendMessage(id,message,cb){if(message.type==='shengjian-fresh-media')cb?.({ok:true,videoId:'1234567890123456',mediaUrl:'https://v26-web.douyinvod.com/video/tos/fresh.mp4',playUrl:'https://www.douyin.com/aweme/v1/play/?video_id=m1'});else sent.push(message);return Promise.resolve();}},
    cookies:{async getAll(){return [{name:'sessionid',value:'fixture',domain:'.douyin.com',path:'/',secure:true}];}},
    downloads:{onChanged:{addListener(){}},download(opts,cb){cb(1);},search(query,cb){cb?.([]);return Promise.resolve([]);}},
    alarms:{create(){},onAlarm:{addListener(){}}},
  };
  const context = {chrome,console,URL,AbortController,Date,Math,Promise,Set,Map,navigator:{userAgent:'fixture'},
    getToken:async()=>'',pickDouyinResolveCookies,browserFallbackMediaUrls,
    browserFallbackMediaUrl:args=>browserFallbackMediaUrls(args)[0]||'',REJECTED_REPLAY_SKIP_CODES:new Set(),
    fetch:async()=>({json:async()=>({ok:true})}),
    setTimeout:()=>({unref(){}}),clearTimeout(){},
    request:async(path,options)=>{calls.push({path,body:options?.body?JSON.parse(options.body):null});
      if(path.endsWith('/fallback')) { job={...job,status:'awaiting_browser',error:null};return{job:{...job,browser_download:{download_url:'https://v26-web.douyinvod.com/video/tos/fresh.mp4',filename:'fixture.mp4'}}}; }
      return {job};},
  };
  vm.runInNewContext(source+'\nglobalThis.fixture={watchLocalTask,pollLocalTask,restoreWatchedTasks,rememberBrowserDownload,takeBrowserDownload};',context);
  await new Promise(resolve=>setImmediate(resolve));
  return {saved,sent,calls,setJob(value){job=value;},...context.fixture};
}

test('browser handoff continues the same ID without an intermediate failure notification',async()=>{
  const job={id:'abc123def456',url:'https://www.douyin.com/video/1234567890123456',status:'awaiting_browser',error:{code:'media_forbidden',diagnostic:{fallback_pending:'browser'}},options:{transcribe_after_download:false}};
  const fixture=await harness(job);fixture.watchLocalTask(job);
  await new Promise(resolve=>setImmediate(resolve));
  const handoffs=fixture.calls.filter(call=>call.path.endsWith('/fallback'));
  assert.equal(handoffs.length,1);
  assert.equal(handoffs[0].path,'/v1/downloads/abc123def456/fallback');
  assert.deepEqual(handoffs[0].body.media_candidates,['https://v26-web.douyinvod.com/video/tos/fresh.mp4','https://www.douyin.com/aweme/v1/play/?video_id=m1']);
  // 先生 2026-10-18：首错换道会给一次安抚（stage=fallback）；除安抚外
  // 不得有任何中间失败/进度通知。
  assert.deepEqual(fixture.sent.filter(item=>item.stage!=='fallback').map(item=>item.message),[]);
  assert.equal(fixture.sent.filter(item=>item.stage==='fallback').length,1);
  assert.equal(fixture.calls.filter(call=>call.path==='/v1/downloads').length,0);
  fixture.setJob({...job,status:'done',download_complete:true,error:null});
  await fixture.pollLocalTask(job.id);
  assert.ok(fixture.sent.map(item=>item.message).includes('下载成功'));
  assert.equal(fixture.sent.filter(item=>item.stage==='download').length,1);
  await fixture.pollLocalTask(job.id);
  assert.equal(fixture.sent.filter(item=>item.stage!=='fallback').length,1);
});

test('download and transcription each notify success once and preserve video identity',async()=>{
  const job={id:'abc123def456',url:'https://www.youtube.com/watch?v=fixture123',status:'transcribing',download_complete:true,job_id:'asr123',options:{transcribe_after_download:true}};
  const fixture=await harness(job);fixture.watchLocalTask(job);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(fixture.sent.filter(item=>item.message==='下载成功').length,1);
  assert.equal(fixture.saved.watchedDownloadTasks[0].downloadNotified,true);
  fixture.setJob({...job,status:'done'});
  await fixture.pollLocalTask(job.id);
  assert.equal(fixture.sent.filter(item=>item.message==='转写成功').length,1);
  assert.ok(fixture.sent.every(item=>item.taskUrl===job.url));
  assert.equal(fixture.saved.watchedDownloadTasks.length,0);
});

test('browser candidate order tries all fresh renditions and independent play endpoints',()=>{
  const old='https://v3-web.douyinvod.com/video/tos/rejected.mp4';
  const first='https://v3-web.douyinvod.com/video/tos/first.mp4';
  const second='https://v26-web.douyinvod.com/video/tos/second.mp4';
  const play='https://www.douyin.com/aweme/v1/play/?video_id=m1';
  assert.deepEqual(browserFallbackMediaUrls({code:'media_forbidden',remembered:{mediaUrl:old},matchingFresh:{mediaUrl:old,mediaCandidates:[old,first,second,'https://v3-web.douyinvod.com/index.m3u8'],playUrl:play}}),[first,second,play]);
});


test('early Chrome events and concurrent map writes cannot lose or duplicate owned transfers',async()=>{
  const fixture=await harness({id:'abc123def456',status:'received'});
  assert.equal(await fixture.takeBrowserDownload(7),null);
  await Promise.all([fixture.rememberBrowserDownload(7,{taskId:'abc123def456'}),fixture.rememberBrowserDownload(8,{taskId:'def123abc456'})]);
  assert.equal(Object.keys(fixture.saved.shengjianBrowserDownloadJobs).length,2);
  assert.equal((await fixture.takeBrowserDownload(7)).taskId,'abc123def456');
  assert.equal(await fixture.takeBrowserDownload(7),null);
  assert.equal((await fixture.takeBrowserDownload(8)).taskId,'def123abc456');
  assert.equal(Object.keys(fixture.saved.shengjianBrowserDownloadJobs).length,0);
});

test('Douyin only reports submission and matching-video download success across all stages',async()=>{
  const job={id:'abc123def456',url:'https://www.douyin.com/video/1234567890123456',status:'awaiting_browser',options:{mode:'browser',transcribe_after_download:true}};
  const fixture=await harness(job);fixture.watchLocalTask(job);
  await new Promise(resolve=>setImmediate(resolve));
  for(const status of ['resolving','downloading','awaiting_browser','queued','transcribing']) {
    fixture.setJob({...job,status,progress:.7,download_complete:['queued','transcribing'].includes(status),job_id:'asr123'});
    await fixture.pollLocalTask(job.id);
  }
  fixture.setJob({...job,status:'done',download_complete:true,job_id:'asr123'});
  await fixture.pollLocalTask(job.id);
  assert.deepEqual(fixture.sent.map(item=>item.message),['下载成功']);
  assert.ok(fixture.sent.every(item=>item.taskUrl===job.url && item.taskId===job.id && item.stage==='download'));
});
