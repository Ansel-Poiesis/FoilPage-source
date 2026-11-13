// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { browserFallbackMediaUrls } from '../browser-extension/fallback-policy.js';
import { pickDouyinResolveCookies } from '../browser-extension/douyin-cookies.js';

const SOURCE = await readFile(new URL('../browser-extension/background.js', import.meta.url),'utf8');
const URL_A='https://www.douyin.com/video/1234567890123456';
const VIDEO_A='1234567890123456';
const MEDIA_A='https://v26-web.douyinvod.com/video/tos/original.mp4?signature=fixture';
const MEDIA_B='https://v3-web.douyinvod.com/video/tos/other.mp4';
const offline=()=>Object.assign(new Error('fixture offline'),{code:'local_api_unavailable'});
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
async function harness({online=true,seed={},fresh=null,websocket=false,handler=null,initialJobs=[],deliveryAck=true,lookupError=false,downloadItems={}}={}) {
  const saved=structuredClone(seed), sent=[], calls=[], transfers=[], timers=[], sockets=[];
  const jobs=new Map(initialJobs.map(job=>[job.id,structuredClone(job)])); let listener, token='fixture-paired-token', now=1000000, cookieReads=0, sequence=0;
  const refs=new Map();
  class FakeWebSocket {
    static CONNECTING=0; static OPEN=1; constructor(){this.readyState=0;sockets.push(this);}
    send(){} close(){this.readyState=3;this.onclose?.();}
  }
  const chrome={
    runtime:{getManifest:()=>({version:'1.0.0'}),getContexts:async()=>[],lastError:null,onMessage:{addListener(value){listener=value;}}},
    storage:{local:{async get(key){if(typeof key==='string')return{[key]:structuredClone(saved[key])};return structuredClone(saved);},async set(data){Object.assign(saved,structuredClone(data));}},onChanged:{addListener(){}}},
    tabs:{query(query,cb){const result=[{id:1}];cb?.(result);return Promise.resolve(result);},sendMessage(id,message,cb){if(message.type==='shengjian-fresh-media'){cb?.(fresh);return Promise.resolve(fresh);}sent.push(message);if(deliveryAck){cb?.({ok:true});return Promise.resolve({ok:true});}return Promise.resolve(undefined);}},
    cookies:{async getAll(){cookieReads++;return[{name:'sessionid',value:'fixture',domain:'.douyin.com',path:'/',secure:true}];}},
    downloads:{onChanged:{addListener(){}},download(opts,cb){transfers.push(opts);cb(transfers.length);},async cancel(){},search(query,cb){if(lookupError)chrome.runtime.lastError={message:'Download service temporarily unavailable'};const items=downloadItems[query.id]||[];cb?.(items);chrome.runtime.lastError=null;return Promise.resolve(items);}},
    alarms:{create(){},onAlarm:{addListener(){}}},
  };
  const context={chrome,URL,AbortController,Date:class extends Date{static now(){return now;}},Math,Promise,Set,Map,console:{log(){},warn(){}},navigator:{userAgent:'fixture'},
    crypto:{randomUUID:()=>`fixture-request-${String(++sequence).padStart(12,'0')}`}, getToken:async()=>token,
    pickDouyinResolveCookies,browserFallbackMediaUrls,
    fetch:async()=>({ok:true,json:async()=>({ok:true,extension_latest:'1.0.0'})}),
    setTimeout(fn,delay){const timer={fn,delay,unref(){}};timers.push(timer);return timer;},clearTimeout(){},
    request:async(path,options)=>{
      const body=options?.body?JSON.parse(options.body):null;calls.push({path,body});
      if(!online)throw offline();
      if(handler){const value=await handler({path,body,jobs,refs});if(value!==undefined)return value;}
      if(path.startsWith('/v1/downloads?'))return{jobs:[...jobs.values()]};
      if(path==='/v1/downloads'){
        if(body.cancel_request_id){const id=refs.get(body.cancel_request_id);const job=jobs.get(id);if(job)job.status='cancelled';return{cancelled:true,job:job||null};}
        let job=jobs.get(refs.get(body.client_request_id));
        if(!job){const id=(jobs.size+1).toString(16).padStart(12,'0');job={id,url:body.url,title:body.title,status:body.mode==='browser'?'awaiting_browser':'downloading',progress:.2,options:body.options,error:null};jobs.set(id,job);refs.set(body.client_request_id,id);}
        return {job:body.mode==='browser'&&job.status==='awaiting_browser'?{...job,browser_download:{download_url:body.media_url||MEDIA_A,filename:'fixture.mp4'}}:job};
      }
      const match=path.match(/^\/v1\/downloads\/([a-f0-9]{12})(?:\/(.*))?$/);
      if(match){const job=jobs.get(match[1]);if(match[2]==='cancel'&&job)job.status='cancelled';return{job};}
      return{ok:true};
    },
  };
  if(websocket)context.WebSocket=FakeWebSocket;
  vm.runInNewContext(SOURCE.replace(/^import .*;\r?\n/gm,'')+'\nglobalThis.fixture={submitPageDownload,submitAuthorizedDownload,flushSubmissionQueue,resumeBackgroundQueue,cancelQueuedTask,taskStateForUrl,status,watchLocalTask,pollLocalTask,restoreWatchedTasks,handleLocalTaskFailed,askTabsForFreshMedia,connectDesktopEvents,handleBrowserDownloadComplete,handleBrowserDownloadInterrupted,recoverBrowserDownloads,flushPendingImports,cancelOwnedBrowserDownloads,submissionQueue,localTaskWatchers,submitMediaHints,queuedMediaHints};',context);
  await settle();
  return {saved,sent,calls,transfers,timers,sockets,jobs,refs,chrome,...context.fixture,
     setOnline(value){online=value;},setToken(value){token=value;},setDeliveryAck(value){deliveryAck=value;},setLookupError(value){lookupError=value;},advance(ms){now+=ms;},get cookieReads(){return cookieReads;},
    message(message){return new Promise(resolve=>listener(message,{},resolve));},async settle(){await settle();}};
}

