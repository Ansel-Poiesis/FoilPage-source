// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import installMock from './ui_fixture.cjs';

const root=path.resolve(import.meta.dirname,'..');
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
async function open(options={},width=1280){
  const page=await browser.newPage({viewport:{width,height:800}});page.setDefaultTimeout(5000);page.errors=[];
  page.on('pageerror',error=>page.errors.push(error.message));
  await page.addInitScript(installMock,{paymentServer:'https://pay.example.com',loggedIn:true,...options});
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href);
  await page.locator('.nav-row[data-page="subscription"]').click();
  await page.waitForSelector('#accountEmailLoginBtn');return page;
}
async function edit(page,avatar=false){
  await page.locator(avatar ? '#accountAvatarBtn' : '#accountProfileBtn').click();
  await page.getByRole('dialog',{name:'编辑资料',exact:true}).waitFor({state:'visible'});
}
async function calls(page){return page.evaluate(()=>window.__fixtureCallArgs.filter(call=>call[0]==='update_account_profile'));}
async function imageFile(page,width=960,height=480,mimeType='image/png'){
  const encoded=await page.evaluate(({width,height,mimeType})=>{
    const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
    const context=canvas.getContext('2d'),pixels=context.createImageData(width,height);
    let seed=731;for(let i=0;i<pixels.data.length;i+=4){seed=(Math.imul(seed,1664525)+1013904223)>>>0;pixels.data[i]=seed&255;pixels.data[i+1]=(seed>>>8)&255;pixels.data[i+2]=(seed>>>16)&255;pixels.data[i+3]=255;}
    context.putImageData(pixels,0,0);return canvas.toDataURL(mimeType).split(',')[1];
  },{width,height,mimeType});
  return {name:'local.'+mimeType.split('/')[1],mimeType,buffer:Buffer.from(encoded,'base64')};
}

