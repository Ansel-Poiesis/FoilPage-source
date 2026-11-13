// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Enumerated release review. Local browser fixtures never contact providers or user profiles. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {pathToFileURL}=require('node:url');
const root=path.resolve(__dirname,'..');
const output=path.resolve(process.env.SHENGJIAN_UI_AUDIT_DIR || path.join(root,'.ui-audit','release-current','ui'));
const installMock=require('./ui_fixture.cjs');
const sizes=[{width:1280,height:800,scale:1},{width:960,height:800,scale:1},{width:560,height:500,scale:1.3}];
const sections=['ui','recognition','agent','control','model','data','startup','about'];
const pages=['workbench','videos','library','faq','subscription','settings','setup','browser','links','import'];
const records=[],failures=[];
fs.mkdirSync(output,{recursive:true});
const files=[];
function walk(directory){for(const item of fs.readdirSync(directory,{withFileTypes:true})){const file=path.join(directory,item.name);if(item.isDirectory())walk(file);else if(/\.(js|css|html)$/.test(file))files.push(file);}}
walk(path.join(root,'app/webview_ui'));walk(path.join(root,'browser-extension'));
const sources=files.map(file=>({path:path.relative(root,file).replaceAll('\\','/'),sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')}));
async function open(browser,size,options={},overrides={}){
  const page=await browser.newPage({viewport:{width:size.width,height:size.height},reducedMotion:'reduce'});
  page.setDefaultTimeout(7000);page.auditErrors=[];page.on('pageerror',error=>page.auditErrors.push(error.message));
  await page.addInitScript(installMock,{loggedIn:true,paymentServer:'https://audit.example.invalid',nativeChrome:true,installed:true,scale:size.scale,...options});
  await page.addInitScript(overrides=>{for(const [method,value] of Object.entries(overrides))__MOCK__[method]=()=>value;},overrides);
  await page.goto(pathToFileURL(path.join(root,'app/webview_ui/index.html')).href,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.nav-row[aria-current=page]');return page;
}
async function capture(page,name,{expand=false}={}){
  if(expand)await page.evaluate(()=>document.querySelectorAll('.page.active details').forEach(node=>node.open=true));
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const record=await page.evaluate(()=>{
    const root=document.querySelector('.page.active') || document.body;
    const visible=node=>!!node.getClientRects().length && !node.closest('[hidden]') && getComputedStyle(node).visibility!=='hidden';
    const selector=node=>node.id?'#'+node.id:node.tagName.toLowerCase()+(node.classList.length?'.'+[...node.classList].slice(0,3).join('.'):'');
    const text=[];const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){
      const node=walker.currentNode,copy=node.textContent.trim().replace(/\s+/g,' '),host=node.parentElement;
      if(!copy || !host || !visible(host) || ['SCRIPT','STYLE','OPTION'].includes(host.tagName))continue;
      const css=getComputedStyle(host),rect=host.getBoundingClientRect();
      text.push({selector:selector(host),text:copy,fontSize:css.fontSize,color:css.color,width:Math.round(rect.width),height:Math.round(rect.height)});
    }
    const controls=[...root.querySelectorAll('button,[role="button"],input:not([type=hidden]),textarea,select,summary,a[href]')].filter(visible).map(node=>{
      const label=node.getAttribute('aria-label') || (node.labels && [...node.labels].map(label=>label.textContent).join(' ')) || node.innerText || node.title || node.placeholder || '';
      const rect=node.getBoundingClientRect();return{selector:selector(node),tag:node.tagName,role:node.getAttribute('role'),name:label.trim().replace(/\s+/g,' '),title:node.title || '',placeholder:node.placeholder || '',disabled:node.disabled===true,width:Math.round(rect.width),height:Math.round(rect.height)};
    });
    return{viewport:{width:innerWidth,height:innerHeight},page:root.id,text,controls,horizontalOverflow:document.documentElement.scrollWidth>innerWidth};
  });
  record.name=name;record.errors=[...(page.auditErrors || [])];
  record.unnamedControls=record.controls.filter(control=>!control.name);
  record.suspiciousText=record.text.filter(row=>/undefined|\bNaN\b|\[object Object\]|\uFFFD|\?{3,}/.test(row.text));
  records.push(record);
  if(record.horizontalOverflow || record.errors.length || record.unnamedControls.length || record.suspiciousText.length)failures.push({name,horizontalOverflow:record.horizontalOverflow,errors:record.errors,unnamedControls:record.unnamedControls,suspiciousText:record.suspiciousText});
  await page.mouse.move(0,0);await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled'});
}
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    for(const size of sizes){
      const suffix=size.width+'x'+size.height+'-scale'+size.scale;
      const page=await open(browser,size);
      for(const view of pages){
        await page.evaluate(view=>switchView(view),view);
        if(view==='videos')await page.waitForSelector('.video-card');
        if(view==='subscription')await page.waitForSelector('#softwareAccessState');
        await capture(page,view+'-'+suffix,{expand:view==='subscription'});
      }
      await page.evaluate(()=>switchView('settings'));
      for(const section of sections){
        await page.locator(`.sitem[data-section=${section}]`).click();
        await capture(page,'settings-'+section+'-'+suffix,{expand:true});
      }
      await page.evaluate(()=>switchView('setup'));
      for(const step of ['login','environment','models']){
        await page.evaluate(step=>Setup.showStep(step),step);await capture(page,'setup-'+step+'-'+suffix,{expand:true});
      }
      await page.evaluate(()=>switchView('browser'));
      for(let step=0;step<3;step++){
        await page.locator('.browser-install-steps button').nth(step).click();await capture(page,'browser-step'+step+'-'+suffix,{expand:true});
      }
      for(const [chapter,count] of [['environment',5],['browser',4],['application',7]]){
        await page.evaluate(chapter=>Onboarding.show({chapter,resume:false}),chapter);
        for(let step=0;step<count;step++){
          // Include the actual guide copy as a separate visible surface.
          const guide=await page.locator('.ob-card').innerText();
          await capture(page,'guide-'+chapter+'-'+step+'-'+suffix);records.at(-1).guideText=guide;
          if(step<count-1)await page.keyboard.press('ArrowRight');
        }
        await page.keyboard.press('Escape');
      }
      await page.evaluate(()=>switchView('library'));await page.locator('.doc-open').first().click();
      await page.waitForSelector('.reader-body h1');await capture(page,'reader-'+suffix);
      await page.evaluate(()=>PublicTerms.open('user-agreement'));await page.waitForSelector('.public-terms-dialog iframe');
      await capture(page,'terms-dialog-'+suffix);await page.keyboard.press('Escape');
      const terms=await browser.newPage({viewport:{width:size.width,height:size.height}});
      await terms.goto(pathToFileURL(path.join(root,'app/webview_ui/terms.html')).href);
      await capture(terms,'terms-full-'+suffix);await terms.close();
      await page.close();
    }
    const scenarios=[
      ['logged-out',{loggedIn:false,accountAccess:false,termsAccepted:false},{}],
      ['service-offline',{serviceOffline:true,contractThrows:true},{}],
      ['model-missing',{installed:false},{}],
      ['video-error',{videosFail:true},{}],
      ['empty-content',{}, {get_tasks:[],get_documents:[],get_downloaded_videos:{ok:true,items:[],total:0}}],
      ['expired-software',{softwareAccess:{ok:true,source:'signed_license',status:'expired',allowed:false,unlocked:false,days_left:0}},{}],
    ];
    for(const [name,options,overrides] of scenarios){
      const page=await open(browser,sizes[2],options,overrides);
      for(const view of (name==='logged-out'?['setup','faq','subscription','settings']:pages)){
        await page.evaluate(view=>switchView(view),view);await capture(page,name+'-'+view,{expand:view==='subscription'});
      }
      if(name==='logged-out'){
        await page.evaluate(()=>switchView('setup'));await page.locator('#setupLoginBtn').click();
        await page.waitForSelector('#loginEmailInput');await capture(page,'logged-out-email-consent');
        records.at(-1).dialogText=await page.locator('#accountAuthModal').innerText();
      }
      await page.close();
    }
  }finally{await browser.close();}
  const uniqueText=[...new Set(records.flatMap(record=>[...record.text.map(row=>row.text),record.guideText,record.dialogText].filter(Boolean)))].sort();
  const evidence={createdAt:new Date().toISOString(),scope:'Local Chromium + synthetic bridge; no native WebView, installed files, live provider, order or private user data validation',sizes,sources,surfaces:records.length,uniqueVisibleText:uniqueText.length,failures,records};
  fs.writeFileSync(path.join(output,'coverage.json'),JSON.stringify(evidence,null,2));
  fs.writeFileSync(path.join(output,'copy-inventory.txt'),uniqueText.join('\n')+'\n','utf8');
  console.log(JSON.stringify({surfaces:records.length,uniqueVisibleText:uniqueText.length,failures,output},null,2));
  if(failures.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