for (const [name, packet] of [
  ['YouTube local', {url:'https://www.youtube.com/watch?v=fixture123'}],
  ['Douyin local media', {url:URL_A, mediaUrl:MEDIA_A}],
  ['Douyin browser media', {url:URL_A, mediaUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'}],
]) {
  test(`${name}: login rejection gives one clear submission response without download failure records`, async()=>{
    const f=await harness({handler:({path,body})=>{
      if(path==='/v1/downloads' && body?.url) throw Object.assign(new Error('请先在 FoilPage 桌面端登录'), {code:'login_required',status:403});
    }});
    const result=await f.submitPageDownload({...packet,title:'来源标题',transcribe:true});
    await f.flushSubmissionQueue();await f.flushSubmissionQueue();
    assert.equal(result.submission_failed,true);
    assert.equal(result.error.code,'login_required');
    assert.match(result.error.message,/FoilPage 桌面端登录/);
    assert.equal(f.calls.filter(call=>call.path==='/v1/downloads' && call.body?.url).length,1);
    assert.equal(f.calls.filter(call=>call.path==='/v1/downloads/failures' || call.path.endsWith('/browser-failure')).length,0);
    assert.equal(f.sent.filter(message=>message.stage==='failure').length,1);
    assert.equal(f.sent.filter(message=>message.stage==='submitted').length,0);
    assert.equal(f.transfers.length,0);assert.equal(f.jobs.size,0);assert.equal(f.cookieReads,0);
    assert.equal((f.saved.pendingDownloadFailureReports||[]).length,0);
  });
}

test('offline click persists one original-video queue entry without false acceptance or credentials',async()=>{
  const f=await harness({online:false,fresh:{ok:true,videoId:'9999999999999999',mediaUrl:MEDIA_B}});
  const packet={url:URL_A,title:'原视频标题',mediaUrl:MEDIA_A,mediaCandidates:[MEDIA_A],transcribe:true};
  const first=await f.submitPageDownload(packet),second=await f.submitPageDownload(packet);
  assert.equal(first.id,second.id);assert.equal(first.queued,true);
  assert.equal(f.saved.shengjianSubmissionQueue.length,1);
  assert.equal(f.calls.filter(c=>c.path==='/v1/downloads').length,0);
  assert.equal(f.sent.filter(m=>m.stage==='waiting').length,0);
  assert.equal(f.sent.filter(m=>m.stage==='submitted').length,0);
  assert.equal(f.cookieReads,0);
  assert.ok(!JSON.stringify(f.saved).includes('signature=fixture'));
  f.setOnline(true);await f.flushSubmissionQueue();await f.settle();
  const submitted=f.calls.find(c=>c.path==='/v1/downloads');
  assert.equal(submitted.body.url,URL_A);assert.equal(submitted.body.title,'原视频标题');
  assert.equal(submitted.body.media_url,MEDIA_A);assert.deepEqual(submitted.body.media_candidates,[MEDIA_A]);
  assert.equal(submitted.body.client_request_id,first.id);
  assert.equal(f.sent.filter(m=>m.stage==='submitted').length,1);
});

test('submission response lost after durable acceptance reuses the same client receipt',async()=>{
  let lose=true;
  const f=await harness({handler:({path,body,jobs,refs})=>{
    if(path==='/v1/downloads'&&lose){lose=false;const job={id:'abc123def456',url:body.url,title:body.title,status:'downloading',progress:.1,options:body.options};jobs.set(job.id,job);refs.set(body.client_request_id,job.id);throw offline();}
  }});
  const queued=await f.submitPageDownload({url:'https://www.youtube.com/watch?v=fixture123',title:'来源标题'});
  assert.equal(queued.queued,true);assert.equal(f.jobs.size,1);assert.equal(f.sent.filter(m=>m.stage==='submitted').length,0);
  await f.flushSubmissionQueue();await f.settle();
  const submits=f.calls.filter(c=>c.path==='/v1/downloads');
  assert.equal(submits.length,2);assert.equal(submits[0].body.client_request_id,submits[1].body.client_request_id);
  assert.equal(f.jobs.size,1);assert.equal(f.sent.filter(m=>m.stage==='submitted').length,1);
  assert.equal(f.saved.shengjianSubmissionQueue[0].taskId,'abc123def456');
});

test('offline queue survives worker restart and rejects a different current video capture',async()=>{
  const first=await harness({online:false});const queued=await first.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
  const restarted=await harness({seed:first.saved,fresh:{ok:true,videoId:'9999999999999999',mediaUrl:MEDIA_B}});
  const submit=restarted.calls.find(c=>c.path==='/v1/downloads');
  assert.equal(submit.body.url,URL_A);assert.equal(submit.body.client_request_id,queued.id);
  assert.equal(submit.body.media_url,'');assert.ok(!JSON.stringify(submit.body).includes(MEDIA_B));
  assert.equal(restarted.transfers[0].url,MEDIA_A); // fixture resolver returns an original-video URL
});

test('a cached original-video response may resume a queue after the page has switched',async()=>{
  const queued={clientId:'fixture-request-000000000001',url:URL_A,title:'原视频',videoId:VIDEO_A,transcribe:false,createdAt:1,status:'handoff_pending'};
  const f=await harness({seed:{shengjianSubmissionQueue:[queued]},fresh:{ok:true,videoId:VIDEO_A,mediaUrl:MEDIA_A,mediaCandidates:[MEDIA_A]}});
  assert.equal(f.calls.find(c=>c.path==='/v1/downloads').body.media_url,MEDIA_A);
});

test('watcher retains progress through more than 200 misses and resumes separate terminal stages',async()=>{
  const f=await harness({online:false});
  const job={id:'abc123def456',url:URL_A,title:'原视频',status:'downloading',progress:.4,options:{transcribe_after_download:true}};
  f.jobs.set(job.id,job);f.watchLocalTask(job);await f.settle();
  for(let i=0;i<250;i++)await f.pollLocalTask(job.id);
  assert.equal(f.localTaskWatchers.size,1);assert.equal(f.saved.watchedDownloadTasks.length,1);
  assert.equal(f.saved.watchedDownloadTasks[0].lastJob.progress,.4);
  assert.equal(f.sent.length,0);assert.equal(f.timers.at(-1).delay,30000);
  f.setOnline(true);Object.assign(job,{status:'transcribing',download_complete:true,job_id:'asr123'});
  await f.pollLocalTask(job.id);await f.pollLocalTask(job.id);
  assert.equal(f.sent.filter(m=>m.stage==='download').length,1);
  Object.assign(job,{status:'done'});await f.pollLocalTask(job.id);await f.pollLocalTask(job.id);
  assert.equal(f.sent.filter(m=>m.stage==='transcribe').length,0);
  assert.ok(f.sent.every(m=>m.taskUrl===URL_A&&m.sourceTitle==='原视频'));
});

test('temporary fallback transport outage does not finish or erase the original media snapshot',async()=>{
  const f=await harness({online:false});
  const job={id:'abc123def456',url:URL_A,status:'awaiting_browser',error:{code:'media_forbidden',diagnostic:{fallback_pending:'browser'}},options:{}};
  f.submitMediaHints.set(job.id,{mediaUrl:MEDIA_A,playUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});
  await f.handleLocalTaskFailed(job);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/finish-fallback')).length,0);
  assert.ok(f.submitMediaHints.has(job.id));
  // 先生 2026-10-18：首错换道必给一次安抚（stage=fallback），离线也不例外；
  // 除此之外仍不得有失败/进度类页面通知。
  const reassurance=f.sent.filter(m=>m.stage==='fallback');
  assert.equal(reassurance.length,1);assert.match(reassurance[0].message,/别急，正在尝试其他下载办法/);
  assert.equal(f.sent.filter(m=>m.stage!=='fallback').length,0);
});

test('fallback reassurance fires once per task even when the stage is retried',async()=>{
  const f=await harness({online:false});
  const job={id:'abc123def456',url:URL_A,status:'awaiting_browser',error:{code:'media_forbidden',diagnostic:{fallback_pending:'browser'}},options:{}};
  f.submitMediaHints.set(job.id,{mediaUrl:MEDIA_A,playUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});
  await f.handleLocalTaskFailed(job);
  // 同 stage 重入被 fallbacksInFlight 去重；换 stage 重入时 fallbackReassured
  // 也保证每个任务只安抚一次。
  await f.handleLocalTaskFailed(job);
  job.error.diagnostic.fallback_pending='resolver';
  await f.handleLocalTaskFailed(job);
  assert.equal(f.sent.filter(m=>m.stage==='fallback').length,1);
});

test('queued click can be cancelled offline and never submits after reconnect',async()=>{
  const f=await harness({online:false});const job=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
  const cancelled=await f.cancelQueuedTask(job.id);assert.equal(cancelled.status,'cancelled');
  f.setOnline(true);await f.flushSubmissionQueue();
  assert.equal(f.calls.filter(c=>c.path==='/v1/downloads').length,0);
  assert.equal(f.queuedMediaHints.size,0);
});

test('ambiguous accepted submission cancels by its durable receipt without another download',async()=>{
  let lose=true;
  const f=await harness({handler:({path,body,jobs,refs})=>{
    if(path==='/v1/downloads'&&!body.cancel_request_id&&lose){lose=false;const job={id:'abc123def456',url:body.url,title:body.title,status:'downloading',progress:.1,options:body.options};jobs.set(job.id,job);refs.set(body.client_request_id,job.id);throw offline();}
  }});
  const queued=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
  f.setOnline(false);await f.cancelQueuedTask(queued.id);f.setOnline(true);await f.flushSubmissionQueue();await f.settle();
  assert.equal(f.jobs.get('abc123def456').status,'cancelled');
  assert.equal(f.calls.filter(c=>c.path==='/v1/downloads'&&!c.body.cancel_request_id).length,1);
  assert.equal(f.transfers.length,0);
});

test('returning to the original page reads durable progress without replaying success notices',async()=>{
  const f=await harness();const result=await f.submitPageDownload({url:'https://www.youtube.com/watch?v=fixture123',title:'原标题'});await f.settle();
  const job=f.jobs.get(result.id);Object.assign(job,{status:'done',download_complete:true});await f.pollLocalTask(job.id);
  const count=f.sent.length,state=await f.taskStateForUrl(job.url);
  assert.equal(state.status,'done');assert.equal(state.title,'原标题');assert.equal(f.sent.length,count);
  const restart=await harness({seed:f.saved});assert.equal(restart.sent.length,0);assert.equal((await restart.taskStateForUrl(job.url)).status,'done');
});

test('offline WebSocket preflight creates no socket and backoff is bounded',async()=>{
  const f=await harness({online:false,websocket:true});
  assert.equal(f.sockets.length,0);assert.ok(f.timers.some(t=>t.delay===5000));
  let timer=f.timers.find(t=>t.delay===5000);timer.fn();await f.settle();
  assert.equal(f.sockets.length,0);assert.ok(f.timers.some(t=>t.delay===10000));
  for(let i=0;i<8;i++){f.timers.at(-1).fn();await f.settle();}
  assert.ok(f.timers.every(t=>t.delay<=60000));
  f.setOnline(true);await Promise.all([f.connectDesktopEvents(),f.connectDesktopEvents(),f.connectDesktopEvents()]);
  assert.equal(f.sockets.length,1);f.sockets[0].readyState=1;f.sockets[0].onopen();await f.settle();
  await f.connectDesktopEvents();assert.equal(f.sockets.length,1);
});

test('download followed by transcription failure keeps a successful download and names the failed stage',async()=>{
  const f=await harness();const result=await f.submitPageDownload({url:'https://www.youtube.com/watch?v=fixture123',title:'原标题',transcribe:true});await f.settle();
  const job=f.jobs.get(result.id);Object.assign(job,{status:'failed',download_complete:true,job_id:'asr123'});await f.pollLocalTask(job.id);
  assert.equal(f.sent.filter(m=>m.message==='下载成功').length,1);
  assert.equal(f.sent.filter(m=>m.message==='转写失败，请在 FoilPage 查看').length,1);
  assert.equal(f.sent.filter(m=>m.message==='下载失败，请在 FoilPage 查看').length,0);
});


test('Chrome interruption during desktop outage retains its owned mapping until acknowledged',async()=>{
  const f=await harness();
  const result=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});await f.settle();
  f.chrome.downloads.search=(query,callback)=>{const items=[{id:1,state:'interrupted',error:'NETWORK_FAILED',bytesReceived:10,totalBytes:100}];callback?.(items);return Promise.resolve(items);};
  f.setOnline(false);await f.handleBrowserDownloadInterrupted(1);
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,1);
  assert.equal(f.calls.filter(call=>call.path==='/v1/download-failures').length,0);
  assert.equal(f.jobs.get(result.id).status,'awaiting_browser');
  f.setOnline(true);await f.recoverBrowserDownloads();
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,0);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,2); // failed transport + acknowledged retry
  assert.equal(f.sent.filter(message=>message.stage==='failure').length,0);
});