test('subscription stays compact and exposes real feature icons without changing payment gates',async()=>{
  const page=await open();
  try{
    assert.equal(await page.locator('#subscriptionTools').evaluate(node=>node.open),false);
    assert.equal(await page.getByRole('heading',{name:'当前使用',exact:true}).count(),0);
    assert.equal(await page.locator('#page-subscription').textContent().then(value=>value.includes('草案')),false);
    assert.equal(await page.locator('#accountSummary').textContent().then(value=>value.includes('余额 0 积分')),true);
    assert.equal(await page.locator('#accountConnectionBtn').count(),0);
    assert.equal(await page.locator('#subscriptionAccountGroup .set-label').count(),0);
    assert.equal(await page.locator('#subscriptionFeatureToggle').getAttribute('aria-expanded'),'false');
    assert.deepEqual(await page.locator('.plan-comparison-core th[scope="row"]').allTextContents(),[
      '月付积分','转写并发','本地转写与自带密钥整理','FoilPage 托管正文与标题','视觉校对与批量整理（自带密钥）',
    ],'正式版核心对比完整展示月付、并发与三类处理能力，不展示年付占位');
    await page.locator('#subscriptionFeatureToggle').click();
    for(const name of ['视频封面、播放与文件位置','后台队列与进度恢复','本地媒体导入','文本编辑与导出','正文与标题独立整理','浏览器插件、配对与随应用更新']){
      const row=page.locator('.plan-comparison tbody tr').filter({has:page.getByRole('rowheader',{name,exact:true})});
      assert.equal(await row.locator('td[data-plan-id="free"] svg').count(),1);
      assert.equal(await row.locator('td[data-plan-id="free"] .sr-only').textContent(),'包含');
    }
    const visual=page.locator('.plan-comparison tbody tr').filter({has:page.getByRole('rowheader',{name:'字幕视觉校对（自带密钥）',exact:true})});
    assert.equal(await visual.locator('td[data-plan-id="free"] .sr-only').textContent(),'包含','BYOK视觉校对是免费基础能力');
    assert.equal(await page.locator('.plan-comparison tbody td[data-included]').count()>0,true);
    for(const button of await page.locator('.plan-card .btn').all()) assert.equal(await button.isDisabled(),true);
    assert.equal((await page.evaluate(()=>window.__fixtureCalls)).includes('begin_subscription'),false);
    await page.locator('#subscriptionTools > summary').click();
    assert.equal(await page.locator('#subscriptionTools').evaluate(node=>node.open),true);
    assert.equal(await page.locator('#accountLogoutBtn').isVisible(),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('confirmed profile saving preserves expanded help and service tools',async()=>{
  const page=await open();
  try{
    await page.locator('#subscriptionTools > summary').click();
    await page.locator('#subscriptionHelp > summary').click();
    await page.locator('#subscriptionConnectionDetails > summary').click();
    assert.equal(await page.locator('#subscriptionHelp').evaluate(node=>node.open),true);
    assert.equal(await page.locator('#subscriptionConnectionDetails').evaluate(node=>node.open),true);
    await edit(page);await page.locator('#profileNameInput').fill('帮助保持展开');await page.locator('#profileSaveBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    for(const id of ['subscriptionTools','subscriptionHelp','subscriptionConnectionDetails']) assert.equal(await page.locator('#'+id).evaluate(node=>node.open),true,id+'展开状态应保留');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const theme of ['dark','paper'])test(`${theme} plan icons hide their redundant visual labels while retaining accessible text`,async()=>{
  const page=await open({theme});
  try{
    await page.waitForSelector('.plan-feature-value svg');
    await page.locator('#subscriptionFeatureToggle').click();
    for(const label of await page.locator('.plan-feature-value .sr-only').all()){
      const hidden=await label.evaluate(node=>{
        const rect=node.getBoundingClientRect(),style=getComputedStyle(node);
        return {width:rect.width,height:rect.height,clipPath:style.clipPath,overflow:style.overflow,text:node.textContent,position:style.position};
      });
      assert.equal(hidden.width,1);assert.equal(hidden.height,1);assert.equal(hidden.clipPath,'inset(50%)');
      assert.equal(hidden.overflow,'hidden');assert.equal(hidden.position,'absolute');assert(['包含','不包含'].includes(hidden.text));
    }
    assert.equal(await page.getByRole('cell',{name:'不包含',exact:true}).count()>0,true,'读屏仍能读取不包含，不仅有无语义的叉图形');
    const shots=path.join(root,'_webview_ui_shots','account-profile');fs.mkdirSync(shots,{recursive:true});
    await page.screenshot({path:path.join(shots,`comparison-icons-${theme}.png`),fullPage:true});
    await page.getByRole('rowheader',{name:'正文与标题独立整理',exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(shots,`comparison-features-${theme}.png`),fullPage:true});
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a signed-out account cannot edit its profile or load a remote avatar',async()=>{
  const page=await open({loggedIn:false,settings:{account_avatar:'https://outside.example/avatar.png'}});
  try{
    assert.equal(await page.locator('#accountProfileBtn').count(),0);
    assert.equal(await page.locator('#accountAvatarBtn').isDisabled(),true);
    assert.equal(await page.locator('#accountAvatarBtn img').count(),0);
    await page.locator('#accountAvatarBtn').evaluate(node=>node.onclick());
    assert.equal(await page.locator('#accountProfileModal').count(),0);
    assert.deepEqual(await calls(page),[]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const width of [1280,560])test(`profile dialog supports focus, keyboard cancellation and clean reopen at ${width}px`,async()=>{
  const page=await open({},width);
  try{
    await edit(page,true);assert.equal(await page.locator('.app').evaluate(node=>node.inert),true);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'profileNameInput');
    await page.locator('#profileNameInput').fill('尚未保存');
    for(let i=0;i<8;i++){await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('#accountProfileModal'))),true);}
    const bounds=await page.getByRole('dialog',{name:'编辑资料',exact:true}).boundingBox();
    assert(bounds.x>=0 && bounds.x+bounds.width<=width);
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'accountAvatarBtn');
    assert.deepEqual(await calls(page),[]);
    await edit(page);assert.equal(await page.locator('#profileNameInput').inputValue(),'微信用户');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('nickname save sends the authoritative profile API without changing avatar or account authorization',async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileNameInput').fill('  折声  ');await page.locator('#profileNameInput').press('Enter');
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.deepEqual(await calls(page),[['update_account_profile','折声',null]]);
    assert((await page.locator('#accountState').textContent()).includes('折声'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'折声');
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'login-fixture');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'accountProfileBtn');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const nickname of ['', 'x'.repeat(33), '名字\u200d'])test(`invalid nickname is rejected inline: ${JSON.stringify(nickname)}`,async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileNameInput').fill(nickname);await page.locator('#profileSaveBtn').click();
    assert.equal(await page.locator('#accountProfileError').isVisible(),true);
    assert((await page.locator('#accountProfileError').textContent()).includes('1–32'));
    assert.equal(await page.locator('#profileNameInput').getAttribute('aria-invalid'),'true');
    assert.deepEqual(await calls(page),[]);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('32 Unicode characters are valid rather than being counted as UTF-16 code units',async()=>{
  const page=await open();
  try{
    await edit(page);const name='🖊'.repeat(32);await page.locator('#profileNameInput').fill(name);await page.locator('#profileSaveBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.equal((await calls(page))[0][1],name);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('avatar selection makes only a local square thumbnail until explicit save',async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileAvatarInput').setInputFiles(await imageFile(page));
    await page.waitForSelector('#profileAvatarPreview img');
    const preview=await page.locator('#profileAvatarPreview img').evaluate(node=>({src:node.src,width:node.naturalWidth,height:node.naturalHeight}));
    assert.equal(preview.width,preview.height);assert(preview.width<=256 && preview.width>0);assert(preview.src.length<=65536);
    assert.match(preview.src,/^data:image\/(?:png|webp);base64,/);assert.deepEqual(await calls(page),[]);
    await page.locator('#profileSaveBtn').click();await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.equal((await calls(page))[0][2],preview.src);
    assert.equal(await page.locator('#accountAvatarBtn img').getAttribute('src'),preview.src);
    assert.equal(await page.evaluate(()=>AppState.settings.account_avatar),preview.src);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const mime of ['image/png','image/jpeg','image/webp'])test(`32px ${mime} avatars remain supported without upscaling`,async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileAvatarInput').setInputFiles(await imageFile(page,32,32,mime));
    await page.waitForSelector('#profileAvatarPreview img');
    const dimensions=await page.locator('#profileAvatarPreview img').evaluate(node=>[node.naturalWidth,node.naturalHeight]);
    assert.deepEqual(dimensions,[32,32]);assert.deepEqual(await calls(page),[]);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a returning login uses the server nickname and avatar instead of the default email label',async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileNameInput').fill('持续使用的昵称');
    await page.locator('#profileAvatarInput').setInputFiles(await imageFile(page,32,32));await page.waitForSelector('#profileAvatarPreview img');
    await page.locator('#profileSaveBtn').click();await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    await page.evaluate(()=>{
      const profile={display_name:AppState.settings.account_display_name,avatar:AppState.settings.account_avatar,user_id:'u-fixture'};
      window.__MOCK__.email_login_verify=()=>{
        window.__MOCK__.save_settings({account_login_token:'renewed-login-fixture',account_user_id:profile.user_id,account_display_name:profile.display_name,account_avatar:profile.avatar});
        return {ok:true,login_token:'renewed-login-fixture',account:profile};
      };
      document.querySelector('#subscriptionTools').open=true;document.querySelector('#accountToolsGroup').parentElement.open=true;
    });
    await page.locator('#accountLogoutBtn').click();await page.waitForFunction(()=>AppState.settings.account_login_token==='');
    await page.locator('#accountEmailLoginBtn').click();await page.locator('#loginEmailInput').fill('default-label@example.com');
    await page.locator('#sendCodeBtn').click();await page.waitForSelector('#loginCodeInput:visible');await page.locator('#verifyCodeBtn').click();
    await page.waitForFunction(()=>AppState.settings.account_login_token==='renewed-login-fixture');
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'持续使用的昵称');
    assert.equal(await page.locator('#accountAvatarBtn img').count(),1);
    assert.equal(await page.locator('#accountState').textContent().then(value=>value.includes('default-label')),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('restoring the default avatar is explicit and cancellation keeps the saved avatar',async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileAvatarInput').setInputFiles(await imageFile(page,80,80));await page.waitForSelector('#profileAvatarPreview img');
    await page.locator('#profileSaveBtn').click();await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    const saved=await page.locator('#accountAvatarBtn img').getAttribute('src');
    await edit(page);await page.locator('#profileRestoreAvatarBtn').click();assert.equal(await page.locator('#profileAvatarPreview img').count(),0);
    await page.locator('#accountProfileClose').click();assert.equal(await page.locator('#accountAvatarBtn img').getAttribute('src'),saved);
    await edit(page);await page.locator('#profileRestoreAvatarBtn').click();await page.locator('#profileSaveBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.equal((await calls(page)).at(-1)[2],'');assert.equal(await page.locator('#accountAvatarBtn img').count(),0);
    assert.equal(await page.evaluate(()=>AppState.settings.account_avatar),'');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const [description,file,expected] of [
  ['unsupported format',{name:'vector.svg',mimeType:'image/svg+xml',buffer:Buffer.from('<svg/>')},'JPG'],
  ['size limit',{name:'large.png',mimeType:'image/png',buffer:Buffer.alloc(8*1024*1024+1)},'8 MB'],
  ['spoofed MIME',{name:'bad.png',mimeType:'image/png',buffer:Buffer.from('this is not a PNG')},'格式'],
  ['undecodable image',{name:'bad.jpg',mimeType:'image/jpeg',buffer:Buffer.from([255,216,255,0])},'换一张'],
])test(`avatar ${description} leaves the real account unchanged`,async()=>{
  const page=await open();
  try{
    await edit(page);await page.locator('#profileAvatarInput').setInputFiles(file);
    await page.waitForSelector('#accountProfileError:not([hidden])');
    assert((await page.locator('#accountProfileError').textContent()).includes(expected));
    assert.equal(await page.locator('#profileAvatarPreview img').count(),0);
    assert.deepEqual(await calls(page),[]);assert.equal(await page.evaluate(()=>AppState.settings.account_avatar || ''),'');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('profile failure remains inline and does not replace the displayed name',async()=>{
  const page=await open({profileFail:true});
  try{
    await edit(page);await page.locator('#profileNameInput').fill('尚未成功');await page.locator('#profileSaveBtn').click();
    await page.waitForSelector('#accountProfileError:not([hidden])');
    assert.equal(await page.locator('#accountProfileError').textContent(),'资料保存失败');
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'微信用户');
    assert.equal(await page.locator('#profileSaveBtn').isDisabled(),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const account of [null,{display_name:'服务返回但缺头像'},{display_name:'服务返回',avatar:'https://outside.example/image.png'},{display_name:'其他账户',avatar:'',user_id:'someone-else'}])test(`an incomplete or mismatched profile response cannot silently complete: ${JSON.stringify(account)}`,async()=>{
  const page=await open();
  try{
    await page.evaluate(account=>{window.__MOCK__.update_account_profile=()=>({ok:true,...(account ? {account} : {})});},account);
    await edit(page);await page.locator('#profileNameInput').fill('用户输入');await page.locator('#profileSaveBtn').click();
    await page.waitForSelector('#accountProfileError:not([hidden])');
    assert.equal(await page.locator('#accountProfileError').textContent(),'账户资料响应无效，请重试');
    assert.equal(await page.locator('#profileNameInput').inputValue(),'用户输入');
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'微信用户');
    assert.equal(await page.locator('#profileSaveBtn').isDisabled(),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('server-rejected profile session clears the actual local account instead of keeping a false login',async()=>{
  const page=await open();
  try{
    await page.evaluate(()=>{window.__MOCK__.update_account_profile=async()=>{
      await window.__MOCK__.save_settings({account_login_token:'',account_display_name:'',account_avatar:'',account_user_id:''});
      return {ok:false,status:'invalid',error:'登录已失效，请重新登录'};
    };});
    await edit(page);await page.locator('#profileNameInput').fill('失效账户');await page.locator('#profileSaveBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    assert.equal(await page.locator('#accountState').textContent(),'未登录');
    assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'');
    assert.equal(await page.locator('#accountProfileBtn').count(),0);
    assert.equal(await page.locator('#accountAvatarBtn').isDisabled(),true);
    assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('busy save cannot duplicate submission and a confirmed result still syncs after closing',async()=>{
  const page=await open({profileDelay:250});
  try{
    await edit(page);await page.locator('#profileNameInput').fill('迟到但已保存');await page.locator('#profileSaveBtn').click();
    assert.equal(await page.locator('#profileSaveBtn').isDisabled(),true);
    await page.locator('form.profile-form').evaluate(node=>node.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    await page.waitForFunction(()=>AppState.settings.account_display_name==='迟到但已保存');
    assert.equal((await calls(page)).length,1);assert.equal(await page.locator('.app').evaluate(node=>node.inert),false);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('late profile response for another account cannot overwrite the current profile',async()=>{
  const page=await open({profileDelay:250});
  try{
    await edit(page);await page.locator('#profileNameInput').fill('旧账户响应');await page.locator('#profileSaveBtn').click();
    await page.evaluate(()=>{AppState.settings.account_login_token='other-session';AppState.settings.account_display_name='当前账户';});
    await page.waitForSelector('#accountProfileError:not([hidden])');
    assert((await page.locator('#accountProfileError').textContent()).includes('账户已变化'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'当前账户');assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

async function beginDelayedSettingsSync(page){
  await page.evaluate(()=>{
    const snapshot=window.__MOCK__.get_settings();
    window.__MOCK__.get_settings=()=>new Promise(resolve=>{window.__releaseOldSettings=()=>resolve(snapshot);});
    document.querySelector('#subscriptionTools').open=true;document.querySelector('#accountToolsGroup').parentElement.open=true;
  });
  await page.getByRole('button',{name:'同步积分',exact:true}).click();
  await page.waitForFunction(()=>typeof window.__releaseOldSettings==='function');
}

test('an old settings response cannot overwrite a confirmed newer nickname and avatar',async()=>{
  const page=await open();
  try{
    await beginDelayedSettingsSync(page);
    await edit(page);await page.locator('#profileNameInput').fill('新资料');
    await page.locator('#profileAvatarInput').setInputFiles(await imageFile(page,32,32));await page.waitForSelector('#profileAvatarPreview img');
    const avatar=await page.locator('#profileAvatarPreview img').getAttribute('src');
    await page.locator('#profileSaveBtn').click();await page.waitForFunction(()=>!document.querySelector('#accountProfileModal'));
    await page.evaluate(()=>window.__releaseOldSettings());
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('积分已同步'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'新资料');
    assert.equal(await page.evaluate(()=>AppState.settings.account_avatar),avatar);
    assert.equal(await page.locator('#accountAvatarBtn img').getAttribute('src'),avatar);
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

for(const change of ['account','service'])test(`old settings cannot restore an account after a ${change} change`,async()=>{
  const page=await open();
  try{
    await beginDelayedSettingsSync(page);
    await page.evaluate(change=>{
      if(change==='account'){AppState.settings.account_login_token='new-session';AppState.settings.account_user_id='new-user';}
      else AppState.settings.subscription_server_url='https://new-service.example.com';
      AppState.settings.account_display_name='当前资料';AppState.settings.account_avatar='';
      window.__releaseOldSettings();
    },change);
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('积分已同步'));
    assert.equal(await page.evaluate(()=>AppState.settings.account_display_name),'当前资料');
    if(change==='account'){
      assert.equal(await page.evaluate(()=>AppState.settings.account_login_token),'new-session');
      assert.equal(await page.evaluate(()=>AppState.settings.account_user_id),'new-user');
    }else assert.equal(await page.evaluate(()=>AppState.settings.subscription_server_url),'https://new-service.example.com');
    assert.deepEqual(page.errors,[]);
  }finally{await page.close();}
});

test('a revoked account push cannot be unlocked by an older settings response',async()=>{
  const page=await open();
  try {
    await beginDelayedSettingsSync(page);
    await page.evaluate(()=>{
      __onState({account_access:{required:true,allowed:false,expires_at:null,reason:'login_required',revision:2}});
      window.__releaseOldSettings();
    });
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('积分已同步'));
    assert.equal(await page.evaluate(()=>AccountAccess.allowed()),false);
    assert.equal(await page.evaluate(()=>AppState.settings.account_access.revision),2);
    assert.equal(await page.locator('[data-page=workbench]').isDisabled(),true);
    assert.equal(await page.locator('[data-page=workbench]').getAttribute('aria-disabled'),'true');
    assert.equal(await page.evaluate(()=>AppState.currentView),'subscription','撤销后保留可用的账户修复界面');
    assert.equal(await page.locator('[data-page=setup]').isEnabled(),true,'登录入口保持可用');
    await page.locator('[data-page=setup]').click();
    assert.equal(await page.evaluate(()=>AppState.currentView),'setup');
    assert.equal(await page.locator('#setupPanel-login').isVisible(),true);
    assert.deepEqual(page.errors,[]);
  } finally {await page.close();}
});