function missingHistoryFixture({online=true,lookupError=false,startedAt=980000}={}) {
  const job={id:'abc123def456',url:URL_A,title:'原视频',status:'awaiting_browser',options:{mode:'browser',browser_fallback:true}};
  return harness({online,lookupError,initialJobs:[job],seed:{shengjianBrowserDownloadJobs:{'7':{taskId:job.id,title:job.title,startedAt}}},handler:({path,body,jobs})=>{
    if(path.endsWith('/browser-failure')) {
      assert.equal(body.browser_error,'BROWSER_DOWNLOAD_HISTORY_MISSING');
      const failed=jobs.get(job.id);failed.status='failed';return{job:failed};
    }
  }});
}

test('confirmed missing Chrome history closes only its durable owned task',async()=>{
  const f=await missingHistoryFixture();
  assert.equal(f.jobs.get('abc123def456').status,'failed');
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,0);
  await f.recoverBrowserDownloads();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,1);
  assert.equal(f.transfers.length,0);
});

test('temporary Chrome lookup errors preserve history ownership and never report missing',async()=>{
  const f=await missingHistoryFixture({lookupError:true});
  await f.recoverBrowserDownloads();
  assert.equal(f.jobs.get('abc123def456').status,'awaiting_browser');
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,1);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,0);
});

test('missing history report survives desktop outage until the original task acknowledges it',async()=>{
  const f=await missingHistoryFixture({online:false});
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,1);
  assert.equal(f.jobs.get('abc123def456').status,'awaiting_browser');
  const offlineReports=f.calls.filter(call=>call.path.endsWith('/browser-failure')).length;
  assert.ok(offlineReports>=1);
  f.setOnline(true);await f.recoverBrowserDownloads();
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,0);
  assert.equal(f.jobs.get('abc123def456').status,'failed');
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,offlineReports+1);
});

test('newly created Chrome transfers have a ten-second lookup grace window',async()=>{
  const f=await missingHistoryFixture({startedAt:995000});
  f.advance(4999);await f.recoverBrowserDownloads();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,0);
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,1);
  f.advance(1);await f.recoverBrowserDownloads();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,1);
  assert.equal(f.jobs.get('abc123def456').status,'failed');
});

test('legacy Chrome ownership without a timestamp gets its own first-observation grace',async()=>{
  const f=await missingHistoryFixture({startedAt:null});
  assert.equal(f.saved.shengjianBrowserDownloadJobs['7'].missingSince,1000000);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,0);
  f.advance(9999);await f.recoverBrowserDownloads();
  assert.equal(f.jobs.get('abc123def456').status,'awaiting_browser');
  f.advance(1);await f.recoverBrowserDownloads();
  assert.equal(f.jobs.get('abc123def456').status,'failed');
});

test('a malformed missing-history acknowledgement never discards the owned download',async()=>{
  const job={id:'abc123def456',url:URL_A,status:'awaiting_browser',options:{mode:'browser'}};
  const f=await harness({initialJobs:[job],seed:{shengjianBrowserDownloadJobs:{'7':{taskId:job.id,startedAt:980000}}},handler:({path})=>{
    if(path.endsWith('/browser-failure'))return{ok:true};
  }});
  await f.recoverBrowserDownloads();
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,1);
  assert.equal(f.jobs.get(job.id).status,'awaiting_browser');
  assert.equal(f.transfers.length,0);
});

test('completed Chrome transfer atomically persists pending import and resumes after restart',async()=>{
  const f=await harness();await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});await f.settle();
  const completed={id:1,state:'complete',filename:'C:/fixture/Downloads/FoilPage/original.mp4'};
  f.chrome.downloads.search=(query,callback)=>{callback?.([completed]);return Promise.resolve([completed]);};
  f.setOnline(false);await f.handleBrowserDownloadComplete(1);
  assert.equal(Object.keys(f.saved.shengjianBrowserDownloadJobs).length,0);
  assert.equal(f.saved.shengjianPendingImports.length,1);
  assert.equal(f.calls.filter(call=>call.path==='/v1/imports').length,0);
  const restart=await harness({seed:f.saved,initialJobs:[...f.jobs.values()]});
  restart.chrome.downloads.search=(query,callback)=>{callback?.([completed]);return Promise.resolve([completed]);};
  await restart.resumeBackgroundQueue();
  assert.equal(restart.calls.filter(call=>call.path==='/v1/imports').length,1);
  assert.equal(restart.saved.shengjianPendingImports.length,0);
});

test('a live Chrome transfer exposes byte progress after leaving the source tab',async()=>{
  const f=await harness();const result=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});await f.settle();
  f.chrome.downloads.search=(query,callback)=>{const items=[{id:1,state:'in_progress',bytesReceived:42,totalBytes:100}];callback?.(items);return Promise.resolve(items);};
  await f.pollLocalTask(result.id);
  const jobs=await f.status(),snapshot=await f.taskStateForUrl(URL_A);
  assert.equal(snapshot.progress,.42);assert.equal(jobs.find(job=>job.id===result.id).progress,.42);
  assert.equal(f.transfers.length,1);
});

test('permanent import rejection keeps its durable work until the terminal report is acknowledged',async()=>{
  const job={id:'abc123def456',url:URL_A,title:'无后缀残留',status:'awaiting_browser',options:{mode:'browser',browser_fallback:true}};
  let failReport=true;
  const f=await harness({initialJobs:[job],seed:{shengjianPendingImports:[{downloadId:7,taskId:job.id,title:job.title,transcribe:false}]},handler:({path,jobs})=>{
    if(path==='/v1/imports')throw Object.assign(new Error('不支持的媒体格式'),{code:'unsupported_media_format',status:415});
    if(path.endsWith('/browser-failure')){
      if(failReport)throw offline();
      const failed=jobs.get(job.id);failed.status='failed';
      return{job:failed};
    }
  }});
  f.chrome.downloads.search=(query,callback)=>{const items=[{id:7,state:'complete',filename:'C:/fixture/Downloads/FoilPage/无后缀残留'}];callback?.(items);return Promise.resolve(items);};
  await f.flushPendingImports();await f.settle();
  assert.equal(f.saved.shengjianPendingImports.length,1,'a lost failure report must stay recoverable');
  assert.equal(f.jobs.get(job.id).status,'awaiting_browser');
  failReport=false;await f.flushPendingImports();await f.settle();
  assert.equal(f.saved.shengjianPendingImports.length,0,'acknowledged terminal failure drains the import work');
  assert.equal(f.jobs.get(job.id).status,'failed');
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).length,2);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/browser-failure')).at(-1).body.phase,'browser_download_import');
  assert.equal(f.transfers.length,0);
});

// 2026-10-20 实机：downloadId 271 的 entry 自 10 月初起永久滞留（任务已不在、
// Chrome 下载记录已被清理），每轮 flush 都 remaining——自更新被它冻死。
test('day-old pending imports with missing Chrome records are drained',async()=>{
  const f=await harness({seed:{shengjianPendingImports:[
    {downloadId:271,taskId:'aac8aff37dc7',title:'已消失的下载',transcribe:false,queuedAt:1000000-25*60*60*1000},
    {downloadId:272,taskId:'bbb222ccc333',title:'新鲜滞留',transcribe:false,queuedAt:1000000-60_000},
  ]}});
  await f.flushPendingImports();await f.settle();
  const remaining=f.saved.shengjianPendingImports||[];
  assert.equal(remaining.length,1,'only the fresh entry survives');
  assert.equal(remaining[0].downloadId,272);
});

test('a day-old completed download still imports after the desktop reconnects',async()=>{
  const job={id:'aac8aff37dc7',url:URL_A,status:'awaiting_browser',options:{}};
  const f=await harness({initialJobs:[job],downloadItems:{271:[{id:271,filename:'C:/Downloads/resumable.mp4',state:'complete'}]},seed:{shengjianPendingImports:[
    {downloadId:271,taskId:job.id,title:'保留的下载',transcribe:false,queuedAt:1000000-25*60*60*1000},
  ]}});
  await f.flushPendingImports();await f.settle();
  assert.ok(f.calls.some(call=>call.path==='/v1/imports'&&call.body.path==='C:/Downloads/resumable.mp4'));
  assert.equal((f.saved.shengjianPendingImports||[]).length,0);
});

test('a late completed Chrome file repairs the same failed desktop task and its receipt',async()=>{
  const job={id:'aac8aff37dc7',url:URL_A,status:'failed',error:{code:'douyin_verification_required'},options:{mode:'browser'}};
  const f=await harness({initialJobs:[job],downloadItems:{271:[{id:271,filename:'C:/Downloads/late-success.mp4',state:'complete'}]},seed:{
    shengjianPendingImports:[{downloadId:271,taskId:job.id,title:'迟到的成功文件',transcribe:false,queuedAt:999000}],
    shengjianSubmissionQueue:[{clientId:'fixture-late-success',url:URL_A,taskId:job.id,status:'failed',lastJob:job,createdAt:999000,submittedNotified:true,failureNotified:true}],
  },handler:({path,body,jobs})=>{
    if(path==='/v1/imports'){
      assert.equal(body.download_id,job.id);
      assert.equal(body.browser_download,true);
      Object.assign(jobs.get(job.id),{status:'done',download_complete:true,progress:1,error:null});
      return {ok:true};
    }
  }});
  await f.flushPendingImports();await f.settle();
  assert.equal(f.calls.filter(call=>call.path==='/v1/imports').length,1);
  assert.equal(f.jobs.size,1,'late success must not create another download task');
  assert.equal(f.saved.shengjianSubmissionQueue[0].lastJob.status,'done');
  assert.equal((f.saved.shengjianPendingImports||[]).length,0);
  assert.ok(f.sent.some(message=>message.stage==='download'&&!message.error));
});

test('status refresh persists recovered success so going offline cannot restore a stale failure',async()=>{
  const failed={id:'aac8aff37dc7',url:URL_A,status:'failed',error:{code:'network_error'}};
  const done={...failed,status:'done',download_complete:true,progress:1,error:null};
  const f=await harness({initialJobs:[done],seed:{shengjianSubmissionQueue:[
    {clientId:'fixture-recovered-cache',url:URL_A,taskId:done.id,status:'failed',lastJob:failed,createdAt:999000,submittedNotified:true,failureNotified:true},
  ]}});
  assert.equal((await f.status()).find(item=>item.id===done.id).status,'done');
  f.setOnline(false);
  assert.equal((await f.status()).find(item=>item.id===done.id).status,'done');
  assert.equal(f.saved.shengjianSubmissionQueue[0].lastJob.status,'done');
});

test('an older failed receipt is reconciled even after success leaves the recent task window',async()=>{
  const failed={id:'aac8aff37dc7',url:URL_A,status:'failed',error:{code:'network_error'}};
  const done={...failed,status:'done',download_complete:true,progress:1,error:null};
  const f=await harness({initialJobs:[done],seed:{shengjianSubmissionQueue:[
    {clientId:'fixture-older-recovery',url:URL_A,taskId:done.id,status:'failed',lastJob:failed,createdAt:999000,submittedNotified:true,failureNotified:true},
  ]},handler:({path})=>{if(path.startsWith('/v1/downloads?'))return{jobs:[]};}});
  assert.equal((await f.status()).find(item=>item.id===done.id).status,'done');
  assert.equal(f.saved.shengjianSubmissionQueue[0].lastJob.status,'done');
});

for(const status of ['cancelled','done'])test(`late browser completion never reimports a ${status} desktop task`,async()=>{
  const job={id:'aac8aff37dc7',url:URL_A,status,options:{mode:'browser'}};
  const f=await harness({initialJobs:[job],downloadItems:{271:[{id:271,filename:'C:/Downloads/retained.mp4',state:'complete'}]},seed:{
    shengjianPendingImports:[{downloadId:271,taskId:job.id,transcribe:false,queuedAt:999000}],
  }});
  await f.flushPendingImports();await f.settle();
  assert.equal(f.calls.filter(call=>call.path==='/v1/imports').length,0);
  assert.equal(f.jobs.get(job.id).status,status);
});

test('a page notification failure cannot block a fallback handoff',async()=>{
  const f=await harness({fresh:{ok:true,videoId:VIDEO_A,mediaUrl:MEDIA_B}});
  const job={id:'abc123def456',url:URL_A,status:'awaiting_browser',error:{code:'media_forbidden',diagnostic:{fallback_pending:'browser'}},options:{}};
  f.jobs.set(job.id,job);
  const query=f.chrome.tabs.query;
  let first=true;
  f.chrome.tabs.query=(...args)=>{if(first){first=false;throw new Error('tabs unavailable');}return query(...args);};
  await assert.doesNotReject(f.handleLocalTaskFailed(job));
  assert.ok(f.calls.some(call=>call.path===`/v1/downloads/${job.id}/fallback`));
});

test('a pending import whose desktop task is gone is dropped immediately',async()=>{
  const f=await harness({seed:{shengjianPendingImports:[
    {downloadId:9,taskId:'aac8aff37dc7',title:'任务已清理',transcribe:false,queuedAt:1000000-60_000},
  ]},handler:({path})=>{
    if(path==='/v1/downloads/aac8aff37dc7')throw Object.assign(new Error('下载任务不存在'),{code:'not_found',status:404});
  }});
  await f.flushPendingImports();await f.settle();
  assert.equal((f.saved.shengjianPendingImports||[]).length,0,'not_found must drain the entry without waiting a day');
  assert.equal(f.calls.filter(call=>call.path==='/v1/imports').length,0);
});

test('unacknowledged failed receipts never become a new submitted notice on worker restart',async()=>{
  const f=await harness({deliveryAck:false});
  const accepted=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:MEDIA_A});await f.settle();
  const job=f.jobs.get(accepted.id);job.status='failed';
  await f.pollLocalTask(job.id);await f.settle();
  assert.equal(f.saved.shengjianSubmissionQueue[0].submittedNotified,false);
  assert.equal(f.saved.shengjianSubmissionQueue[0].failureNotified,true,'failure dedup is part of the durable receipt');
  const restored=await harness({seed:f.saved,initialJobs:[job]});
  await restored.resumeBackgroundQueue();await restored.settle();
  assert.equal(restored.sent.filter(message=>message.stage==='submitted').length,0,'a failed task must not replay submission success');
  assert.equal(restored.sent.filter(message=>message.stage==='failure').length,0,'already reported failure stays deduplicated');
  const restoredAgain=await harness({seed:restored.saved,initialJobs:[job]});
  await restoredAgain.resumeBackgroundQueue();await restoredAgain.settle();
  assert.equal(restoredAgain.sent.length,0,'a second worker recovery keeps terminal receipts quiet');
});


test('cancellation while an accepted POST is in flight never starts the Chrome download',async()=>{
  let release,started;
  const pending=new Promise(resolve=>{release=resolve;}),waiting=new Promise(resolve=>{started=resolve;});
  const f=await harness({handler:async({path,body,jobs,refs})=>{
    if(path==='/v1/downloads'&&!body.cancel_request_id){
      const job={id:'abc123def456',url:body.url,title:body.title,status:'awaiting_browser',options:body.options};jobs.set(job.id,job);refs.set(body.client_request_id,job.id);
      started();await pending;return{job:{...job,browser_download:{download_url:MEDIA_A,filename:'fixture.mp4'}}};
    }
  }});
  const submit=f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:'https://www.douyin.com/aweme/v1/play/?video_id=fixture'});
  await waiting;const entry=[...f.submissionQueue.values()][0];
  const cancel=f.cancelQueuedTask(entry.clientId);await f.settle();release();
  await Promise.all([submit,cancel]);await f.settle();
  assert.equal(f.transfers.length,0);assert.equal(f.jobs.get('abc123def456').status,'cancelled');
  assert.equal(f.sent.filter(message=>message.stage==='submitted').length,0);
});

test('a restarted worker cancels an owned live Chrome transfer from its durable mapping',async()=>{
  const job={id:'abc123def456',url:URL_A,title:'原视频',status:'awaiting_browser',options:{mode:'browser'}};
  const seed={shengjianBrowserDownloadJobs:{'7':{taskId:job.id,title:job.title,videoId:VIDEO_A}}};
  const f=await harness({seed,initialJobs:[job]});
  const cancelled=[];f.chrome.downloads.cancel=async id=>cancelled.push(id);
  await f.cancelOwnedBrowserDownloads(job.id);
  assert.deepEqual(cancelled,[7]);
  f.chrome.downloads.search=(query,callback)=>{const items=[{id:7,state:'in_progress',bytesReceived:60,totalBytes:100}];callback?.(items);return Promise.resolve(items);};
  f.watchLocalTask(job);await f.settle();await f.pollLocalTask(job.id);
  assert.equal(f.saved.watchedDownloadTasks[0].lastJob.progress,.6);
});


test('authorized browser submission accepts once and snapshots all original-video candidates',async()=>{
  const f=await harness();const play='https://www.douyin.com/aweme/v1/play/?video_id=fixture';
  const job=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:play,playUrl:play,mediaCandidates:[play,MEDIA_A]});await f.settle();
  assert.equal(f.sent.filter(message=>message.stage==='submitted').length,1);
  assert.equal(f.sent.find(message=>message.stage==='submitted').taskUrl,URL_A);
  assert.equal(f.saved.shengjianSubmissionQueue[0].submittedNotified,true);
  assert.equal(f.transfers.length,1);assert.equal(f.cookieReads,0);
  assert.deepEqual(Array.from(f.submitMediaHints.get(job.id).mediaCandidates),[play,MEDIA_A]);
  assert.ok(!JSON.stringify(f.saved).includes('signature=fixture'));
});

test('an unacknowledged Douyin receipt stays pending and is retried when the page listener returns',async()=>{
  const f=await harness({deliveryAck:false});
  const job=await f.submitPageDownload({url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
  await f.settle();
  assert.equal(job.status,'awaiting_browser','the API acceptance is still returned to the page caller');
  assert.equal(f.saved.shengjianSubmissionQueue[0].submittedNotified,false,'a failed one-way send is not recorded as delivered');
  f.setDeliveryAck(true);
  await f.flushSubmissionQueue();
  assert.equal(f.saved.shengjianSubmissionQueue[0].submittedNotified,true,'a later page acknowledgement closes the receipt');
  assert.equal(f.sent.filter(message=>message.stage==='submitted').length,2,'the pending receipt is retried once the target listener can acknowledge it');
});


test('a permanent local authorization rejection never claims durable download acceptance',async()=>{
  const f=await harness({handler:({path})=>{if(path.startsWith('/v1/downloads?'))throw Object.assign(new Error('请重新连接 FoilPage'),{code:'unauthorized',status:401});}});
  const response=await f.message({type:'shengjian-page-submit',url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
  assert.equal(response.ok,false);assert.equal(response.job.submission_failed,true);
  assert.match(response.message,/浏览器.*允许连接.*重新连接/);assert.equal(response.feedback_sent,true);
  assert.equal(f.sent.filter(message=>message.stage==='submitted').length,0);
  assert.equal(f.sent.filter(message=>message.stage==='failure').length,1);
  assert.equal(f.calls.filter(call=>call.path==='/v1/downloads').length,0);
  assert.equal(f.transfers.length,0);assert.equal(f.cookieReads,0);
});

for(const [code,expected] of [
  ['douyin_verification_required',/播放 2 秒/],
  ['login_required',/FoilPage 桌面端登录/],
  ['unauthorized',/浏览器.*允许连接.*重新连接/],
  ['resolver_error',/任务页查看原因/],
]) {
  test(`runtime submission rejection ${code} delivers its classified next step`,async()=>{
    const f=await harness({handler:({path})=>{
      if(path==='/v1/downloads')throw Object.assign(new Error('本地任务未能创建'),{code,status:400});
    }});
    const response=await f.message({type:'shengjian-page-submit',url:URL_A,title:'原视频',mediaUrl:MEDIA_A});
    assert.equal(response.ok,false);assert.equal(response.feedback_sent,true);
    assert.equal(response.job.error.code,code);assert.match(response.message,expected);
    const failure=f.sent.filter(message=>message.stage==='failure');
    assert.equal(failure.length,1);assert.equal(failure[0].message,response.message);
    if(code!=='douyin_verification_required')assert.doesNotMatch(response.message,/F5|播放 2 秒/);
    assert.equal(f.transfers.length,0);
  });
}


for (const pageUrl of [URL_A, 'https://www.youtube.com/watch?v=fixture123']) {
for (const [body, title, bodyStatus, titleStatus, label, error] of [
  [true, false, 'done', 'disabled', '正文转写成功', false],
  [false, true, 'disabled', 'done', '标题转写成功', false],
  [true, true, 'done', 'done', '正文与标题转写成功', false],
  [true, true, 'done', 'skipped', '正文转写成功，标题处理失败', true],
  [true, true, 'skipped', 'done', '标题转写成功，正文处理失败', true],
  [true, true, 'skipped', 'skipped', '转写完成，正文与标题处理失败', true],
  [false, false, 'disabled', 'disabled', '转写成功', false],
]) {
  test(`linked transcription outcome stays in the task; page policy ${pageUrl}: ${label}`, async () => {
    const f=await harness();
    const accepted=await f.submitPageDownload({url:pageUrl,title:'原视频标题',mediaUrl:MEDIA_A,transcribe:true});
    const job=f.jobs.get(accepted.id);
    Object.assign(job,{status:'transcribing',progress:.8,download_complete:true,job_id:'asr123',
      body_enabled_snapshot:body,title_enabled_snapshot:title,body_status:bodyStatus,title_status:titleStatus,
      source_path:'C:/private/source.mp4',final_path:'C:/private/final.txt'});
    await f.pollLocalTask(job.id);await f.settle();
    Object.assign(job,{status:'done',transcription_label:label,transcription_error:error});
    await f.pollLocalTask(job.id);await f.pollLocalTask(job.id);
    const terminal=f.sent.filter(message=>message.stage==='transcribe');
    assert.equal(terminal.length,pageUrl === URL_A ? 0 : 1);
    if (terminal.length) { assert.equal(terminal[0].message,label);assert.equal(terminal[0].error,error); }
    assert.equal(f.sent.filter(message=>message.stage==='download').length,1);
    assert.equal(f.sent.filter(message=>message.stage==='failure').length,0);
    // The retained original-video snapshot survives navigation/restart and omits private queue paths.
    const snapshot=await f.taskStateForUrl(pageUrl);
    assert.equal(snapshot.transcription_label,label);assert.equal(snapshot.transcription_error,error);
    assert.equal(snapshot.body_enabled_snapshot,body);assert.equal(snapshot.title_enabled_snapshot,title);
    assert.equal(snapshot.body_status,bodyStatus);assert.equal(snapshot.title_status,titleStatus);
    assert.ok(!JSON.stringify(f.saved).includes('C:/private'));
    const restarted=await harness({seed:f.saved,initialJobs:[job]});
    assert.equal(restarted.sent.filter(message=>message.stage==='transcribe').length,0);
  });
}
}

// ---------------------------------------------------------------------------
// TASK-8.17 封面载荷：cover_url 必须经 queuedMediaHints 队列中转到达 POST
// /v1/downloads（历史上这一跳漏存导致 cover_url 恒为空串）；service worker
// 重启后经 shengjian-fresh-media 重建提示时封面同样要跟上。
// ---------------------------------------------------------------------------
test('douyin submission carries cover_url through the queued hints hop',async()=>{
  const cover='https://p3-ecmc.douyinpic.com/obj/tos/covers/fixture.jpeg';
  const f=await harness({});
  await f.submitPageDownload({url:URL_A,title:'带封面下载',mediaUrl:MEDIA_A,coverUrl:cover,transcribe:false});
  await f.settle();
  const posted=f.calls.filter(call=>call.path==='/v1/downloads'&&call.body?.url);
  assert.equal(posted.length,1);
  assert.equal(posted[0].body.cover_url,cover,'queuedMediaHints 必须携带 coverUrl 进 POST 载荷');
});

test('hint rebuild from the fresh-media response also carries the cover',async()=>{
  const cover='https://p9-pc.douyinpic.com/obj/tos/covers/fresh-rebuild.jpeg';
  const f=await harness({fresh:{ok:true,videoId:VIDEO_A,mediaUrl:MEDIA_A,coverUrl:cover}});
  // 消息不带媒体提示：flush 时会向页面询问 fresh-media 重建。
  await f.submitPageDownload({url:URL_A,title:'离线重放封面',transcribe:false});
  await f.settle();
  const posted=f.calls.filter(call=>call.path==='/v1/downloads'&&call.body?.url);
  assert.equal(posted.length,1);
  assert.equal(posted[0].body.cover_url,cover,'fresh-media 重建的提示必须连同 cover_url 提交');
});

// ---------------------------------------------------------------------------
// TASK-8.18 图集载荷：kind:"gallery" 经队列中转直达 POST /v1/downloads——
// 无媒体提示、无 Cookie，桌面按签名直链逐张下载（取证④）。
// ---------------------------------------------------------------------------
const NOTE_URL='https://www.douyin.com/note/2234567890123456789';
const GALLERY_ROWS=[['https://p3-sign.douyinpic.com/tos-cn-i-0813/obj-1-a.webp','https://p9-pc.douyinpic.com/tos-cn-i-0813/obj-1-b.jpeg'],['https://p3-sign.douyinpic.com/tos-cn-i-0813/obj-2.webp']];

test('gallery submission posts the full image set without media hints or cookies',async()=>{
  const f=await harness({});
  await f.submitPageDownload({url:NOTE_URL,title:'格子图集',kind:'gallery',videoId:'2234567890123456789',
    galleryImages:GALLERY_ROWS,coverUrl:'https://p3-sign.douyinpic.com/tos-cn-i-0813/cover.webp',transcribe:true});
  await f.settle();
  const posted=f.calls.filter(call=>call.path==='/v1/downloads'&&call.body?.url===NOTE_URL);
  assert.equal(posted.length,1,'图集提交只发一次');
  const body=posted[0].body;
  assert.equal(body.mode,'local');
  assert.deepEqual(body.gallery_images,GALLERY_ROWS,'整组候选原样直达（形状由桌面再验）');
  assert.equal(body.cover_url,'https://p3-sign.douyinpic.com/tos-cn-i-0813/cover.webp');
  assert.equal(body.options.transcribe_after_download,false,'图文无音轨，绝不转写');
  assert.equal(body.options.browser_fallback,false);
  assert.equal('browser_session' in body,false,'图集不需要浏览器授权 Cookie');
  assert.equal('media_url' in body,false);
});

test('gallery rebuild after service-worker restart carries the set via fresh-media',async()=>{
  const f=await harness({fresh:{ok:true,videoId:'2234567890123456789',gallery:GALLERY_ROWS}});
  await f.submitPageDownload({url:NOTE_URL,title:'重启重建',kind:'gallery',videoId:'2234567890123456789'});
  await f.settle();
  const posted=f.calls.filter(call=>call.path==='/v1/downloads'&&call.body?.url===NOTE_URL);
  assert.equal(posted.length,1);
  assert.deepEqual(posted[0].body.gallery_images,GALLERY_ROWS,'fresh 响应的 gallery 也要能提交');
});
